"""Clean-room web evidence search for PathCopilot's non-WSI home chat.

The module deliberately searches a fixed allowlist of biomedical/scholarly
APIs.  It never fetches a user-supplied URL, so the chat feature cannot become
an SSRF proxy.  Provider ranking is fused locally and the returned source IDs
are stable for the lifetime of one answer (S1, S2, ...).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import logging
import math
import os
import re
import threading
import time
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import requests


logger = logging.getLogger(__name__)

PUBMED_SEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
EUROPE_PMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"

DEFAULT_TIMEOUT = (4, 18)
MAX_QUERY_LENGTH = 600
MAX_SNIPPET_LENGTH = 1_600
MAX_CONTEXT_LENGTH = 22_000
DEFAULT_SEARCH_CONTEXT_SIZE = "medium"
SEARCH_CONTEXT_PROFILES = {
    "low": {
        "provider_limit": 4,
        "min_sources": 3,
        "max_sources": 5,
        "relative_score_floor": 0.75,
    },
    "medium": {
        "provider_limit": 6,
        "min_sources": 6,
        "max_sources": 10,
        "relative_score_floor": 0.30,
    },
    "high": {
        "provider_limit": 8,
        "min_sources": 10,
        "max_sources": 15,
        "relative_score_floor": 0.18,
    },
}
_CACHE_TTL_SECONDS = 15 * 60
_CACHE_MAX_ITEMS = 64
_SEARCH_CACHE: dict[
    tuple[tuple[str, ...], str],
    tuple[float, list[dict[str, Any]]],
] = {}
_CACHE_LOCK = threading.RLock()

_SENSITIVE_QUERY_PATTERNS = (
    re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", re.I),
    re.compile(r"https?://\S+", re.I),
    re.compile(r"\b(?:mrn|medical record|patient id|case id)\s*[:#=-]?\s*[\w-]+", re.I),
    re.compile(r"\b(?:patient|name|named)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b"),
    re.compile(r"\b(?:dob|date of birth)\s*[:#=-]?\s*\S+", re.I),
    re.compile(r"(?:姓名|患者姓名|住院号|病案号|病历号|身份证号|手机号|电话|住址|地址)\s*[:：]?\s*[^\s,;，；]+"),
    re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    re.compile(r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b"),
    re.compile(r"\b\d{6,}\b"),
)


@dataclass
class EvidenceSource:
    title: str
    url: str
    snippet: str = ""
    provider: str = ""
    year: str = ""
    journal: str = ""
    doi: str = ""
    pmid: str = ""
    authors: str = ""
    query: str = ""
    score: float = 0.0
    providers: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.title = _clean_text(self.title, 500)
        self.url = _safe_public_url(self.url)
        self.snippet = _clean_text(self.snippet, MAX_SNIPPET_LENGTH)
        self.provider = _clean_text(self.provider, 80)
        self.year = _clean_text(self.year, 12)
        self.journal = _clean_text(self.journal, 180)
        self.doi = _normalize_doi(self.doi)
        self.pmid = re.sub(r"\D", "", str(self.pmid or ""))[:20]
        self.authors = _clean_text(self.authors, 300)
        self.query = _clean_text(self.query, MAX_QUERY_LENGTH)
        if self.provider:
            self.providers.add(self.provider)

    def public_dict(self, source_id: str) -> dict[str, Any]:
        host = urlparse(self.url).hostname or ""
        return {
            "id": source_id,
            "title": self.title,
            "url": self.url,
            "domain": host.lower(),
            "provider": " · ".join(sorted(self.providers)) or self.provider,
            "year": self.year,
            "journal": self.journal,
            "snippet": self.snippet,
            "doi": self.doi,
            "pmid": self.pmid,
        }


def _clean_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    return text[:limit]


def _normalize_doi(value: Any) -> str:
    doi = str(value or "").strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.I)
    return doi.strip().lower()[:300]


def _safe_public_url(value: Any) -> str:
    url = str(value or "").strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return url[:2_000]


def sanitize_external_query(value: Any) -> str:
    """Return a minimal scholarly query that is safe for public APIs.

    Raw CJK user text is never sent out.  The chat model must first translate
    it into a concise English medical-concept query, after which deterministic
    redaction removes common direct identifiers.  This intentionally fails
    closed when planning fails.
    """

    query = _clean_text(value, MAX_QUERY_LENGTH)
    if not query or re.search(r"[\u3400-\u9fff]", query):
        return ""
    for pattern in _SENSITIVE_QUERY_PATTERNS:
        query = pattern.sub(" ", query)
    query = re.sub(r"[^A-Za-z0-9 Greekgreek+./(),:'\" -]", " ", query)
    query = " ".join(query.split()).strip(" ,;:-")[:320]
    if len(re.findall(r"[A-Za-z]{2,}", query)) < 2:
        return ""
    return query


def _request_headers() -> dict[str, str]:
    contact = os.environ.get("PATHCOPILOT_SEARCH_CONTACT", "").strip()
    agent = "PathCopilot/2.0 biomedical-evidence-search"
    if contact:
        agent += f" (mailto:{contact[:200]})"
    return {"Accept": "application/json", "User-Agent": agent}


def _element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return _clean_text("".join(element.itertext()), MAX_SNIPPET_LENGTH)


def _pubmed_year(article: ET.Element) -> str:
    for path in (
        ".//Article/Journal/JournalIssue/PubDate/Year",
        ".//ArticleDate/Year",
        ".//PubMedPubDate[@PubStatus='pubmed']/Year",
    ):
        value = article.findtext(path, "").strip()
        if re.fullmatch(r"\d{4}", value):
            return value
    medline_date = article.findtext(
        ".//Article/Journal/JournalIssue/PubDate/MedlineDate", ""
    )
    match = re.search(r"\b(?:19|20)\d{2}\b", medline_date)
    return match.group(0) if match else ""


def _parse_pubmed_xml(xml_text: str, query: str = "") -> list[EvidenceSource]:
    root = ET.fromstring(xml_text)
    results: list[EvidenceSource] = []
    for item in root.findall(".//PubmedArticle"):
        pmid = item.findtext(".//MedlineCitation/PMID", "")
        title = _element_text(item.find(".//Article/ArticleTitle"))
        if not title or not pmid:
            continue
        abstract_parts = []
        for abstract in item.findall(".//Article/Abstract/AbstractText"):
            label = _clean_text(abstract.attrib.get("Label", ""), 80)
            content = _element_text(abstract)
            if content:
                abstract_parts.append(f"{label}: {content}" if label else content)
        doi = ""
        for article_id in item.findall(".//PubmedData/ArticleIdList/ArticleId"):
            if str(article_id.attrib.get("IdType", "")).lower() == "doi":
                doi = _element_text(article_id)
                break
        authors = []
        for author in item.findall(".//Article/AuthorList/Author")[:4]:
            collective = author.findtext("CollectiveName", "").strip()
            family = author.findtext("LastName", "").strip()
            initials = author.findtext("Initials", "").strip()
            name = collective or " ".join(part for part in (family, initials) if part)
            if name:
                authors.append(name)
        results.append(
            EvidenceSource(
                title=title,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                snippet=" ".join(abstract_parts),
                provider="PubMed",
                year=_pubmed_year(item),
                journal=_element_text(item.find(".//Article/Journal/Title")),
                doi=doi,
                pmid=pmid,
                authors=", ".join(authors),
                query=query,
            )
        )
    return results


def _search_pubmed(query: str, limit: int) -> list[EvidenceSource]:
    params: dict[str, Any] = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": limit,
        "sort": "relevance",
        "tool": "PathCopilot",
    }
    email = os.environ.get("PUBMED_EMAIL", "").strip()
    api_key = os.environ.get("PUBMED_API_KEY", "").strip()
    if email:
        params["email"] = email
    if api_key:
        params["api_key"] = api_key
    response = requests.get(
        PUBMED_SEARCH_URL,
        params=params,
        headers=_request_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    ids = response.json().get("esearchresult", {}).get("idlist", [])
    ids = [re.sub(r"\D", "", str(value)) for value in ids if str(value).strip()]
    if not ids:
        return []
    fetch_params: dict[str, Any] = {
        "db": "pubmed",
        "id": ",".join(ids[:limit]),
        "rettype": "abstract",
        "retmode": "xml",
        "tool": "PathCopilot",
    }
    if email:
        fetch_params["email"] = email
    if api_key:
        fetch_params["api_key"] = api_key
    response = requests.get(
        PUBMED_FETCH_URL,
        params=fetch_params,
        headers={**_request_headers(), "Accept": "application/xml"},
        timeout=DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    return _parse_pubmed_xml(response.text, query=query)


def _search_europe_pmc(query: str, limit: int) -> list[EvidenceSource]:
    response = requests.get(
        EUROPE_PMC_SEARCH_URL,
        params={
            "query": query,
            "format": "json",
            "pageSize": limit,
            "resultType": "core",
        },
        headers=_request_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    records = response.json().get("resultList", {}).get("result", [])
    results: list[EvidenceSource] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        title = record.get("title") or ""
        source = str(record.get("source") or "MED")
        source_id = str(record.get("id") or record.get("pmid") or "")
        pmid = str(record.get("pmid") or "")
        doi = _normalize_doi(record.get("doi"))
        if pmid:
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        elif source_id:
            url = f"https://europepmc.org/article/{source}/{source_id}"
        elif doi:
            url = f"https://doi.org/{doi}"
        else:
            continue
        results.append(
            EvidenceSource(
                title=title,
                url=url,
                snippet=record.get("abstractText") or "",
                provider="Europe PMC",
                year=record.get("pubYear") or record.get("firstPublicationDate") or "",
                journal=record.get("journalTitle") or "",
                doi=doi,
                pmid=pmid,
                authors=record.get("authorString") or "",
                query=query,
                score=math.log1p(max(int(record.get("citedByCount") or 0), 0)) / 10_000,
            )
        )
    return results


def _openalex_abstract(inverted_index: Any) -> str:
    if not isinstance(inverted_index, dict):
        return ""
    ordered: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int) and position >= 0:
                ordered.append((position, str(word)))
    ordered.sort(key=lambda value: value[0])
    return _clean_text(" ".join(word for _, word in ordered), MAX_SNIPPET_LENGTH)


def _search_openalex(query: str, limit: int) -> list[EvidenceSource]:
    params: dict[str, Any] = {
        "search": query,
        "per-page": limit,
        "sort": "relevance_score:desc",
    }
    contact = os.environ.get("PATHCOPILOT_SEARCH_CONTACT", "").strip()
    if contact:
        params["mailto"] = contact
    response = requests.get(
        OPENALEX_WORKS_URL,
        params=params,
        headers=_request_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    results: list[EvidenceSource] = []
    for record in response.json().get("results", []):
        if not isinstance(record, dict):
            continue
        location = record.get("primary_location") or {}
        source = location.get("source") or {}
        doi = _normalize_doi(record.get("doi"))
        url = location.get("landing_page_url") or (f"https://doi.org/{doi}" if doi else "")
        if not url:
            url = record.get("id") or ""
        authors = []
        for authorship in (record.get("authorships") or [])[:4]:
            author = authorship.get("author") if isinstance(authorship, dict) else {}
            name = author.get("display_name") if isinstance(author, dict) else ""
            if name:
                authors.append(str(name))
        results.append(
            EvidenceSource(
                title=record.get("display_name") or record.get("title") or "",
                url=url,
                snippet=_openalex_abstract(record.get("abstract_inverted_index")),
                provider="OpenAlex",
                year=record.get("publication_year") or "",
                journal=source.get("display_name") if isinstance(source, dict) else "",
                doi=doi,
                authors=", ".join(authors),
                query=query,
                score=math.log1p(max(int(record.get("cited_by_count") or 0), 0)) / 12_000,
            )
        )
    return results


def _source_key(source: EvidenceSource) -> str:
    if source.doi:
        return f"doi:{source.doi}"
    if source.pmid:
        return f"pmid:{source.pmid}"
    normalized_title = re.sub(r"[^a-z0-9]+", "", source.title.lower())
    return f"title:{normalized_title[:240]}"


def _merge_source(target: EvidenceSource, incoming: EvidenceSource) -> None:
    target.providers.update(incoming.providers)
    target.score += incoming.score
    if len(incoming.snippet) > len(target.snippet):
        target.snippet = incoming.snippet
    for field_name in ("year", "journal", "doi", "pmid", "authors", "url"):
        if not getattr(target, field_name) and getattr(incoming, field_name):
            setattr(target, field_name, getattr(incoming, field_name))


def _deduplicate_and_rank(
    provider_results: Iterable[tuple[str, int, int, EvidenceSource]],
    limit: int,
) -> list[EvidenceSource]:
    provider_weight = {"PubMed": 1.08, "Europe PMC": 1.04, "OpenAlex": 1.0}
    merged: dict[str, EvidenceSource] = {}
    for provider, query_index, rank, source in provider_results:
        if not source.title or not source.url:
            continue
        # Reciprocal-rank fusion.  The second query is useful but deliberately
        # carries slightly less weight than the primary clinical query.
        query_weight = 1.0 if query_index == 0 else 0.82
        source.score += provider_weight.get(provider, 1.0) * query_weight / (60 + rank)
        key = _source_key(source)
        if key in merged:
            _merge_source(merged[key], source)
        else:
            merged[key] = source
    ranked = sorted(
        merged.values(),
        key=lambda item: (item.score, bool(item.snippet), item.year),
        reverse=True,
    )
    return ranked[:limit]


def normalize_search_context_size(value: Any) -> str:
    context_size = str(value or "").strip().lower()
    if context_size not in SEARCH_CONTEXT_PROFILES:
        return DEFAULT_SEARCH_CONTEXT_SIZE
    return context_size


def _select_adaptive_sources(
    ranked: Sequence[EvidenceSource],
    context_size: str,
) -> list[EvidenceSource]:
    """Select a quality-sensitive evidence set within a context-size budget.

    Like hosted web search, context size controls breadth rather than promising
    an exact citation count.  Strong cross-provider matches can make a compact
    set sufficient; broader questions retain more independently ranked items.
    """

    profile = SEARCH_CONTEXT_PROFILES[normalize_search_context_size(context_size)]
    candidates = list(ranked[:profile["max_sources"]])
    if len(candidates) <= profile["min_sources"]:
        return candidates

    top_score = max(float(candidates[0].score or 0.0), 1e-9)
    selected = list(candidates[:profile["min_sources"]])
    for source in candidates[profile["min_sources"]:]:
        relative_score = float(source.score or 0.0) / top_score
        corroborated = len(source.providers) > 1
        if relative_score >= profile["relative_score_floor"] or corroborated:
            selected.append(source)
    return selected


def normalize_queries(queries: Sequence[str] | str) -> list[str]:
    values = [queries] if isinstance(queries, str) else list(queries or [])
    normalized = []
    for value in values:
        query = sanitize_external_query(value)
        if query and query.casefold() not in {item.casefold() for item in normalized}:
            normalized.append(query)
    return normalized[:2]


def _cache_get(key: tuple[tuple[str, ...], str]) -> list[dict[str, Any]] | None:
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _SEARCH_CACHE.get(key)
        if not cached:
            return None
        created_at, value = cached
        if now - created_at > _CACHE_TTL_SECONDS:
            _SEARCH_CACHE.pop(key, None)
            return None
        return [dict(item) for item in value]


def _cache_put(key: tuple[tuple[str, ...], str], value: list[dict[str, Any]]) -> None:
    with _CACHE_LOCK:
        if len(_SEARCH_CACHE) >= _CACHE_MAX_ITEMS:
            oldest = min(_SEARCH_CACHE, key=lambda item: _SEARCH_CACHE[item][0])
            _SEARCH_CACHE.pop(oldest, None)
        _SEARCH_CACHE[key] = (time.monotonic(), [dict(item) for item in value])


def search_pathology_web(
    queries: Sequence[str] | str,
    context_size: str = DEFAULT_SEARCH_CONTEXT_SIZE,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Search fixed biomedical sources and return ranked public source dicts.

    Errors are intentionally returned as provider names rather than exception
    text so network details and credentials never reach the browser.
    """

    normalized = normalize_queries(queries)
    if not normalized:
        return [], []
    context_size = normalize_search_context_size(context_size)
    profile = SEARCH_CONTEXT_PROFILES[context_size]
    cache_key = (tuple(value.casefold() for value in normalized), context_size)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached, []

    jobs = []
    errors: list[str] = []
    flattened: list[tuple[str, int, int, EvidenceSource]] = []
    providers = (
        ("PubMed", _search_pubmed),
        ("Europe PMC", _search_europe_pmc),
        ("OpenAlex", _search_openalex),
    )
    with ThreadPoolExecutor(max_workers=min(6, len(normalized) * len(providers))) as pool:
        for query_index, query in enumerate(normalized):
            for provider_name, search_fn in providers:
                future = pool.submit(search_fn, query, profile["provider_limit"])
                jobs.append((future, provider_name, query_index))
        for future, provider_name, query_index in jobs:
            try:
                provider_items = future.result()
            except Exception as exc:
                logger.warning("Web evidence provider %s failed: %s", provider_name, str(exc)[:180])
                errors.append(provider_name)
                continue
            for rank, item in enumerate(provider_items, 1):
                flattened.append((provider_name, query_index, rank, item))

    ranked = _deduplicate_and_rank(flattened, profile["max_sources"])
    selected = _select_adaptive_sources(ranked, context_size)
    public = [item.public_dict(f"S{index}") for index, item in enumerate(selected, 1)]
    _cache_put(cache_key, public)
    return public, sorted(set(errors))


def build_evidence_context(sources: Sequence[dict[str, Any]]) -> str:
    if not sources:
        return "No retrievable external source was found for this query."
    blocks = []
    total = 0
    for source in sources:
        source_id = _clean_text(source.get("id"), 12)
        lines = [
            f"[{source_id}] {_clean_text(source.get('title'), 500)}",
            "Provider: " + _clean_text(source.get("provider"), 120),
            "Published: " + _clean_text(source.get("year"), 12),
            "Journal: " + _clean_text(source.get("journal"), 180),
            "URL: " + _safe_public_url(source.get("url")),
            "Excerpt: " + _clean_text(source.get("snippet"), MAX_SNIPPET_LENGTH),
        ]
        block = "\n".join(line for line in lines if not line.endswith(": "))
        if total + len(block) > MAX_CONTEXT_LENGTH:
            break
        blocks.append(block)
        total += len(block)
    return "\n\n".join(blocks)


def _response_output_text(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    text_parts: list[str] = []
    annotations: list[dict[str, Any]] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict) or content.get("type") != "output_text":
                continue
            text_parts.append(str(content.get("text") or ""))
            annotations.extend(
                annotation
                for annotation in (content.get("annotations") or [])
                if isinstance(annotation, dict) and annotation.get("type") == "url_citation"
            )
    return "".join(text_parts).strip(), annotations


def _hosted_sources(payload: dict[str, Any], annotations: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        action = item.get("action") or {}
        if isinstance(action, dict):
            candidates.extend(value for value in (action.get("sources") or []) if isinstance(value, dict))
    candidates.extend(annotations)

    sources: list[dict[str, Any]] = []
    seen_urls = set()
    for candidate in candidates:
        url = _safe_public_url(candidate.get("url"))
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        sources.append({
            "id": f"S{len(sources) + 1}",
            "title": _clean_text(candidate.get("title") or urlparse(url).hostname, 500),
            "url": url,
            "domain": (urlparse(url).hostname or "").lower(),
            "provider": "OpenAI Web Search",
            "year": "",
            "journal": "",
            "snippet": "",
            "doi": "",
            "pmid": "",
        })
    return sources[:20]


def _annotate_hosted_text(
    text: str,
    annotations: Sequence[dict[str, Any]],
    sources: Sequence[dict[str, Any]],
) -> str:
    source_id_by_url = {source.get("url"): source.get("id") for source in sources}
    insertions: dict[int, list[str]] = {}
    for annotation in annotations:
        source_id = source_id_by_url.get(_safe_public_url(annotation.get("url")))
        try:
            end_index = int(annotation.get("end_index"))
        except (TypeError, ValueError):
            continue
        if source_id and 0 <= end_index <= len(text):
            insertions.setdefault(end_index, []).append(str(source_id))
    for end_index in sorted(insertions, reverse=True):
        labels = "".join(f"[{value}]" for value in dict.fromkeys(insertions[end_index]))
        text = text[:end_index] + labels + text[end_index:]
    return text



def clear_search_cache() -> None:
    with _CACHE_LOCK:
        _SEARCH_CACHE.clear()

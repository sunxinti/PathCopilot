"""Official FastMCP PubMed evidence server for PathCopilot."""

from __future__ import annotations

import importlib.util
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


@lru_cache(maxsize=1)
def _knowledge_retrieval() -> Any:
    """Load the legacy PubMed helper without importing ``agent.__init__``.

    Importing the package initializes the WSI/LLM runtime, whose historical
    provider status messages are printed to stdout.  That is harmless in the
    web app but corrupts the JSON-RPC stream of a stdio MCP server.
    """

    module_path = (
        Path(__file__).resolve().parents[1] / "agent" / "knowledge_retrieval.py"
    )
    spec = importlib.util.spec_from_file_location(
        "pathcopilot_pubmed_knowledge_retrieval",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load PubMed helper: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _keywords(query: str) -> list[str]:
    try:
        extracted = _knowledge_retrieval().extract_diagnostic_keywords(query)
    except Exception:
        extracted = []
    if extracted:
        return [str(item).strip() for item in extracted if str(item).strip()][:3]
    query = query.translate(str.maketrans({"，": ",", "；": ";"}))
    tokens = [
        item.strip()
        for item in re.split(r"[,;，；\n]+", query)
        if item.strip()
    ]
    return tokens[:3] or [query.strip()]


def create_server() -> FastMCP:
    server = FastMCP(
        "PathCopilot PubMed",
        instructions=(
            "Search PubMed only when the pathology Agent explicitly requests "
            "external evidence. Results are supporting evidence, not a final "
            "diagnosis. Return PMID-linked summaries."
        ),
    )

    @server.tool(name="search_pubmed")
    def search_pubmed_tool(
        query: str,
        max_results: int = 3,
    ) -> dict[str, Any]:
        """Search PubMed for pathology evidence relevant to a specific query."""

        clean_query = " ".join(str(query).split()).strip()
        if not clean_query:
            raise ValueError("query is required")
        limit = min(max(int(max_results), 1), 5)
        knowledge_retrieval = _knowledge_retrieval()

        api_key = os.environ.get("PUBMED_API_KEY", "").strip() or None
        knowledge_retrieval.PUBMED_API_KEY = api_key
        knowledge_retrieval.REQUEST_INTERVAL = 0.1 if api_key else 0.35

        keywords = _keywords(clean_query)
        articles = knowledge_retrieval.search_pubmed(
            keywords,
            max_results=limit,
        )
        compact = [
            {
                key: article.get(key, "")
                for key in (
                    "pmid",
                    "title",
                    "abstract",
                    "first_author",
                    "year",
                    "journal",
                )
            }
            for article in articles[:limit]
        ]
        return {
            "source": "PubMed",
            "query": clean_query,
            "keywords": keywords,
            "count": len(compact),
            "articles": compact,
            "context": knowledge_retrieval.format_knowledge_context(compact)
            if compact
            else "No matching PubMed articles were found.",
        }

    return server


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()

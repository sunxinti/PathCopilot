#!/usr/bin/env python3
"""Resolve PathCopilot cohort file lists through the GDC API and download them.

The default action only creates a standard GDC manifest and an audit JSON file.
Pass --download to invoke gdc-client after manifest resolution.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_ENDPOINT = "https://api.gdc.cancer.gov/files"
FIELDS = "file_id,file_name,md5sum,file_size,state,access,cases.submitter_id"


def load_requested_names(path: Path) -> list[str]:
    names = [
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate names in {path}: {duplicates[:5]}")
    return names


def query_project(endpoint: str, project: str, timeout: int) -> list[dict[str, Any]]:
    filters = {
        "op": "and",
        "content": [
            {"op": "=", "content": {"field": "cases.project.project_id", "value": project}},
            {"op": "=", "content": {"field": "data_type", "value": "Slide Image"}},
            {"op": "=", "content": {"field": "data_format", "value": "SVS"}},
            {"op": "=", "content": {"field": "access", "value": "open"}},
        ],
    }
    payload = urllib.parse.urlencode(
        {
            "filters": json.dumps(filters, separators=(",", ":")),
            "fields": FIELDS,
            "format": "JSON",
            "size": "5000",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "PathCopilot-cohort-downloader/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GDC API returned HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach GDC API: {exc.reason}") from exc
    return list(body.get("data", {}).get("hits", []))


def resolve_records(requested: list[str], hits: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str], dict[str, list[str]]]:
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_slide: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for hit in hits:
        name = str(hit.get("file_name", ""))
        if not name:
            continue
        by_name[name.lower()].append(hit)
        by_slide[name.split(".", 1)[0].upper()].append(hit)

    resolved: list[dict[str, Any]] = []
    unresolved: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    for name in requested:
        candidates = by_name.get(name.lower(), [])
        if not candidates:
            candidates = by_slide.get(name.split(".", 1)[0].upper(), [])
        if len(candidates) == 1:
            resolved.append(candidates[0])
        elif not candidates:
            unresolved.append(name)
        else:
            ambiguous[name] = [str(item.get("file_name", "")) for item in candidates]
    return resolved, unresolved, ambiguous


def write_manifest(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["id\tfilename\tmd5\tsize\tstate"]
    for record in sorted(records, key=lambda item: str(item.get("file_name", "")).lower()):
        lines.append(
            "\t".join(
                [
                    str(record.get("file_id", "")),
                    str(record.get("file_name", "")),
                    str(record.get("md5sum", "")),
                    str(record.get("file_size", "")),
                    str(record.get("state", "")),
                ]
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def run_download(gdc_client: str, manifest: Path, destination: Path, token_file: Path | None) -> int:
    executable = shutil.which(gdc_client) if Path(gdc_client).name == gdc_client else gdc_client
    if not executable or not Path(executable).exists():
        raise FileNotFoundError(
            "gdc-client was not found. Install it from https://gdc.cancer.gov/access-data/gdc-data-transfer-tool "
            "or pass --gdc-client /path/to/gdc-client."
        )
    destination.mkdir(parents=True, exist_ok=True)
    command = [str(executable), "download", "-m", str(manifest), "-d", str(destination)]
    if token_file is not None:
        command.extend(["-t", str(token_file)])
    print("Running:", " ".join(command))
    return subprocess.run(command, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, choices=["TCGA-UCEC", "TCGA-COAD", "TCGA-LUAD"])
    parser.add_argument("--id-file", type=Path, required=True, help="text file containing exact SVS file names")
    parser.add_argument("--work-dir", type=Path, required=True, help="manifest, audit, and optional download directory")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--download", action="store_true", help="invoke gdc-client after resolving the manifest")
    parser.add_argument("--gdc-client", default="gdc-client")
    parser.add_argument("--token-file", type=Path, help="optional GDC token; not needed for open TCGA slide images")
    args = parser.parse_args()

    requested = load_requested_names(args.id_file)
    print(f"Resolving {len(requested)} requested {args.project} SVS files through {args.endpoint}")
    hits = query_project(args.endpoint, args.project, args.timeout)
    resolved, unresolved, ambiguous = resolve_records(requested, hits)

    args.work_dir.mkdir(parents=True, exist_ok=True)
    manifest = args.work_dir / f"{args.project.lower()}_gdc_manifest.tsv"
    audit = args.work_dir / f"{args.project.lower()}_resolution.json"
    write_manifest(manifest, resolved)
    audit.write_text(
        json.dumps(
            {
                "project": args.project,
                "requested": len(requested),
                "gdc_project_hits": len(hits),
                "resolved": len(resolved),
                "unresolved": unresolved,
                "ambiguous": ambiguous,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"Resolved: {len(resolved)}/{len(requested)}")
    print(f"Manifest: {manifest}")
    print(f"Audit: {audit}")
    if unresolved or ambiguous:
        print(
            f"Resolution is incomplete ({len(unresolved)} unresolved, {len(ambiguous)} ambiguous). "
            "Review the audit file before downloading.",
            file=sys.stderr,
        )
        return 2
    if args.download:
        return run_download(args.gdc_client, manifest, args.work_dir / "files", args.token_file)
    print("Manifest only. Re-run with --download to invoke gdc-client.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

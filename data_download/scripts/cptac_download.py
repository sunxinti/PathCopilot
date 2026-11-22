#!/usr/bin/env python3
"""Validate or download the frozen CPTAC-UCEC slide manifest from TCIA PathDB."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ALLOWED_HOST = "pathdb.cancerimagingarchive.net"


def load_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {"patient_id", "slide_id", "tissue_group", "file_name", "download_url"}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"manifest is missing columns: {sorted(missing)}")
        rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]

    seen: set[str] = set()
    for row in rows:
        slide_id = row["slide_id"]
        if not slide_id or slide_id in seen:
            raise ValueError(f"empty or duplicate slide_id: {slide_id!r}")
        seen.add(slide_id)
        if row["file_name"] != f"{slide_id}.svs":
            raise ValueError(f"file name does not match slide ID: {row['file_name']}")
        parsed = urlparse(row["download_url"])
        if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
            raise ValueError(f"unexpected download host for {slide_id}: {row['download_url']}")
    return rows


def probe(row: dict[str, str], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        row["download_url"],
        headers={"User-Agent": "PathCopilot-cohort-downloader/1.0", "Range": "bytes=0-0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(1)
            return {
                "slide_id": row["slide_id"],
                "ok": response.status in {200, 206},
                "status": response.status,
                "content_range": response.headers.get("Content-Range", ""),
                "content_length": response.headers.get("Content-Length", ""),
            }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"slide_id": row["slide_id"], "ok": False, "error": str(exc)}


def download_one(row: dict[str, str], output_dir: Path, timeout: int, overwrite: bool) -> dict[str, Any]:
    patient_dir = output_dir / row["patient_id"]
    destination = patient_dir / row["file_name"]
    if destination.exists() and destination.stat().st_size > 0 and not overwrite:
        return {"slide_id": row["slide_id"], "status": "existing", "path": str(destination)}

    patient_dir.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(
        row["download_url"], headers={"User-Agent": "PathCopilot-cohort-downloader/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, partial.open("wb") as handle:
            shutil.copyfileobj(response, handle, length=1024 * 1024)
        os.replace(partial, destination)
        return {
            "slide_id": row["slide_id"],
            "status": "downloaded",
            "path": str(destination),
            "size": destination.stat().st_size,
        }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"slide_id": row["slide_id"], "status": "failed", "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--check-urls", action="store_true", help="probe each public TCIA URL without downloading the WSI")
    parser.add_argument("--download", action="store_true", help="download all slides in the frozen manifest")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    rows = load_manifest(args.manifest)
    print(f"Validated CPTAC-UCEC manifest: {len(rows)} slides")
    if not args.check_urls and not args.download:
        print("Manifest only. Use --check-urls to probe TCIA or --download to fetch the SVS files.")
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.check_urls:
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {pool.submit(probe, row, args.timeout): row for row in rows}
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                print(f"[{len(results)}/{len(rows)}] {result['slide_id']}: {'ok' if result.get('ok') else 'failed'}")
        audit = args.output_dir / "cptac_ucec_url_audit.json"
        audit.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        failures = [item for item in results if not item.get("ok")]
        print(f"URL check: {len(results) - len(failures)}/{len(results)} reachable; audit: {audit}")
        if failures:
            return 2

    if args.download:
        results = []
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {
                pool.submit(download_one, row, args.output_dir / "files", args.timeout, args.overwrite): row
                for row in rows
            }
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                print(f"[{len(results)}/{len(rows)}] {result['slide_id']}: {result['status']}")
        audit = args.output_dir / "cptac_ucec_download_audit.json"
        audit.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        failures = [item for item in results if item["status"] == "failed"]
        print(f"Download audit: {audit}")
        if failures:
            print(f"Failed downloads: {len(failures)}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

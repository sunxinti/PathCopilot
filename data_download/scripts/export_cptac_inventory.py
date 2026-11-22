#!/usr/bin/env python3
"""Export a sanitized CPTAC-UCEC cohort manifest from the frozen mapping CSV.

Only public identifiers and TCIA download URLs are written. Local filesystem
paths and other workstation-specific fields are deliberately omitted.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from urllib.parse import urlparse


EXPECTED_COLUMNS = {"patient", "slide_id", "group", "file_name", "download_url"}
ALLOWED_HOST = "pathdb.cancerimagingarchive.net"


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = EXPECTED_COLUMNS.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"missing columns in {path}: {sorted(missing)}")
        rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]

    seen: set[str] = set()
    for row in rows:
        slide_id = row["slide_id"]
        if not slide_id or slide_id in seen:
            raise ValueError(f"empty or duplicate slide_id: {slide_id!r}")
        seen.add(slide_id)
        if row["file_name"] != f"{slide_id}.svs":
            raise ValueError(f"unexpected file name for {slide_id}: {row['file_name']}")
        parsed = urlparse(row["download_url"])
        if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
            raise ValueError(f"unexpected download URL for {slide_id}: {row['download_url']}")
    return sorted(rows, key=lambda row: row["slide_id"])


def write_lines(path: Path, header: list[str], values: list[str]) -> None:
    path.write_text("\n".join([*header, *values]) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping-csv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected", type=int, default=135)
    args = parser.parse_args()

    rows = read_rows(args.mapping_csv)
    if len(rows) != args.expected:
        raise ValueError(f"expected {args.expected} rows, found {len(rows)}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    common = [
        "# project=CPTAC-UCEC",
        f"# analytic_wsi_count={len(rows)}",
        f"# unique_patient_count={len({row['patient'] for row in rows})}",
        "# source=TCIA PathDB/caMicroscope",
        "# This public-identifier inventory contains no image data or local paths.",
    ]
    write_lines(
        args.output_dir / "cptac_ucec_slide_ids.txt",
        [*common, "# One CPTAC slide ID per non-comment line."],
        [row["slide_id"] for row in rows],
    )
    write_lines(
        args.output_dir / "cptac_ucec_svs_files.txt",
        [*common, "# One exact SVS file name per non-comment line."],
        [row["file_name"] for row in rows],
    )
    manifest = args.output_dir / "cptac_ucec_download_manifest.tsv"
    with manifest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["patient_id", "slide_id", "tissue_group", "file_name", "download_url"],
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "patient_id": row["patient"],
                    "slide_id": row["slide_id"],
                    "tissue_group": row["group"],
                    "file_name": row["file_name"],
                    "download_url": row["download_url"],
                }
            )
    print(f"CPTAC-UCEC: exported {len(rows)} slides from {len({row['patient'] for row in rows})} patients")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

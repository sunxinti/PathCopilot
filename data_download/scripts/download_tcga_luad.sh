#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOWNLOAD_ARGS=()
if [[ "${1:-}" == "--download" ]]; then DOWNLOAD_ARGS+=(--download); fi
python3 "$ROOT/scripts/gdc_download.py" --project TCGA-LUAD --id-file "$ROOT/ids/tcga_luad_svs_files.txt" --work-dir "$ROOT/work/tcga_luad" "${DOWNLOAD_ARGS[@]}"

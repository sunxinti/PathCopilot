#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOWNLOAD_ARGS=()
if [[ "${1:-}" == "--download" ]]; then DOWNLOAD_ARGS+=(--download); fi
python3 "$ROOT/scripts/gdc_download.py" --project TCGA-UCEC --id-file "$ROOT/ids/tcga_ucec_svs_files.txt" --work-dir "$ROOT/work/tcga_ucec" "${DOWNLOAD_ARGS[@]}"

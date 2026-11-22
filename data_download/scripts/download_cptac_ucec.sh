#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --check-urls|--download) ARGS+=("$arg") ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done
python3 "$ROOT/scripts/cptac_download.py" --manifest "$ROOT/ids/cptac_ucec_download_manifest.tsv" --output-dir "$ROOT/work/cptac_ucec" "${ARGS[@]}"

#!/usr/bin/env python3
"""Coordinate manifest for feature extraction: one row per slide with its SVS file and CLAM patch coordinates.

Expects CLAM patching output (create_patches_fp.py) under --patch-root, grouped by patch size, i.e.
<patch-root>/ps<size>/patches/<svs stem>.h5. The patch size for each slide is chosen so that the read
region corresponds to about 20x (0.5 um/px) before resizing to 224 px; slides are patched with CLAM's
tcga.csv preset.

Output: TSV with columns slide, svs_path, patch_size, patch_level, n_coords, patch_h5.
The slide ID is the SVS file stem up to the first '.', e.g. TCGA-xx-xxxx-01Z-00-DX1.
"""
import argparse
import csv
import re
from pathlib import Path

import h5py


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--svs-dir", required=True)
    ap.add_argument("--patch-root", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rows, missing = [], []
    for svs in sorted(Path(args.svs_dir).glob("*.svs")):
        hits = sorted(Path(args.patch_root).glob(f"ps*/patches/{svs.stem}.h5"))
        if not hits:
            missing.append(svs.stem)
            continue
        with h5py.File(hits[0], "r") as f:
            n, level = f["coords"].shape[0], int(f["coords"].attrs.get("patch_level", 0))
        rows.append({"slide": svs.stem.split(".")[0], "svs_path": str(svs),
                     "patch_size": re.match(r"ps(\d+)", hits[0].parent.parent.name).group(1),
                     "patch_level": level, "n_coords": n, "patch_h5": str(hits[0])})
    if not rows:
        raise SystemExit("no slides with patch coordinates")
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"{args.out}: {len(rows)} slides, {sum(r['n_coords'] for r in rows)} patches; no coordinates: {missing}")


if __name__ == "__main__":
    main()

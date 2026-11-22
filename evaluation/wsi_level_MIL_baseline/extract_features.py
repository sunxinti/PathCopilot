#!/usr/bin/env python3
"""CONCH / UNI patch-feature extraction on CLAM patch coordinates, tolerant of unreadable tiles.

* Slides, SVS files and coordinates come from the manifest written by build_coord_manifest.py, so both
  encoders see exactly the same patches.
* Each read region (patch_size px at patch_level, about 20x) is resized to 224 px by the encoder
  transform from CLAM's get_encoder.
* A tile whose read raises (e.g. a corrupt JPEG tile in the source SVS) is skipped and logged, and the
  OpenSlide handle is reopened, instead of aborting the slide.
* Output: <out-dir>/h5_files/<slide>.h5 with datasets "features" (N x D), "coords" and "skipped_coords";
  an existing output file is never overwritten, and an interrupted slide resumes from its .partial file.

Requires CLAM (https://github.com/mahmoodlab/CLAM, set CLAM_ROOT) and the gated encoder weights,
whose paths are given by CONCH_CKPT_PATH or UNI_CKPT_PATH as expected by CLAM's get_encoder.
--scan-only reads every tile without a model or GPU and reports unreadable tiles.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import h5py
import numpy as np
import openslide

CKPT_ENV = {"conch_v1": "CONCH_CKPT_PATH", "uni_v1": "UNI_CKPT_PATH"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--encoder", choices=sorted(CKPT_ENV), required=True)
    p.add_argument("--manifest", required=True, help="TSV from build_coord_manifest.py")
    p.add_argument("--slides", default="all", help="'all' or a text file with one slide ID per line")
    p.add_argument("--out-dir", required=True, help="e.g. mil_workdir/features/tcga/conch")
    p.add_argument("--batch-size", type=int, default=256)
    p.add_argument("--read-workers", type=int, default=8)
    p.add_argument("--target-patch-size", type=int, default=224)
    p.add_argument("--scan-only", action="store_true")
    return p.parse_args()


def load_rows(args) -> list[dict]:
    with open(args.manifest, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    if args.slides != "all":
        wanted = [s.strip() for s in Path(args.slides).read_text().splitlines() if s.strip()]
        by_slide = {r["slide"]: r for r in rows}
        missing = [s for s in wanted if s not in by_slide]
        if missing:
            raise SystemExit(f"Slides not in manifest: {missing}")
        rows = [by_slide[s] for s in wanted]
    return rows


class TileReader:
    """Thread-local OpenSlide handles; a handle is reopened after any read error."""

    def __init__(self, svs_path: str, level: int, size: int, transform):
        self.svs_path, self.level, self.size, self.transform = svs_path, level, size, transform
        self.local = threading.local()
        self.handles, self.lock = [], threading.Lock()

    def _slide(self):
        s = getattr(self.local, "slide", None)
        if s is None:
            s = openslide.OpenSlide(self.svs_path)
            self.local.slide = s
            with self.lock:
                self.handles.append(s)
        return s

    def read(self, xy):
        try:
            img = self._slide().read_region(xy, self.level, (self.size, self.size)).convert("RGB")
            return (self.transform(img) if self.transform else True), None
        except Exception as exc:  # unreadable JPEG tile, latched OpenSlide error, ...
            old = getattr(self.local, "slide", None)
            self.local.slide = None
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            return None, f"{type(exc).__name__}: {exc}"[:300]

    def close(self):
        with self.lock:
            for s in self.handles:
                try:
                    s.close()
                except Exception:
                    pass


def init_partial(path: Path, dim: int):
    with h5py.File(path, "w") as h:
        h.create_dataset("features", shape=(0, dim), maxshape=(None, dim), dtype="float32", chunks=True)
        h.create_dataset("coords", shape=(0, 2), maxshape=(None, 2), dtype="int64", chunks=True)
        h.create_dataset("skipped_coords", shape=(0, 2), maxshape=(None, 2), dtype="int64", chunks=True)
        h.attrs["next_index"] = 0


def append(ds, data):
    old = ds.shape[0]
    ds.resize(old + data.shape[0], axis=0)
    ds[old:] = data


def process_slide(row, args, model, transform, device, dim) -> dict:
    key = row["slide"]
    out_dir = Path(args.out_dir) / ("scan" if args.scan_only else "h5_files")
    rep_dir = Path(args.out_dir) / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    rep_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"{key}.h5"
    report_path = rep_dir / f"{key}{'.scan' if args.scan_only else ''}.json"
    if (final.exists() and not args.scan_only) or (args.scan_only and report_path.exists()):
        return {"slide": key, "status": "skipped_existing"}

    with h5py.File(row["patch_h5"], "r") as h:
        coords = np.asarray(h["coords"][:], dtype=np.int64)
    level, size = int(row["patch_level"]), int(row["patch_size"])
    reader = TileReader(row["svs_path"], level, size, None if args.scan_only else transform)
    started = time.time()
    errors: list[str] = []

    if args.scan_only:
        n_bad = 0
        with ThreadPoolExecutor(args.read_workers) as ex:
            for ok, err in ex.map(reader.read, (tuple(map(int, c)) for c in coords)):
                if ok is None:
                    n_bad += 1
                    if len(errors) < 20:
                        errors.append(err)
        reader.close()
        report = {"slide": key, "mode": "scan", "n_coords": len(coords), "unreadable": n_bad,
                  "readable_fraction": 1 - n_bad / len(coords), "first_errors": errors,
                  "elapsed_seconds": round(time.time() - started, 1)}
        report_path.write_text(json.dumps(report, indent=1))
        return {k: report[k] for k in ("slide", "n_coords", "unreadable", "readable_fraction", "elapsed_seconds")}

    import torch
    partial = out_dir / f"{key}.partial.h5"
    if not partial.exists():
        init_partial(partial, dim)
    with h5py.File(partial, "r") as h:
        start = int(h.attrs["next_index"])
    with ThreadPoolExecutor(args.read_workers) as ex:
        for b0 in range(start, len(coords), args.batch_size):
            batch = [tuple(map(int, c)) for c in coords[b0:b0 + args.batch_size]]
            results = list(ex.map(reader.read, batch))
            imgs = [t for t, _ in results if t is not None]
            kept = [xy for xy, (t, _) in zip(batch, results) if t is not None]
            bad = [xy for xy, (t, _) in zip(batch, results) if t is None]
            errors += [e for _, e in results if e][: max(0, 50 - len(errors))]
            feats = np.zeros((0, dim), np.float32)
            if imgs:
                with torch.inference_mode():
                    feats = model(torch.stack(imgs).to(device, non_blocking=True)).float().cpu().numpy()
            with h5py.File(partial, "a") as h:
                append(h["features"], feats)
                append(h["coords"], np.asarray(kept, np.int64).reshape(-1, 2))
                append(h["skipped_coords"], np.asarray(bad, np.int64).reshape(-1, 2))
                h.attrs["next_index"] = b0 + len(batch)
    reader.close()

    with h5py.File(partial, "a") as h:
        n_feat, n_skip = h["features"].shape[0], h["skipped_coords"].shape[0]
        if n_feat == 0:
            raise RuntimeError(f"{key}: no readable patches")
        h.attrs.update({"complete": True, "encoder": args.encoder, "embedding_dim": dim, "slide": key,
                        "read_patch_size": size, "patch_level": level, "target_patch_size": args.target_patch_size,
                        "source_patch_count": len(coords), "readable_patch_count": n_feat,
                        "unreadable_patch_count": n_skip})
    os.rename(partial, final)
    report = {"slide": key, "n_coords": len(coords), "readable": n_feat, "unreadable": n_skip,
              "first_errors": errors[:20], "elapsed_seconds": round(time.time() - started, 1)}
    report_path.write_text(json.dumps(report, indent=1))
    return {k: report[k] for k in ("slide", "n_coords", "readable", "unreadable", "elapsed_seconds")}


def main() -> None:
    args = parse_args()
    rows = load_rows(args)
    model = transform = device = None
    dim = 0
    if not args.scan_only:
        import torch
        ckpt = os.environ.get(CKPT_ENV[args.encoder])
        if not ckpt or not Path(ckpt).is_file():
            raise SystemExit(f"Set {CKPT_ENV[args.encoder]} to the {args.encoder} checkpoint")
        clam_root = os.environ.get("CLAM_ROOT")
        if not clam_root:
            raise SystemExit("Set CLAM_ROOT to a local clone of https://github.com/mahmoodlab/CLAM")
        sys.path.insert(0, clam_root)
        from models import get_encoder
        device = torch.device("cuda")
        model, transform = get_encoder(args.encoder, target_img_size=args.target_patch_size)
        model = model.eval().to(device)
        with torch.inference_mode():
            dim = int(model(torch.zeros(1, 3, args.target_patch_size, args.target_patch_size, device=device)).shape[-1])
    for i, row in enumerate(rows, 1):
        try:
            res = process_slide(row, args, model, transform, device, dim)
        except Exception as exc:
            res = {"slide": row["slide"], "status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}
        res["i"] = f"{i}/{len(rows)}"
        print(json.dumps(res), flush=True)


if __name__ == "__main__":
    main()

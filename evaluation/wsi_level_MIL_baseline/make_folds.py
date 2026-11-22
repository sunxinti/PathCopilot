#!/usr/bin/env python3
"""Patient-level 5-fold split, stratified on the Q2 x Q3 combination, generated once and shared by
every question, encoder and aggregator. Asserts that no patient appears in more than one fold."""
import argparse
import csv
from collections import Counter
from pathlib import Path

from sklearn.model_selection import StratifiedGroupKFold

from mil_common import add_root_arg, load_labels


def main():
    ap = argparse.ArgumentParser()
    add_root_arg(ap)
    ap.add_argument("--labels", required=True, help="label set name created by make_labels.py")
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()

    rows, _ = load_labels(args.root, args.labels)
    y = [f"{r['Q2']}_{r['Q3']}" for r in rows]
    groups = [r["patient"] for r in rows]
    fold_of = {}
    skf = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=args.seed)
    for k, (_, test_idx) in enumerate(skf.split(rows, y, groups)):
        for i in test_idx:
            fold_of[rows[i]["slide"]] = k
    pat_folds = {}
    for r in rows:
        pat_folds.setdefault(r["patient"], set()).add(fold_of[r["slide"]])
    assert all(len(v) == 1 for v in pat_folds.values()), "a patient spans several folds"
    out = Path(args.root) / "labels" / "folds.tsv"
    with open(out, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["slide", "patient", "fold"])
        for r in rows:
            w.writerow([r["slide"], r["patient"], fold_of[r["slide"]]])
    print(f"{out}: {len(rows)} slides, {len(pat_folds)} patients, seed {args.seed}")
    for k in range(5):
        members = [r for r in rows if fold_of[r["slide"]] == k]
        print(f"fold {k}: {len(members)} slides, {len({r['patient'] for r in members})} patients, "
              f"Q2 {dict(Counter(r['Q2'] for r in members))}, Q3 {dict(Counter(r['Q3'] for r in members))}")


if __name__ == "__main__":
    main()

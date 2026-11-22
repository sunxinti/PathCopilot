#!/usr/bin/env python3
"""Convert a long-format label sheet into labels/<name>.tsv and labels/<name>.meta.json.

Input TSV columns (one row per slide x question): slide, patient, question (Q1..Q6),
label (class name), code (integer class code, 0..K-1 within each question).
Every code must map to exactly one class name per question, and every slide must have all six questions.
"""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

from mil_common import QUESTIONS, add_root_arg


def main():
    ap = argparse.ArgumentParser()
    add_root_arg(ap)
    ap.add_argument("--long-tsv", required=True)
    ap.add_argument("--name", required=True, help="label set name, e.g. tcga_ucec")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.long_tsv, encoding="utf-8-sig"), delimiter="\t"))
    names = defaultdict(dict)          # question -> code -> name
    wide = {}
    for r in rows:
        q, code, name = r["question"], int(r["code"]), r["label"]
        if names[q].setdefault(code, name) != name:
            raise SystemExit(f"{q}: code {code} maps to both {names[q][code]} and {name}")
        wide.setdefault(r["slide"], {"slide": r["slide"], "patient": r["patient"]})[q] = code
    bad = [s for s, w in wide.items() if any(q not in w for q in QUESTIONS)]
    if bad:
        raise SystemExit(f"slides missing a question: {bad[:5]}")
    if any(sorted(names[q]) != list(range(len(names[q]))) for q in QUESTIONS):
        raise SystemExit("class codes must be 0..K-1 for every question")

    out_dir = Path(args.root) / "labels"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{args.name}.tsv"
    with open(out, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["slide", "patient", *QUESTIONS], delimiter="\t")
        w.writeheader()
        for s in sorted(wide):
            w.writerow(wide[s])
    meta = {q: [names[q][c] for c in sorted(names[q])] for q in QUESTIONS}
    (out_dir / f"{args.name}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{out}: {len(wide)} slides")
    for q in QUESTIONS:
        counts = defaultdict(int)
        for w_ in wide.values():
            counts[w_[q]] += 1
        print(q, {meta[q][c]: counts[c] for c in sorted(counts)})


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""One-shot MIL baselines: one labelled slide per class, test folds unchanged.

For each question, fold and seed, the training pool is the training part of the fully supervised run
(same validation split, so validation patients stay unused). One slide is drawn at random for every
class present in the pool. There is no validation set: training runs for the median selected epoch
count of the three fully supervised runs with the same encoder, aggregator, question and fold, so
train_mil.py must have finished first. Classes absent from the training subset cannot be predicted
and are masked out of the argmax.

Writes runs/<tag>/<encoder>/<agg>/<question>/oneshot/seed<s>/fold<k>/{pred.tsv,run.json}, plus
weights.pt with --save-weights. By default, each fold is trained with seeds 0, 1 and 2 in separate runs.
Each seed controls both the slide draw and the training randomness.
"""
import argparse
import csv
import json
import random
import statistics
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import torch

from mil_common import ENCODERS, FOLDS, QUESTIONS, SEEDS, add_root_arg, build_model, load_features_to_gpu, load_folds, load_labels
from train_mil import evaluate, forward, set_seed, val_split


def parse():
    ap = argparse.ArgumentParser()
    add_root_arg(ap)
    ap.add_argument("--encoder", choices=sorted(ENCODERS), required=True)
    ap.add_argument("--agg", choices=["avgpool", "abmil", "clam_sb"], required=True)
    ap.add_argument("--tasks", nargs="+", default=list(QUESTIONS))
    ap.add_argument("--folds", nargs="+", type=int, default=list(FOLDS))
    ap.add_argument("--seeds", nargs="+", type=int, default=list(SEEDS))
    ap.add_argument("--labels", required=True, help="label set name created by make_labels.py")
    ap.add_argument("--cohort", default="tcga", help="feature sub-directory under features/")
    ap.add_argument("--main-tag", default="full", help="tag of the fully supervised run (source of epochs)")
    ap.add_argument("--tag", default="oneshot", help="output namespace under runs/")
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--wd", type=float, default=1e-5)
    ap.add_argument("--dropout", type=float, default=0.25)
    ap.add_argument("--val-splits", type=int, default=7)
    ap.add_argument("--save-weights", action="store_true")
    ap.add_argument("--device", default="cuda:0")
    return ap.parse_args()


def main_run(args, task, fold):
    """Median selected epoch count and n_train of the fully supervised runs for this question and fold."""
    d = Path(args.root) / "runs" / args.main_tag / args.encoder / args.agg / task
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in d.glob(f"seed*/fold{fold}/run.json")]
    assert len(runs) == len(SEEDS), f"{d}: expected {len(SEEDS)} fully supervised runs for fold {fold}, found {len(runs)}"
    return int(statistics.median(r["best_epoch"] + 1 for r in runs)), runs[0]["n_train"]


def draw_oneshot(pool, labels, task, fold, seed):
    """One slide per class present in the pool; deterministic for a given question, fold and seed."""
    rng = random.Random(f"{task}-{fold}-{seed}")
    by_class = defaultdict(list)
    for s in sorted(pool):
        by_class[labels[s][task]].append(s)
    return sorted(rng.choice(v) for _, v in sorted(by_class.items()))


def run_one(args, task, fold, seed, feats, labels, patient, fold_of, n_classes, out_dir):
    device = torch.device(args.device)
    slides = [s for s in labels if s in fold_of]
    test = [s for s in slides if fold_of[s] == fold]
    train_all = [s for s in slides if fold_of[s] != fold]
    pool, _ = val_split(train_all, labels, patient, task, fold, args.val_splits)
    epochs, n_main = main_run(args, task, fold)
    assert len(pool) == n_main, (task, fold, len(pool), n_main)
    train = draw_oneshot(pool, labels, task, fold, seed)
    assert not ({patient[s] for s in test} & {patient[s] for s in train}), "patient leakage across folds"
    seen = sorted({labels[s][task] for s in train})

    set_seed(seed)
    t0 = time.time()
    model = build_model(args.agg, ENCODERS[args.encoder], n_classes, args.dropout).to(device)
    opt = torch.optim.Adam(filter(lambda p: p.requires_grad, model.parameters()), lr=args.lr, weight_decay=args.wd)
    for _ in range(epochs):
        model.train()
        order = train[:]
        random.shuffle(order)
        for s in order:
            y = torch.tensor([labels[s][task]], device=device)
            _, loss = forward(model, args.agg, feats[s].float(), y)
            opt.zero_grad()
            loss.backward()
            opt.step()
    _, probs = evaluate(model, args.agg, feats, test, labels, task, device)

    out_dir.mkdir(parents=True, exist_ok=True)
    if args.save_weights:
        torch.save({"state_dict": {k: v.cpu() for k, v in model.state_dict().items()}, "encoder": args.encoder, "agg": args.agg, "task": task,
                    "fold": fold, "seed": seed, "labels": args.labels, "class_names": args.class_names[task], "seen_classes": seen,
                    "dropout": args.dropout, "epochs": epochs}, out_dir / "weights.tmp")
        (out_dir / "weights.tmp").replace(out_dir / "weights.pt")
    with open(out_dir / "pred.tsv", "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["slide", "patient", "fold", "label", "pred"] + [f"p{c}" for c in range(n_classes)])
        for s in test:
            p = probs[s]
            w.writerow([s, patient[s], fold, labels[s][task], seen[int(np.argmax(p[seen]))]] + [f"{v:.6f}" for v in p])
    (out_dir / "run.json").write_text(json.dumps({
        "encoder": args.encoder, "agg": args.agg, "task": task, "tier": "oneshot", "fold": fold, "seed": seed, "labels": args.labels,
        "n_train": len(train), "n_pool": len(pool), "n_test": len(test), "seen_classes": seen, "epochs": epochs,
        "seconds": round(time.time() - t0, 1), "lr": args.lr, "wd": args.wd, "dropout": args.dropout}, indent=1), encoding="utf-8")
    return len(train), epochs, time.time() - t0


def main():
    args = parse()
    rows, meta = load_labels(args.root, args.labels)
    args.class_names = meta
    fold_of = load_folds(args.root)
    labels = {r["slide"]: {q: int(r[q]) for q in QUESTIONS} for r in rows}
    patient = {r["slide"]: r["patient"] for r in rows}
    device = torch.device(args.device)
    t0 = time.time()
    feats, missing = load_features_to_gpu(args.root, args.cohort, args.encoder, sorted(labels), device)
    assert not missing, f"missing features: {missing[:5]}"
    print(f"[{args.encoder} {args.agg}] loaded {len(feats)} slides to {device} in {time.time()-t0:.0f}s", flush=True)
    for task in args.tasks:
        for seed in args.seeds:
            for fold in args.folds:
                out = Path(args.root) / "runs" / args.tag / args.encoder / args.agg / task / "oneshot" / f"seed{seed}" / f"fold{fold}"
                if (out / "run.json").exists():  # written last
                    continue
                n, ep, sec = run_one(args, task, fold, seed, feats, labels, patient, fold_of, len(meta[task]), out)
                print(json.dumps({"task": task, "seed": seed, "fold": fold, "n_train": n, "epochs": ep, "sec": round(sec, 1)}), flush=True)


if __name__ == "__main__":
    main()

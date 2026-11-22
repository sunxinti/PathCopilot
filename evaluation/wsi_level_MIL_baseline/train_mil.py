#!/usr/bin/env python3
"""Fully supervised MIL baselines with patient-level 5-fold cross-validation.

One process = one encoder: features are loaded onto the GPU once, then every requested
aggregator x question x seed x fold run trains from that copy. Each run writes
runs/<tag>/<encoder>/<agg>/<question>/seed<s>/fold<k>/{pred.tsv,run.json}, plus weights.pt with
--save-weights. Finished runs (run.json present) are skipped, so an interrupted job can be restarted.

Fixed CLAM defaults, no hyperparameter search: Adam, lr 1e-4, weight decay 1e-5, dropout 0.25,
up to 200 epochs, early stopping on validation loss (patience 20, not before epoch 50); the
best-validation-loss weights are used for the test fold. Plain cross-entropy, no class reweighting
or oversampling. CLAM-SB adds its instance-clustering loss (bag 0.7, instance 0.3).
Validation = about one-seventh of the training patients (stratified, fixed per question and fold),
never the test fold. By default, each fold is trained with seeds 0, 1 and 2 in separate runs.
"""
import argparse
import copy
import csv
import json
import random
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from sklearn.model_selection import StratifiedGroupKFold

from mil_common import ENCODERS, FOLDS, QUESTIONS, SEEDS, add_root_arg, build_model, load_features_to_gpu, load_folds, load_labels


def parse():
    ap = argparse.ArgumentParser()
    add_root_arg(ap)
    ap.add_argument("--encoder", choices=sorted(ENCODERS), required=True)
    ap.add_argument("--aggs", nargs="+", default=["avgpool", "abmil", "clam_sb"])
    ap.add_argument("--tasks", nargs="+", default=list(QUESTIONS))
    ap.add_argument("--folds", nargs="+", type=int, default=list(FOLDS))
    ap.add_argument("--seeds", nargs="+", type=int, default=list(SEEDS))
    ap.add_argument("--labels", required=True, help="label set name created by make_labels.py")
    ap.add_argument("--cohort", default="tcga", help="feature sub-directory under features/")
    ap.add_argument("--tag", default="full", help="output namespace under runs/")
    ap.add_argument("--max-epochs", type=int, default=200)
    ap.add_argument("--patience", type=int, default=20)
    ap.add_argument("--stop-epoch", type=int, default=50)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--wd", type=float, default=1e-5)
    ap.add_argument("--dropout", type=float, default=0.25)
    ap.add_argument("--bag-weight", type=float, default=0.7)
    ap.add_argument("--val-splits", type=int, default=7, help="1/val-splits of training patients -> validation")
    ap.add_argument("--save-weights", action="store_true", help="keep the best-validation-loss weights for external testing")
    ap.add_argument("--device", default="cuda:0")
    return ap.parse_args()


def set_seed(seed):
    random.seed(seed); np.random.seed(seed); torch.manual_seed(seed); torch.cuda.manual_seed_all(seed)


def val_split(train_slides, labels, patient, task, fold, n_splits):
    """Fixed stratified patient-level validation split inside the training folds."""
    y = [labels[s][task] for s in train_slides]
    g = [patient[s] for s in train_slides]
    sgkf = StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=1000 + fold)
    tr_idx, va_idx = next(sgkf.split(train_slides, y, g))
    return [train_slides[i] for i in tr_idx], [train_slides[i] for i in va_idx]


def forward(model, agg, h, y=None, bag_weight=0.7):
    """Returns (logits, loss). CLAM-SB adds its instance-clustering loss during training."""
    if agg == "clam_sb":
        if y is not None and model.training:
            logits, _, _, _, res = model(h, label=y, instance_eval=True)
            loss = bag_weight * F.cross_entropy(logits, y) + (1 - bag_weight) * res["instance_loss"]
            return logits, loss
        logits = model(h)[0]
    else:
        logits = model(h)
    return logits, (F.cross_entropy(logits, y) if y is not None else None)


@torch.no_grad()
def evaluate(model, agg, feats, slides, labels, task, device):
    """Mean validation loss and per-slide class probabilities."""
    model.eval()
    losses, probs = [], {}
    for s in slides:
        y = torch.tensor([labels[s][task]], device=device)
        logits, loss = forward(model, agg, feats[s].float(), y)
        losses.append(loss.item())
        probs[s] = F.softmax(logits, dim=1)[0].cpu().numpy()
    return float(np.mean(losses)) if losses else float("nan"), probs


def run_one(args, agg, task, fold, seed, feats, labels, patient, fold_of, n_classes, out_dir):
    set_seed(seed)
    device = torch.device(args.device)
    slides = [s for s in labels if s in fold_of]
    test = [s for s in slides if fold_of[s] == fold]
    train_all = [s for s in slides if fold_of[s] != fold]
    assert not ({patient[s] for s in test} & {patient[s] for s in train_all}), "patient leakage across folds"
    train, val = val_split(train_all, labels, patient, task, fold, args.val_splits)
    assert not ({patient[s] for s in val} & {patient[s] for s in test})
    train = [s for s in train if s in feats]
    val = [s for s in val if s in feats]

    model = build_model(agg, ENCODERS[args.encoder], n_classes, args.dropout).to(device)
    opt = torch.optim.Adam(filter(lambda p: p.requires_grad, model.parameters()), lr=args.lr, weight_decay=args.wd)
    best = (float("inf"), None, -1)
    bad_epochs, epochs_run, t0 = 0, 0, time.time()
    for epoch in range(args.max_epochs):
        model.train()
        order = train[:]
        random.shuffle(order)
        for s in order:
            y = torch.tensor([labels[s][task]], device=device)
            _, loss = forward(model, agg, feats[s].float(), y, args.bag_weight)
            opt.zero_grad()
            loss.backward()
            opt.step()
        val_loss, _ = evaluate(model, agg, feats, val, labels, task, device)
        epochs_run = epoch + 1
        if val_loss < best[0]:
            best = (val_loss, copy.deepcopy(model.state_dict()), epoch)
            bad_epochs = 0
        else:
            bad_epochs += 1
            if bad_epochs >= args.patience and epoch >= args.stop_epoch:
                break
    model.load_state_dict(best[1])
    _, probs = evaluate(model, agg, feats, [s for s in test if s in feats], labels, task, device)

    out_dir.mkdir(parents=True, exist_ok=True)
    if args.save_weights:
        torch.save({"state_dict": {k: v.cpu() for k, v in best[1].items()}, "encoder": args.encoder, "agg": agg, "task": task,
                    "fold": fold, "seed": seed, "labels": args.labels, "class_names": args.class_names[task], "dropout": args.dropout,
                    "best_epoch": best[2], "best_val_loss": best[0]}, out_dir / "weights.tmp")
        (out_dir / "weights.tmp").replace(out_dir / "weights.pt")
    with open(out_dir / "pred.tsv", "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["slide", "patient", "fold", "label", "pred"] + [f"p{c}" for c in range(n_classes)])
        for s in test:
            if s in probs:
                p = probs[s]
                w.writerow([s, patient[s], fold, labels[s][task], int(p.argmax())] + [f"{v:.6f}" for v in p])
            else:  # no features: no prediction
                w.writerow([s, patient[s], fold, labels[s][task], ""] + [""] * n_classes)
    (out_dir / "run.json").write_text(json.dumps({
        "encoder": args.encoder, "agg": agg, "task": task, "fold": fold, "seed": seed, "labels": args.labels,
        "n_train": len(train), "n_val": len(val), "n_test": len(test),
        "best_epoch": best[2], "epochs_run": epochs_run, "best_val_loss": best[0],
        "seconds": round(time.time() - t0, 1), "lr": args.lr, "wd": args.wd, "dropout": args.dropout,
        "max_epochs": args.max_epochs, "patience": args.patience, "stop_epoch": args.stop_epoch,
        "bag_weight": args.bag_weight}, indent=1), encoding="utf-8")
    return best[2], epochs_run, time.time() - t0


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
    print(f"[{args.encoder}] loaded {len(feats)} slides to {device} in {time.time()-t0:.0f}s; "
          f"missing features: {len(missing)} {missing[:5]}", flush=True)
    for task in args.tasks:
        n_classes = len(meta[task])
        for agg in args.aggs:
            for seed in args.seeds:
                for fold in args.folds:
                    out = Path(args.root) / "runs" / args.tag / args.encoder / agg / task / f"seed{seed}" / f"fold{fold}"
                    if (out / "run.json").exists():  # written last
                        continue
                    be, ep, sec = run_one(args, agg, task, fold, seed, feats, labels, patient, fold_of, n_classes, out)
                    print(json.dumps({"enc": args.encoder, "agg": agg, "task": task, "seed": seed, "fold": fold,
                                      "best_epoch": be, "epochs": ep, "sec": round(sec, 1)}), flush=True)


if __name__ == "__main__":
    main()

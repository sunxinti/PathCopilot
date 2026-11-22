#!/usr/bin/env python3
"""Apply saved TCGA-trained MIL models to an external cohort (e.g. CPTAC-UCEC) without retraining or tuning.

Every saved model (encoder x aggregator x question x seed x fold) is applied separately, and one row
per model and slide is written; no averaging or scoring is done here. One-shot models predict only the
classes seen during training. Q6 (LVSI focus count) is not evaluable in CPTAC-UCEC, so the default
questions are Q1-Q5.

Output: <out> TSV with columns encoder, agg, task, seed, fold, slide, pred, probs.
"""
import argparse
import csv
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

from mil_common import ENCODERS, add_root_arg, build_model, load_features_to_gpu
from train_mil import forward


def main():
    ap = argparse.ArgumentParser()
    add_root_arg(ap)
    ap.add_argument("--tag", default="full", help="training namespace under runs/ (full or oneshot)")
    ap.add_argument("--cohort", default="cptac", help="feature sub-directory of the external cohort")
    ap.add_argument("--slides", required=True, help="text file with one external slide ID per line")
    ap.add_argument("--tasks", nargs="+", default=["Q1", "Q2", "Q3", "Q4", "Q5"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--device", default="cuda:0")
    args = ap.parse_args()
    device = torch.device(args.device)
    slides = [s.strip() for s in Path(args.slides).read_text(encoding="utf-8").splitlines() if s.strip()]

    ckpts = sorted((Path(args.root) / "runs" / args.tag).glob("**/seed*/fold*/weights.pt"))
    ckpts = [c for c in ckpts if c.relative_to(Path(args.root) / "runs" / args.tag).parts[2] in args.tasks]
    if not ckpts:
        raise SystemExit("no weights.pt found; train with --save-weights first")
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["encoder", "agg", "task", "seed", "fold", "slide", "pred", "probs"])
        for enc in sorted(ENCODERS):
            enc_ckpts = [c for c in ckpts if c.relative_to(Path(args.root) / "runs" / args.tag).parts[0] == enc]
            if not enc_ckpts:
                continue
            feats, missing = load_features_to_gpu(args.root, args.cohort, enc, slides, device)
            if missing:
                print(f"[{enc}] missing features for {len(missing)} slides: {missing[:5]}", flush=True)
            for ck in enc_ckpts:
                c = torch.load(ck, map_location="cpu")
                names = c["class_names"]
                seen = c.get("seen_classes", list(range(len(names))))
                model = build_model(c["agg"], ENCODERS[enc], len(names), c["dropout"]).to(device)
                model.load_state_dict(c["state_dict"])
                model.eval()
                with torch.no_grad():
                    for s in slides:
                        if s not in feats:
                            continue
                        p = F.softmax(forward(model, c["agg"], feats[s].float())[0], dim=1)[0].cpu().numpy()
                        pred = names[seen[int(np.argmax(p[seen]))]]
                        w.writerow([enc, c["agg"], c["task"], c["seed"], c["fold"], s, pred, ",".join(f"{v:.6f}" for v in p)])
                del model
                print(enc, c["agg"], c["task"], f"seed{c['seed']}", f"fold{c['fold']}", flush=True)
            del feats
    print(args.out)


if __name__ == "__main__":
    main()

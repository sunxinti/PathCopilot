"""Shared pieces for the supervised MIL baselines on TCGA-UCEC.

Work directory layout (``--root``, default ``./mil_workdir``)::

    labels/<name>.tsv            one row per slide: slide, patient, Q1..Q6 (integer class codes)
    labels/<name>.meta.json      question -> ordered class names (code k is the k-th name)
    labels/folds.tsv             slide, patient, fold (0-4), from make_folds.py
    features/<cohort>/<encoder>/h5_files/<slide>.h5   patch features, dataset "features" (N x D)
    runs/<tag>/...               training outputs written by train_mil.py / train_oneshot.py

Labels and folds are not distributed with this repository; create them with make_labels.py and
make_folds.py from the reference answers.

CLAM-SB and the gated-attention block are imported from the official CLAM repository
(https://github.com/mahmoodlab/CLAM), which is not vendored here. Point CLAM_ROOT to a local clone.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

import h5py
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

QUESTIONS = ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6")
ENCODERS = {"conch": 512, "uni": 1024, "virchow2": 1280}
SEEDS = (0, 1, 2)
FOLDS = (0, 1, 2, 3, 4)


def add_root_arg(ap):
    ap.add_argument("--root", default=os.environ.get("MIL_ROOT", "mil_workdir"),
                    help="work directory with labels/, features/ and runs/ (default: $MIL_ROOT or ./mil_workdir)")


# ---------------------------------------------------------------- labels / folds
def read_tsv(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def load_labels(root, name):
    root = Path(root)
    rows = read_tsv(root / "labels" / f"{name}.tsv")
    meta = json.loads((root / "labels" / f"{name}.meta.json").read_text(encoding="utf-8"))
    return rows, meta


def load_folds(root):
    return {r["slide"]: int(r["fold"]) for r in read_tsv(Path(root) / "labels" / "folds.tsv")}


# ---------------------------------------------------------------- features
def feature_dir(root, cohort, encoder):
    return Path(root) / "features" / cohort / encoder / "h5_files"


def load_features_to_gpu(root, cohort, encoder, slides, device):
    """Load each slide's patch features once into GPU memory (fp16). Returns (features, missing slides)."""
    d = feature_dir(root, cohort, encoder)
    feats, missing = {}, []
    for s in slides:
        p = d / f"{s}.h5"
        if not p.is_file():
            missing.append(s)
            continue
        with h5py.File(p, "r") as h:
            x = np.asarray(h["features"][:], dtype=np.float16)
        if x.ndim != 2 or x.shape[1] != ENCODERS[encoder] or x.shape[0] == 0:
            missing.append(s)
            continue
        feats[s] = torch.from_numpy(x).to(device)
    return feats, missing


# ---------------------------------------------------------------- models
def _clam():
    clam_root = os.environ.get("CLAM_ROOT")
    if not clam_root or not Path(clam_root, "models", "model_clam.py").is_file():
        raise SystemExit("Set CLAM_ROOT to a local clone of https://github.com/mahmoodlab/CLAM")
    if clam_root not in sys.path:
        sys.path.insert(0, clam_root)
    from models.model_clam import CLAM_SB, Attn_Net_Gated  # official implementation
    return CLAM_SB, Attn_Net_Gated


class AvgPoolMIL(nn.Module):
    """Mean of patch features followed by a linear classifier."""
    def __init__(self, embed_dim, n_classes, dropout=0.25):
        super().__init__()
        self.drop = nn.Dropout(dropout)
        self.classifier = nn.Linear(embed_dim, n_classes)

    def forward(self, h):
        return self.classifier(self.drop(h.mean(0, keepdim=True)))


class ABMIL(nn.Module):
    """Gated-attention MIL (Ilse et al., 2018), built from CLAM's Attn_Net_Gated (CLAM-SB without
    the instance-clustering loss)."""
    def __init__(self, embed_dim, n_classes, dropout=0.25):
        super().__init__()
        _, Attn_Net_Gated = _clam()
        self.fc = nn.Sequential(nn.Linear(embed_dim, 512), nn.ReLU(), nn.Dropout(dropout))
        self.attention = Attn_Net_Gated(L=512, D=256, dropout=dropout, n_classes=1)
        self.classifier = nn.Linear(512, n_classes)

    def forward(self, h):
        a, h = self.attention(self.fc(h))         # a: N x 1, h: N x 512
        a = F.softmax(a.transpose(0, 1), dim=1)   # 1 x N
        return self.classifier(a @ h)


def build_model(agg, embed_dim, n_classes, dropout=0.25):
    if agg == "avgpool":
        return AvgPoolMIL(embed_dim, n_classes, dropout)
    if agg == "abmil":
        return ABMIL(embed_dim, n_classes, dropout)
    if agg == "clam_sb":
        CLAM_SB, _ = _clam()
        # CLAM defaults: gated attention, small size, k_sample 8, CE instance loss; categorical tasks -> subtyping
        return CLAM_SB(gate=True, size_arg="small", dropout=dropout, k_sample=8, n_classes=n_classes,
                       instance_loss_fn=nn.CrossEntropyLoss(), subtyping=True, embed_dim=embed_dim)
    raise ValueError(agg)

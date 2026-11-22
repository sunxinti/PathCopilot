# Supervised MIL baselines on TCGA-UCEC

This directory contains the code for the supervised multiple-instance learning (MIL) baselines
reported alongside PathCopilot on the six TCGA-UCEC questions, with external application to CPTAC-UCEC.
Only code is provided: labels, fold assignments, extracted features, model weights and predictions
are not distributed.

## Design

| Item | Setting |
| --- | --- |
| Slides | 562 TCGA-UCEC diagnostic WSIs; 135 CPTAC-UCEC WSIs for external application |
| Encoders (frozen) | CONCH (512-d), UNI (1,024-d), Virchow2 (1,280-d) |
| Aggregators | Mean pooling + linear classifier (`avgpool`), gated-attention MIL (`abmil`), CLAM-SB (`clam_sb`) |
| Questions | Q1–Q6, one model per question; classes follow the categories used to score PathCopilot |
| Split | Patient-level 5-fold cross-validation, stratified on Q2 × Q3 (`make_folds.py`, seed 2026) |
| Validation | About one-seventh of the training patients, fixed per question and fold |
| Optimisation | Official CLAM defaults, no tuning: Adam, lr 1e-4, weight decay 1e-5, dropout 0.25, cross-entropy without reweighting; CLAM-SB bag/instance loss weights 0.7/0.3 |
| Stopping | Up to 200 epochs, early stopping on validation loss (patience 20, not before epoch 50); best-validation-loss weights |
| One-shot | One randomly drawn labelled slide per class in the same training pool; fixed epoch count = median selected epoch of the three fully supervised runs for the same configuration and fold |
| Runs | Seeds 0, 1 and 2 |
| External | TCGA-trained models applied to CPTAC-UCEC (Q1–Q5) without retraining or tuning |

## Requirements

```bash
python -m pip install -r evaluation/wsi_level_MIL_baseline/requirements.txt
git clone https://github.com/mahmoodlab/CLAM.git
export CLAM_ROOT=/path/to/CLAM
```

CLAM-SB and the gated-attention block are imported from the official CLAM repository, which is
not included here (CLAM is distributed under its own license). CONCH, UNI and Virchow2 are gated
models on Hugging Face; request access and download the weights from the model pages before
feature extraction.

All scripts use one work directory, set with `--root` or the `MIL_ROOT` environment variable:

```text
$MIL_ROOT/
├── labels/      <name>.tsv, <name>.meta.json, folds.tsv, cptac_slides.txt
├── features/    tcga/<encoder>/h5_files/<slide>.h5, cptac/<encoder>/h5_files/<slide>.h5
├── runs/        full/... and oneshot/... (training outputs)
└── predictions/ external predictions
```

## 1. Labels and folds

Prepare a long-format TSV from the reference answers, one row per slide and question, with columns
`slide`, `patient`, `question` (Q1–Q6), `label` (class name) and `code` (integer 0..K−1 per question).

```bash
cd evaluation/wsi_level_MIL_baseline
python make_labels.py --long-tsv labels_long.tsv --name tcga_ucec
python make_folds.py --labels tcga_ucec
```

## 2. Patch features

Patch the slides with CLAM (`create_patches_fp.py --seg --patch --preset tcga.csv`), choosing the patch
size per slide so that the read region corresponds to about 20x (0.5 µm/px), and store the output as
`<patch-root>/ps<size>/patches/<svs stem>.h5`. Then build the coordinate manifest and extract CONCH and UNI
features on the same coordinates:

```bash
python build_coord_manifest.py --svs-dir /path/to/TCGA-UCEC --patch-root /path/to/patches --out tcga_coords.tsv
CONCH_CKPT_PATH=/path/to/conch/pytorch_model.bin python extract_features.py --encoder conch_v1 \
  --manifest tcga_coords.tsv --out-dir $MIL_ROOT/features/tcga/conch
UNI_CKPT_PATH=/path/to/uni/pytorch_model.bin python extract_features.py --encoder uni_v1 \
  --manifest tcga_coords.tsv --out-dir $MIL_ROOT/features/tcga/uni
```

Unreadable tiles in the source SVS files are skipped and logged rather than aborting the slide.
Virchow2 features were extracted as the class token (1,280-d, fp16) of 224-px tiles at 0.5 µm/px on a
tissue grid (thumbnail tissue fraction ≥ 0.05, tile tissue fraction ≥ 0.15). Any extractor that writes
`features/<cohort>/virchow2/h5_files/<slide>.h5` with a `features` dataset (N × 1,280) can be used.
Repeat the same steps for CPTAC-UCEC under `features/cptac/`.

## 3. Training and external application

```bash
MIL_ROOT=/path/to/mil_workdir CLAM_ROOT=/path/to/CLAM LABELS=tcga_ucec \
  bash evaluation/wsi_level_MIL_baseline/run_mil_baseline.sh
```

The script runs, for seeds 0, 1 and 2:

1. `train_mil.py`: fully supervised models for every encoder, aggregator, question and fold
   (`runs/full/<encoder>/<agg>/<question>/seed<s>/fold<k>/`).
2. `train_oneshot.py`: one-shot models (`runs/oneshot/<encoder>/<agg>/<question>/oneshot/seed<s>/fold<k>/`).
3. `predict_external.py`: applies every saved model to CPTAC-UCEC and writes one row per model and slide.

Each run directory contains `pred.tsv` (test-fold predictions and class probabilities), `run.json`
(settings, training size and epochs) and, with `--save-weights`, `weights.pt`. Finished runs are skipped,
so interrupted jobs can be restarted. Individual steps can be run directly, for example
`python train_mil.py --encoder conch --aggs abmil --tasks Q2 --labels tcga_ucec --seeds 0 1 2`.

Accuracy in the paper was computed from these predictions separately for each run and then averaged
across runs; for CPTAC-UCEC, each fold-specific model was evaluated separately, averaged across the five
models within each run and then across the three runs.

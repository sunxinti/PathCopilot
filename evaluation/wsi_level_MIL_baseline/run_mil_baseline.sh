#!/usr/bin/env bash
# Supervised MIL baselines on TCGA-UCEC (patient-level 5-fold CV) and external inference on CPTAC-UCEC.
# Seeds 0, 1 and 2; 3 encoders x 3 aggregators x 6 questions x 5 folds per seed.
#
# Before running:
#   * patch features in $MIL_ROOT/features/{tcga,cptac}/{conch,uni,virchow2}/h5_files/<slide>.h5 (see README.md)
#   * labels in $MIL_ROOT/labels/<LABELS>.tsv/.meta.json (make_labels.py) and $MIL_ROOT/labels/folds.tsv (make_folds.py)
#   * CLAM_ROOT pointing to a clone of https://github.com/mahmoodlab/CLAM
#   * $MIL_ROOT/labels/cptac_slides.txt listing the CPTAC-UCEC slide IDs
#
# Usage: MIL_ROOT=/path/to/mil_workdir CLAM_ROOT=/path/to/CLAM LABELS=tcga_ucec bash run_mil_baseline.sh
set -euo pipefail
cd "$(dirname "$0")"
: "${MIL_ROOT:?set MIL_ROOT}" "${CLAM_ROOT:?set CLAM_ROOT}" "${LABELS:?set LABELS}"
export MIL_ROOT CLAM_ROOT
PY=${PYTHON:-python}
DEVICE=${DEVICE:-cuda:0}
SEEDS="0 1 2"

# 1. Fully supervised training. For each encoder, aggregator, question, fold and seed,
# save the weights from the epoch with the lowest validation loss.
for enc in conch uni virchow2; do
  $PY -u train_mil.py --encoder $enc --labels "$LABELS" --tag full --seeds $SEEDS --save-weights --device "$DEVICE"
done

# 2. One-shot: one labelled slide per class, epochs taken from the fully supervised runs of step 1.
for enc in conch uni virchow2; do
  for agg in avgpool abmil clam_sb; do
    $PY -u train_oneshot.py --encoder $enc --agg $agg --labels "$LABELS" --main-tag full --tag oneshot \
      --seeds $SEEDS --save-weights --device "$DEVICE"
  done
done

# 3. External CPTAC-UCEC inference (Q1-Q5), one row per model and slide; no retraining, tuning or averaging.
mkdir -p "$MIL_ROOT/predictions"
for tag in full oneshot; do
  $PY -u predict_external.py --tag $tag --cohort cptac --slides "$MIL_ROOT/labels/cptac_slides.txt" \
    --out "$MIL_ROOT/predictions/cptac_${tag}.tsv" --device "$DEVICE"
done

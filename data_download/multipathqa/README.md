# MultiPathQA data preparation

MultiPathQA is the WSI question-answering benchmark used by
`evaluation/wsi_level_MultiPathQA/run_multipathqa_eval.py`. Neither the
benchmark CSV nor the underlying WSIs are redistributed by PathCopilot.

The released CSV contains 934 WSI-level questions spanning five benchmark
tasks: TCGA cancer classification, GTEx organ classification, PANDA grading,
SlideBenchVQA and pathologist-authored ExpertVQA. The official dataset card
reports 868 per-task WSI occurrences and 862 unique slides after deduplicating
slides shared by more than one task.

## 1. Obtain the benchmark CSV

Download `MultiPathQA.csv` from the official
[MultiPathQA dataset page](https://huggingface.co/datasets/tbuckley/MultiPathQA).
For example, using the Hugging Face CLI:

```bash
python -m pip install --upgrade huggingface_hub
hf download tbuckley/MultiPathQA MultiPathQA.csv \
  --repo-type dataset \
  --local-dir /path/to/multipathqa_data
```

The evaluator accepts the CSV at any location through `--csv`.

The CSV includes benchmark questions, answer options, labels and slide
identifiers. Treat it as benchmark data: do not pass its gold answer or scoring
fields into the WSI Agent. The released evaluator enforces this separation.

## 2. Obtain the source WSIs

The CSV references slides from multiple source datasets. Obtain each WSI from
its original repository and comply with that repository's access and license
terms:

- TCGA slides: [NCI Genomic Data Commons](https://portal.gdc.cancer.gov/)
- GTEx slides: [GTEx Portal](https://gtexportal.org/)
- PANDA slides: [Kaggle PANDA Challenge](https://www.kaggle.com/c/prostate-cancer-grade-assessment)
- SlideBench/SlideChat-derived entries: follow the provenance and access notes
  in the official MultiPathQA dataset card.

The frozen TCGA-UCEC/COAD/LUAD lists in the parent `data_download/ids/`
directory belong to PathCopilot's separate TCGA cohort evaluations. They are
not a complete manifest for MultiPathQA and must not be presented as one.

Place the downloaded slides under a directory supplied with `--img_dir`. The
runner resolves each CSV record using its `image_path` and `file_id`, reports
missing slides before evaluation, and runs only records whose WSI can be
resolved.

Because the WSI sources use different access systems, PathCopilot does not
claim that a single script can lawfully or reliably download the complete
benchmark. Record the source repository and acquisition date for every local
WSI collection used in a reported evaluation.

## 3. Run one benchmark subset

From `evaluation/wsi_level_MultiPathQA/`:

```bash
python run_multipathqa_eval.py \
  --csv /path/to/multipathqa_data/MultiPathQA.csv \
  --img_dir /path/to/multipathqa_wsi \
  --benchmark tcga_expert_vqa \
  --provider openrouter \
  --lang en
```

Supported subset names are `tcga_expert_vqa`, `tcga`, `tcga_slidebench`,
`panda`, and `gtex`.

## Evaluator mapping

- Evaluation entry point: `evaluation/wsi_level_MultiPathQA/run_multipathqa_eval.py`
- Benchmark table argument: `--csv /path/to/MultiPathQA.csv`
- WSI root argument: `--img_dir /path/to/multipathqa_wsi`
- Missing-slide behavior: reported and excluded before model execution
- Dataset or WSI redistribution by this repository: no

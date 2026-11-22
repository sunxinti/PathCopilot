# Patch-level evaluation

This directory contains the public PathMMU patch-level evaluation used by
PathCopilot. It is independent of the WSI-, patient- and cohort-level
pipelines.

## Data

Request PathMMU access from the
[official dataset page](https://huggingface.co/datasets/jamessyx/PathMMU),
then arrange the files as follows:

```text
pathmmu_data/
├── data.json
└── images/
```

The dataset is not redistributed in this repository.
See the central [PathMMU data-preparation guide](../../data_download/pathmmu/README.md)
for the official access procedure and the directory layout expected by this
evaluator.

## Installation

From the repository root:

```bash
python -m pip install -r evaluation/patch_level/requirements.txt
export OPENROUTER_API_KEY=YOUR_OPENROUTER_API_KEY
```

The public evaluator uses only OpenRouter's official OpenAI-compatible
endpoint. No API key is included in the repository.

## Usage

```bash
# Quick smoke test
python evaluation/patch_level/eval_pathmmu.py \
  --data_path /path/to/pathmmu_data \
  --category pdv \
  --limit 50

# Full validation set
python evaluation/patch_level/eval_pathmmu.py \
  --data_path /path/to/pathmmu_data \
  --category val

# Select another OpenRouter model
python evaluation/patch_level/eval_pathmmu.py \
  --data_path /path/to/pathmmu_data \
  --category pdv \
  --model google/gemini-3.1-pro-preview
```

Use `--output_dir` to choose the output location and `--no_resume` to disable
checkpoint resumption. Run `python evaluation/patch_level/eval_pathmmu.py
--help` for the complete argument list.

Responses that fail or cannot be parsed into a single valid option are scored as
incorrect and remain in the denominator.

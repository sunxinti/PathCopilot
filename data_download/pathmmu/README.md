# PathMMU data preparation

PathMMU is the patch-level benchmark used by
`evaluation/patch_level/eval_pathmmu.py`. The dataset is not redistributed by
PathCopilot.

The official release contains 33,428 multimodal multiple-choice questions and
24,067 pathology images assembled from PubMed, educational content and other
pathology image sources. PathCopilot's reported patch-level evaluation uses
the 744-question Atlas, EduContent, and PubMed subset. PathCopilot evaluates
the released question/image pairs; it does not recreate or alter the PathMMU
labels.

## Access status

The Hugging Face repository is visible publicly but its files are gated. Access
requires the maintainer's application, acceptance of the dataset terms and a
Hugging Face account whose email matches the access form. Approval is controlled
by the PathMMU maintainers, not by PathCopilot.

## Obtain the official dataset

1. Open the official [PathMMU dataset page](https://huggingface.co/datasets/jamessyx/PathMMU).
2. Complete the maintainer's access form, accept the license and acceptable-use
   terms, and request access with the same email address as the Hugging Face
   account.
3. After access is approved, download the dataset with the Hugging Face web
   interface or an authenticated Hugging Face CLI. For example:

   ```bash
   python -m pip install --upgrade huggingface_hub
   hf auth login
   hf download jamessyx/PathMMU \
     --repo-type dataset \
     --local-dir /path/to/pathmmu_data
   ```

The official dataset card notes that PubMed and EduContent images are available
from Hugging Face, while some other subsets require the acquisition steps in
the official [PathMMU repository](https://github.com/PathMMU-Benchmark/PathMMU).
Follow those source-specific terms rather than redistributing the files.

Do not put a Hugging Face access token in a shell script, README, `.env` file
that will be committed, or evaluation result artifact.

## Layout expected by PathCopilot

Arrange the material supplied by PathMMU so that the evaluator receives a root
directory containing:

```text
pathmmu_data/
├── data.json
└── images/
```

Then run, from the PathCopilot repository root:

```bash
python evaluation/patch_level/eval_pathmmu.py \
  --data_path /path/to/pathmmu_data \
  --category pdv \
  --limit 50
```

The evaluator stops before any model call if a referenced image is missing and
lists each missing path. Check both `data.json` and the required images before a
full run.

## Evaluator mapping

- Evaluation entry point: `evaluation/patch_level/eval_pathmmu.py`
- Data argument: `--data_path /path/to/pathmmu_data`
- Required metadata file: `data.json`
- Required image root: `images/`
- Dataset redistribution by this repository: no

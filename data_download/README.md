# Public data preparation

This directory is the central data-preparation index for the PathCopilot
evaluations. Evaluation code remains under `evaluation/`; downloaded datasets,
access credentials and generated manifests are not committed to this
repository. The directory name does not imply that every dataset can be
redistributed or downloaded without accepting its source terms.

## Evaluation datasets

| Evaluation | Data description | Official source |
|---|---|---|
| Patch level | PathMMU: 744 pathology multiple-choice questions (Atlas, EduContent, and PubMed) used in the PathCopilot patch-level evaluation | [Hugging Face](https://huggingface.co/datasets/jamessyx/PathMMU) and [official repository](https://github.com/PathMMU-Benchmark/PathMMU) |
| WSI question answering | MultiPathQA: 934 questions over 862 unique source WSIs after cross-task deduplication | [Hugging Face](https://huggingface.co/datasets/tbuckley/MultiPathQA) |
| TCGA WSI cohorts | Diagnostic H&E WSIs from TCGA-UCEC, TCGA-COAD and TCGA-LUAD | [NCI Genomic Data Commons](https://portal.gdc.cancer.gov/) |
| CPTAC WSI cohort | Frozen PathCopilot subset of 135 CPTAC-UCEC WSIs from 95 patients | [The Cancer Imaging Archive](https://www.cancerimagingarchive.net/collection/cptac-ucec/) |
| PUMCH patient level | Institutional multi-slide patient cases | Peking Union Medical College Hospital (PUMCH) |

PathMMU is access-controlled by its maintainers, and MultiPathQA references
WSIs hosted by several source repositories. For those datasets, this directory
documents the official acquisition route instead of copying restricted data or
presenting an incomplete one-command downloader. See the
[PathMMU access and layout guide](pathmmu/README.md) and the
[MultiPathQA CSV and source-WSI guide](multipathqa/README.md).
The official PathMMU release is larger (33,428 questions and 24,067 images);
PathCopilot's reported patch-level evaluation uses the 744-question Atlas,
EduContent, and PubMed subset. MultiPathQA's released CSV contains 934
questions; the official dataset card reports 868 per-task WSI occurrences and
862 unique slides after cross-task deduplication. The PUMCH cohort is described
in the [PUMCH evaluation README](../evaluation/patient_level_PUMCH/README.md).

## TCGA and CPTAC WSI lists

The files below provide public slide identifiers and reproducible download helpers for four public WSI cohorts used in the PathCopilot experiments:

- [TCGA-UCEC](https://portal.gdc.cancer.gov/projects/TCGA-UCEC) (uterine corpus endometrial carcinoma)
- [TCGA-COAD](https://portal.gdc.cancer.gov/projects/TCGA-COAD) (colon adenocarcinoma)
- [TCGA-LUAD](https://portal.gdc.cancer.gov/projects/TCGA-LUAD) (lung adenocarcinoma)
- [CPTAC-UCEC](https://www.cancerimagingarchive.net/collection/cptac-ucec/) (CPTAC uterine corpus endometrial carcinoma)

No WSI data are redistributed here. TCGA files are resolved through the NCI Genomic Data Commons (GDC) API. CPTAC-UCEC files are listed with their public TCIA PathDB/caMicroscope URLs.

## Included files

`ids/` contains two plain-text lists per cohort:

- `*_svs_files.txt`: exact public SVS file names.
- `*_slide_ids.txt`: public slide IDs for cohort inspection and reporting.

For CPTAC-UCEC, `cptac_ucec_download_manifest.tsv` additionally records the 135 frozen analytic slide IDs, patient IDs, tissue group, file names, and public TCIA download URLs. It contains no local paths.

`ids/cohort_summary.tsv` records the frozen analytic WSI and case counts used in
the manuscript. The committed public identifier lists contain exactly 562
TCGA-UCEC, 455 TCGA-COAD and 541 TCGA-LUAD WSIs, matching the manuscript cohort
denominators. These lists were checked against the final evaluation records.

The CPTAC-UCEC evaluation set comprises 135 WSIs from 95 patients (105 tumor and 30 normal slides), matching the manuscript; all 135 are listed in `cptac_ucec_download_manifest.tsv`.

## Requirements

- Python 3.9 or later (standard library only)
- Internet access to `https://api.gdc.cancer.gov`
- Internet access to `https://pathdb.cancerimagingarchive.net` for per-slide CPTAC-UCEC downloads
- Optional: the official `gdc-client` executable for downloading the resolved files

Download the GDC Data Transfer Tool from:

<https://gdc.cancer.gov/access-data/gdc-data-transfer-tool>

The TCGA slide images are open-access; a GDC token is normally not required.

Keep downloaded WSIs outside the Git working tree when practical. Never commit
Hugging Face tokens, GDC tokens, Kaggle credentials, institutional data or
generated download audits containing local absolute paths.

## Resolve a GDC manifest first

Linux/WSL (primary reproduction environment):

```bash
cd data_download
bash scripts/download_tcga_ucec.sh
bash scripts/download_tcga_coad.sh
bash scripts/download_tcga_luad.sh
```

This creates `work/<cohort>/*_gdc_manifest.tsv` and a JSON resolution audit. No WSI is downloaded during this step. A nonzero exit code indicates unresolved or ambiguous file names; inspect the audit before continuing.

## Download after reviewing the audit

Linux/WSL:

```bash
bash scripts/download_tcga_ucec.sh --download
bash scripts/download_tcga_coad.sh --download
bash scripts/download_tcga_luad.sh --download
```

Files are written under `work/<cohort>/files/`, which is excluded from Git.

## Validate or download CPTAC-UCEC

CPTAC-UCEC does not use the GDC manifest route above. Its frozen 135-slide manifest points to public TCIA PathDB/caMicroscope SVS URLs.

Linux/WSL:

```bash
bash scripts/download_cptac_ucec.sh
bash scripts/download_cptac_ucec.sh --check-urls
bash scripts/download_cptac_ucec.sh --download
```

CPTAC-UCEC files are written to `work/cptac_ucec/files/<patient_id>/`. The download audit is saved under `work/cptac_ucec/`.

PathDB availability can vary by network and TLS/proxy configuration. If `-CheckUrls` reports a uniform TLS or connection failure, do not interpret that result as a missing slide ID. Retry from another network or use TCIA's official **CPTAC-UCEC Pathology Discovery Cohort** download on the [CPTAC-UCEC collection page](https://www.cancerimagingarchive.net/collection/cptac-ucec/). The official bulk SVS route uses IBM Aspera; after downloading the package, retain the files listed in `ids/cptac_ucec_svs_files.txt` to reproduce the frozen 135-slide analytic cohort.

## Optional Windows data-preparation wrappers

PathCopilot evaluation is reproduced in Linux/WSL. The `.ps1` wrappers below are provided only as an optional convenience for downloading the same public files from Windows:

```powershell
Set-Location data_download

.\scripts\download_tcga_ucec.ps1
.\scripts\download_tcga_coad.ps1
.\scripts\download_tcga_luad.ps1
.\scripts\download_cptac_ucec.ps1

# Add -Download only after reviewing the generated audit.
.\scripts\download_cptac_ucec.ps1 -CheckUrls
.\scripts\download_cptac_ucec.ps1 -Download
```

If Windows blocks local PowerShell scripts, use `powershell -NoProfile -ExecutionPolicy Bypass -File <script.ps1>`; this does not alter the Linux/WSL evaluation path.

## Maintainer-only CPTAC manifest export

The CPTAC-UCEC public manifest was exported from the frozen evaluation mapping with:

```bash
python scripts/export_cptac_inventory.py \
  --mapping-csv "<path-to-frozen-patient-slide-mapping.csv>" \
  --output-dir ids \
  --expected 135
```

## Data source and permitted use

The TCGA identifiers resolve to diagnostic slide images in the NCI GDC projects TCGA-UCEC, TCGA-COAD, and TCGA-LUAD. CPTAC-UCEC identifiers resolve to the public pathology collection hosted by The Cancer Imaging Archive (TCIA). Users are responsible for following the applicable GDC and TCIA data-use terms and documenting the source release/query date used in their own analysis.

# PathCopilot MultiPathQA evaluation snapshot

This directory is the source snapshot for the fixed, single-question
MultiPathQA WSI evaluation. It contains the Gemini 3.1 evaluation Agent's
pathology prompts, red/blue/green multiscale workflow, answer normalization
and 1-based option handling. Scoring rules and the knowledge-lookup (D)
behaviour are described below.

## Legacy FOV identifiers

The internal values `10` / `40` / `160` (and the prompt labels `10×` / `40×` /
`160×`) are routing identifiers retained to reproduce the original evaluation.
They do **not** denote optical-objective or image-acquisition magnifications.

| Paper FOV hierarchy | This snapshot | Color |
|:--------------------|:--------------|:------|
| FOV Level 0 | global thumbnail | — |
| FOV Level 1 | wider relative FOV (legacy ID `10`) | red |
| FOV Level 2 | intermediate FOV (legacy ID `40`) | blue |
| FOV Level 3 | narrower relative FOV (legacy ID `160`) | green |

Diagnostic ROIs are cropped from native WSI pixel space (OpenSlide pyramid
level 0). That OpenSlide level is unrelated to FOV Level 0. No 160× optical
image or super-resolution is generated. Prompt text still uses the legacy
`10×` / `40×` / `160×` labels so the original prompt wording is unchanged.

The evaluator now runs through a local **Agent Runtime shadow control plane**:

- one durable Run is created for every WSI question that is actually executed;
- the existing pathology Orchestrator remains the sole executor;
- emitted red (10×), blue (40×), and green (160×) events are projected into
  explicitly parented Agent Tasks;
- lifecycle and audit events are stored in SQLite;
- Runtime state is metadata only and never participates in prediction or
  scoring;
- Runtime setup, projection, or finalization failure is fail-open and cannot
  replace the pathology result.

The established `agent.agent_loop.run_agent()` is still called exactly once
per executed question with its original arguments. Runtime itself does not
change prompts, model messages, ROI decisions, or report generation.

This snapshot is a headless CLI. There is no web application or login system.
`app.py` is the in-process WSI file registry used by `run_agent()`.

## Data preparation

The benchmark CSV and source WSIs are not redistributed with this evaluator.
Follow the central [MultiPathQA data-preparation guide](../../data_download/multipathqa/README.md)
to obtain `MultiPathQA.csv` and the required source slides. The TCGA/CPTAC
cohort lists elsewhere in `data_download/` are not a complete MultiPathQA WSI
manifest.

## Run

Configure an OpenRouter key in the environment, then run for example:

```bash
export OPENROUTER_API_KEY="your-openrouter-key"
python run_multipathqa_eval.py \
  --csv /path/to/MultiPathQA.csv \
  --img_dir /path/to/wsi \
  --benchmark tcga_expert_vqa \
  --provider openrouter \
  --lang en
```

`--runtime-mode shadow` is the default. Its database is written beside the
evaluation result files and is ignored by Git. Use `--runtime-db PATH` to
override the location. `--runtime-mode off` is retained for an exact legacy
execution-path comparison.

The per-question JSON preserves the original scoring fields, adds deterministic
protocol metadata to `rec`, and stores the Runtime receipt as a sibling
`agent_runtime` object. Resumed/skipped questions do not create duplicate Runs.

Attempted questions with a reference answer count in accuracy and per-class recall even if inference or answer extraction fails. Such attempts record an empty prediction, a score of zero and the error, and are retried on ordinary resume. Missing input slides, unattempted questions and questions without a reference are outside this denominator.

## Benchmark network boundary

This snapshot does **not** include the product's PubMed MCP, Web Search,
inbound MCP server, CLI/managed Worker, or Flask Runtime integration. The only
network endpoint used by `run_multipathqa_eval.py` is the configured LLM
provider (OpenRouter). `knowledge_retrieval.search_pubmed` and
`agent/mcp_tools.py` are retained in the source but are not called by this runner.

The prompts retain action D. Selecting it returns `暂无结果` (no results)
and records the lookup as disabled.

## Deterministic candidate-filter protocol

The primary benchmark retains the legacy 10×/40×/160× relative-FOV geometry
(paper FOV Levels 1–3). Global
and local candidate grids use the same morphology-free mask (`gray < 220` and
per-pixel RGB standard deviation `> 10`) with a 15% tissue threshold. Empty
candidate sets fail closed and are never repopulated with blank or
geometric-centre boxes.

Grid-cache identity includes the view profile, mask policy, threshold, WSI
dimensions, and candidate-layout version. Results created before this protocol
are not silently mixed into resumed summary statistics; rerun them explicitly
with `--only` or `--no_resume`. Before any model call, the runner also verifies
that `app`, `agent.agent_loop`, and `agent.gpt_client` resolve inside this
benchmark snapshot.

## Leakage and label isolation

The question, options, and label-free Agent events may be persisted for local
audit. Gold answers, correctness, and scoring fields are never passed into the
Runtime. The original report artifact remains auditable, while its deterministic
WSI filename header is redacted only from the separate answer-extractor model
input so a TCGA filename cannot act as a diagnostic shortcut.

Do not publish generated Runtime databases without review: they can contain
questions, WSI basenames, and model-generated pathology messages.

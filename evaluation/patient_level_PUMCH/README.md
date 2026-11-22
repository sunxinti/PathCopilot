# PathCopilot PUMCH Patient Level Auto Evaluation

This directory is a terminal-only evaluation snapshot for the PUMCH Patient Level task. It accepts all slides for one patient before a run starts, analyzes the slides with the existing red–blue–green pathology Agent, and performs patient-level synthesis only after every slide Agent has settled. By default slides are started concurrently (`--wsi-schedule parallel`) to match the manuscript protocol; `--wsi-schedule sequential` runs one slide to completion before starting the next. The 14 predefined patient-level questions are kept in `agent/agent_loop.py`. Thirteen questions are assessed from the slide images and FIGO stage is synthesized at patient level; specimen information comes only from the manifest's specimen label (`patient_identifier`, e.g. "子宫及双附件切除标本"), which contains no diagnostic information. The PUMCH cohort whole-slide images are not included in this repository.

## Agent Runtime

Agent Runtime is enabled in `shadow` mode by default. It creates one durable
Run per patient and records the following first-class Task hierarchy in a local
SQLite event store:

```text
Patient Orchestrator
├── WSI Agent 1
│   └── Red ROI Agents → Blue ROI Agents → Green ROI Agents
└── WSI Agent 2
    └── Red ROI Agents → Blue ROI Agents → Green ROI Agents
```

The Runtime owns Run/Task identity, lifecycle state, event ordering, and audit
records. The pathology Agent remains the sole executor: Runtime does not
rewrite prompts, choose ROIs, or change patient-level synthesis. Repeated ROI identifiers such as `R1` are
scoped under their WSI Task, so concurrently analyzed slides cannot collide.

Shadow persistence is fail-open: if Runtime initialization or projection
fails, the pathology evaluation continues once through the established path
and the Runtime error is recorded in the run receipt. Use `--runtime-mode off`
only for an explicit observational-equivalence check.

The package does not contain a web application, upload service, or login system. `run_single_wsi_for_patient()` is an internal worker used by the patient-level scheduler; it is not an independent single-WSI evaluation entry point.

## API-native single-WSI secondary review

After all first-pass red–blue–green paths for each WSI have completed, the
single-slide Orchestrator may expose one OpenAI-compatible native function tool,
`secondary_review`. It is used only for a precise, focal, image-checkable claim
that could materially change that slide's answer and is isolated or discordant
with other saved evidence. The independent reviewer receives the marked
whole-slide location, available red/blue parent context, and the target 160×
field. Zero calls is the preferred and valid decision; the default limit is
three calls per WSI.

This is a single-WSI adjudication step, not a cross-slide visual tool. The
patient-level Orchestrator synthesizes the per-slide outputs and must respect
their adjudications: only `CONFIRMED` may be reported as a confirmed positive
for a selected claim; `REJECTED`, `INDETERMINATE`, `SKIP`, and `NOT_RUN` remain
unconfirmed. Set `PATHCOPILOT_SECONDARY_REVIEW=0` to disable this optional step,
or `PATHCOPILOT_SECONDARY_REVIEW_MAX=N` to set the per-WSI budget (bounded to
0–8). Native tool lifecycle events are stored by Agent Runtime separately from
outbound MCP calls.

## Native outbound MCP evidence tools

Outbound MCP is **off by default**, as in the reported PUMCH evaluation; with it
off, D (Knowledge) uses only the local WHO/FIGO criteria table in
`agent/knowledge_retrieval.py` (no network call). When enabled with
`--outbound-mcp`, the Agent uses the official Python MCP SDK (`ClientSession` over stdio) to discover and call two
allowlisted FastMCP servers:

- `pubmed.search_pubmed`: PMID-linked PubMed evidence;
- `web_search.search_pathology_web`: bounded biomedical search across PubMed,
  Europe PMC, and OpenAlex.

The Orchestrator/Sub-agent decides whether D(Knowledge) is needed. When
outbound MCP is enabled, PubMed is queried first; the bounded Web Search MCP is used only when PubMed fails or
returns no usable result. The result is returned to the same Sub-agent for one
replanning step. External evidence may refine a differential diagnosis but is
never treated as slide-level morphology.

Server commands, timeouts, and tool allowlists are declared in
`mcp.outbound.json`; credentials are read only from environment variables.
Use `--outbound-mcp` only for exploratory runs outside the reported protocol. Similarly,
the fixed MultiPathQA benchmark snapshot does not include these MCP tools.

## PUMCH data availability

The PUMCH patient cohort and its whole-slide images are **not open-sourced** with this evaluator, because they are identifiable clinical materials protected by patient privacy, institutional ethics approval, and data-use restrictions. This snapshot ships only the runnable Patient Level Agent; it does not contain PUMCH WSI files, patient databases, or case-level source data.

Qualified researchers who need access for a legitimate study may contact the **corresponding author** listed in the manuscript to apply under the applicable institutional approvals and data-use agreement. Requests are reviewed case by case. Contact does not guarantee access.

## Input manifest

WSI files remain outside version control. Relative slide paths are resolved from the manifest directory. `patient_identifier` and each slide `part` are intentionally visible to the model because they describe the specimen and sampling site; `patient_id` is the filesystem-safe run key.

```json
{
  "schema_version": 1,
  "patients": [
    {
      "patient_id": "P001",
      "patient_identifier": "子宫及双附件切除标本",
      "slides": [
        {"path": "slides/P001_01.svs", "part": "子宫体"},
        {"path": "slides/P001_02.svs", "part": "左侧输卵管"}
      ]
    }
  ]
}
```

Supported slide suffixes are `.svs`, `.tif`, `.tiff`, `.ndpi`, `.mrxs`, `.scn`, `.bif`, `.vms`, and `.vmu`.

## Run

Install OpenSlide at the operating-system level, install the Python requirements, and provide one or more comma-separated OpenRouter keys:

```bash
export OPENROUTER_API_KEYS="your_openrouter_key"
python run_pumch_eval.py --manifest patients.json --lang zh --events
```

Ordinary or strictly rate-limited API services should prefer sequential WSI scheduling. Parallel scheduling is the default to follow the published evaluation protocol:

```bash
python run_pumch_eval.py --manifest patients.json --lang zh --events \
  --wsi-schedule sequential --api-max-concurrency 3
```

Useful options:

- `--patient P001`: run only selected patient IDs; repeat the option for more than one patient.
- `--cache rerun`: rerun every slide (default).
- `--cache reuse`: reuse verified slide evidence under the current patient’s `auto/` directory. The slide ID, filename, part and AI-only provenance must match. Caches failing these checks are skipped; if several verified attempts exist, the most recent is used. Missing slides are reanalysed. A patient report is reused only when its patient identity and ordered slide evidence also match; otherwise it is synthesized again.
- `--events`: write a local `events.jsonl` audit stream.
- `--summary PATH`: choose the atomic run-summary output path.
- `--runtime-mode shadow|off`: enable the Agent Runtime audit/control plane
  (default `shadow`) or run the exact pathology path without Runtime.
- `--runtime-db PATH`: choose the Runtime SQLite database path; the default is
  `outputs/agent_runtime.sqlite3`.
- `--outbound-mcp`: enable the PubMed and bounded Web Search MCP servers
  (off by default, as in the reported evaluation).
- `--wsi-schedule parallel|sequential`: `parallel` (default) starts every
  independent WSI together; `sequential` finishes one WSI before starting the
  next. Intra-slide red/blue/green analysis is unchanged. For APIs that handle
  concurrent long-running multimodal requests stably, parallel is appropriate;
  for ordinary or strictly rate-limited services, sequential is recommended.
- `--api-max-concurrency N`: cap in-flight OpenRouter/model HTTP requests
  (default `3`). This gate is independent of WSI scheduling; unlimited
  concurrency is not supported. Parallel mode still starts one worker thread
  per pending WSI, so OpenSlide handles, image encoding, and memory are not
  capped by this flag. Prefer `sequential` when local resources or the API
  service are limited.

Patients are evaluated sequentially. Within each patient, WSI workers follow `--wsi-schedule`; the final patient report is generated after those workers complete or fail. The chosen schedule and API concurrency limit are recorded in `run_summary.json`, Agent Runtime context, `run_schedule.json`, and (on a fresh or partial rerun) report metadata. Reusing both the verified slides and a matching patient report writes `run_schedule.json` and the patient tree metadata but does not rewrite HTML/text reports. The process exits with `0` only when every selected patient completes successfully, `1` for a runtime/partial failure, `2` for manifest or input validation errors, and `130` for interruption. Ctrl+C promptly reconciles Runtime state and unblocks threads waiting on the API gate. An HTTP request already in progress cannot be safely cancelled by `requests`; its non-daemon worker may therefore keep the Python process alive until the response arrives or the 180-second request timeout expires.

## Outputs

Generated material is written under `outputs/patient_<patient_id>/auto/`, including per-slide evidence, the patient analysis tree, text/HTML/Word reports, optional events, and `run_summary.json`. The default Agent Runtime database is `outputs/agent_runtime.sqlite3`. These are runtime artifacts and must not be committed.

No API keys, PUMCH WSI files, patient databases, logs, caches, or generated reports are included in this snapshot.

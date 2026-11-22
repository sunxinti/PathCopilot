# PathCopilot TCGA cohort runner

This directory contains the cancer-specific TCGA cohort runners together with
the same durable Agent Runtime control plane used by the open-source
application. The original pathology `run_agent()` remains the only analysis
executor; Runtime shadow mode records Run/Task/ROI-Agent identities and a
privacy-bounded SQLite event stream.

This snapshot is a headless CLI. There is no web application or login system.
Slide paths are registered in `file_registry.py`. The
batch runner always calls `run_agent(mode="ai")` once per SVS. The `patient`
column in `results_index_<cancer>.csv` is the TCGA barcode parsed from the filename for
indexing; this snapshot does not run multi-slide patient-level aggregation.

## Runtime and outbound MCP

- Agent Runtime is enabled by default in `shadow` mode.
- Outbound MCP (PubMed and bounded Web Search) is **off by default**, as in the
  reported evaluation. `--outbound-mcp` enables it: PubMed is then called through
  the official MCP Python SDK and a FastMCP stdio server when the pathology Agent
  selects D (Knowledge), and the bounded Web Search server uses only PubMed,
  Europe PMC, and OpenAlex.
- API keys are never stored in this directory. Set `OPENROUTER_API_KEY` or
  `OPENROUTER_API_KEYS`; `PUBMED_API_KEY` is optional.

The unified cohort runner sets `PATHCOPILOT_OUTBOUND_MCP=0` by default. With
outbound MCP off (or unavailable), D (Knowledge) for UCEC uses the local WHO/FIGO
criteria table in `agent/knowledge_retrieval.py` (no network call); LUAD and COAD
receive no external evidence. Runtime can be disabled with `--runtime-mode off`; this does not
change MCP or the current D-action evidence injection.

## Cancer-specific prompts

Use `--cancer ucec|luad|coad` to select the corresponding prompt
and question blocks. Each prompt begins with a short, diagnosis-neutral organ
line (uterus, lung, or colorectum; resection specimen, H&E) and instructs the
model to use only morphology visible on the current slide. The selection does
not change the red-blue-green executor, Agent Runtime, or MCP wiring.

Question counts follow Supplementary Tables S5, S11 and S12: UCEC has six
questions; COAD and LUAD each have seven.
The shared parser and Memory tree use the selected profile's question count.

The runner only produces the per-slide reports (`pathology_report.txt`). Reports
were scored manually by pathologists against the prespecified reference answers.

The optional `secondary_review` tool is organ-agnostic. After all first-pass
paths finish, the main Orchestrator may request a bounded independent review
only for a precise, image-checkable, isolated or cross-scale-discordant claim
that could materially change the active question. It is not tied to LVSI or to
any fixed question number. It is exposed as an API-native function tool from
`agent/secondary_review.py`; it is intentionally separate from the official
outbound MCP Client/Server used for PubMed and Web Search.

## Examples

```bash
export OPENROUTER_API_KEY="your-key"
python run_fulleval.py --cancer ucec \
  --svs_dir /path/to/TCGA-UCEC-svs \
  --limit 1

python run_fulleval.py --cancer luad \
  --svs_dir /path/to/TCGA-LUAD-svs --limit 1

python run_fulleval.py --cancer coad \
  --svs_dir /path/to/TCGA-COAD-svs --limit 1
```

Runtime events are written to a cancer-specific SQLite file under
`runtime_data/` by default. Outputs, databases, WSI files, and
credentials must not be committed.

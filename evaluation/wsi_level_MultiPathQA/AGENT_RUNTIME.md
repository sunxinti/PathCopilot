# MultiPathQA Agent Runtime boundary

The Runtime is an additive lifecycle and audit layer around the validated
Gemini 3.1 pathology Agent. It is not a replacement pathology algorithm and it
does not make model or tool decisions.

For each executed benchmark question, `run_multipathqa_eval.py` performs this
sequence:

1. inject the unchanged single-question prompt into the legacy Agent;
2. register the WSI in the existing in-process file registry;
3. create a Runtime Run and root `pathology.orchestrator` Task;
4. wrap the existing no-op evaluation socket with an `agent.*` event observer;
5. invoke the original `run_agent()` exactly once;
6. project red, blue, and green ROI events into durable child Agent Tasks;
7. reconcile the Runtime state after the original report is verified;
8. attach a label-free Runtime receipt outside the scored `rec` object.

The Runtime observer forwards every event unchanged. Projection persistence
errors are recorded by type and never block the legacy Agent. Setup and
finalization errors are likewise sanitized and cannot alter the report or
score.

No product MCP client/server or Web Search component is part of this boundary.
All Runtime code uses only Python's standard library and SQLite.


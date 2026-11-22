CREATE TABLE IF NOT EXISTS runtime_runs (
    run_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    parent_run_id TEXT,
    client_request_id TEXT,
    idempotency_key TEXT,
    state TEXT NOT NULL CHECK (state IN (
        'created', 'queued', 'running', 'waiting', 'finalizing',
        'cancelling', 'succeeded', 'partial', 'failed', 'cancelled'
    )),
    state_version INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    root_task_id TEXT,
    terminal_intent TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT,
    last_seq INTEGER NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    ended_at TEXT,
    cancel_requested_at TEXT,
    FOREIGN KEY (parent_run_id) REFERENCES runtime_runs(run_id),
    FOREIGN KEY (run_id, root_task_id)
        REFERENCES runtime_tasks(run_id, task_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_runs_owner_idempotency
ON runtime_runs(owner_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_runs_active_context
ON runtime_runs(owner_id, session_id, file_id)
WHERE state IN (
    'created', 'queued', 'running', 'waiting', 'finalizing', 'cancelling'
);

CREATE INDEX IF NOT EXISTS ix_runtime_runs_owner_updated
ON runtime_runs(owner_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS runtime_tasks (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runtime_runs(run_id) ON DELETE RESTRICT,
    parent_task_id TEXT,
    continuation_of_task_id TEXT,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'created', 'blocked', 'ready', 'running', 'waiting',
        'cancelling', 'succeeded', 'failed', 'cancelled', 'skipped'
    )),
    state_version INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    depth INTEGER NOT NULL CHECK (depth >= 0),
    required INTEGER NOT NULL CHECK (required IN (0, 1)),
    wait_reason TEXT CHECK (
        wait_reason IS NULL OR wait_reason IN (
            'children', 'user_input', 'external_tool', 'resource',
            'rate_limit', 'retry_backoff'
        )
    ),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    ended_at TEXT,
    cancel_requested_at TEXT,
    UNIQUE(run_id, task_id),
    FOREIGN KEY (run_id, parent_task_id)
        REFERENCES runtime_tasks(run_id, task_id),
    FOREIGN KEY (run_id, continuation_of_task_id)
        REFERENCES runtime_tasks(run_id, task_id)
);

CREATE INDEX IF NOT EXISTS ix_runtime_tasks_run_state
ON runtime_tasks(run_id, state);

CREATE INDEX IF NOT EXISTS ix_runtime_tasks_run_parent
ON runtime_tasks(run_id, parent_task_id);

CREATE TABLE IF NOT EXISTS runtime_events (
    global_offset INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runtime_runs(run_id) ON DELETE RESTRICT,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    event_type TEXT NOT NULL,
    event_version INTEGER NOT NULL CHECK (event_version >= 1),
    producer TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_id TEXT,
    parent_subject_id TEXT,
    attempt_id TEXT,
    correlation_id TEXT NOT NULL,
    causation_id TEXT,
    idempotency_key TEXT,
    visibility TEXT NOT NULL CHECK (visibility IN ('user', 'internal', 'audit')),
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (
        length(content_hash) = 64
        AND content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    UNIQUE(run_id, seq),
    UNIQUE(run_id, event_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_events_run_idempotency
ON runtime_events(run_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_runtime_events_run_seq
ON runtime_events(run_id, seq);

CREATE INDEX IF NOT EXISTS ix_runtime_events_subject
ON runtime_events(run_id, subject_kind, subject_id, seq);

CREATE INDEX IF NOT EXISTS ix_runtime_events_global_offset
ON runtime_events(global_offset);

CREATE TABLE IF NOT EXISTS runtime_artifacts (
    run_id TEXT NOT NULL REFERENCES runtime_runs(run_id) ON DELETE RESTRICT,
    artifact_id TEXT NOT NULL,
    task_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('staging', 'ready', 'failed')),
    kind TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    display_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    byte_size INTEGER,
    sha256 TEXT,
    created_at TEXT NOT NULL,
    ready_at TEXT,
    error_json TEXT,
    source_event_id TEXT,
    PRIMARY KEY(run_id, artifact_id),
    UNIQUE(run_id, relative_path),
    FOREIGN KEY (run_id, task_id)
        REFERENCES runtime_tasks(run_id, task_id),
    FOREIGN KEY (run_id, source_event_id)
        REFERENCES runtime_events(run_id, event_id)
);

CREATE INDEX IF NOT EXISTS ix_runtime_artifacts_run_state
ON runtime_artifacts(run_id, state);

CREATE TRIGGER IF NOT EXISTS tr_runtime_runs_no_delete
BEFORE DELETE ON runtime_runs
BEGIN
    SELECT RAISE(ABORT, 'runtime runs are append-only');
END;

CREATE TRIGGER IF NOT EXISTS tr_runtime_tasks_no_delete
BEFORE DELETE ON runtime_tasks
BEGIN
    SELECT RAISE(ABORT, 'runtime tasks are append-only');
END;

CREATE TRIGGER IF NOT EXISTS tr_runtime_events_no_delete
BEFORE DELETE ON runtime_events
BEGIN
    SELECT RAISE(ABORT, 'runtime events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS tr_runtime_artifacts_no_delete
BEFORE DELETE ON runtime_artifacts
BEGIN
    SELECT RAISE(ABORT, 'runtime artifacts are append-only');
END;

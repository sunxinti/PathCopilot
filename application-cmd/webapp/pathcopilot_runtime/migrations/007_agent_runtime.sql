CREATE TABLE IF NOT EXISTS runtime_agent_context_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runtime_runs(run_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL,
    parent_snapshot_id TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1),
    context_json TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (
        length(content_hash) = 64
        AND content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, snapshot_id),
    FOREIGN KEY (run_id, task_id)
        REFERENCES runtime_tasks(run_id, task_id),
    FOREIGN KEY (run_id, parent_snapshot_id)
        REFERENCES runtime_agent_context_snapshots(run_id, snapshot_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_context_idempotency
ON runtime_agent_context_snapshots(run_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_agent_context_task_created
ON runtime_agent_context_snapshots(run_id, task_id, created_at);

CREATE TABLE IF NOT EXISTS runtime_agent_messages (
    message_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runtime_runs(run_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL,
    sender_task_id TEXT,
    recipient_task_id TEXT,
    role TEXT NOT NULL CHECK (
        role IN ('system', 'user', 'assistant', 'tool')
    ),
    kind TEXT NOT NULL CHECK (
        kind IN (
            'message', 'instruction', 'summary', 'evidence',
            'tool_result', 'followup'
        )
    ),
    content_text TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL CHECK (
        length(content_hash) = 64
        AND content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    consumed_at TEXT,
    UNIQUE(run_id, message_id),
    FOREIGN KEY (run_id, task_id)
        REFERENCES runtime_tasks(run_id, task_id),
    FOREIGN KEY (run_id, sender_task_id)
        REFERENCES runtime_tasks(run_id, task_id),
    FOREIGN KEY (run_id, recipient_task_id)
        REFERENCES runtime_tasks(run_id, task_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_message_idempotency
ON runtime_agent_messages(run_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_agent_message_task_created
ON runtime_agent_messages(run_id, task_id, created_at);

CREATE INDEX IF NOT EXISTS ix_agent_mailbox_recipient
ON runtime_agent_messages(run_id, recipient_task_id, consumed_at, created_at);

CREATE TABLE IF NOT EXISTS runtime_agent_tool_calls (
    tool_call_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runtime_runs(run_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL,
    name TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')
    ),
    arguments_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT,
    error_json TEXT,
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    UNIQUE(run_id, tool_call_id),
    FOREIGN KEY (run_id, task_id)
        REFERENCES runtime_tasks(run_id, task_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_tool_call_idempotency
ON runtime_agent_tool_calls(run_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_agent_tool_call_task_created
ON runtime_agent_tool_calls(run_id, task_id, created_at);

CREATE TRIGGER IF NOT EXISTS tr_agent_context_no_update
BEFORE UPDATE ON runtime_agent_context_snapshots
BEGIN
    SELECT RAISE(ABORT, 'Agent context snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS tr_agent_context_no_delete
BEFORE DELETE ON runtime_agent_context_snapshots
BEGIN
    SELECT RAISE(ABORT, 'Agent context snapshots are append-only');
END;

CREATE TRIGGER IF NOT EXISTS tr_agent_messages_no_delete
BEFORE DELETE ON runtime_agent_messages
BEGIN
    SELECT RAISE(ABORT, 'Agent messages are append-only');
END;

CREATE TRIGGER IF NOT EXISTS tr_agent_tool_calls_no_delete
BEFORE DELETE ON runtime_agent_tool_calls
BEGIN
    SELECT RAISE(ABORT, 'Agent tool calls are append-only');
END;

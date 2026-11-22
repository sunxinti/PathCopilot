ALTER TABLE runtime_tasks
ADD COLUMN spawn_idempotency_key TEXT;

ALTER TABLE runtime_tasks
ADD COLUMN creation_fingerprint TEXT;

ALTER TABLE runtime_tasks
ADD COLUMN cancel_waiting_on_children INTEGER NOT NULL DEFAULT 0
    CHECK (cancel_waiting_on_children IN (0, 1));

CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_tasks_run_spawn_idempotency
ON runtime_tasks(run_id, spawn_idempotency_key)
WHERE spawn_idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS runtime_run_policies (
    run_id TEXT PRIMARY KEY
        REFERENCES runtime_runs(run_id) ON DELETE RESTRICT,
    max_total_tasks INTEGER NOT NULL CHECK (max_total_tasks >= 1),
    max_task_depth INTEGER NOT NULL CHECK (max_task_depth >= 0),
    created_at TEXT NOT NULL
);

INSERT INTO runtime_run_policies(
    run_id, max_total_tasks, max_task_depth, created_at
)
SELECT run_id, 256, 8, created_at
FROM runtime_runs
WHERE NOT EXISTS (
    SELECT 1
    FROM runtime_run_policies policy
    WHERE policy.run_id = runtime_runs.run_id
);

CREATE TRIGGER IF NOT EXISTS tr_runtime_run_policies_no_update
BEFORE UPDATE ON runtime_run_policies
BEGIN
    SELECT RAISE(ABORT, 'runtime Run policies are immutable');
END;

CREATE TRIGGER IF NOT EXISTS tr_runtime_run_policies_no_delete
BEFORE DELETE ON runtime_run_policies
BEGIN
    SELECT RAISE(ABORT, 'runtime Run policies are append-only');
END;

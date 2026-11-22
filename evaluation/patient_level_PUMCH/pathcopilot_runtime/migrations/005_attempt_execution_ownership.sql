ALTER TABLE runtime_tasks
ADD COLUMN attempt_managed INTEGER NOT NULL DEFAULT 0
    CHECK (attempt_managed IN (0, 1));

ALTER TABLE runtime_task_leases
ADD COLUMN worker_slot_id TEXT;

ALTER TABLE runtime_task_leases
ADD COLUMN claim_request_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_task_leases_worker_slot_active
ON runtime_task_leases(worker_id, worker_slot_id)
WHERE worker_slot_id IS NOT NULL
  AND state IN ('active', 'revoking');

CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_task_leases_claim_request
ON runtime_task_leases(worker_id, worker_slot_id, claim_request_id)
WHERE worker_slot_id IS NOT NULL
  AND claim_request_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_runtime_tasks_attempt_managed
ON runtime_tasks(attempt_managed, state, updated_at);

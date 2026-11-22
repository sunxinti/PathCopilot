ALTER TABLE runtime_tasks
ADD COLUMN active_attempt_id TEXT;

ALTER TABLE runtime_tasks
ADD COLUMN active_lease_id TEXT;

ALTER TABLE runtime_tasks
ADD COLUMN fencing_token INTEGER NOT NULL DEFAULT 0
    CHECK (fencing_token >= 0);

ALTER TABLE runtime_tasks
ADD COLUMN next_retry_at TEXT;

ALTER TABLE runtime_tasks
ADD COLUMN cancel_deadline_at TEXT;

CREATE TABLE IF NOT EXISTS runtime_task_retry_policies (
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    max_attempts INTEGER NOT NULL DEFAULT 1 CHECK (max_attempts >= 1),
    initial_backoff_seconds REAL NOT NULL DEFAULT 0
        CHECK (initial_backoff_seconds >= 0),
    max_backoff_seconds REAL NOT NULL DEFAULT 0
        CHECK (
            max_backoff_seconds >= 0
            AND (
                max_backoff_seconds = 0
                OR max_backoff_seconds >= initial_backoff_seconds
            )
        ),
    backoff_multiplier REAL NOT NULL DEFAULT 2
        CHECK (backoff_multiplier >= 1),
    jitter_ratio REAL NOT NULL DEFAULT 0
        CHECK (jitter_ratio >= 0 AND jitter_ratio <= 1),
    retryable_error_codes_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, task_id),
    FOREIGN KEY (run_id, task_id)
        REFERENCES runtime_tasks(run_id, task_id)
);

INSERT INTO runtime_task_retry_policies(
    run_id,
    task_id,
    max_attempts,
    initial_backoff_seconds,
    max_backoff_seconds,
    backoff_multiplier,
    jitter_ratio,
    retryable_error_codes_json,
    created_at
)
SELECT
    run_id,
    task_id,
    1,
    0,
    0,
    2,
    0,
    '[]',
    created_at
FROM runtime_tasks
WHERE NOT EXISTS (
    SELECT 1
    FROM runtime_task_retry_policies policy
    WHERE policy.run_id = runtime_tasks.run_id
      AND policy.task_id = runtime_tasks.task_id
);

CREATE TABLE IF NOT EXISTS runtime_task_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    state TEXT NOT NULL CHECK (state IN (
        'active', 'succeeded', 'failed', 'cancelled', 'expired'
    )),
    state_version INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    retry_policy_json TEXT NOT NULL,
    retryable INTEGER CHECK (retryable IS NULL OR retryable IN (0, 1)),
    failure_code TEXT,
    result_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    ended_at TEXT,
    UNIQUE(run_id, task_id, attempt_no),
    UNIQUE(run_id, attempt_id),
    FOREIGN KEY (run_id, task_id)
        REFERENCES runtime_tasks(run_id, task_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_task_attempts_active
ON runtime_task_attempts(run_id, task_id)
WHERE state = 'active';

CREATE INDEX IF NOT EXISTS ix_runtime_task_attempts_task
ON runtime_task_attempts(run_id, task_id, attempt_no);

CREATE INDEX IF NOT EXISTS ix_runtime_task_attempts_state
ON runtime_task_attempts(state, updated_at);

CREATE TABLE IF NOT EXISTS runtime_task_leases (
    lease_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    lease_epoch INTEGER NOT NULL CHECK (lease_epoch >= 1),
    worker_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'active', 'revoking', 'released', 'expired', 'revoked'
    )),
    state_version INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    fencing_token INTEGER NOT NULL CHECK (fencing_token >= 1),
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    released_at TEXT,
    release_reason TEXT,
    UNIQUE(attempt_id, lease_epoch),
    UNIQUE(run_id, task_id, fencing_token),
    UNIQUE(run_id, lease_id),
    FOREIGN KEY (run_id, task_id)
        REFERENCES runtime_tasks(run_id, task_id),
    FOREIGN KEY (run_id, attempt_id)
        REFERENCES runtime_task_attempts(run_id, attempt_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_task_leases_authoritative
ON runtime_task_leases(run_id, task_id)
WHERE state IN ('active', 'revoking');

CREATE INDEX IF NOT EXISTS ix_runtime_task_leases_attempt
ON runtime_task_leases(attempt_id, lease_epoch);

CREATE INDEX IF NOT EXISTS ix_runtime_task_leases_expiry
ON runtime_task_leases(state, expires_at);

ALTER TABLE runtime_artifacts
ADD COLUMN attempt_id TEXT
    REFERENCES runtime_task_attempts(attempt_id);

ALTER TABLE runtime_artifacts
ADD COLUMN lease_id TEXT
    REFERENCES runtime_task_leases(lease_id);

ALTER TABLE runtime_artifacts
ADD COLUMN fencing_token INTEGER
    CHECK (fencing_token IS NULL OR fencing_token >= 1);

CREATE INDEX IF NOT EXISTS ix_runtime_artifacts_attempt
ON runtime_artifacts(run_id, task_id, attempt_id);

CREATE INDEX IF NOT EXISTS ix_runtime_tasks_claimable
ON runtime_tasks(state, next_retry_at, created_at);

CREATE INDEX IF NOT EXISTS ix_runtime_tasks_cancel_watchdog
ON runtime_tasks(state, cancel_deadline_at)
WHERE cancel_deadline_at IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS tr_runtime_task_attempts_no_delete
BEFORE DELETE ON runtime_task_attempts
BEGIN
    SELECT RAISE(ABORT, 'runtime task attempts are append-only');
END;

CREATE TRIGGER IF NOT EXISTS tr_runtime_task_retry_policies_no_update
BEFORE UPDATE ON runtime_task_retry_policies
BEGIN
    SELECT RAISE(ABORT, 'runtime Task retry policies are immutable');
END;

CREATE TRIGGER IF NOT EXISTS tr_runtime_task_retry_policies_no_delete
BEFORE DELETE ON runtime_task_retry_policies
BEGIN
    SELECT RAISE(ABORT, 'runtime Task retry policies are append-only');
END;

CREATE TRIGGER IF NOT EXISTS tr_runtime_task_leases_no_delete
BEFORE DELETE ON runtime_task_leases
BEGIN
    SELECT RAISE(ABORT, 'runtime task leases are append-only');
END;

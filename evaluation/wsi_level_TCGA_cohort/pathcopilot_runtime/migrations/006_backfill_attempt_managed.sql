-- Migration 005 is immutable because applied databases pin its checksum.
-- Backfill 004/005-era Tasks that already carry Attempt execution evidence.
UPDATE runtime_tasks
SET attempt_managed = 1
WHERE attempt_managed = 0
  AND (
      active_attempt_id IS NOT NULL
      OR active_lease_id IS NOT NULL
      OR fencing_token <> 0
      OR next_retry_at IS NOT NULL
      OR EXISTS (
          SELECT 1
          FROM runtime_task_attempts AS attempts
          WHERE attempts.run_id = runtime_tasks.run_id
            AND attempts.task_id = runtime_tasks.task_id
      )
  );

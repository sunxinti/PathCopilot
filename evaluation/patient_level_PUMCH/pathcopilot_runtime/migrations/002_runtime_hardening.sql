ALTER TABLE runtime_runs
ADD COLUMN creation_fingerprint TEXT;

CREATE TRIGGER IF NOT EXISTS tr_runtime_events_no_update
BEFORE UPDATE ON runtime_events
BEGIN
    SELECT RAISE(ABORT, 'runtime events are immutable');
END;

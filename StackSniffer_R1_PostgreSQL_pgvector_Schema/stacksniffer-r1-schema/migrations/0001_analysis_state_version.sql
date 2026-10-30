-- 0001: a persisted version for every analysis status change.
--
-- Event streams use it as the SSE event id (backend/routers/analyses.py), so ids stay ordered
-- across API restarts and across states that repeat (a retried stage). Existing analyses start
-- at 1; only increases from there matter.
--
-- The trigger sets the new version inside the UPDATE that changes the status, under that row's
-- lock: a concurrent status change waits for the lock and then sees the committed version, so
-- two states can never share a version, and a reader sees status and version from one row.

ALTER TABLE core.analysis ADD COLUMN IF NOT EXISTS state_version BIGINT NOT NULL DEFAULT 1;

CREATE OR REPLACE FUNCTION core.bump_analysis_state_version()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = core, public, pg_temp
AS $$
BEGIN
    NEW.state_version := OLD.state_version + 1;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS analysis_state_version_trg ON core.analysis;

CREATE TRIGGER analysis_state_version_trg
BEFORE UPDATE OF status ON core.analysis
FOR EACH ROW
WHEN (OLD.status IS DISTINCT FROM NEW.status)
EXECUTE FUNCTION core.bump_analysis_state_version();

-- v0.19.0: unified agent_runs table replaces routine_runs.
--
-- Tracks one row per LLM-call site (main agent loop, synthesis, skill
-- creator, routine fire). Replaces routine_runs as the single source of
-- truth for per-run history; the old data is copied across with
-- source='routine' so existing UI continues to work.
--
-- status values:
--   running  — row inserted at run start, not yet finalized
--   ok       — agent.run finished, no error marker
--   err      — agent.run raised or reply matched a dry-run error marker
--   aborted  — abort_event fired mid-run; finished_at may be NULL
--   missed   — routine slot lapsed while server was offline; tokens=0
--   skipped  — per-thread fire lock held; tokens=0
BEGIN;

CREATE TABLE IF NOT EXISTS agent_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id       TEXT NOT NULL,
    cron_id         INTEGER,
    source          TEXT NOT NULL,
    scheduled_at    REAL,
    started_at      REAL NOT NULL,
    finished_at     REAL,
    duration_ms     INTEGER,
    status          TEXT NOT NULL,
    error           TEXT,
    result_preview  TEXT,
    model           TEXT,
    provider        TEXT,
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    cost_usd        REAL
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_thread_id  ON agent_runs(thread_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_started_at ON agent_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_cron_id    ON agent_runs(cron_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_source     ON agent_runs(source);

-- Copy existing routine_runs into agent_runs (best-effort; legacy installs
-- that never created the table get a no-op via the EXISTS clause).
INSERT INTO agent_runs
    (cron_id, thread_id, scheduled_at, started_at, finished_at,
     duration_ms, status, error, result_preview, source)
SELECT cron_id, COALESCE(thread_id, ''), scheduled_at, started_at, finished_at,
       duration_ms, status, error, result_preview, 'routine'
FROM routine_runs
WHERE EXISTS (SELECT name FROM sqlite_master WHERE type='table' AND name='routine_runs');

DROP TABLE IF EXISTS routine_runs;

COMMIT;

-- v0.22.0: durable goal runtime — Phase 1 of long-running agent architecture.
--
-- Three tables that let a backend worker process pick up user goals, run them
-- for hours, checkpoint state between rounds, and resume after kill -9.
--
-- goals             — one row per /goal request; durable queue + status machine
-- goal_checkpoints  — orchestrator state snapshots (messages + plan + facts)
-- goal_events       — append-only event log for observability + debugging
--
-- The lease protocol (worker_id + lease_expires_at) lets a second worker take
-- over a goal if the first one dies without releasing — see db.claim_next_goal.
BEGIN;

CREATE TABLE goals (
    id                TEXT PRIMARY KEY,            -- 'g_<random16>'
    thread_id         TEXT,                        -- chat thread that created it (nullable for API/cli)
    source            TEXT NOT NULL,               -- 'web' | 'cli' | 'telegram' | 'scheduler' | 'api'
    user_input        TEXT NOT NULL,               -- original user request text
    status            TEXT NOT NULL,               -- 'pending'|'running'|'paused'|'done'|'failed'|'aborted'
    plan              TEXT,                        -- JSON: {subtasks: [...], current_index: N}; null until orchestrator sets it
    result            TEXT,                        -- final reply when status='done'
    error             TEXT,                        -- error message when status='failed'
    budget_usd        REAL,                        -- hard $ cap; null = unbounded
    budget_seconds    INTEGER,                     -- hard wall-clock cap; null = unbounded
    cost_usd          REAL DEFAULT 0,              -- running total, rolled up from agent_runs
    started_at        REAL,                        -- unix ts; set on first claim
    finished_at       REAL,                        -- unix ts; set on terminal status
    created_at        REAL NOT NULL,
    -- Worker lease — lets one process safely claim a goal, and lets others
    -- detect when that process died (lease_expires_at < now()) and take over.
    worker_id         TEXT,                        -- 'host_<hostname>_<pid>_<uuid6>'
    lease_expires_at  REAL,                        -- worker must heartbeat before this
    -- Free-form metadata: telegram chat id, web session id, scheduler cron id, etc.
    meta              TEXT                         -- JSON
);

-- Hot path for claim_next_goal: find runnable goals fast.
CREATE INDEX idx_goals_status_lease ON goals (status, lease_expires_at);
CREATE INDEX idx_goals_thread ON goals (thread_id);
CREATE INDEX idx_goals_created ON goals (created_at);


CREATE TABLE goal_checkpoints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id         TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    round_num       INTEGER NOT NULL,              -- orchestrator round when snapshot taken
    subtask_index   INTEGER NOT NULL DEFAULT -1,   -- -1 = planning phase, >=0 = subtask in flight
    messages_blob   BLOB NOT NULL,                 -- gzipped JSON of orchestrator messages[]
    plan_snapshot   TEXT NOT NULL,                 -- JSON: same shape as goals.plan
    facts_snapshot  TEXT NOT NULL DEFAULT '{}',    -- JSON: snapshot of goal_facts (Phase 4)
    timestamp       REAL NOT NULL,
    UNIQUE (goal_id, round_num)
);

-- DESC index so "load latest" is a single seek.
CREATE INDEX idx_checkpoints_goal_round ON goal_checkpoints (goal_id, round_num DESC);


CREATE TABLE goal_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id     TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    timestamp   REAL NOT NULL,
    event_type  TEXT NOT NULL,                     -- see docs/superpowers/plans/...arch.md for the enum
    payload     TEXT NOT NULL DEFAULT '{}'        -- JSON, schema per event_type
);

CREATE INDEX idx_events_goal_time ON goal_events (goal_id, timestamp);

COMMIT;

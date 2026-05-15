-- v0.22.1: structured fact store scoped to a single goal.
--
-- Phase 2 of long-running agent: the orchestrator and subagents save
-- intermediate findings (URLs, IDs, credentials, counts) here as typed
-- key/value rows. Facts bypass context compaction entirely — when the
-- orchestrator's messages get trimmed, the facts table is still the
-- source of truth.
--
-- Per-goal scoping is enforced by the (goal_id, key) primary key;
-- ON DELETE CASCADE cleans up automatically when a goal row goes away.
BEGIN;

CREATE TABLE goal_facts (
    goal_id            TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    key                TEXT NOT NULL,
    value              TEXT NOT NULL,
    source_subtask_id  TEXT,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL,
    PRIMARY KEY (goal_id, key)
);

CREATE INDEX idx_goal_facts_goal ON goal_facts (goal_id);

COMMIT;

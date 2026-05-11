-- v0.18.7: persistent storage for `canvas` skill artifacts (dashboards,
-- saved forms, mockups, prototypes).
--
-- The canvas skill renders model-generated HTML in a sandboxed right-side
-- panel. Three use cases need persistence:
--   * dashboards the user wants to reopen later ("show me my sales
--     dashboard from last week")
--   * form definitions that get reused across threads
--   * mockups / prototypes referenced in multiple conversations
--
-- Slug is the primary key — human-meaningful identifier (e.g.
-- "weekly-sales", "new-client-form"). UI shows artifacts by title in
-- the `Canvases` left-nav view.
--
-- HTML is stored inline (not on disk). The skill enforces a 256 KB cap
-- both at the tool entry point and at the POST /api/canvas/artifacts
-- endpoint to bound storage growth.
BEGIN;

CREATE TABLE IF NOT EXISTS canvas_artifacts (
    slug       TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    html       TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    thread_id  TEXT,
    meta       TEXT
);

CREATE INDEX IF NOT EXISTS idx_canvas_artifacts_updated
    ON canvas_artifacts(updated_at DESC);

COMMIT;

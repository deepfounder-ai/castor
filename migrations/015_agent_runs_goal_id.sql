-- Wire agent_runs back to the goal that spawned them.
--
-- Motivation: budget_usd is a USER-set hard cap on a goal's USD spend, but
-- it was dead-storage — orchestrator.py reads `goals.cost_usd` for the
-- check and NOTHING writes to that column. As a result, every goal we ran
-- (g_0937821f088f4580, g_1043a97d08d342ce, and the whole history) shows
-- cost_usd=0 in `goals` while `agent_runs` shows $3+ actually spent.
--
-- The cleanest tie is goal_id directly on each agent_runs row. Then the
-- budget check (and any UI cost-per-goal widget) sums agent_runs by
-- goal_id, no flaky time-window / source heuristic. Subagent runs already
-- propagate ctx.goal_id via TurnContext — agent_loop.insert_agent_run just
-- needs to write it through.
--
-- NULL goal_id = non-goal run (CLI / Telegram / Web chat / scheduler) —
-- preserves the existing analytics shape for everything else.

ALTER TABLE agent_runs ADD COLUMN goal_id TEXT;

-- Index used by the per-goal cost summation in orchestrator.py budget
-- enforcement + future "Cost per goal" UI columns.
CREATE INDEX IF NOT EXISTS idx_agent_runs_goal_id
    ON agent_runs(goal_id)
    WHERE goal_id IS NOT NULL;

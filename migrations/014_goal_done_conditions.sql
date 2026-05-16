-- Goal-level acceptance criteria: 0..N validators that MUST pass before
-- mark_goal_done can fire. Independent of the per-subtask done_condition
-- list in goals.plan.subtasks[].done_condition.
--
-- Motivation: the drayage research stress-test (g_5c4e6e3dc90c4f47) closed
-- as done with 11/11 subtasks validated, but the orchestrator capitulated
-- on the synthesis report (the deliverable the user actually wanted) —
-- because that wasn't a subtask in the plan. Per-subtask gates protect
-- only what's in the plan; goal-level criteria protect what's in the user's
-- request, regardless of how the orchestrator chose to break it down.
--
-- Stored as JSON array of {kind, spec} objects matching goal_validators
-- contract. NULL / empty array = no goal-level criteria (back-compat).

ALTER TABLE goals ADD COLUMN done_conditions TEXT;

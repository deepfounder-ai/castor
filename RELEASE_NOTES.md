## v0.23.1 — Goal Runtime Hardening

v0.23.0 shipped the Goal Runtime as a new architecture. Real production stress-tests on long LinkedIn networking goals (50+ subagent dispatches across 100+ minutes) surfaced a class of bugs that were invisible in unit tests because the affected code paths were dead in v0.23.0 — the budget cap never fired, the workspace was never isolated, secrets never got scrubbed in goal storage. This release wires them all up and adds the production-shaped tests that should have caught them.

**Backwards compatible** — no schema break beyond one additive migration (`015_agent_runs_goal_id.sql`). Goals submitted on v0.23.0 keep running; the new behaviours apply from the next claim onward.

### Goal-runtime fixes

- **Per-goal workspace at `~/.castor/workspace/goals/<goal_id>/`.** Each goal now runs in its own dir. The shared workspace is invisible to the orchestrator inside a goal context — no more 60-round shell-mining sweeps over leftover CSVs / screenshots from prior goals. Symmetric writer/validator path rewriting catches the orchestrator's habit of writing `~/.castor/workspace/foo.csv` and routes it under the goal dir transparently.

- **Budget cap (`budget_usd`) actually works.** Migration 015 adds `agent_runs.goal_id` and rolls up costs via a LEFT JOIN. Before this commit, `goals.cost_usd` was dead storage (never written), so the orchestrator's per-round budget check read 0 forever. The Goals UI Cost column now displays real spend.

- **Provider transient errors → paused (not failed).** OpenRouter 402 / 429, upstream 5xx, etc. classify as transient. The goal goes to `paused` with a per-class backoff (402: 300s, 429: 60s, 5xx: 30s) so a topped-up account or expired rate-limit window lets the goal resume from the latest checkpoint — no work lost.

- **Pause-with-backoff prevents reclaim thrash.** Without this, the worker's 5s poll cycle would re-claim a 402-paused goal and immediately burn another 402, in a tight loop. The backoff repurposes `lease_expires_at` as a "don't reclaim before this time" marker (no schema change).

- **`~`-expansion bug in goal_validators.** `_resolve("~/.castor/workspace/foo.txt")` was looking up `<workspace>/~/.castor/workspace/foo.txt`. Every regex/files check on a `~`-prefixed path falsely failed with "file does not exist", which forced the acceptance gate to mark working goals as failed.

- **Skipped subtasks no longer block the gate.** A subtask marked `status="skipped"` (e.g. orchestrator hit a quota early) had its `done_condition` evaluated anyway. The gate now correctly bypasses skipped entries.

- **Orchestrator anti-capitulation prompt rule.** The "Knowing when you have ENOUGH" section in `prompts/orchestrator.md` used to say "20-30 leads is enough for an MVP." That cap applied to vague quantities only — but the orchestrator also obeyed it for user-specified numbers ("100 invites" → delivered 50). The rule is now scoped: explicit numeric targets in the user_input are LAW; scaling them down is labelled as capitulation, not engineering.

### Security: secrets no longer leak through goal storage

Forensic inspection of a production goal showed plaintext credentials in three goal-runtime tables (`goal_facts`, `goal_events`, `goal_checkpoints.messages_blob`). The `_scrub_secrets()` regex set that `memory.save()` has used since v0.17.18 was never applied to these new v0.22 storage paths.

- **Shared `secret_scrub.py` module.** Patterns moved out of `memory.py`. `scrub_text` for free-form text, `scrub_fact(key, value)` adds a key-name heuristic — keys named `password`, `api_key`, `access_token`, `private_key`, `session_cookie`, etc. fully redact their value regardless of shape, catching plain string passwords that don't match any provider regex.

- **Four goal storage paths now scrub on insert.** `db.fact_save`, `db.log_goal_event`, `db.save_checkpoint`, and `db.attach_goal_output` all pass values through the appropriate scrub before write. `save_checkpoint` also walks `tool_calls[].function.arguments` so the orchestrator's habit of putting credentials in dispatch prompts gets caught.

- **Natural-language credential phrasing.** Added a second regex for "Fill in the password field (#password) with: hunter2" style prose — the dispatch-prompt pattern that exposed a LinkedIn password in production. Keeps innocent technical writing intact.

- **Browser subagent now has direct vault access.** Added `secret_get` / `secret_list` to the browser subagent's tool whitelist + a new `Credential handling` section in `prompts/orchestrator.md`. The orchestrator no longer needs to fetch credentials and ferry them across the trust boundary into dispatch prompts — the subagent fetches them locally and the raw value never enters orchestrator messages, events, or checkpoints.

### Behaviour changes

- **Worker daemon costs now roll up per goal.** Old goals (created before migration 015) keep showing `cost_usd: 0.0` since their `agent_runs` rows have no `goal_id` link. New goals get accurate per-goal cost tracking immediately.

- **A paused goal with `retry_after_sec` set is invisible to `claim_next_goal` until the deadline elapses.** Existing pause paths (worker shutdown, user pause) don't set the deadline, so they stay immediately reclaimable — same as before.

- **`mark_goal_paused(reason, retry_after_sec=N)`** is the new signature. Old callers (the keyword-only `reason=` form) keep working.

### Migrations

`015_agent_runs_goal_id.sql` — adds `goal_id TEXT` column to `agent_runs` with a partial index. Backward-compatible (existing rows get NULL, treated as "non-goal run" by the budget rollup).

### Tests

96 new tests across 4 new + several updated files. Full suite: 1345 passing, 24 skipped.

- `test_goal_workspace_isolation.py` (13) — per-goal workspace creation, path-rewrite invariants, cross-goal isolation.
- `test_provider_error_classification.py` (15) — 402/429/5xx classification, integration with `goal_runner.run`, backoff blocking immediate reclaim.
- `test_secret_scrub_goals.py` (25) — every goal storage path scrubs, including `attach_goal_output` and tool_calls.arguments.
- `test_agent_runs.py` (+5) — `goal_id` column persistence, `get_goal_total_cost`, `get_goal` / `list_goals` cost rollup contract.

### Upgrading

`git pull` + restart the server (or `castor-worker`). The first goal_runner claim on the new code applies migration 015 automatically. No config changes required.

If you have goals paused or failed on v0.23.0 with `APIStatusError 402` (OpenRouter out of credits) in the error field, you can manually convert them to `paused` to make them resumable:

```sql
UPDATE goals
SET status='paused', error=NULL, finished_at=NULL,
    worker_id=NULL, lease_expires_at=NULL
WHERE status='failed' AND error LIKE '%402%';
```

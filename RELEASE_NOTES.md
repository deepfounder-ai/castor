## v0.23.4 — Secret-scrub bundle (3 CRITICAL fixes)

Security-focused patch release. Closes the secret-scrubbing bypass family flagged by the whole-codebase architecture review (cross-cutting §4.1): three CRITICAL findings and one HIGH, all in a single PR. No schema migrations. No breaking changes. Drop-in upgrade.

### What changed

Three persistence paths were skipping the redaction layer that `memory.save` has used since v0.17.18. Every site now shares the same `secret_scrub.scrub_text` / `scrub_fact` engine.

**C1 — `db.save_message` (chat history)**

Chat history was the project's largest secret surface: every user turn, every tool call, every tool result landed in `messages.{content, tool_calls, meta}` verbatim. The same redaction layer that `save_checkpoint` uses in-flight is now applied at message persistence. The `fact_save({"key": "linkedin_password", "value": "..."})` structural special-case is mirrored so plain-string passwords keyed by a self-identifying name are caught — not just provider-regex shapes.

**C2 — `synthesis.py` (entity / wiki summaries)**

The night synthesis pass calls `memory._save_single` directly to persist LLM-summarised entity and wiki blobs. `memory.save` scrubbed at its entry, so direct callers bypassed redaction. `_save_single` now scrubs by default; `memory.save` passes `scrub=False` (it already scrubbed at the boundary). Synthesis paths pick up the scrub for free.

**C3 — `trajectory.tool_start` / `tool_end` (JSONL audit trail)**

Trajectory recorder is opt-in but ships with a 30-day default retention — a tool that echoed a secret would persist it on disk longer than the chat that triggered it. `args` dict and `result_preview` now run through `secret_scrub`. The `fact_save` structural special-case is reused so passwords stored under `{"key": "...", "value": "..."}` shape are caught.

**H4 — `trajectory.prune_old` actually wired**

`prune_old(days)` was defined since v0.22 but never called — the "30-day rotation" was documented but never fired. New `__trajectory_prune__` system task at 04:00 daily, registered only when `trajectory_enabled`, routes through `_execute_task` to `trajectory.prune_old(trajectory_keep_days)`. Stateless fast path — no LLM, zero cost.

### Why this matters

The architecture review's verdict was "the security story is mostly honoured in the spec, but the implementation has at least three places where secret-scrubbing is bypassed on real persistence paths. Close those (small surgical fixes) and Castor's defensive posture matches what its docs already promise." This release closes those three places.

### Tests

1590 passing (was 1500 in v0.23.3). 16 new tests in `tests/test_scrub_bundle.py` pin every surface area:

- `_save_single` scrubs by default; `scrub=False` opt-out works.
- `memory.save` → `_save_single` chain scrubs once, no double-warning.
- `save_message` scrubs content / tool_calls (incl. fact_save shape) / meta.
- `save_message` passes clean text byte-for-byte.
- `tool_start` scrubs args dict, incl. fact_save keyed-as-secret value.
- `tool_end` scrubs result_preview; empty-result safe.
- `_register_trajectory_prune` is opt-in (skips when trajectory disabled).
- `_execute_task` routes the task name to `prune_old`.
- `_is_routine` returns False (system task stays on fast path).
- `prune_old` actually deletes stale `*.jsonl` files.

The only failure in the full suite is `tests/test_serial_port_skill::test_list_ports_empty_includes_platform_hints` — pre-existing platform flake on main, unrelated to this PR.

### Upgrading

`git pull` + restart. No config or schema changes.

To audit pre-v0.23.4 chat history for secrets, run the existing `memory.reindex_from_markdown` recovery flow (added in v0.23.3) — atoms re-embedded from markdown source get re-scrubbed on the way back into Qdrant.

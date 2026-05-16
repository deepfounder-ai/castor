# Acceptance Gate — Spec for Parallel Implementation

**Status:** active spec, do not modify after parallel agents start
**Date:** 2026-05-16
**Goal:** prevent orchestrator capitulation by adding machine-verifiable acceptance criteria + a runtime completion gate that re-enters the loop with remediation on failure.

This document is the **contract** between three parallel implementation workstreams. Read it fully before changing anything. If you find an ambiguity, raise it — do not improvise.

---

## Motivation

Goal `g_45ef493d1b0f4de5` failed because the orchestrator hit one `write_file` escape error, capitulated, and wrote a final reply saying "data is on disk, please run this bash command yourself". The `goal_runner` auto-skip backstop quietly closed 3 subtasks and marked the goal `done`. Anthropic's pattern for this (see `.claude/skills/agents-best-practices/`): the `Stop` hook returns `{decision: block, reason: <remediation>}` until the model verifies its work via tool observations.

We port that pattern. Each subtask carries a machine-checkable `done_condition`. Before the goal can complete, the runtime runs every condition; failures inject a remediation message and re-enter the orchestrator loop.

---

## 1. `done_condition` schema

Every subtask in a plan **must** carry a `done_condition` object. Closed set of `kind` values — no free-text.

```jsonc
{
  "kind": "files_exist" | "min_count" | "regex_in_file" | "shell_returns_zero" | "http_200",
  "spec": { /* kind-specific */ }
}
```

### Kinds + specs

| `kind` | `spec` shape | Pass when |
|---|---|---|
| `files_exist` | `{"paths": ["abs/or/rel/path", ...]}` | All listed paths exist on disk. |
| `min_count` | `{"glob": "docs/module_*.md", "min": 50}` | `glob.glob(spec.glob)` returns ≥ `min` matches. |
| `regex_in_file` | `{"path": "...", "pattern": "..."}` | File exists AND `re.search(pattern, file.read(), re.MULTILINE)` matches. |
| `shell_returns_zero` | `{"cmd": "test -d ...", "timeout": 10}` | `subprocess.run(cmd, shell=True, timeout=spec.timeout).returncode == 0`. `timeout` defaults to 10s if absent, capped at 60s. |
| `http_200` | `{"url": "https://..."}` | HTTP GET returns status 200..299. 10s connect+read timeout. No redirects beyond 3. |

### Path resolution

- `files_exist.paths`, `regex_in_file.path`: relative paths resolve against `~/.castor/workspace/` (the workspace dir). Absolute paths used as-is.
- `min_count.glob`: same rule — relative globs anchored at workspace.
- `shell_returns_zero.cmd`: shell runs with `cwd=~/.castor/workspace/`.

### Validation of the criterion itself (NOT the subtask)

Before storing a criterion, reject malformed ones with `ValueError`:

- `kind` must be one of the 5 above.
- `spec` must be a dict.
- Per-kind required fields must be present and the right type.
- `min_count.min` ≥ 1.
- `regex_in_file.pattern` must compile (`re.compile`); reject if it doesn't.
- `shell_returns_zero.cmd` must be a non-empty string.
- `http_200.url` must start with `http://` or `https://`.

---

## 2. `goal_validators` module (Workstream A)

**New file:** `goal_validators.py` at repo root (top-level module, importable as `import goal_validators`).

### Public API

```python
def validate_criterion(criterion: dict) -> None:
    """Raise ValueError if `criterion` is malformed (wrong kind, missing spec fields, bad regex, etc).
    Returns None on success. Pure schema check — does NOT execute the criterion."""

def run_validator(criterion: dict) -> tuple[bool, str]:
    """Execute `criterion` against the filesystem / shell / HTTP.

    Returns (passed, remediation).
      passed=True  → criterion satisfied. `remediation` is the empty string.
      passed=False → criterion not yet met. `remediation` is a SHORT, MODEL-READABLE
                     instruction in the style of Anthropic's hook-block messages.
                     Examples (all must be present in code):
                       "Expected file 'docs/API.md' to exist, but it does not. Create it."
                       "Expected at least 50 files matching glob 'docs/module_*.md', found 0. Generate them (use a small Python script + shell rather than write_file with a huge literal — escapes break easily)."
                       "Expected regex 'Module Index' to match in 'docs/API.md', not found. Add the section."
                       "Shell command exited with code 2, expected 0. stderr: <first 200 chars>. Fix the underlying issue."
                       "HTTP GET https://... returned 503. Wait and retry, or check the URL is correct."

    NEVER raises on a failing criterion — `passed=False` is the normal channel.
    Catches and swallows OSError / re.error / subprocess.TimeoutExpired / requests errors,
    converting them into `(False, "<diagnostic remediation>")`. The point is to give the
    orchestrator a model-readable next step, not crash the gate.

    Must NOT raise on malformed criterion either (caller is expected to have called
    validate_criterion first); but if it sees one, return (False, "Malformed criterion: <details>").
    """
```

### Implementation notes

- Use stdlib only: `pathlib`, `glob`, `re`, `subprocess`, `urllib.request`. NO `requests` dependency (we don't want to add it just for this).
- For `http_200`, use `urllib.request.urlopen(url, timeout=10)` and check `response.status`. Wrap in try/except — any exception → `(False, "<diagnostic>")`.
- `shell_returns_zero`: `subprocess.run(cmd, shell=True, capture_output=True, timeout=spec.timeout, cwd=workspace_root)`. On `CalledProcessError` / `TimeoutExpired` → `(False, "...")`. Include first 200 chars of stderr in the remediation when available.
- Path resolution: define a single internal helper `_resolve(rel_or_abs: str) -> Path` that applies the workspace-root rule.
- Workspace root: `Path(os.path.expanduser(os.environ.get("CASTOR_DATA_DIR", "~/.castor"))) / "workspace"` — match how `config.py` resolves DATA_DIR. (Look at `config.py` briefly to confirm; mirror the same logic.)

### Tests (also in workstream A)

**New file:** `tests/test_goal_validators.py`

At minimum 20 tests, structured per-kind:

- `files_exist`:
  - pass: 2 files that exist
  - fail: 1 missing → remediation mentions the missing path
  - fail: empty `paths` list → malformed
- `min_count`:
  - pass: glob with 5 matches, min=3
  - fail: glob with 0 matches, min=1 → remediation includes the glob pattern + actual count
  - fail: missing `glob` key → malformed
- `regex_in_file`:
  - pass: file with matching line
  - fail: file exists but pattern not found → remediation
  - fail: file does not exist → remediation
  - fail: invalid regex on validate_criterion → ValueError
- `shell_returns_zero`:
  - pass: `true` (returns 0)
  - fail: `false` (returns 1) → remediation includes exit code
  - fail: `cmd: "sleep 5"` with timeout=1 → remediation mentions timeout
  - fail: empty cmd → malformed
- `http_200`:
  - pass: monkeypatched urlopen returning 200
  - fail: monkeypatched urlopen returning 500 → remediation includes status
  - fail: network error (monkeypatched to raise) → remediation includes error class
- `validate_criterion`:
  - all 5 happy paths
  - unknown kind → ValueError
  - non-dict criterion → ValueError
  - non-dict spec → ValueError

Use `tmp_path` fixture (pytest builtin) for file-based tests so we don't pollute the workspace.
Use `monkeypatch` for `urllib.request.urlopen` in http_200 tests — DO NOT make real network calls.

---

## 3. DB + plan tool integration (Workstream B)

### Schema changes — **NONE in SQL**

`done_condition` lives inside the existing `goals.plan` JSON column. No migration. Just extend the dict shape.

### `db.set_goal_plan` (modify)

**Current signature:** `set_goal_plan(goal_id: str, subtasks: list[dict]) -> dict`

**Modify:** Each input subtask **must** carry a `done_condition` dict. Reject the call (raise `ValueError`) if any subtask is missing one OR if any criterion fails `goal_validators.validate_criterion`.

The stored subtask dict gains two new fields:

```python
{
  "id": "st_1",
  "title": "...",
  "description": "...",
  "done_condition": { "kind": "...", "spec": {...} },   # NEW — copied from input as-is
  "validation_passed": False,                            # NEW — set to True only when gate verifies pass
  "last_validation_failure": None,                       # NEW — remediation string from last failed validator run
  "status": "pending",
  # ... existing fields ...
}
```

### `db.update_subtask` (modify)

When `status="completed"` is requested:

1. Load the subtask's `done_condition`.
2. Call `goal_validators.run_validator(done_condition)`.
3. If `passed=True`:
   - Write `status="completed"`, `validation_passed=True`, `last_validation_failure=None`.
   - Behave as today.
4. If `passed=False`:
   - **DO NOT** write `status="completed"`. Keep status at whatever it was (likely `in_progress`).
   - Write `validation_passed=False`, `last_validation_failure=<remediation>`.
   - Bump `attempts` by 1.
   - **Return a special marker** so the caller (the tool wrapper) can surface this to the orchestrator. Suggested: return the plan as usual, but the caller checks `validation_passed` after the call.

For `status` values other than `completed` (`in_progress`, `failed`, `skipped`): behave as today, no validator invocation.

### `tools._goal_plan_set_impl` (modify)

Accept `done_condition` per subtask in the tool args. Pass through to `db.set_goal_plan`. On `ValueError` from db.set_goal_plan, return the error string verbatim (the orchestrator must see the validation failure to fix its plan).

Also update the tool **schema** in `tools.py::TOOLS` — the subtasks array's item-object now requires `done_condition` (object with `kind` enum + `spec` object).

### `tools._subtask_update_impl` (modify)

When `status="completed"`, after calling `db.update_subtask`, check if the returned plan's subtask has `validation_passed=False`. If so, return a string in the form:

```
Subtask {id} NOT marked complete: validator failed.
Remediation: {last_validation_failure}
Fix this and call subtask_update again with status=completed.
```

(The orchestrator sees this as a tool result and continues the loop.)

If the validator passed, return the existing success string.

### Tests (also in workstream B)

**New file:** `tests/test_goal_plan_done_conditions.py`

- `set_goal_plan` rejects plan with missing `done_condition`.
- `set_goal_plan` rejects plan with malformed criterion (each kind).
- `update_subtask(status="completed")` with passing condition → status becomes completed, validation_passed=True.
- `update_subtask(status="completed")` with failing condition → status NOT advanced, validation_passed=False, last_validation_failure populated, attempts++.
- `_subtask_update_impl` returns the remediation string when validator fails.
- `_subtask_update_impl` returns success string when validator passes.

Use `qwe_temp_data_dir` fixture (exists in `tests/conftest.py`) for the workspace.

---

## 4. Goal-runner completion gate (Workstream C)

### `orchestrator.run_orchestrator` (modify)

Add an **optional** kwarg `system_notes: list[str] | None = None`. When non-empty, insert each note as an additional `{"role": "system", "content": note}` message **after** the main system prompt but **before** the user input (when starting fresh). When **resuming** from a checkpoint, insert them at the END of `messages` as the most recent context. This way the orchestrator's NEXT round sees the notes.

Document this clearly in the docstring. The body of the function is otherwise unchanged.

### `goal_runner.run` (rewrite the finalization section)

Current shape (relevant excerpt):

```python
result = await loop.run_in_executor(None, lambda: orchestrator.run_orchestrator(goal_id=goal_id, ctx=ctx))
# ... auto-skip backstop ...
db.mark_goal_done(goal_id, result=reply)
```

New shape (full replacement of the post-orchestrator section):

```python
MAX_GATE_ATTEMPTS = 3  # config-overridable: config.get("acceptance_gate_max_attempts")
gate_attempt = 0
system_notes: list[str] = []
final_result = None

while True:
    gate_attempt += 1
    final_result = await loop.run_in_executor(
        None,
        lambda notes=tuple(system_notes): orchestrator.run_orchestrator(
            goal_id=goal_id, ctx=ctx, system_notes=list(notes)
        ),
    )
    # Cooperative cancellation / shutdown still wins — same as today.
    if shutdown_event.is_set():
        db.mark_goal_paused(goal_id, reason="worker_shutdown")
        return

    # Run acceptance gate over all subtasks.
    plan = db.get_goal_plan(goal_id) or {}
    failures: list[tuple[str, str]] = []   # [(subtask_id, remediation), ...]
    for st in plan.get("subtasks", []):
        cond = st.get("done_condition")
        if not cond:
            # Plans now always have done_condition — but be defensive.
            continue
        passed, remediation = goal_validators.run_validator(cond)
        if not passed:
            failures.append((st["id"], remediation))
            # Reset validation flags on the plan so it's visible in UI.
            db.update_subtask(
                goal_id, st["id"],
                validation_passed=False,
                last_validation_failure=remediation,
            )
        else:
            # Mark as validated if it wasn't already.
            if not st.get("validation_passed"):
                db.update_subtask(goal_id, st["id"], validation_passed=True)

    if not failures:
        # All gates passed — proceed to mark done as normal.
        break

    db.log_goal_event(goal_id, "acceptance_gate_blocked", {
        "attempt": gate_attempt,
        "failure_count": len(failures),
        "failures": [{"subtask_id": sid, "remediation": rem[:300]} for sid, rem in failures],
    })

    if gate_attempt >= MAX_GATE_ATTEMPTS:
        db.mark_goal_failed(
            goal_id,
            error=f"acceptance_gate_exhausted: {len(failures)} subtask(s) still failing after {MAX_GATE_ATTEMPTS} attempts",
        )
        return

    # Build a remediation note and re-enter the orchestrator.
    note_lines = [
        "ACCEPTANCE GATE: The following subtasks have NOT met their done_condition.",
        "You CANNOT finish the goal until every condition passes. Address each one and re-run subtask_update with status=completed once your work makes the validator pass.",
        "",
    ]
    for sid, rem in failures:
        note_lines.append(f"- {sid}: {rem}")
    system_notes = [" ".join(note_lines).strip() if False else "\n".join(note_lines)]
    # Continue the while loop — orchestrator runs again, now sees the notes.

# Gate passed — proceed.
reply = (final_result.get("reply") if isinstance(final_result, dict) else "") or ""
db.mark_goal_done(goal_id, result=reply)
```

### REMOVE the auto-skip backstop

The existing block in `goal_runner.run()`:

```python
# ── Plan-completion backstop ──
# ... auto-skip pending/in_progress subtasks ...
```

**Delete entirely.** Under the new architecture, leftover non-terminal subtasks are caught by the acceptance gate (since their done_condition won't have passed) and trigger remediation. Auto-skip was the antipattern that hid the original failure.

### Tests (also in workstream C)

**New file:** `tests/test_acceptance_gate.py`

- Gate passes on first attempt when all conditions met → goal marked done.
- Gate blocks on first attempt, orchestrator (mocked) "fixes" the issue on second attempt → goal marked done after second attempt.
- Gate exhausts MAX_GATE_ATTEMPTS attempts → goal marked failed with reason mentioning "acceptance_gate_exhausted".
- `goal_lifecycle_event` row written for each block with attempt number + failure count.
- `validation_passed` flag is set per subtask after gate runs (both true and false cases).

Mock `orchestrator.run_orchestrator` entirely (don't run a real LLM). Use `goal_validators.run_validator` as-is — the gate's job is the orchestration, not the validation.

---

## 5. What I (the orchestrating Claude) handle later

Once A, B, C are merged:

- `prompts/orchestrator.md` updates (loop invariant #6, recovery ladder, "no bash for user in final reply" rule, instructions on how to write good done_conditions for different task shapes).
- UI: Goal-detail Plan tab shows the done_condition + validation status per subtask.
- One end-to-end integration test stitching all three pieces together.
- Final lint + full suite + single squash commit.

**Workstreams A/B/C must NOT touch:**
- `prompts/orchestrator.md`
- `static/index.html`
- `prompts/subagent_*.md`

These are coordinated changes I'll do after the swarm lands.

---

## Out of scope (do not implement)

- A 6th validator kind. Closed set of 5.
- Validator caching. Run every time — they're cheap.
- Parallel validator execution. Run serially in order.
- Backfill of done_condition for old plans in production DB. The gate is defensive (skips subtasks without done_condition); existing goals just won't get gate enforcement.

---

## Done criteria for the swarm

Each workstream is done when its tests pass locally with `pytest` (run only the new test files first, then the full suite to confirm no regressions). A worktree that breaks the full suite is NOT done.

"""Regression tests for goal_plan_set validation + empty-plan guard.

Covers 3 bugs from goal g_bad567ebaf7d4a56:

1. tools.py error message listed wrong done_condition kind names → model
   followed the (wrong) guidance and kept failing. Now lists the real 5
   kinds with one-line spec hints.

2. goal_validators.validate_criterion did an exact membership check on
   kind with no fuzzy hint. ``files_exists`` (extra s) → hard reject with
   no suggestion. Now uses ``difflib.get_close_matches`` to offer "Did you
   mean 'files_exist'?" when the input is a near-miss.

3. goal_runner gate passed vacuously when the plan was empty (all
   goal_plan_set calls failed → no subtasks → no failures → gate passes →
   goal marked done with zero work). Now the runner checks for an empty
   plan and re-enters the orchestrator with a remediation note (or fails
   after max attempts).
"""
from __future__ import annotations

import asyncio

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────


def _bind_active_goal(monkeypatch, goal_id):
    """Patch tools._require_goal_id so the impls think we're in a goal turn."""
    import tools as t

    monkeypatch.setattr(t, "_require_goal_id", lambda: goal_id)


# ─────────────────────────────────────────────────────────────────────────────
#  Fix 1: error message lists real kinds
# ─────────────────────────────────────────────────────────────────────────────


def test_goal_plan_set_error_lists_real_kinds(qwe_temp_data_dir, monkeypatch):
    """When a subtask is missing done_condition, the error message must list
    the 5 REAL validator kinds — not the old wrong names."""
    import db
    import tools as t

    gid = db.create_goal(user_input="x", source="cli")
    _bind_active_goal(monkeypatch, gid)

    out = t._goal_plan_set_impl({"subtasks": [
        {"title": "Research costs", "description": "..."},
    ]})
    assert out.startswith("Error:")
    # Must contain all 5 real kinds
    for kind in ("files_exist", "min_count", "regex_in_file",
                 "shell_returns_zero", "http_200"):
        assert kind in out, f"error message should mention {kind!r}"

    # Must NOT contain any of the old wrong kinds
    for wrong in ("file_exists", "http_returns", "regex_in_output", "llm_check"):
        assert wrong not in out, f"error message should NOT mention old kind {wrong!r}"


# ─────────────────────────────────────────────────────────────────────────────
#  Fix 2: fuzzy matching in validate_criterion
# ─────────────────────────────────────────────────────────────────────────────


def test_fuzzy_match_suggests_correction(qwe_temp_data_dir):
    """Near-miss kind values get a 'Did you mean X?' suggestion."""
    import goal_validators

    with pytest.raises(ValueError, match=r"Did you mean 'files_exist'"):
        goal_validators.validate_criterion({
            "kind": "files_exists",  # extra 's'
            "spec": {"paths": ["x"]},
        })


def test_fuzzy_match_file_exist_singular():
    """``file_exist`` (missing plural s) → suggests ``files_exist``."""
    import goal_validators

    with pytest.raises(ValueError, match=r"Did you mean 'files_exist'"):
        goal_validators.validate_criterion({
            "kind": "file_exist",
            "spec": {"paths": ["x"]},
        })


def test_fuzzy_match_no_suggestion_for_garbage():
    """Completely unrelated kind gets no 'Did you mean' hint."""
    import goal_validators

    with pytest.raises(ValueError) as exc_info:
        goal_validators.validate_criterion({
            "kind": "zzzzz_nonsense",
            "spec": {},
        })
    assert "Did you mean" not in str(exc_info.value)


# ─────────────────────────────────────────────────────────────────────────────
#  Fix 3: empty plan triggers remediation, not vacuous pass
# ─────────────────────────────────────────────────────────────────────────────


def test_empty_plan_triggers_remediation_then_fails(qwe_temp_data_dir, monkeypatch):
    """Orchestrator returns without ever creating a plan (all goal_plan_set
    calls failed). The runner must NOT let the gate pass vacuously — it
    should re-enter the orchestrator with a PLAN REQUIRED note, and
    ultimately fail after max attempts if the plan stays empty."""
    import db
    import goal_runner
    import orchestrator

    goal_id = db.create_goal(user_input="Research construction costs", source="cli")
    # Deliberately do NOT call db.set_goal_plan — simulates all calls failing.

    orch_calls: list[dict] = []

    def _fake_orch(*, goal_id, ctx, system_notes=None, **kw):
        notes = list(system_notes or [])
        orch_calls.append({"system_notes": notes})
        # Orchestrator never creates a plan — just returns a hallucinated reply.
        return {
            "reply": "Here are the costs...",
            "rounds": 1,
            "tools_used": [],
            "cost_usd": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

    monkeypatch.setattr(orchestrator, "run_orchestrator", _fake_orch)
    monkeypatch.setattr(goal_runner, "_gate_max_attempts", lambda: 3)

    async def _go():
        shutdown = asyncio.Event()
        await goal_runner.run(goal_id, shutdown)

    asyncio.run(_go())

    # Orchestrator was called exactly 3 times (max_attempts).
    assert len(orch_calls) == 3, f"expected 3 attempts, got {len(orch_calls)}"

    # First call: no system notes.
    assert orch_calls[0]["system_notes"] == []

    # Subsequent calls: must carry the PLAN REQUIRED remediation note.
    for i in range(1, 3):
        notes = orch_calls[i]["system_notes"]
        assert len(notes) >= 1, f"attempt {i+1} should have system_notes"
        assert "PLAN REQUIRED" in notes[0], (
            f"remediation note should contain PLAN REQUIRED, got: {notes[0][:100]}"
        )

    # Goal must be FAILED (not done).
    g = db.get_goal(goal_id)
    assert g["status"] == "failed", (
        f"goal should be failed after empty-plan exhaustion; got {g['status']!r}"
    )
    assert "no_plan_created" in (g.get("error") or ""), (
        f"error should mention no_plan_created; got {g.get('error')!r}"
    )


def test_empty_plan_with_goal_level_done_conditions_still_runs_gate(
    qwe_temp_data_dir, monkeypatch,
):
    """When the plan is empty BUT the goal has goal-level done_conditions,
    the runner should NOT trigger the empty-plan guard — it should run
    the goal-level validators instead (they'll likely fail, which is the
    correct behaviour)."""
    import db
    import goal_runner
    import goal_validators
    import orchestrator

    goal_id = db.create_goal(
        user_input="x",
        source="cli",
        done_conditions=[{
            "kind": "files_exist",
            "spec": {"paths": ["/nonexistent/file.txt"]},
        }],
    )
    # No plan set — but goal-level done_conditions exist.

    orch_calls: list[dict] = []

    def _fake_orch(*, goal_id, ctx, system_notes=None, **kw):
        orch_calls.append({"system_notes": list(system_notes or [])})
        return {
            "reply": "done",
            "rounds": 1,
            "tools_used": [],
            "cost_usd": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

    monkeypatch.setattr(orchestrator, "run_orchestrator", _fake_orch)
    monkeypatch.setattr(goal_runner, "_gate_max_attempts", lambda: 2)

    # The goal-level validator will always fail (file doesn't exist).
    # We DON'T monkeypatch run_validator — let the real one run.

    async def _go():
        shutdown = asyncio.Event()
        await goal_runner.run(goal_id, shutdown)

    asyncio.run(_go())

    # Goal should fail due to acceptance_gate_exhausted (goal-level cond
    # never passes), NOT no_plan_created.
    g = db.get_goal(goal_id)
    assert g["status"] == "failed"
    assert "acceptance_gate_exhausted" in (g.get("error") or ""), (
        f"should be gate exhaustion, not empty-plan; got {g.get('error')!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Fix 4: skipped subtasks must not block the acceptance gate
# ─────────────────────────────────────────────────────────────────────────────


def test_skipped_subtask_does_not_block_gate(qwe_temp_data_dir, monkeypatch):
    """Orchestrator marks a subtask 'skipped' (e.g. target exceeded early).
    The gate must ignore its unfulfilled done_condition — otherwise a
    goal that completed its objective is rejected because a redundant
    batch wasn't executed (observed: g_f625be68c733482c, 106 invites
    sent but gate failed on the skipped batch 6)."""
    import db
    import goal_runner
    import orchestrator

    goal_id = db.create_goal(user_input="send 100 invites", source="cli")
    # Create plan with 3 subtasks: 2 completed, 1 skipped.
    db.set_goal_plan(goal_id, [
        {
            "title": "Batch 1",
            "description": "Send 50 invites",
            "done_condition": {
                "kind": "shell_returns_zero",
                "spec": {"cmd": "true"},
            },
        },
        {
            "title": "Batch 2",
            "description": "Send 60 invites",
            "done_condition": {
                "kind": "shell_returns_zero",
                "spec": {"cmd": "true"},
            },
        },
        {
            "title": "Batch 3 (redundant)",
            "description": "Not needed — target exceeded",
            "done_condition": {
                "kind": "shell_returns_zero",
                "spec": {"cmd": "false"},  # would FAIL if checked
            },
        },
    ])
    db.update_subtask(goal_id, "st_1", status="completed",
                      result_summary="50 sent")
    db.update_subtask(goal_id, "st_2", status="completed",
                      result_summary="60 sent, target exceeded")
    db.update_subtask(goal_id, "st_3", status="skipped",
                      result_summary="Not needed")

    call_count = 0

    def _fake_orch(*, goal_id, ctx, system_notes=None, **kw):
        nonlocal call_count
        call_count += 1
        return {
            "reply": "All done, 110 invites sent.",
            "rounds": 1,
            "tools_used": [],
            "cost_usd": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

    monkeypatch.setattr(orchestrator, "run_orchestrator", _fake_orch)

    async def _go():
        shutdown = asyncio.Event()
        await goal_runner.run(goal_id, shutdown)

    asyncio.run(_go())

    g = db.get_goal(goal_id)
    assert g["status"] == "done", (
        f"goal should be done (skipped subtask must not block gate); "
        f"got {g['status']!r}, error={g.get('error')!r}"
    )
    # Orchestrator only called once — gate passes on first attempt.
    assert call_count == 1

"""Goal-level done_conditions: validators on the GOAL itself, independent
of per-subtask criteria.

Motivation: g_5c4e6e3dc90c4f47 closed as done with all 11 subtasks validated,
but the orchestrator capitulated on the final synthesis report (the user's
actual deliverable). Per-subtask gates protect what's IN the plan;
goal-level gates protect the user's REQUEST regardless of how the
orchestrator chose to plan.

Tests cover: DB API (create/get/set/validate), goal_runner integration
(gate-loop runs goal-level criteria, blocks mark_done on failure,
re-enters orchestrator with remediation), and exhaustion behavior.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import db


# ── DB layer ────────────────────────────────────────────────────────────────


def test_create_goal_stores_done_conditions(qwe_temp_data_dir):
    """A goal created with done_conditions has them persisted on the row."""
    gid = db.create_goal(
        user_input="Build a report",
        source="cli",
        done_conditions=[
            {"kind": "files_exist", "spec": {"paths": ["report.md"]}},
            {"kind": "min_count", "spec": {"glob": "docs/*.md", "min": 5}},
        ],
    )
    g = db.get_goal(gid)
    assert g is not None
    assert len(g["done_conditions"]) == 2
    assert g["done_conditions"][0]["kind"] == "files_exist"
    assert g["done_conditions"][1]["spec"]["min"] == 5

    # Helper API
    same = db.get_goal_done_conditions(gid)
    assert same == g["done_conditions"]


def test_create_goal_without_done_conditions_empty_list(qwe_temp_data_dir):
    """Goals without done_conditions get an empty list, not None.
    UI / clients can iterate safely without isinstance checks."""
    gid = db.create_goal(user_input="x", source="cli")
    g = db.get_goal(gid)
    assert g["done_conditions"] == []
    assert db.get_goal_done_conditions(gid) == []


def test_create_goal_rejects_malformed_done_condition(qwe_temp_data_dir):
    """A criterion that fails validate_criterion is rejected at insertion
    — the goal never lands on disk."""
    import pytest

    with pytest.raises(ValueError, match=r"goal-level done_condition\[0\]"):
        db.create_goal(
            user_input="x",
            source="cli",
            done_conditions=[
                {"kind": "not_a_real_kind", "spec": {}},
            ],
        )

    # And nothing landed.
    assert db.list_goals(limit=10) == []


def test_set_goal_done_conditions_replaces_existing(qwe_temp_data_dir):
    """Setting new criteria replaces the old set entirely (idempotent)."""
    gid = db.create_goal(
        user_input="x",
        source="cli",
        done_conditions=[{"kind": "files_exist", "spec": {"paths": ["a.md"]}}],
    )
    assert len(db.get_goal_done_conditions(gid)) == 1

    db.set_goal_done_conditions(gid, [
        {"kind": "min_count", "spec": {"glob": "*.md", "min": 1}},
        {"kind": "shell_returns_zero", "spec": {"cmd": "true"}},
    ])
    new = db.get_goal_done_conditions(gid)
    assert len(new) == 2
    assert new[0]["kind"] == "min_count"
    assert new[1]["kind"] == "shell_returns_zero"


def test_set_goal_done_conditions_atomic_on_bad_criterion(qwe_temp_data_dir):
    """If ANY criterion is malformed, the whole set replacement aborts.
    Don't half-apply — the LLM caller needs an all-or-nothing contract."""
    import pytest

    gid = db.create_goal(
        user_input="x",
        source="cli",
        done_conditions=[{"kind": "files_exist", "spec": {"paths": ["a.md"]}}],
    )
    with pytest.raises(ValueError, match=r"done_condition\[1\]"):
        db.set_goal_done_conditions(gid, [
            {"kind": "files_exist", "spec": {"paths": ["b.md"]}},  # valid
            {"kind": "made_up_kind", "spec": {}},                  # invalid
        ])
    # Original criterion still in place.
    survives = db.get_goal_done_conditions(gid)
    assert len(survives) == 1
    assert survives[0]["spec"]["paths"] == ["a.md"]


def test_set_goal_done_conditions_clears_with_empty_list(qwe_temp_data_dir):
    """Passing an empty list clears all goal-level criteria."""
    gid = db.create_goal(
        user_input="x",
        source="cli",
        done_conditions=[{"kind": "files_exist", "spec": {"paths": ["a.md"]}}],
    )
    db.set_goal_done_conditions(gid, [])
    assert db.get_goal_done_conditions(gid) == []


# ── goal_runner gate integration ────────────────────────────────────────────


def _stub_db_for_runner(monkeypatch, goal_row: dict, plan: dict):
    """Stub the minimum db API surface goal_runner.run touches."""
    monkeypatch.setattr(db, "get_goal", lambda gid: goal_row)
    monkeypatch.setattr(db, "load_latest_checkpoint", lambda gid: None)
    monkeypatch.setattr(db, "log_goal_event", lambda *a, **kw: None)
    monkeypatch.setattr(db, "mark_goal_paused", lambda *a, **kw: None)

    done_calls = []
    failed_calls = []
    monkeypatch.setattr(db, "mark_goal_done",
                        lambda gid, *, result: done_calls.append({"gid": gid, "result": result}))
    monkeypatch.setattr(db, "mark_goal_failed",
                        lambda gid, *, error: failed_calls.append({"gid": gid, "error": error}))

    monkeypatch.setattr(db, "get_goal_plan", lambda gid: plan)
    monkeypatch.setattr(db, "update_subtask", lambda *a, **kw: None)
    monkeypatch.setattr(db, "auto_attach_workspace_outputs", lambda gid: [])
    return done_calls, failed_calls


def test_gate_blocks_mark_done_when_goal_level_criterion_fails(
    qwe_temp_data_dir, monkeypatch, tmp_path,
):
    """Even with all subtasks validated, a failing goal-level criterion
    keeps the goal in flight: re-enter orchestrator with remediation,
    don't call mark_goal_done.

    Mocked orchestrator runs once and "completes" — but the goal-level
    files_exist criterion points at a file that does not exist. Gate
    must NOT call mark_goal_done; instead it builds a system_note and
    re-enters. Then we make the file appear on attempt 2 → gate passes →
    mark_done fires.
    """
    import goal_runner
    import orchestrator

    missing = tmp_path / "synthesis.md"
    goal_id = "g_block_test"
    plan = {
        "subtasks": [
            {
                "id": "st_1",
                "title": "data",
                "status": "completed",
                "validation_passed": True,
                "done_condition": {"kind": "shell_returns_zero",
                                   "spec": {"cmd": "true"}},
            }
        ]
    }
    goal_row = {
        "id": goal_id,
        "source": "cli",
        "user_input": "x",
        "status": "running",
        "started_at": 0,
        "done_conditions": [
            {"kind": "files_exist", "spec": {"paths": [str(missing)]}},
        ],
    }
    done_calls, failed_calls = _stub_db_for_runner(monkeypatch, goal_row, plan)

    notes_received: list[list] = []

    def _fake_run_orchestrator(*, goal_id, ctx, system_notes=None, **kw):
        notes_received.append(list(system_notes or []))
        # On attempt 2 (system_notes non-empty), create the file so the
        # goal-level criterion can pass.
        if system_notes:
            missing.write_text("now I exist")
        return {"reply": "ok", "rounds": 1, "tools_used": [], "cost_usd": 0,
                "prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(orchestrator, "run_orchestrator", _fake_run_orchestrator)

    async def _go():
        await goal_runner.run(goal_id, asyncio.Event())

    asyncio.run(_go())

    # Two orchestrator passes: first attempted with no notes (file missing),
    # second got the remediation note (file then written → gate passes).
    assert len(notes_received) == 2
    assert notes_received[0] == []
    assert any("goal-level deliverables NOT met" in n
               for n in notes_received[1])
    # mark_goal_done fired (the gate eventually passed).
    assert len(done_calls) == 1
    assert len(failed_calls) == 0


def test_gate_remediation_mentions_goal_level_failure(
    qwe_temp_data_dir, monkeypatch, tmp_path,
):
    """The system_note injected to the orchestrator must:
      - Have the 'ACCEPTANCE GATE — goal-level deliverables NOT met' header
      - Include the specific remediation from the validator
      - Tell the orchestrator to re-plan (goal_plan_set) — not just retry
    """
    import goal_runner
    import orchestrator

    missing = tmp_path / "report.md"
    goal_row = {
        "id": "g_remediation",
        "source": "cli",
        "user_input": "x",
        "status": "running",
        "started_at": 0,
        "done_conditions": [
            {"kind": "files_exist", "spec": {"paths": [str(missing)]}},
        ],
    }
    plan = {"subtasks": []}
    _stub_db_for_runner(monkeypatch, goal_row, plan)
    # Tighten cap so the test ends quickly even without success.
    monkeypatch.setattr(goal_runner, "_gate_max_attempts", lambda: 2)

    captured: list[list] = []

    def _fake_run_orchestrator(*, goal_id, ctx, system_notes=None, **kw):
        captured.append(list(system_notes or []))
        return {"reply": "x", "rounds": 1, "tools_used": [], "cost_usd": 0,
                "prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(orchestrator, "run_orchestrator", _fake_run_orchestrator)

    async def _go():
        await goal_runner.run("g_remediation", asyncio.Event())

    asyncio.run(_go())

    # Attempt 2 carries the remediation note.
    assert len(captured) >= 2
    rem_text = "\n".join(captured[1])
    assert "ACCEPTANCE GATE — goal-level deliverables NOT met" in rem_text
    assert "MANDATORY" in rem_text
    assert "goal_plan_set" in rem_text  # Tells orchestrator to re-plan, not just retry
    assert str(missing) in rem_text or "report.md" in rem_text


def test_gate_exhausts_on_repeated_goal_level_failure(
    qwe_temp_data_dir, monkeypatch, tmp_path,
):
    """If the goal-level criterion keeps failing for MAX_GATE_ATTEMPTS,
    the goal is marked failed with acceptance_gate_exhausted."""
    import goal_runner
    import orchestrator

    missing = tmp_path / "never.md"  # never created
    goal_row = {
        "id": "g_exhaust",
        "source": "cli",
        "user_input": "x",
        "status": "running",
        "started_at": 0,
        "done_conditions": [
            {"kind": "files_exist", "spec": {"paths": [str(missing)]}},
        ],
    }
    plan = {"subtasks": []}
    done_calls, failed_calls = _stub_db_for_runner(monkeypatch, goal_row, plan)
    monkeypatch.setattr(goal_runner, "_gate_max_attempts", lambda: 3)

    monkeypatch.setattr(orchestrator, "run_orchestrator",
                        lambda **kw: {"reply": "x", "rounds": 1, "tools_used": [],
                                      "cost_usd": 0, "prompt_tokens": 0,
                                      "completion_tokens": 0})

    async def _go():
        await goal_runner.run("g_exhaust", asyncio.Event())

    asyncio.run(_go())

    assert len(done_calls) == 0
    assert len(failed_calls) == 1
    assert "acceptance_gate_exhausted" in failed_calls[0]["error"]
    assert "goal-level" in failed_calls[0]["error"]


def test_gate_passes_when_goal_level_criterion_satisfied(
    qwe_temp_data_dir, monkeypatch, tmp_path,
):
    """Happy path: goal-level criterion already satisfied → gate doesn't
    intervene, mark_done fires after first orchestrator pass."""
    import goal_runner
    import orchestrator

    existing = tmp_path / "exists.md"
    existing.write_text("here from the start")

    goal_row = {
        "id": "g_happy",
        "source": "cli",
        "user_input": "x",
        "status": "running",
        "started_at": 0,
        "done_conditions": [
            {"kind": "files_exist", "spec": {"paths": [str(existing)]}},
        ],
    }
    plan = {"subtasks": []}
    done_calls, failed_calls = _stub_db_for_runner(monkeypatch, goal_row, plan)

    invocations = []
    def _fake_run(**kw):
        invocations.append(kw.get("system_notes"))
        return {"reply": "done", "rounds": 1, "tools_used": [],
                "cost_usd": 0, "prompt_tokens": 0, "completion_tokens": 0}
    monkeypatch.setattr(orchestrator, "run_orchestrator", _fake_run)

    async def _go():
        await goal_runner.run("g_happy", asyncio.Event())

    asyncio.run(_go())

    # ONE orchestrator pass, no remediation note injected.
    assert len(invocations) == 1
    assert not invocations[0]
    # mark_goal_done called with the orchestrator's reply.
    assert len(done_calls) == 1
    assert done_calls[0]["result"] == "done"
    assert len(failed_calls) == 0

"""Wall-clock + USD budget enforcement on goals.

Before this commit, goals.budget_seconds and goals.budget_usd were
PERSISTED on the goal row but never CHECKED at runtime. A goal with
budget_seconds=60 could run for hours; one with budget_usd=0.50 could
burn a $50 spend without anyone noticing.

This test verifies the per-round budget check in
orchestrator.run_orchestrator fires ctx.abort_event when either limit
is breached, and that the agent loop sees the abort and stops cleanly.
"""
from __future__ import annotations

import threading
import time

import pytest


def _stub_orch_dependencies(monkeypatch, scripted_replies=None):
    """Patch providers + run_loop so run_orchestrator runs without an LLM.

    run_loop is replaced with a stub that fires the round-complete
    callback N times before returning — that's enough to exercise the
    budget check on a real wall clock without spending tokens.
    """
    import orchestrator
    import providers

    monkeypatch.setattr(providers, "get_client", lambda: object())
    monkeypatch.setattr(providers, "get_model", lambda: "fake-model")

    rounds_fired = []

    def _fake_run_loop(**kw):
        ctx = kw.get("ctx")
        messages = kw.get("messages") or []
        # Fire the round callback every 0.05s for up to 30 iterations.
        # The budget check sets ctx.abort_event when exceeded.
        for i in range(30):
            if ctx is not None and ctx.abort_event is not None and ctx.abort_event.is_set():
                break
            if ctx is not None and ctx.on_round_complete is not None:
                ctx.on_round_complete(i + 1, list(messages))
            rounds_fired.append(i + 1)
            time.sleep(0.05)
        return {
            "reply": "stopped" if (ctx and ctx.abort_event and ctx.abort_event.is_set()) else "done",
            "tool_calls": [],
            "completion_tokens": 0,
            "prompt_tokens": 0,
            "rounds": len(rounds_fired),
            "tok_per_sec": 0.0,
        }

    monkeypatch.setattr(orchestrator, "run_loop", _fake_run_loop)
    return rounds_fired


def test_wall_clock_budget_aborts_orchestrator(qwe_temp_data_dir, monkeypatch):
    """A goal with budget_seconds=1 stops within ~1s, not after the
    fake run_loop's full 30 iterations (~1.5s)."""
    import db
    import orchestrator
    from turn_context import TurnContext

    rounds = _stub_orch_dependencies(monkeypatch)

    goal_id = db.create_goal(
        user_input="test", source="cli", budget_seconds=1,
    )
    # Simulate the worker stamping started_at.
    conn = db._get_conn()
    conn.execute(
        "UPDATE goals SET started_at=? WHERE id=?",
        (time.time(), goal_id),
    )
    conn.commit()

    ctx = TurnContext(
        source="cli", goal_id=goal_id,
        abort_event=threading.Event(),
    )
    started = time.time()
    result = orchestrator.run_orchestrator(goal_id, ctx=ctx)
    elapsed = time.time() - started

    # Aborted within ~budget+small overhead, NOT the full 1.5s the fake loop
    # would otherwise burn.
    assert elapsed < 1.5, f"expected abort within ~1s, took {elapsed:.2f}s"
    assert ctx.abort_event.is_set(), "budget check should have set abort_event"
    # Event was logged
    types = [e["event_type"] for e in db.get_goal_events(goal_id)]
    assert "budget_exceeded" in types


def test_usd_budget_aborts_orchestrator(qwe_temp_data_dir, monkeypatch):
    """A goal with budget_usd=0.001 stops when summed agent_runs.cost_usd
    crosses the cap.

    Pre-migration-015: this test wrote ``goals.cost_usd`` directly to fake
    the spend. That column was never actually populated by the runtime —
    the budget check was reading dead storage. The fix sums agent_runs
    rows tied to the goal via the new ``goal_id`` column, so the test
    now inserts a real agent_run with cost_usd > budget.
    """
    import db
    import orchestrator
    from turn_context import TurnContext

    _stub_orch_dependencies(monkeypatch)

    goal_id = db.create_goal(
        user_input="test", source="cli", budget_usd=0.001,
    )
    conn = db._get_conn()
    conn.execute(
        "UPDATE goals SET started_at=? WHERE id=?",
        (time.time(), goal_id),
    )
    conn.commit()
    # Insert a priced agent_run tagged with the goal_id — this is what
    # subagent + orchestrator rounds do in production. The budget check
    # sums these, not the (always-zero) goals.cost_usd column.
    run_id = db.insert_agent_run(
        thread_id="t_dummy", source="orchestrator", started_at=time.time(),
        goal_id=goal_id,
    )
    db.finalize_agent_run(
        run_id, finished_at=time.time(), duration_ms=10,
        status="ok", cost_usd=0.01,
    )

    ctx = TurnContext(
        source="cli", goal_id=goal_id,
        abort_event=threading.Event(),
    )
    orchestrator.run_orchestrator(goal_id, ctx=ctx)

    # summed cost (0.01) >> budget (0.001) so first round-callback aborts
    assert ctx.abort_event.is_set()
    types = [e["event_type"] for e in db.get_goal_events(goal_id)]
    assert "budget_exceeded" in types


def test_no_budget_means_no_abort(qwe_temp_data_dir, monkeypatch):
    """A goal with neither budget set runs the fake loop to completion
    without the budget check ever firing abort."""
    import db
    import orchestrator
    from turn_context import TurnContext

    _stub_orch_dependencies(monkeypatch)

    goal_id = db.create_goal(user_input="test", source="cli")  # no budgets
    conn = db._get_conn()
    conn.execute(
        "UPDATE goals SET started_at=? WHERE id=?",
        (time.time(), goal_id),
    )
    conn.commit()

    ctx = TurnContext(
        source="cli", goal_id=goal_id,
        abort_event=threading.Event(),
    )
    orchestrator.run_orchestrator(goal_id, ctx=ctx)

    assert not ctx.abort_event.is_set(), "no budget = no abort"
    types = [e["event_type"] for e in db.get_goal_events(goal_id)]
    assert "budget_exceeded" not in types


def test_budget_check_preserves_user_callback(qwe_temp_data_dir, monkeypatch):
    """The budget wrapper must still call any pre-existing on_round_complete
    callback (the goal_runner's checkpoint saver, normally)."""
    import db
    import orchestrator
    from turn_context import TurnContext

    _stub_orch_dependencies(monkeypatch)

    goal_id = db.create_goal(
        user_input="test", source="cli", budget_seconds=60,
    )
    conn = db._get_conn()
    conn.execute(
        "UPDATE goals SET started_at=? WHERE id=?",
        (time.time(), goal_id),
    )
    conn.commit()

    user_cb_calls = []

    def _user_cb(round_num, messages):
        user_cb_calls.append(round_num)

    ctx = TurnContext(
        source="cli", goal_id=goal_id,
        abort_event=threading.Event(),
        on_round_complete=_user_cb,
    )
    orchestrator.run_orchestrator(goal_id, ctx=ctx)

    # User callback got every round; budget didn't fire (60s >> ~1.5s run)
    assert len(user_cb_calls) > 0
    assert not ctx.abort_event.is_set()

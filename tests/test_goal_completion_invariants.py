"""Plan/goal completion invariants — fixes from live LinkedIn lead-gen run.

Live run exposed two bugs:

1. Orchestrator dispatched ``dispatch_subagent(subtask_id="st_2b")`` —
   `st_2b` doesn't exist in the plan. `update_subtask` silently returned
   None, the dispatch still ran, and the plan's `st_2.attempts` froze at
   the wrong value forever. UI looked stuck while the agent was actually
   busy under a fabricated ID.

2. Orchestrator wrote a perfectly good final summary (with 20 real lead
   profiles) but didn't call `subtask_update` to close `st_2`/`st_3`/
   `st_4` first. `goal_runner.run()` then called `mark_goal_done` →
   goal status flipped to DONE while plan still showed 3 subtasks
   pending. Inconsistent state visible in the UI.
"""
from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────────────
#  dispatch_subagent rejects fabricated subtask IDs
# ─────────────────────────────────────────────────────────────────────────────


def test_dispatch_subagent_rejects_unknown_subtask_id(qwe_temp_data_dir):
    """A dispatch with subtask_id NOT in the plan returns a clear error
    string listing the valid IDs — instead of silently running and leaving
    the plan inconsistent."""
    import db
    import tools
    from turn_context import TurnContext

    goal_id = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(goal_id, [
        {"title": "A", "description": ""},
        {"title": "B", "description": ""},
    ])
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=goal_id))

    result = tools.execute("dispatch_subagent", {
        "type": "browser",
        "prompt": "go",
        "subtask_id": "st_2b",  # hallucinated — only st_1 + st_2 exist
    })
    assert "Error" in result
    assert "st_2b" in result
    # Tells the orchestrator what IDs are actually valid
    assert "st_1" in result and "st_2" in result
    # And how to fix it
    assert "goal_plan_set" in result


def test_dispatch_subagent_accepts_valid_subtask_id(qwe_temp_data_dir, monkeypatch):
    """Sanity: a valid subtask_id flows through normally."""
    import db
    import subagent
    import tools
    from turn_context import TurnContext

    goal_id = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(goal_id, [
        {"title": "A", "description": ""},
        {"title": "B", "description": ""},
    ])
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=goal_id))

    captured: dict = {}

    def _fake_run(**kw):
        captured.update(kw)
        return "subagent done"

    monkeypatch.setattr(subagent, "run_subagent", _fake_run)

    result = tools.execute("dispatch_subagent", {
        "type": "browser",
        "prompt": "go",
        "subtask_id": "st_1",
    })
    assert result == "subagent done"
    assert captured["subtask_id"] == "st_1"


def test_dispatch_subagent_without_plan_still_works(qwe_temp_data_dir, monkeypatch):
    """A goal with no plan yet (orchestrator dispatching before goal_plan_set?)
    must not crash on the validation lookup."""
    import db
    import subagent
    import tools
    from turn_context import TurnContext

    goal_id = db.create_goal(user_input="x", source="cli")
    # No db.set_goal_plan() — plan is None
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=goal_id))

    monkeypatch.setattr(subagent, "run_subagent",
                        lambda **kw: "ok")

    result = tools.execute("dispatch_subagent", {
        "type": "research",
        "prompt": "x",
        "subtask_id": "anything",
    })
    # No validation when there's no plan → dispatch succeeds
    assert result == "ok"


# ─────────────────────────────────────────────────────────────────────────────
#  goal_runner: no auto-skip backstop (replaced by acceptance gate 2026-05-16)
# ─────────────────────────────────────────────────────────────────────────────
#
# Pre-2026-05-16, goal_runner had an "auto-skip backstop": if the orchestrator
# returned a final reply but left subtasks pending/in_progress, the runner
# silently flipped them to ``skipped`` so the plan looked tidy. That was the
# antipattern that masked the LinkedIn lead-gen failure — broken work got
# papered over and the goal was marked done.
#
# The new architecture replaces the backstop with an acceptance gate (run
# every subtask's ``done_condition`` validator, re-enter the orchestrator
# with remediation notes on failure, up to N attempts). The tests below
# now verify the NEW contract: leftover subtasks without done_conditions
# are passed through (the gate is defensive — it skips conditionless
# subtasks rather than blocking) and statuses are preserved verbatim.
# Full gate behaviour is exercised in tests/test_acceptance_gate.py.


def test_goal_runner_preserves_subtask_statuses_no_autoskip(qwe_temp_data_dir):
    """The old auto-skip backstop is GONE. When the orchestrator returns a
    final reply, the runner no longer mutates pending/in_progress subtasks
    into ``skipped``. Their original statuses are preserved as-is — if the
    plan has machine-checkable done_conditions, the acceptance gate handles
    them; if not (legacy plans built without done_conditions), the runner
    trusts the orchestrator's say-so and marks the goal done."""
    import asyncio
    import db
    import goal_runner
    import orchestrator

    goal_id = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(goal_id, [
        {"title": "A", "description": ""},
        {"title": "B", "description": ""},
        {"title": "C", "description": ""},
    ])
    # Mark st_1 done, leave st_2 in_progress, st_3 pending — exactly what
    # the LinkedIn run looked like at completion.
    db.update_subtask(goal_id, "st_1", status="completed",
                      result_summary="A done")
    db.update_subtask(goal_id, "st_2", status="in_progress",
                      result_summary="working on B")

    def _fake_orch(**kw):
        return {
            "reply": "Final summary: did A, partial B, C not attempted.",
            "rounds": 5, "tools_used": [], "cost_usd": 0.0,
            "prompt_tokens": 0, "completion_tokens": 0,
        }
    import unittest.mock
    with unittest.mock.patch.object(orchestrator, "run_orchestrator",
                                     side_effect=_fake_orch):
        async def _go():
            shutdown = asyncio.Event()
            await goal_runner.run(goal_id, shutdown)
        asyncio.run(_go())

    # Goal is done with the reply
    g = db.get_goal(goal_id)
    assert g["status"] == "done"
    assert "Final summary" in g["result"]

    # Statuses are preserved verbatim — no auto-skip mutations.
    plan = db.get_goal_plan(goal_id)
    statuses = {st["id"]: st["status"] for st in plan["subtasks"]}
    assert statuses == {
        "st_1": "completed",
        "st_2": "in_progress",
        "st_3": "pending",
    }


def test_goal_runner_preserves_terminal_subtask_summaries(qwe_temp_data_dir):
    """Already-terminal subtasks (completed/failed/skipped) keep their
    result_summary exactly as the orchestrator wrote it — the runner never
    mutates them."""
    import asyncio
    import db
    import goal_runner
    import orchestrator

    goal_id = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(goal_id, [
        {"title": "A", "description": ""},
        {"title": "B", "description": ""},
        {"title": "C", "description": ""},
    ])
    db.update_subtask(goal_id, "st_1", status="completed",
                      result_summary="A explicitly done")
    db.update_subtask(goal_id, "st_2", status="failed",
                      result_summary="B blocked by captcha")
    # st_3 stays pending

    def _fake_orch(**kw):
        return {"reply": "done", "rounds": 1, "tools_used": [],
                "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0}
    import unittest.mock
    with unittest.mock.patch.object(orchestrator, "run_orchestrator",
                                     side_effect=_fake_orch):
        async def _go():
            shutdown = asyncio.Event()
            await goal_runner.run(goal_id, shutdown)
        asyncio.run(_go())

    plan = db.get_goal_plan(goal_id)
    by_id = {st["id"]: st for st in plan["subtasks"]}
    # st_1 stays completed with its original summary
    assert by_id["st_1"]["status"] == "completed"
    assert by_id["st_1"]["result_summary"] == "A explicitly done"
    # st_2 stays failed with its original summary
    assert by_id["st_2"]["status"] == "failed"
    assert by_id["st_2"]["result_summary"] == "B blocked by captcha"
    # st_3 is left alone (no auto-skip)
    assert by_id["st_3"]["status"] == "pending"


def test_goal_runner_complete_plan_no_mutations(qwe_temp_data_dir):
    """When the plan is already complete (orchestrator marked every subtask),
    the runner is a no-op on the plan — no spurious mutations."""
    import asyncio
    import db
    import goal_runner
    import orchestrator

    goal_id = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(goal_id, [{"title": "A", "description": ""}])
    db.update_subtask(goal_id, "st_1", status="completed",
                      result_summary="A done")

    def _fake_orch(**kw):
        return {"reply": "all done", "rounds": 1, "tools_used": [],
                "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0}
    import unittest.mock
    with unittest.mock.patch.object(orchestrator, "run_orchestrator",
                                     side_effect=_fake_orch):
        async def _go():
            shutdown = asyncio.Event()
            await goal_runner.run(goal_id, shutdown)
        asyncio.run(_go())

    plan = db.get_goal_plan(goal_id)
    # No mutations — original result_summary intact
    assert plan["subtasks"][0]["result_summary"] == "A done"

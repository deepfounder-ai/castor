"""Anti-pattern coach over agent_runs + goals.

PoC inspired by Microsoft's `AI Engineer Coach`_ — local-first analysis
of recent session data, no LLM call, no data leaves the machine.

Each test seeds a controlled set of rows into ``agent_runs`` / ``goals``,
runs the rule, asserts the headline. Rules MUST never raise — broken
rules are skipped with a log line (test pins that contract too).

.. _AI Engineer Coach: https://github.com/microsoft/AI-Engineering-Coach
"""
from __future__ import annotations

import json
import time

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — seed fake agent_runs / goals into the sandbox DB
# ─────────────────────────────────────────────────────────────────────────────


def _seed_run(*, thread_id="t_demo", source="web", duration_ms=1000,
              cost_usd=0.01, input_tokens=1000, output_tokens=100,
              goal_id=None, cron_id=None, started_at=None,
              result_preview=None):
    """Insert a finalized agent_run row at the given timestamp."""
    import db
    rid = db.insert_agent_run(
        thread_id=thread_id, source=source,
        started_at=started_at if started_at is not None else time.time(),
        status="running", goal_id=goal_id, cron_id=cron_id,
    )
    db.finalize_agent_run(
        rid,
        finished_at=time.time(),
        duration_ms=duration_ms,
        status="ok",
        result_preview=result_preview,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )
    return rid


def _seed_goal(*, status="done", plan_subtasks=None, done_conditions=None):
    """Insert a goals row, optionally with a plan + done_conditions JSON."""
    import db
    goal_id = db.create_goal(user_input="t", source="cli")
    plan = {"subtasks": plan_subtasks or []}
    conn = db._get_conn()
    conn.execute(
        "UPDATE goals SET status=?, plan=?, done_conditions=?, started_at=? WHERE id=?",
        (status, json.dumps(plan),
         json.dumps(done_conditions) if done_conditions else None,
         time.time() - 3600, goal_id),
    )
    conn.commit()
    return goal_id


# ─────────────────────────────────────────────────────────────────────────────
# Per-rule unit tests
# ─────────────────────────────────────────────────────────────────────────────


def test_no_findings_returns_empty_report(qwe_temp_data_dir):
    import coach
    out = coach.run_pass(lookback_days=7)
    assert "No anti-patterns detected" in out


def test_rule_mega_session_fires_above_threshold(qwe_temp_data_dir):
    import coach
    # Two runs in window — one mega (45min), one normal
    _seed_run(duration_ms=45 * 60 * 1000)
    _seed_run(duration_ms=2 * 60 * 1000)
    out = coach.run_pass(lookback_days=7)
    assert "mega_session" in out
    # Headline mentions the count + max
    assert "1 session" in out
    assert "45min" in out


def test_rule_mega_session_excludes_subagents(qwe_temp_data_dir):
    """Subagent runs routinely take 5-15min; they should NOT trigger
    the mega-session rule — only orchestrator / web / cli / etc."""
    import coach
    _seed_run(source="subagent_browser", duration_ms=60 * 60 * 1000)  # 60min subagent
    out = coach.run_pass(lookback_days=7)
    assert "mega_session" not in out


def test_rule_cost_outlier_fires(qwe_temp_data_dir):
    import coach
    _seed_run(cost_usd=5.50, goal_id="g_demo")
    _seed_run(cost_usd=0.10)
    out = coach.run_pass(lookback_days=7)
    assert "cost_outlier" in out
    assert "5.50" in out  # the headline call-out


def test_rule_capitulating_goals_fires_on_failed_subtask(qwe_temp_data_dir):
    """Goal marked done but plan has a failed subtask → capitulation."""
    import coach
    _seed_goal(
        status="done",
        plan_subtasks=[
            {"id": "st_1", "status": "completed"},
            {"id": "st_2", "status": "failed"},
        ],
    )
    out = coach.run_pass(lookback_days=7)
    assert "capitulating_goals" in out


def test_rule_capitulating_goals_fires_on_empty_plan(qwe_temp_data_dir):
    """Goal marked done with neither subtasks nor goal-level
    done_conditions → no acceptance check happened."""
    import coach
    _seed_goal(status="done", plan_subtasks=[], done_conditions=None)
    out = coach.run_pass(lookback_days=7)
    assert "capitulating_goals" in out


def test_rule_capitulating_goals_quiet_when_clean(qwe_temp_data_dir):
    """Goal done with all-completed subtasks and goal-level criteria
    is NOT a capitulation."""
    import coach
    _seed_goal(
        status="done",
        plan_subtasks=[
            {"id": "st_1", "status": "completed"},
            {"id": "st_2", "status": "completed"},
        ],
        done_conditions=[{"kind": "files_exist", "spec": {"paths": ["x"]}}],
    )
    out = coach.run_pass(lookback_days=7)
    assert "capitulating_goals" not in out


def test_rule_shell_heavy_fires_on_high_ratio(qwe_temp_data_dir):
    """Token ratio >50:1 in/out for 3+ runs flags shell-poking pattern."""
    import coach
    for _ in range(4):
        _seed_run(input_tokens=200_000, output_tokens=2_000, cost_usd=0.20)
    out = coach.run_pass(lookback_days=7)
    assert "shell_heavy" in out


def test_rule_shell_heavy_quiet_on_balanced_runs(qwe_temp_data_dir):
    """Normal runs (in/out ratio ~5:1) should NOT trip the heuristic."""
    import coach
    for _ in range(10):
        _seed_run(input_tokens=5_000, output_tokens=1_000, cost_usd=0.01)
    out = coach.run_pass(lookback_days=7)
    assert "shell_heavy" not in out


def test_rule_synthesis_overspend_fires_when_cron_burned_money(
    qwe_temp_data_dir
):
    """If any of the system synthesis crons have nonzero cost in the
    window above the per-day budget → regression of v0.23.2 fix."""
    import db
    import coach
    # Register a fake __synthesis__ cron row
    cron_id = db.execute(
        "INSERT INTO scheduled_tasks (name, task, schedule, next_run, repeat, enabled) "
        "VALUES (?,?,?,?,1,1)",
        ("__synthesis__", "__synthesis__", "daily", time.time()),
    )
    # Wire two priced runs against it
    _seed_run(cron_id=cron_id, cost_usd=1.50, source="routine")
    _seed_run(cron_id=cron_id, cost_usd=0.40, source="routine")
    out = coach.run_pass(lookback_days=1)
    assert "synthesis_overspend" in out


def test_rule_no_skills_fires_when_threshold_met_and_zero_hits(
    qwe_temp_data_dir
):
    """Lots of web/cli/telegram activity, zero skill-tool usage."""
    import coach
    for _ in range(35):
        # result_preview doesn't reference any skill tool
        _seed_run(source="web", result_preview="ok")
    out = coach.run_pass(lookback_days=7)
    assert "no_skills_used" in out


def test_rule_no_skills_quiet_when_skill_used(qwe_temp_data_dir):
    """Same activity but with a skill-tool marker in any run → quiet."""
    import coach
    for i in range(35):
        preview = "ok" if i > 0 else "called tool_search('schedule')"
        _seed_run(source="web", result_preview=preview)
    out = coach.run_pass(lookback_days=7)
    assert "no_skills_used" not in out


# ─────────────────────────────────────────────────────────────────────────────
# Report assembly + safety net
# ─────────────────────────────────────────────────────────────────────────────


def test_findings_sorted_by_severity_descending(qwe_temp_data_dir):
    """Multiple findings — highest-severity first in the report."""
    import coach
    # Seed for capitulating_goals (sev=80) + mega_session (sev=60)
    _seed_goal(
        status="done",
        plan_subtasks=[{"id": "st_1", "status": "failed"}],
    )
    _seed_run(duration_ms=45 * 60 * 1000)
    out = coach.run_pass(lookback_days=7)
    cap_idx = out.find("capitulating_goals")
    mega_idx = out.find("mega_session")
    assert 0 <= cap_idx < mega_idx, "higher-severity finding must come first"


def test_report_persisted_to_memory(qwe_temp_data_dir, monkeypatch):
    """The report is saved with tag='coach' so recall surfaces it."""
    import coach
    saved: list = []
    import memory
    monkeypatch.setattr(memory, "save", lambda text, **kw: saved.append((text, kw)))
    coach.run_pass(lookback_days=7)
    assert saved, "memory.save was not called"
    text, kw = saved[0]
    assert kw.get("tag") == "coach"
    assert "Castor coach" in text


def test_report_archived_to_disk(qwe_temp_data_dir):
    """An archive markdown is written under DATA_DIR/uploads/."""
    import coach
    import config
    coach.run_pass(lookback_days=7)
    archive_files = list((config.DATA_DIR / "uploads").glob("coach-*.md"))
    assert archive_files, "no archive file was written"


def test_run_pass_never_raises_on_broken_rule(qwe_temp_data_dir, monkeypatch):
    """If any individual rule crashes, the rest still run and the
    report still assembles. Required by the module-level contract."""
    import coach

    def _broken(_):
        raise RuntimeError("boom")

    # Swap one rule for the broken one — leave the others.
    real_rules = list(coach._RULES)
    new_rules = (_broken,) + tuple(real_rules[1:])
    monkeypatch.setattr(coach, "_RULES", new_rules)
    # Seed something so a non-broken rule has data to report
    _seed_goal(status="done", plan_subtasks=[{"id": "st_1", "status": "failed"}])
    out = coach.run_pass(lookback_days=7)
    # No exception, and the surviving rule still produced a finding
    assert "capitulating_goals" in out


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler wire-up (fast-path dispatch + system task list)
# ─────────────────────────────────────────────────────────────────────────────


def test_scheduler_is_routine_false_for_coach_task(qwe_temp_data_dir):
    """Coach must NOT route through ``agent.run`` — same fast-path
    requirement as ``__synthesis_continuous__`` (v0.23.2)."""
    import scheduler
    assert scheduler._is_routine(scheduler.COACH_TASK_NAME) is False


def test_scheduler_execute_task_dispatches_coach(qwe_temp_data_dir,
                                                  monkeypatch):
    """``__coach_daily__`` goes through the fast ``_execute_task`` path
    straight to ``coach.run_pass``."""
    import scheduler
    import coach
    monkeypatch.setattr(coach, "run_pass",
                        lambda **kw: "coach output")
    result = scheduler._execute_task(scheduler.COACH_TASK_NAME)
    assert result == "coach output"


def test_register_coach_skips_when_disabled(qwe_temp_data_dir):
    """Default config (coach_enabled=0) → no row inserted."""
    import db
    import scheduler
    scheduler._register_coach()
    row = db.fetchone(
        "SELECT id FROM scheduled_tasks WHERE name=?",
        (scheduler.COACH_TASK_NAME,),
    )
    assert row is None


def test_register_coach_inserts_when_enabled(qwe_temp_data_dir):
    """``coach_enabled=1`` → row created with the documented schedule."""
    import config
    import db
    import scheduler
    config.set("coach_enabled", 1)
    scheduler._register_coach()
    row = db.fetchone(
        "SELECT id, schedule FROM scheduled_tasks WHERE name=?",
        (scheduler.COACH_TASK_NAME,),
    )
    assert row is not None
    assert row[1] == "daily 09:00"

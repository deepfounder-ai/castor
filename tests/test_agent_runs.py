"""Unit tests for db.py helpers added in v0.19.0 cost-tracking work."""
import time
import pytest


def test_insert_agent_run_returns_id(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(
        thread_id="t1", source="web", started_at=time.time(),
        status="running", model="gpt-4o-mini", provider="openai",
    )
    assert isinstance(rid, int) and rid > 0


def test_insert_agent_run_row_visible(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                              started_at=1000.0, status="running")
    row = db._get_conn().execute(
        "SELECT thread_id, source, started_at, status FROM agent_runs WHERE id=?",
        (rid,)).fetchone()
    assert row == ("t1", "web", 1000.0, "running")


def test_finalize_agent_run_updates_metrics(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                              started_at=1000.0, status="running")
    db.finalize_agent_run(rid, finished_at=1001.5, duration_ms=1500,
                          status="ok", result_preview="reply",
                          input_tokens=100, output_tokens=50, cost_usd=0.001)
    row = db._get_conn().execute(
        "SELECT finished_at, duration_ms, status, input_tokens, output_tokens, cost_usd "
        "FROM agent_runs WHERE id=?", (rid,)).fetchone()
    assert row == (1001.5, 1500, "ok", 100, 50, 0.001)


def test_finalize_handles_null_finished_at(qwe_temp_data_dir):
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web",
                              started_at=1000.0, status="running")
    db.finalize_agent_run(rid, finished_at=None, duration_ms=None,
                          status="aborted", input_tokens=80, output_tokens=20)
    row = db._get_conn().execute(
        "SELECT finished_at, duration_ms, status FROM agent_runs WHERE id=?",
        (rid,)).fetchone()
    assert row == (None, None, "aborted")


def test_insert_skipped_run_writes_zero_tokens(qwe_temp_data_dir):
    import db
    rid = db.insert_skipped_run(cron_id=5, thread_id="t1",
                                scheduled_at=1000.0, reason="missed")
    row = db._get_conn().execute(
        "SELECT status, started_at, input_tokens, output_tokens "
        "FROM agent_runs WHERE id=?", (rid,)).fetchone()
    assert row == ("missed", 1000.0, 0, 0)


def test_get_thread_totals_sums_correctly(qwe_temp_data_dir):
    import db
    for (i, o, c) in [(100, 50, 0.01), (200, 80, 0.02), (50, 30, None)]:
        rid = db.insert_agent_run(thread_id="t1", source="web",
                                  started_at=time.time(), status="running")
        db.finalize_agent_run(rid, finished_at=time.time(), duration_ms=100,
                              status="ok", input_tokens=i, output_tokens=o,
                              cost_usd=c)
    totals = db.get_thread_totals("t1")
    assert totals["input_tokens"] == 350
    assert totals["output_tokens"] == 160
    # COALESCE on cost_usd treats NULL as 0 in the sum
    assert abs(totals["cost_usd"] - 0.03) < 1e-9
    assert totals["run_count"] == 3


def test_get_thread_totals_empty(qwe_temp_data_dir):
    import db
    totals = db.get_thread_totals("ghost")
    assert totals == {"input_tokens": 0, "output_tokens": 0,
                      "cost_usd": 0.0, "run_count": 0}


def test_get_runs_for_thread_ordering_and_limit(qwe_temp_data_dir):
    import db
    ids = []
    for t in [1000.0, 2000.0, 3000.0]:
        rid = db.insert_agent_run(thread_id="t1", source="web",
                                  started_at=t, status="running")
        ids.append(rid)
    rows = db.get_runs_for_thread("t1", limit=2)
    assert [r["id"] for r in rows] == [ids[2], ids[1]]


def test_get_period_totals_filters_by_source(qwe_temp_data_dir):
    import db
    for src, tok in [("web", 100), ("routine", 200), ("synthesis", 50)]:
        rid = db.insert_agent_run(thread_id="t1", source=src,
                                  started_at=1500.0, status="running")
        db.finalize_agent_run(rid, finished_at=1501.0, duration_ms=1000,
                              status="ok", input_tokens=tok, output_tokens=0)
    t_routine = db.get_period_totals(1000.0, 2000.0, source="routine")
    assert t_routine["total_input_tokens"] == 200
    t_all = db.get_period_totals(1000.0, 2000.0)
    assert t_all["total_input_tokens"] == 350
    assert "by_source" in t_all
    assert t_all["by_source"]["synthesis"]["input_tokens"] == 50


# ─────────────────────────────────────────────────────────────────────────────
# Migration 015: goal_id column on agent_runs + get_goal_total_cost helper.
# ─────────────────────────────────────────────────────────────────────────────


def test_insert_agent_run_persists_goal_id(qwe_temp_data_dir):
    """insert_agent_run accepts goal_id and writes it to the new column."""
    import db
    rid = db.insert_agent_run(
        thread_id="t1", source="orchestrator", started_at=1000.0,
        goal_id="g_test123",
    )
    row = db._get_conn().execute(
        "SELECT goal_id FROM agent_runs WHERE id=?", (rid,),
    ).fetchone()
    assert row[0] == "g_test123"


def test_insert_agent_run_goal_id_default_null(qwe_temp_data_dir):
    """Non-goal runs (CLI, scheduler, web chat) keep goal_id NULL."""
    import db
    rid = db.insert_agent_run(thread_id="t1", source="web", started_at=1000.0)
    row = db._get_conn().execute(
        "SELECT goal_id FROM agent_runs WHERE id=?", (rid,),
    ).fetchone()
    assert row[0] is None


def test_get_goal_total_cost_sums_priced_runs(qwe_temp_data_dir):
    """get_goal_total_cost sums cost_usd across all runs tagged with this goal."""
    import db
    # Two runs tagged with the goal — should sum
    r1 = db.insert_agent_run(
        thread_id="t1", source="orchestrator", started_at=1000.0,
        goal_id="g_alpha",
    )
    db.finalize_agent_run(r1, finished_at=1001.0, duration_ms=1000,
                          status="ok", cost_usd=0.40)
    r2 = db.insert_agent_run(
        thread_id="t1", source="subagent_browser", started_at=1100.0,
        goal_id="g_alpha",
    )
    db.finalize_agent_run(r2, finished_at=1101.0, duration_ms=1000,
                          status="ok", cost_usd=1.50)
    # An unrelated run on a different goal — must NOT count
    r3 = db.insert_agent_run(
        thread_id="t2", source="orchestrator", started_at=1200.0,
        goal_id="g_beta",
    )
    db.finalize_agent_run(r3, finished_at=1201.0, duration_ms=1000,
                          status="ok", cost_usd=99.0)
    # A non-goal run — must NOT count
    r4 = db.insert_agent_run(
        thread_id="t1", source="web", started_at=1300.0,
    )
    db.finalize_agent_run(r4, finished_at=1301.0, duration_ms=1000,
                          status="ok", cost_usd=2.0)

    total = db.get_goal_total_cost("g_alpha")
    assert total == pytest.approx(1.90, rel=1e-6)


def test_get_goal_total_cost_unknown_goal_returns_zero(qwe_temp_data_dir):
    """An unknown goal_id sums to 0.0 (NULL→0 by COALESCE)."""
    import db
    assert db.get_goal_total_cost("g_doesnotexist") == 0.0


def test_get_goal_total_cost_null_costs_treated_as_zero(qwe_temp_data_dir):
    """Runs with NULL cost_usd (unpriced models) sum to 0, never trip the cap."""
    import db
    rid = db.insert_agent_run(
        thread_id="t1", source="orchestrator", started_at=1000.0,
        goal_id="g_local_model",
    )
    db.finalize_agent_run(rid, finished_at=1001.0, duration_ms=1000,
                          status="ok", cost_usd=None)
    # Sum of a single NULL → 0.0 by COALESCE
    assert db.get_goal_total_cost("g_local_model") == 0.0


def test_get_goal_includes_live_cost_from_agent_runs(qwe_temp_data_dir):
    """db.get_goal returns the live agent_runs sum, NOT the dead
    goals.cost_usd column (which nothing in the runtime ever writes to).
    Regression for the Goals UI showing $0 for every row."""
    import db
    goal_id = db.create_goal(user_input="x", source="cli")
    r1 = db.insert_agent_run(
        thread_id="t1", source="orchestrator", started_at=1000.0,
        goal_id=goal_id,
    )
    db.finalize_agent_run(r1, finished_at=1001.0, duration_ms=1000,
                          status="ok", cost_usd=1.25)
    r2 = db.insert_agent_run(
        thread_id="t1", source="subagent_browser", started_at=1100.0,
        goal_id=goal_id,
    )
    db.finalize_agent_run(r2, finished_at=1101.0, duration_ms=1000,
                          status="ok", cost_usd=0.75)
    g = db.get_goal(goal_id)
    assert g["cost_usd"] == pytest.approx(2.00, rel=1e-6)


def test_list_goals_returns_live_cost_per_row(qwe_temp_data_dir):
    """db.list_goals uses a LEFT JOIN to roll up costs; each row's
    cost_usd is the sum of its agent_runs, not the dead column."""
    import db
    g1 = db.create_goal(user_input="alpha", source="cli")
    g2 = db.create_goal(user_input="beta", source="cli")
    r1 = db.insert_agent_run(
        thread_id="t1", source="orch", started_at=1000.0, goal_id=g1,
    )
    db.finalize_agent_run(r1, finished_at=1001.0, duration_ms=10,
                          status="ok", cost_usd=2.50)
    r2 = db.insert_agent_run(
        thread_id="t2", source="orch", started_at=1100.0, goal_id=g2,
    )
    db.finalize_agent_run(r2, finished_at=1101.0, duration_ms=10,
                          status="ok", cost_usd=0.05)
    # Unrelated non-goal run — must NOT leak into either total
    r3 = db.insert_agent_run(thread_id="t3", source="web", started_at=1200.0)
    db.finalize_agent_run(r3, finished_at=1201.0, duration_ms=10,
                          status="ok", cost_usd=10.0)

    listed = {g["id"]: g["cost_usd"] for g in db.list_goals()}
    assert listed[g1] == pytest.approx(2.50, rel=1e-6)
    assert listed[g2] == pytest.approx(0.05, rel=1e-6)

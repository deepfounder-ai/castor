"""Phase 2a tests: plan storage + structured facts.

These tests cover the storage primitives that the orchestrator (Phase 2b)
and subagents (Phase 2c) will use. No LLM in scope — pure data layer.
"""
from __future__ import annotations

import pytest


# ─────────────────────────────────────────────────────────────────────────────
#  Plan: set / update / read
# ─────────────────────────────────────────────────────────────────────────────


def test_set_goal_plan_assigns_stable_ids(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    plan = db.set_goal_plan(gid, [
        {"title": "Search for leads", "description": "Use LinkedIn"},
        {"title": "Extract emails", "description": "From profiles"},
        {"title": "Save to CRM", "description": "..."},
    ])
    ids = [st["id"] for st in plan["subtasks"]]
    assert ids == ["st_1", "st_2", "st_3"]
    # All start as pending
    statuses = [st["status"] for st in plan["subtasks"]]
    assert statuses == ["pending", "pending", "pending"]


def test_set_goal_plan_persists_to_goal_row(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{"title": "do thing", "description": "..."}])
    g = db.get_goal(gid)
    assert g["plan"] is not None
    assert g["plan"]["subtasks"][0]["title"] == "do thing"


def test_set_goal_plan_replaces_existing(qwe_temp_data_dir):
    """Calling set_goal_plan again replaces the previous plan."""
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{"title": "A", "description": ""}])
    db.set_goal_plan(gid, [
        {"title": "B", "description": ""},
        {"title": "C", "description": ""},
    ])
    plan = db.get_goal_plan(gid)
    titles = [st["title"] for st in plan["subtasks"]]
    assert titles == ["B", "C"]


def test_set_goal_plan_logs_event(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{"title": "A", "description": ""}])
    events = db.get_goal_events(gid)
    types = [e["event_type"] for e in events]
    assert "plan_set" in types


def test_update_subtask_changes_status_and_timestamps(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [
        {"title": "A", "description": ""},
        {"title": "B", "description": ""},
    ])
    # Mark st_1 in_progress → started_at populated
    plan = db.update_subtask(gid, "st_1", status="in_progress")
    st1 = next(s for s in plan["subtasks"] if s["id"] == "st_1")
    assert st1["status"] == "in_progress"
    assert st1["started_at"] is not None
    assert st1["finished_at"] is None
    # current_index follows in_progress transitions
    assert plan["current_index"] == 0

    # Mark st_1 completed → finished_at populated
    plan = db.update_subtask(gid, "st_1", status="completed",
                             result_summary="found 47 results")
    st1 = next(s for s in plan["subtasks"] if s["id"] == "st_1")
    assert st1["status"] == "completed"
    assert st1["finished_at"] is not None
    assert st1["result_summary"] == "found 47 results"


def test_update_subtask_records_dispatched_subagent(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{"title": "A", "description": ""}])
    db.update_subtask(gid, "st_1", dispatched_subagent="browser")
    plan = db.get_goal_plan(gid)
    assert plan["subtasks"][0]["dispatched_subagent"] == "browser"


def test_update_subtask_bump_attempts(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{"title": "A", "description": ""}])
    db.update_subtask(gid, "st_1", bump_attempts=True)
    db.update_subtask(gid, "st_1", bump_attempts=True)
    plan = db.get_goal_plan(gid)
    assert plan["subtasks"][0]["attempts"] == 2


def test_update_subtask_returns_none_for_missing(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{"title": "A", "description": ""}])
    assert db.update_subtask(gid, "st_999", status="completed") is None


def test_update_subtask_returns_none_when_no_plan(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    # No plan set yet
    assert db.update_subtask(gid, "st_1", status="completed") is None


def test_update_subtask_rejects_invalid_status(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{"title": "A", "description": ""}])
    with pytest.raises(ValueError):
        db.update_subtask(gid, "st_1", status="bogus")


def test_update_subtask_logs_status_specific_event(qwe_temp_data_dir):
    """Each status transition logs a corresponding event type for observability."""
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{"title": "A", "description": ""}])
    db.update_subtask(gid, "st_1", status="in_progress")
    db.update_subtask(gid, "st_1", status="completed", result_summary="done")
    types = [e["event_type"] for e in db.get_goal_events(gid)]
    assert "subtask_in_progress" in types
    assert "subtask_completed" in types


def test_goal_plan_is_complete(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    # No plan → not complete
    assert db.goal_plan_is_complete(gid) is False
    db.set_goal_plan(gid, [
        {"title": "A", "description": ""},
        {"title": "B", "description": ""},
    ])
    # All pending → not complete
    assert db.goal_plan_is_complete(gid) is False
    db.update_subtask(gid, "st_1", status="completed")
    assert db.goal_plan_is_complete(gid) is False
    db.update_subtask(gid, "st_2", status="skipped")
    assert db.goal_plan_is_complete(gid) is True


# ─────────────────────────────────────────────────────────────────────────────
#  Facts: save / get / delete / list_keys
# ─────────────────────────────────────────────────────────────────────────────


def test_fact_save_and_get_round_trip(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.fact_save(gid, "login_url", "https://example.com/login")
    db.fact_save(gid, "results_count", "47")
    facts = db.fact_get(gid)
    assert facts == {"login_url": "https://example.com/login", "results_count": "47"}


def test_fact_save_upserts_on_duplicate_key(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.fact_save(gid, "count", "10")
    db.fact_save(gid, "count", "47")  # overwrite
    assert db.fact_get(gid) == {"count": "47"}


def test_fact_save_records_source_subtask(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.fact_save(gid, "first_url", "https://...", source_subtask_id="st_2")
    conn = db._get_conn()
    row = conn.execute(
        "SELECT source_subtask_id FROM goal_facts WHERE goal_id=? AND key=?",
        (gid, "first_url"),
    ).fetchone()
    assert row[0] == "st_2"


def test_fact_get_filters_by_keys(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.fact_save(gid, "a", "1")
    db.fact_save(gid, "b", "2")
    db.fact_save(gid, "c", "3")
    assert db.fact_get(gid, keys=["a", "c"]) == {"a": "1", "c": "3"}


def test_fact_save_rejects_invalid_keys(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    with pytest.raises(ValueError):
        db.fact_save(gid, "", "v")
    with pytest.raises(ValueError):
        db.fact_save(gid, "has\nnewline", "v")


def test_fact_list_keys(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.fact_save(gid, "zebra", "1")
    db.fact_save(gid, "apple", "2")
    db.fact_save(gid, "mango", "3")
    assert db.fact_list_keys(gid) == ["apple", "mango", "zebra"]


def test_fact_delete(qwe_temp_data_dir):
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.fact_save(gid, "k", "v")
    assert db.fact_delete(gid, "k") is True
    assert db.fact_get(gid) == {}
    # Deleting non-existent key returns False
    assert db.fact_delete(gid, "k") is False


def test_facts_isolated_between_goals(qwe_temp_data_dir):
    """Facts are scoped to (goal_id, key) — different goals don't see each other."""
    import db
    g1 = db.create_goal(user_input="a", source="cli")
    g2 = db.create_goal(user_input="b", source="cli")
    db.fact_save(g1, "common_key", "from g1")
    db.fact_save(g2, "common_key", "from g2")
    assert db.fact_get(g1) == {"common_key": "from g1"}
    assert db.fact_get(g2) == {"common_key": "from g2"}


def test_facts_cascade_delete_with_goal(qwe_temp_data_dir):
    """Deleting a goal row removes its facts via ON DELETE CASCADE."""
    import db
    gid = db.create_goal(user_input="x", source="cli")
    db.fact_save(gid, "k", "v")
    conn = db._get_conn()
    # Foreign keys are off by default in sqlite3; the migration runs with
    # them ON for the cascade to fire. Verify the pragma is set, then delete.
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if not fk:
        conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM goals WHERE id=?", (gid,))
    conn.commit()
    # facts gone
    row = conn.execute(
        "SELECT COUNT(*) FROM goal_facts WHERE goal_id=?", (gid,)
    ).fetchone()
    assert row[0] == 0

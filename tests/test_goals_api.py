"""HTTP surface tests for /api/goals (Phase 1).

Uses TestClient against the real FastAPI app with CASTOR_DATA_DIR pointed at a
tempdir. Mirrors the fixture pattern in tests/test_integration.py so the
isolation rules (no real ~/.castor data leaks) are identical.
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(scope="module", autouse=True)
def _goals_api_env():
    original = os.environ.get("CASTOR_DATA_DIR")
    tmp_root = Path(tempfile.mkdtemp(prefix="qwe_goals_api_"))
    os.environ["CASTOR_DATA_DIR"] = str(tmp_root)
    (tmp_root / ".migrated_v2").write_text("test skip\n")
    (tmp_root / ".migrated_from_qwe_qwe").write_text("test skip\n")
    _reload_core()
    try:
        yield tmp_root
    finally:
        _close_db()
        if original is not None:
            os.environ["CASTOR_DATA_DIR"] = original
        else:
            os.environ.pop("CASTOR_DATA_DIR", None)
        if tmp_root.exists():
            shutil.rmtree(tmp_root, ignore_errors=True)
        _reload_core()


def _close_db():
    db_mod = sys.modules.get("db")
    if db_mod is None:
        return
    try:
        _local = getattr(db_mod, "_local", None)
        conn = getattr(_local, "conn", None) if _local else None
        if conn is not None:
            conn.close()
        if _local is not None:
            _local.conn = None
        db_mod._migrated = False
    except Exception:
        pass


def _reload_core():
    _close_db()
    for mod in ("config", "db", "soul", "threads", "presets", "server"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
        else:
            importlib.import_module(mod)


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import server
    with TestClient(server.app) as c:
        yield c


def test_create_goal_returns_id_and_pending_status(client):
    r = client.post("/api/goals", json={"user_input": "scrape leads", "source": "api"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"].startswith("g_")
    assert body["status"] == "pending"


def test_create_goal_rejects_empty_input(client):
    r = client.post("/api/goals", json={"user_input": "   "})
    assert r.status_code == 400


def test_get_goal_returns_full_row(client):
    gid = client.post("/api/goals", json={"user_input": "x", "source": "api"}).json()["id"]
    r = client.get(f"/api/goals/{gid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == gid
    assert body["status"] == "pending"
    assert body["user_input"] == "x"
    assert body["source"] == "api"
    assert body["cost_usd"] == 0.0


def test_get_goal_404_for_missing(client):
    r = client.get("/api/goals/g_nope")
    assert r.status_code == 404


def test_list_goals_filters_by_status(client):
    import db
    g1 = client.post("/api/goals", json={"user_input": "a"}).json()["id"]
    g2 = client.post("/api/goals", json={"user_input": "b"}).json()["id"]
    db.mark_goal_done(g1, result="done")

    pending = client.get("/api/goals?status=pending").json()["goals"]
    done = client.get("/api/goals?status=done").json()["goals"]
    pending_ids = {g["id"] for g in pending}
    done_ids = {g["id"] for g in done}
    assert g2 in pending_ids
    assert g1 in done_ids


def test_get_goal_events_returns_at_least_creation_event(client):
    gid = client.post("/api/goals", json={"user_input": "x"}).json()["id"]
    r = client.get(f"/api/goals/{gid}/events")
    assert r.status_code == 200
    types = [e["event_type"] for e in r.json()["events"]]
    assert "goal_created" in types


def test_get_goal_events_404_for_missing(client):
    r = client.get("/api/goals/g_nope/events")
    assert r.status_code == 404


def test_pause_goal_transitions_to_paused(client):
    gid = client.post("/api/goals", json={"user_input": "x"}).json()["id"]
    r = client.post(f"/api/goals/{gid}/pause")
    assert r.status_code == 200
    assert r.json()["status"] == "paused"
    # Verify persisted state.
    body = client.get(f"/api/goals/{gid}").json()
    assert body["status"] == "paused"


def test_pause_already_done_goal_conflicts(client):
    import db
    gid = client.post("/api/goals", json={"user_input": "x"}).json()["id"]
    db.mark_goal_done(gid, result="finished")
    r = client.post(f"/api/goals/{gid}/pause")
    assert r.status_code == 409


def test_abort_goal_marks_aborted(client):
    gid = client.post("/api/goals", json={"user_input": "x"}).json()["id"]
    r = client.post(f"/api/goals/{gid}/abort")
    assert r.status_code == 200
    assert r.json()["status"] == "aborted"
    # Aborted goals are NOT re-claimable.
    import db
    assert db.claim_next_goal("worker_X", lease_sec=60) != gid

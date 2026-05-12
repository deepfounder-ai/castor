"""Unit tests for analytics-related HTTP endpoints (cost tracking)."""
import time
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import server
    return TestClient(server.app)


def test_threads_endpoint_includes_token_fields(client, qwe_temp_data_dir):
    import db, threads
    threads.create("Test Thread T1")
    # Find the created thread id
    all_t = threads.list_all()
    tid = all_t[0]["id"]
    rid = db.insert_agent_run(thread_id=tid, source="web",
                              started_at=time.time(), status="running")
    db.finalize_agent_run(rid, finished_at=time.time(), duration_ms=10,
                          status="ok", input_tokens=100, output_tokens=50,
                          cost_usd=0.001)
    r = client.get("/api/threads")
    assert r.status_code == 200
    sess = [s for s in r.json() if s.get("thread_id") == tid or s.get("id") == tid]
    assert sess and sess[0]["input_tokens"] == 100
    assert sess[0]["cost_usd"] == 0.001
    assert sess[0]["run_count"] == 1


def test_thread_runs_endpoint(client, qwe_temp_data_dir):
    import db, time
    for tok in (100, 200, 300):
        rid = db.insert_agent_run(thread_id="t1", source="web",
                                  started_at=time.time(), status="running")
        db.finalize_agent_run(rid, finished_at=time.time(), duration_ms=10,
                              status="ok", input_tokens=tok, output_tokens=tok,
                              cost_usd=tok * 1e-6)
    r = client.get("/api/threads/t1/runs")
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 3
    assert runs[0]["input_tokens"] == 300  # newest first
    assert runs[2]["input_tokens"] == 100


def test_thread_runs_empty_thread_returns_empty_list(client, qwe_temp_data_dir):
    r = client.get("/api/threads/never-existed/runs")
    assert r.status_code == 200
    assert r.json() == []

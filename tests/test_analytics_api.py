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

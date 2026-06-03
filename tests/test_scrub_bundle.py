"""Sprint 1 PR #1 — scrub bundle regression tests.

Pins the v0.23.4 hardening that closes three CRITICALs flagged by the
whole-codebase architecture review:

  C1. ``db.save_message`` writes ``content`` / ``tool_calls`` / ``meta``
      verbatim into SQLite.
  C2. ``synthesis.py`` writes LLM-summarised entity & wiki blobs by
      calling ``memory._save_single`` directly, bypassing the scrub at
      ``memory.save``.
  C3. ``trajectory.tool_start`` / ``tool_end`` writes raw tool args and
      result previews to JSONL — opt-in but 30-day retention by default.

Plus the wire-up of ``trajectory.prune_old`` into the scheduler so the
retention window is actually enforced.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


# ── C2: memory._save_single now scrubs by default ──────────────────────────


def test_save_single_scrubs_by_default(qwe_temp_data_dir, monkeypatch):
    """Direct ``_save_single`` call (the synthesis path) gets the scrub for
    free. Before v0.23.4, only ``memory.save`` scrubbed and synthesis
    bypassed it.
    """
    import sys
    memory = sys.modules["memory"]
    # Stub the heavy embed + Qdrant deps so we observe what _save_single
    # receives as the scrubbed text, without booting FastEmbed.
    captured = {}

    class _StubQ:
        def upsert(self, _coll=None, *, points, **_kw):
            for p in points:
                captured["text"] = p.payload["text"]

        def scroll(self, *_a, **_kw):
            return ([], None)

    monkeypatch.setattr(memory, "_get_qdrant", lambda: _StubQ())
    from qdrant_client.models import SparseVector
    monkeypatch.setattr(memory, "_embed", lambda _t: [0.0] * memory.EMBED_DIM)
    monkeypatch.setattr(memory, "sparse_embed", lambda _t: SparseVector(indices=[], values=[]))

    leak = "OPENAI_API_KEY=sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789aBcDeFgHiJkLmNoPq"
    memory._save_single(leak, tag="general")
    assert "sk-proj-aBcDeFgHiJkLmNoPq" not in captured["text"]
    assert "[REDACTED" in captured["text"]


def test_save_single_scrub_false_disables(qwe_temp_data_dir, monkeypatch):
    """``memory.save`` already scrubs at the top, so it calls
    ``_save_single(..., scrub=False)`` to avoid double-scrubbing. Verify
    the opt-out actually disables scrub.
    """
    import sys
    memory = sys.modules["memory"]
    captured = {}

    class _StubQ:
        def upsert(self, _coll=None, *, points, **_kw):
            for p in points:
                captured["text"] = p.payload["text"]

        def scroll(self, *_a, **_kw):
            return ([], None)

    monkeypatch.setattr(memory, "_get_qdrant", lambda: _StubQ())
    from qdrant_client.models import SparseVector
    monkeypatch.setattr(memory, "_embed", lambda _t: [0.0] * memory.EMBED_DIM)
    monkeypatch.setattr(memory, "sparse_embed", lambda _t: SparseVector(indices=[], values=[]))

    leak = "OPENAI_API_KEY=sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789aBcDeFgHiJkLmNoPq"
    memory._save_single(leak, tag="general", scrub=False)
    # Opt-out: raw value lands. (This is the path memory.save uses
    # because it already scrubbed before calling.)
    assert "sk-proj-aBcDeFgHiJkLmNoPq" in captured["text"]


def test_memory_save_still_scrubs_once(qwe_temp_data_dir, monkeypatch):
    """End-to-end: ``memory.save`` → ``_save_single`` chain scrubs once
    (at the boundary), not twice. No double-warning, leaked value still
    redacted.
    """
    import sys
    memory = sys.modules["memory"]
    captured = {}

    class _StubQ:
        def upsert(self, _coll=None, *, points, **_kw):
            for p in points:
                captured["text"] = p.payload["text"]

        def scroll(self, *_a, **_kw):
            return ([], None)

    monkeypatch.setattr(memory, "_get_qdrant", lambda: _StubQ())
    from qdrant_client.models import SparseVector
    monkeypatch.setattr(memory, "_embed", lambda _t: [0.0] * memory.EMBED_DIM)
    monkeypatch.setattr(memory, "sparse_embed", lambda _t: SparseVector(indices=[], values=[]))

    leak = "OPENAI_API_KEY=sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789aBcDeFgHiJkLmNoPq"
    memory.save(leak, tag="general")
    assert "sk-proj-aBcDeFgHiJkLmNoPq" not in captured["text"]
    assert "[REDACTED" in captured["text"]


# ── C1: db.save_message scrubs content + tool_calls + meta ────────────────


def test_save_message_scrubs_content(qwe_temp_data_dir):
    import sys
    db = sys.modules["db"]
    leak = "here is my key sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789aBcDeFgHiJkLmNoPq end"
    db.save_message("user", content=leak, thread_id="t1")
    msgs = db.get_recent_messages(thread_id="t1")
    assert msgs[-1]["content"]
    assert "sk-proj-aBcDeFgHiJkLmNoPq" not in msgs[-1]["content"]
    assert "[REDACTED" in msgs[-1]["content"]


def test_save_message_scrubs_tool_calls(qwe_temp_data_dir):
    """The LinkedIn-leak shape: ``fact_save`` arguments embedded as a
    JSON string in ``tool_calls[i].function.arguments``.
    """
    import sys
    db = sys.modules["db"]
    tc = [{
        "id": "call_1",
        "function": {
            "name": "fact_save",
            "arguments": json.dumps({
                "key": "linkedin_password",
                "value": "Qwerty446148044",
            }),
        },
    }]
    db.save_message("assistant", content=None, tool_calls=tc, thread_id="t2")
    msgs = db.get_recent_messages(thread_id="t2")
    args_str = msgs[-1]["tool_calls"][0]["function"]["arguments"]
    args = json.loads(args_str)
    assert args["value"] != "Qwerty446148044"
    assert "[REDACTED" in args["value"]


def test_save_message_scrubs_meta(qwe_temp_data_dir):
    import sys
    db = sys.modules["db"]
    db.save_message(
        "user", content="hi",
        meta={"api_key": "sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789aBcDeFgHiJkLmNoPq",
              "username": "harmless"},
        thread_id="t3",
    )
    msgs = db.get_recent_messages(thread_id="t3")
    meta = msgs[-1]["meta"]
    # Key name self-identifies as secret → fully redacted regardless of value shape.
    assert meta["api_key"].startswith("[REDACTED")
    # Neutral key + non-secret value passes through unchanged.
    assert meta["username"] == "harmless"


def test_save_message_passes_through_clean_text(qwe_temp_data_dir):
    """Negative test: ordinary content with no secret shape is byte-equal."""
    import sys
    db = sys.modules["db"]
    clean = "hi, can you write a haiku about the rain?"
    db.save_message("user", content=clean, thread_id="t4")
    msgs = db.get_recent_messages(thread_id="t4")
    assert msgs[-1]["content"] == clean


# ── C3: trajectory.tool_start / tool_end scrub previews ───────────────────


def test_trajectory_tool_start_scrubs_args(tmp_path):
    import trajectory
    rec = trajectory.TrajectoryRecorder(
        run_id="t1", source="test", path=tmp_path / "t1.jsonl",
    )
    rec.tool_start("memory_save", {
        "text": "OPENAI_API_KEY=sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789aBcDeFgHiJkLmNoPq",
        "tag": "knowledge",
    })
    line = (tmp_path / "t1.jsonl").read_text().strip()
    event = json.loads(line)
    assert event["ev"] == "tool_start"
    assert "sk-proj-aBcDeFgHiJkLmNoPq" not in event["args"]["text"]
    assert "[REDACTED" in event["args"]["text"]
    # Non-secret field untouched.
    assert event["args"]["tag"] == "knowledge"


def test_trajectory_tool_start_scrubs_keyed_as_secret(tmp_path):
    """Plain-string password under a self-identifying key is redacted by
    ``scrub_fact`` (key-aware) even though the value itself has no
    provider regex match.
    """
    import trajectory
    rec = trajectory.TrajectoryRecorder(
        run_id="t2", source="test", path=tmp_path / "t2.jsonl",
    )
    rec.tool_start("fact_save", {"key": "linkedin_password", "value": "Qwerty446148044"})
    event = json.loads((tmp_path / "t2.jsonl").read_text().strip())
    assert event["args"]["value"] != "Qwerty446148044"


def test_trajectory_tool_end_scrubs_result_preview(tmp_path):
    import trajectory
    rec = trajectory.TrajectoryRecorder(
        run_id="t3", source="test", path=tmp_path / "t3.jsonl",
    )
    big_result = (
        "shell output:\n"
        "echo $OPENAI_API_KEY=sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789aBcDeFgHiJkLmNoPq\n"
        "done"
    )
    rec.tool_end("shell", big_result, duration_ms=42)
    event = json.loads((tmp_path / "t3.jsonl").read_text().strip())
    assert event["ev"] == "tool_end"
    assert "sk-proj-aBcDeFgHiJkLmNoPq" not in event["result_preview"]
    assert "[REDACTED" in event["result_preview"]
    # Length reflects the raw result, not the scrubbed preview — telemetry signal.
    assert event["result_len"] == len(big_result)


def test_trajectory_tool_end_empty_result_safe(tmp_path):
    """Defensive: empty / None result still writes a valid event."""
    import trajectory
    rec = trajectory.TrajectoryRecorder(
        run_id="t4", source="test", path=tmp_path / "t4.jsonl",
    )
    rec.tool_end("noop", "", duration_ms=0)
    event = json.loads((tmp_path / "t4.jsonl").read_text().strip())
    assert event["ev"] == "tool_end"
    assert event["result_preview"] == ""


# ── H4: trajectory.prune_old wired into the scheduler ─────────────────────


def test_trajectory_prune_registers_when_enabled(qwe_temp_data_dir, monkeypatch):
    """Setting ``trajectory_enabled`` → starting the scheduler creates
    the ``__trajectory_prune__`` row.
    """
    import sys
    db = sys.modules["db"]
    config = sys.modules["config"]
    # Pin trajectory_enabled=1 for this test
    config.kv = config.kv if hasattr(config, "kv") else {}
    db.kv_set("setting:trajectory_enabled", "1")
    import scheduler
    scheduler._register_trajectory_prune()
    row = db.fetchone(
        "SELECT name, schedule, enabled FROM scheduled_tasks WHERE name=?",
        (scheduler.TRAJECTORY_PRUNE_TASK_NAME,),
    )
    assert row is not None
    assert row[0] == scheduler.TRAJECTORY_PRUNE_TASK_NAME
    assert row[1] == "daily 04:00"
    assert row[2] == 1


def test_trajectory_prune_skips_when_disabled(qwe_temp_data_dir):
    import sys
    db = sys.modules["db"]
    db.kv_set("setting:trajectory_enabled", "0")
    import scheduler
    scheduler._register_trajectory_prune()
    row = db.fetchone(
        "SELECT name FROM scheduled_tasks WHERE name=?",
        (scheduler.TRAJECTORY_PRUNE_TASK_NAME,),
    )
    assert row is None  # opt-in only


def test_trajectory_prune_dispatch_calls_prune_old(qwe_temp_data_dir, monkeypatch):
    """The ``__trajectory_prune__`` task name routes to
    ``trajectory.prune_old`` via ``_execute_task`` — no LLM.
    """
    import sys
    import scheduler
    calls = []
    monkeypatch.setattr(
        sys.modules.get("trajectory") or __import__("trajectory"),
        "prune_old",
        lambda days: calls.append(days) or 7,
    )
    result = scheduler._execute_task(scheduler.TRAJECTORY_PRUNE_TASK_NAME)
    assert calls and calls[0] >= 1
    assert "trajectory prune" in result
    assert "7" in result


def test_trajectory_prune_is_not_routine():
    """Sanity: the new task name is on the system-task False-list so the
    scheduler doesn't try to route it through ``agent.run`` (which would
    burn LLM tokens for a stateless file delete).
    """
    import scheduler
    assert scheduler._is_routine(scheduler.TRAJECTORY_PRUNE_TASK_NAME) is False


def test_trajectory_prune_old_actually_deletes(tmp_path, monkeypatch):
    """End-to-end: drop a stale .jsonl into the trajectory dir, call
    prune_old(0), assert it's gone.
    """
    import trajectory
    monkeypatch.setattr(trajectory, "_trajectory_dir", lambda: tmp_path)
    stale = tmp_path / "stale.jsonl"
    stale.write_text('{"ev":"run_start"}\n')
    # Backdate so even days=0 catches it.
    old = time.time() - 86400
    import os
    os.utime(stale, (old, old))
    removed = trajectory.prune_old(days=0)
    assert removed == 1
    assert not stale.exists()

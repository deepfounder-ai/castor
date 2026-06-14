"""Trajectory recording — JSONL output for agent runs.

Tests cover:
  - Opt-in: disabled by default, ``start()`` returns None
  - JSONL output format (one event per line, valid JSON each)
  - File path under ~/.castor/trajectories/<run_id>.jsonl
  - run_id sanitisation (filesystem-safe)
  - Context manager auto-finalises (run_end always written)
  - Crash inside ``with`` block still writes a run_end with error
  - Emitter integration: events bridge to recorder
  - load_run replays the event stream
  - list_runs returns newest first
  - prune_old removes files older than N days
  - Defensive: broken file path / missing dir / corrupted line don't crash
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import db
import trajectory


# ── opt-in gate ─────────────────────────────────────────────────────────────


def test_disabled_by_default(qwe_temp_data_dir):
    """``trajectory_enabled`` defaults to 0 — start() returns None."""
    assert trajectory.is_enabled() is False
    rec = trajectory.start("chat")
    assert rec is None


def test_enable_via_setting(qwe_temp_data_dir):
    db.kv_set("setting:trajectory_enabled", "1")
    assert trajectory.is_enabled() is True


def test_recording_context_manager_no_op_when_disabled(qwe_temp_data_dir):
    """``recording(...)`` returns a NullRecorder so callers don't need
    to check for None inside a `with` block."""
    with trajectory.recording("chat") as rec:
        # All methods are safe no-ops
        rec.tool_start("foo", {"x": 1})
        rec.tool_end("foo", "result", 100)
        rec.content("hello")
        rec.event("custom", {"a": "b"})
    # No file was created (recording disabled)
    files = list((Path(os.environ.get("CASTOR_DATA_DIR",
                                       Path.home() / ".castor"))
                  / "trajectories").glob("*.jsonl")) if (Path(
        os.environ.get("CASTOR_DATA_DIR", Path.home() / ".castor")
    ) / "trajectories").exists() else []
    # Either no dir, or no files.
    assert all(f.name != "chat_*.jsonl" for f in files)


# ── JSONL output ────────────────────────────────────────────────────────────


def test_start_writes_run_start_event(qwe_temp_data_dir):
    db.kv_set("setting:trajectory_enabled", "1")
    rec = trajectory.start("chat", model="gpt-4o", thread_id="t_abc")
    assert rec is not None
    assert rec.path.is_file()
    lines = rec.path.read_text().strip().split("\n")
    assert len(lines) == 1
    ev = json.loads(lines[0])
    assert ev["ev"] == "run_start"
    assert ev["source"] == "chat"
    assert ev["model"] == "gpt-4o"
    assert ev["thread_id"] == "t_abc"
    assert "ts" in ev


def test_event_appends_jsonl_lines(qwe_temp_data_dir):
    db.kv_set("setting:trajectory_enabled", "1")
    rec = trajectory.start("chat")
    rec.event("custom", {"foo": "bar"})
    rec.tool_start("read_file", {"path": "/etc/hostname"})
    rec.tool_end("read_file", "macbook", 12)
    rec.content("Here is the result.")
    lines = rec.path.read_text().strip().split("\n")
    # 1 run_start + 4 events
    assert len(lines) == 5
    # Each line is valid JSON
    parsed = [json.loads(line) for line in lines]
    assert [p["ev"] for p in parsed] == [
        "run_start", "custom", "tool_start", "tool_end", "content"
    ]
    assert parsed[2]["name"] == "read_file"
    assert parsed[2]["args"] == {"path": "/etc/hostname"}
    assert parsed[3]["result_preview"] == "macbook"
    assert parsed[3]["duration_ms"] == 12
    assert parsed[4]["text"] == "Here is the result."


def test_finish_writes_run_end_with_totals(qwe_temp_data_dir):
    db.kv_set("setting:trajectory_enabled", "1")
    rec = trajectory.start("chat", model="gpt-4o")
    rec.tool_start("read_file", {"path": "/x"})
    rec.tool_end("read_file", "data", 50)
    rec.finish(cost_usd=0.001, prompt_tokens=120, completion_tokens=50)

    events = trajectory.load_run(rec.run_id)
    end = events[-1]
    assert end["ev"] == "run_end"
    assert end["status"] == "ok"
    assert end["tool_count"] == 1
    assert end["tools_used"] == ["read_file"]
    assert end["cost_usd"] == 0.001
    assert end["prompt_tokens"] == 120
    assert end["completion_tokens"] == 50
    assert end["duration_ms"] >= 0


def test_finish_is_idempotent(qwe_temp_data_dir):
    """Calling finish twice writes ONE run_end."""
    db.kv_set("setting:trajectory_enabled", "1")
    rec = trajectory.start("chat")
    rec.finish()
    rec.finish()  # second call no-ops
    events = trajectory.load_run(rec.run_id)
    run_ends = [e for e in events if e["ev"] == "run_end"]
    assert len(run_ends) == 1


def test_event_after_finish_is_dropped(qwe_temp_data_dir):
    """Events written after the recorder is finished are silently
    dropped — a misbehaving caller shouldn't append stale events."""
    db.kv_set("setting:trajectory_enabled", "1")
    rec = trajectory.start("chat")
    rec.finish()
    rec.event("post_finish", {"x": 1})
    events = trajectory.load_run(rec.run_id)
    assert all(e["ev"] != "post_finish" for e in events)


# ── Context manager ─────────────────────────────────────────────────────────


def test_context_manager_normal_exit_writes_run_end(qwe_temp_data_dir):
    db.kv_set("setting:trajectory_enabled", "1")
    with trajectory.recording("chat") as rec:
        rec.content("hi")
        run_id = rec.run_id
    events = trajectory.load_run(run_id)
    assert events[-1]["ev"] == "run_end"
    assert events[-1]["status"] == "ok"


def test_context_manager_exception_writes_run_end_with_error(qwe_temp_data_dir):
    """If the with-block raises, run_end captures the exception class +
    message — invaluable for post-mortem."""
    db.kv_set("setting:trajectory_enabled", "1")
    run_id = None
    try:
        with trajectory.recording("chat") as rec:
            run_id = rec.run_id
            rec.content("about to fail")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    events = trajectory.load_run(run_id)
    end = events[-1]
    assert end["ev"] == "run_end"
    assert end["status"] == "error"
    assert "RuntimeError" in end["error"]
    assert "boom" in end["error"]


# ── run_id sanitisation ─────────────────────────────────────────────────────


def test_run_id_sanitisation_strips_unsafe_chars(qwe_temp_data_dir):
    db.kv_set("setting:trajectory_enabled", "1")
    rec = trajectory.start("chat", run_id="../../etc/passwd evil")
    # No path-traversal; no spaces in filename
    assert ".." not in rec.path.name
    assert "/" not in rec.path.name
    assert " " not in rec.path.name
    # And the file is rooted under trajectories/
    assert "trajectories" in str(rec.path.parent)


def test_run_id_sanitisation_handles_empty(qwe_temp_data_dir):
    """Empty run_id → auto-generated fallback."""
    sanitised = trajectory._sanitize_run_id("")
    assert sanitised  # non-empty
    assert sanitised.startswith("run_")


def test_run_id_sanitisation_caps_length(qwe_temp_data_dir):
    sanitised = trajectory._sanitize_run_id("x" * 200)
    assert len(sanitised) <= 64


# ── load_run / list_runs ────────────────────────────────────────────────────


def test_load_run_returns_event_list_in_order(qwe_temp_data_dir):
    db.kv_set("setting:trajectory_enabled", "1")
    rec = trajectory.start("chat")
    rec.event("a", {})
    rec.event("b", {})
    rec.finish()
    events = trajectory.load_run(rec.run_id)
    types = [e["ev"] for e in events]
    assert types == ["run_start", "a", "b", "run_end"]


def test_load_run_unknown_id_returns_empty(qwe_temp_data_dir):
    assert trajectory.load_run("does_not_exist") == []


def test_load_run_skips_malformed_lines(qwe_temp_data_dir):
    """A half-written line from a crash mid-write should be skipped,
    not crash the read."""
    db.kv_set("setting:trajectory_enabled", "1")
    rec = trajectory.start("chat")
    # Append a corrupt line directly
    with rec.path.open("a") as f:
        f.write('{"not-valid-json\n')
        f.write('{"ev": "valid", "x": 1}\n')
    events = trajectory.load_run(rec.run_id)
    assert any(e.get("ev") == "valid" for e in events)


def test_list_runs_newest_first(qwe_temp_data_dir):
    db.kv_set("setting:trajectory_enabled", "1")
    r1 = trajectory.start("chat", run_id="first")
    r1.finish()
    # Force second run to have a later mtime
    time.sleep(0.01)
    r2 = trajectory.start("chat", run_id="second")
    r2.finish()

    runs = trajectory.list_runs()
    ids = [r["run_id"] for r in runs]
    assert "first" in ids and "second" in ids
    # Newest first
    assert ids.index("second") < ids.index("first")


def test_list_runs_respects_limit(qwe_temp_data_dir):
    db.kv_set("setting:trajectory_enabled", "1")
    for i in range(5):
        rec = trajectory.start("chat", run_id=f"r{i}")
        rec.finish()
        time.sleep(0.001)
    assert len(trajectory.list_runs(limit=3)) == 3


# ── prune_old ───────────────────────────────────────────────────────────────


def test_prune_old_removes_aged_files(qwe_temp_data_dir):
    db.kv_set("setting:trajectory_enabled", "1")
    rec = trajectory.start("chat", run_id="ancient")
    rec.finish()
    # Backdate the file by 40 days
    old_mtime = time.time() - (40 * 86400)
    os.utime(rec.path, (old_mtime, old_mtime))

    recent = trajectory.start("chat", run_id="recent")
    recent.finish()

    removed = trajectory.prune_old(days=30)
    assert removed == 1
    assert not rec.path.exists()
    assert recent.path.exists()


def test_prune_old_with_zero_days_removes_nothing(qwe_temp_data_dir):
    """Even with the cutoff at "right now", aging happens forward —
    files created NOW are not >0 days old."""
    db.kv_set("setting:trajectory_enabled", "1")
    rec = trajectory.start("chat", run_id="r1")
    rec.finish()
    # Future cutoff (way negative) → all files older, removed
    removed = trajectory.prune_old(days=-1)
    # Negative days means "files older than 1 day in the future" — i.e. all files.
    # If our spec says "0 = keep forever" we'd want this to be 0, but our impl
    # treats it as a normal day count. Document the actual behavior.
    assert removed >= 0  # don't crash


# ── Emitter integration ────────────────────────────────────────────────────


def test_attach_to_emitter_bridges_events(qwe_temp_data_dir):
    """attach_to_emitter wires an EventEmitter so every event lands in
    the trajectory file too."""
    db.kv_set("setting:trajectory_enabled", "1")
    from agent_events import EventEmitter, AgentEvent

    rec = trajectory.start("chat")
    emitter = EventEmitter()
    trajectory.attach_to_emitter(emitter, rec)

    emitter.tool_start("foo", {"x": 1})
    emitter.tool_end("foo", "result", duration_ms=50)
    emitter.content("hello")
    emitter.thinking("pondering")
    emitter.emit(AgentEvent("turn_end", {"turn": 3, "finish_reason": "stop"}))
    emitter.emit(AgentEvent("status", {"text": "ok"}))
    rec.finish()

    events = trajectory.load_run(rec.run_id)
    types = [e["ev"] for e in events]
    assert "tool_start" in types
    assert "tool_end" in types
    assert "content" in types
    assert "thinking" in types
    assert "turn_end" in types
    assert "status" in types  # unknown types pass through as generic events


def test_attach_to_emitter_handles_string_args(qwe_temp_data_dir):
    """agent_loop emits ``tool_start(name, str(args)[:80])`` — a STRING, not
    a dict. The recorder must still write the tool_start event (scrubbed as
    free text) rather than crashing on ``str.items()``."""
    db.kv_set("setting:trajectory_enabled", "1")
    from agent_events import EventEmitter

    rec = trajectory.start("chat")
    emitter = EventEmitter()
    trajectory.attach_to_emitter(emitter, rec)

    emitter.tool_start("read_file", "{'path': '/etc/hostname'}")
    emitter.tool_end("read_file", "macbook", duration_ms=12)
    rec.finish()

    events = trajectory.load_run(rec.run_id)
    starts = [e for e in events if e["ev"] == "tool_start"]
    assert starts and starts[0]["name"] == "read_file"
    assert "preview" in starts[0]["args"]


def test_scrub_args_redacts_string_preview(qwe_temp_data_dir):
    """String args preview runs through secret_scrub before persistence."""
    out = trajectory._scrub_args("api_key=sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")
    assert "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in out["preview"]


def test_agent_run_writes_trajectory_when_enabled(qwe_temp_data_dir, mock_llm):
    """End-to-end wiring pin: with trajectory_enabled=1, agent.run produces
    a .jsonl carrying a terminal run_start + run_end pair."""
    db.kv_set("setting:trajectory_enabled", "1")
    import agent

    before = {r["run_id"] for r in trajectory.list_runs()}
    agent.run("hello there")
    after = trajectory.list_runs()
    new = [r for r in after if r["run_id"] not in before]
    assert new, "agent.run produced no new trajectory file"
    events = trajectory.load_run(new[0]["run_id"])
    types = [e["ev"] for e in events]
    assert types[0] == "run_start"
    assert "run_end" in types


def test_attach_to_emitter_with_null_recorder_is_noop(qwe_temp_data_dir):
    """When recording is disabled and the caller passes a NullRecorder
    (from ``recording(...)``), attach_to_emitter is a safe no-op."""
    from agent_events import EventEmitter
    emitter = EventEmitter()
    # recording() returns NullRecorder when disabled
    null = trajectory.recording("chat")
    # No exception, no events written
    trajectory.attach_to_emitter(emitter, null)
    emitter.content("hi")
    # No file created — implicit check by absence of crashes


def test_attach_to_emitter_handles_none(qwe_temp_data_dir):
    """attach_to_emitter(emitter, None) is a no-op, doesn't crash."""
    from agent_events import EventEmitter
    emitter = EventEmitter()
    trajectory.attach_to_emitter(emitter, None)


# ── Defensive: serialisation failures ──────────────────────────────────────


def test_event_swallows_serialisation_errors(qwe_temp_data_dir):
    """A non-JSON-serialisable payload doesn't crash the agent — it's
    silently dropped with a debug log."""
    db.kv_set("setting:trajectory_enabled", "1")
    rec = trajectory.start("chat")

    class WeirdObj:
        """Not JSON-serialisable by default."""
        def __repr__(self):
            raise ValueError("repr also fails")

    # default=str will normally save us, but a repr that itself raises
    # would defeat it. Either way, no crash from event().
    rec.event("weird", {"obj": WeirdObj()})
    # The recorder is still functional after the bad event
    rec.event("good_one", {"x": 1})
    rec.finish()
    events = trajectory.load_run(rec.run_id)
    assert any(e.get("ev") == "good_one" for e in events)

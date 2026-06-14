"""Trajectory recording — capture every agent run as JSONL for audit & replay.

Inspired by Hermes Agent's `batch_runner.py` trajectory output. Each
agent run (chat turn, orchestrator round, subagent dispatch, routine
fire) can be recorded to ~/.castor/trajectories/<run_id>.jsonl, one
event per line.

## What gets recorded

A trajectory file is a stream of JSON events:

  {"ev": "run_start",  "ts": ..., "source": "chat", "model": "...", ...}
  {"ev": "tool_start", "ts": ..., "name": "browser_open", "args": {...}}
  {"ev": "tool_end",   "ts": ..., "name": "browser_open", "result_preview": "...", "duration_ms": 1234}
  {"ev": "content",    "ts": ..., "text": "..."}     # final LLM reply chunks
  {"ev": "thinking",   "ts": ..., "text": "..."}     # extended-thinking blocks
  {"ev": "turn_end",   "ts": ..., "turn": 3}
  {"ev": "run_end",    "ts": ..., "duration_ms": ..., "tools_used": [...], "cost_usd": ...}

## Why JSONL not JSON

  - Append-only, no rewriting on each event (crash-safe — a partial
    file is still readable up to the last newline)
  - Streaming-friendly — replay tools can read incrementally
  - Each line is a self-contained event with its own timestamp

## What's NOT recorded

  - Raw API request/response bodies (too large; we keep tool args/results)
  - Anything under 0 chars / null entries (skipped silently)
  - Goals / subagents detail beyond what's in events (caller decides
    how granular to subscribe)

## Privacy

Trajectory files contain whatever the user typed and whatever tools
returned. They live entirely on disk — never uploaded. Disabled by
default (config setting ``trajectory_enabled``). Rotate after
``trajectory_keep_days`` (default 30) — wired into the scheduler as
``__trajectory_prune__`` so the rotation actually happens.

v0.23.4 — every ``args_preview`` / ``result_preview`` ALSO runs through
``secret_scrub`` before the file write, mirroring the
``db.save_message`` / ``save_checkpoint`` redaction layer. Without
this, a tool that reads/echoes an API key would land it on disk
unscrubbed, defeating the rotation window.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import secret_scrub

_log = logging.getLogger("castor.trajectory")


def _scrub_args(args: dict | None, tool_name: str = "") -> dict:
    """Shallow-scrub a tool-call args dict for trajectory persistence.

    Mirrors :func:`db._scrub_meta` but tuned for tool args: every
    string-typed value runs through ``scrub_fact`` (so a value under a
    self-identifying key like ``api_key`` is fully redacted), every
    nested string list element runs through ``scrub_text``.

    Structural special-case for ``fact_save``: its args have shape
    ``{"key": "linkedin_password", "value": "..."}``. The ``key`` field
    NAMES the secret; the ``value`` field HOLDS it. Generic per-key
    scrubbing misses this because the dict key is literally ``"value"``,
    not a self-identifying name. Same shape that :func:`db.save_checkpoint`
    handles for ``tool_calls``.
    """
    if not args:
        return {}
    # The emitter integration passes a pre-stringified args preview
    # (``str(args)[:80]``) rather than the original dict. Scrub it as free
    # text and wrap so ``tool_start`` events still land on disk instead of
    # crashing on ``str.items()``.
    if isinstance(args, str):
        sv, _ = secret_scrub.scrub_text(args)
        return {"preview": sv}
    if not isinstance(args, dict):
        return {}
    out: dict[str, Any] = {}
    fact_save_key = None
    if tool_name == "fact_save" and isinstance(args.get("key"), str):
        fact_save_key = args["key"]
    for k, v in args.items():
        if fact_save_key is not None and k == "value" and isinstance(v, str):
            sv, _ = secret_scrub.scrub_fact(fact_save_key, v)
            out[k] = sv
            continue
        if isinstance(v, str):
            sv, _ = secret_scrub.scrub_fact(str(k), v)
            out[k] = sv
        elif isinstance(v, list):
            new_list = []
            for item in v:
                if isinstance(item, str):
                    si, _ = secret_scrub.scrub_text(item)
                    new_list.append(si)
                else:
                    new_list.append(item)
            out[k] = new_list
        else:
            out[k] = v
    return out


def _trajectory_dir() -> Path:
    """Resolve the directory where trajectory .jsonl files live.

    Lazy import of config so this module stays importable in tests that
    don't touch the agent runtime.
    """
    import config
    path = Path(config.DATA_DIR) / "trajectories"
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_enabled() -> bool:
    """Trajectory recording is opt-in. Reads ``trajectory_enabled``
    from config. Defensive — returns False on any error so a config
    glitch never accidentally enables recording."""
    try:
        import config
        return bool(config.get("trajectory_enabled"))
    except Exception:
        return False


@dataclass
class TrajectoryRecorder:
    """One recorder per run. Holds the file path + write lock + counters.

    Use as a context manager:

        rec = trajectory.start("chat", model="gpt-4o", thread_id="t_abc")
        # ... emit events ...
        rec.finish(cost_usd=0.01, tools_used=["read_file"])

    Or via ``with``:

        with trajectory.recording("chat", model=...) as rec:
            rec.event("custom", {"foo": "bar"})

    Auto-finalises on close (writes ``run_end`` even if the run
    crashed). All write paths are guarded — a broken file handle never
    crashes the calling agent.
    """
    run_id: str
    source: str
    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _start_ts: float = field(default_factory=time.time)
    _finished: bool = False
    _tool_count: int = 0
    _tools_used: list[str] = field(default_factory=list)

    def event(self, ev_type: str, payload: dict[str, Any] | None = None) -> None:
        """Append one event to the trajectory file. Never raises."""
        if self._finished:
            return
        line = {
            "ev": ev_type,
            "ts": time.time(),
        }
        if payload:
            line.update(payload)
        try:
            data = json.dumps(line, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as e:
            _log.debug(f"trajectory event serialisation failed: {e}")
            return
        try:
            with self._lock:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(data + "\n")
        except OSError as e:
            _log.debug(f"trajectory write failed: {e}")

    def tool_start(self, name: str, args: dict[str, Any] | None = None) -> None:
        self._tool_count += 1
        if name not in self._tools_used:
            self._tools_used.append(name)
        # Scrub before write — trajectory files persist 30 days by default.
        safe_args = _scrub_args(args, tool_name=name)
        self.event("tool_start", {"name": name, "args": safe_args})

    def tool_end(self, name: str, result: str, duration_ms: int = 0) -> None:
        # Truncate long results to keep files reasonable. Full result
        # is in the LLM message history anyway; trajectory is for audit.
        # Scrub after truncation (cheaper) — the patterns we care about
        # all fit within 200 chars, never mind 1000.
        raw = result or ""
        preview = raw[:1000]
        safe_preview, _ = secret_scrub.scrub_text(preview)
        self.event("tool_end", {
            "name": name,
            "result_preview": safe_preview,
            "result_len": len(raw),
            "duration_ms": duration_ms,
        })

    def content(self, text: str) -> None:
        if not text:
            return
        self.event("content", {"text": text})

    def thinking(self, text: str) -> None:
        if not text:
            return
        self.event("thinking", {"text": text})

    def turn(self, n: int, finish_reason: str | None = None) -> None:
        self.event("turn_end", {"turn": n, "finish_reason": finish_reason})

    def finish(self, *, cost_usd: float | None = None,
               tools_used: list[str] | None = None,
               prompt_tokens: int | None = None,
               completion_tokens: int | None = None,
               status: str = "ok",
               error: str | None = None) -> None:
        """Final event for the run. Idempotent — second call is a no-op."""
        if self._finished:
            return
        self._finished = True
        duration_ms = int((time.time() - self._start_ts) * 1000)
        payload = {
            "status": status,
            "duration_ms": duration_ms,
            "tool_count": self._tool_count,
            "tools_used": tools_used or self._tools_used,
        }
        if cost_usd is not None:
            payload["cost_usd"] = cost_usd
        if prompt_tokens is not None:
            payload["prompt_tokens"] = prompt_tokens
        if completion_tokens is not None:
            payload["completion_tokens"] = completion_tokens
        if error:
            payload["error"] = error
        self._finished = False  # temporarily reopen so event() writes
        self.event("run_end", payload)
        self._finished = True

    # Context-manager interface ------------------------------------------

    def __enter__(self) -> "TrajectoryRecorder":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if not self._finished:
            if exc_type is not None:
                self.finish(status="error", error=f"{exc_type.__name__}: {exc_val}")
            else:
                self.finish()


# ── Public factories ────────────────────────────────────────────────────────


# Reserved characters that could break filesystem layout — replaced with
# underscore in run_id. Keep the filter strict; we own the run_id format.
# Dots are excluded so ``..`` sequences can't sneak through and the
# generated filename never has a hidden-file prefix.
_SAFE_RUN_ID_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


def _sanitize_run_id(run_id: str) -> str:
    """Replace anything outside ``[A-Za-z0-9_-]`` with ``_``. Strips
    leading underscores from the result, then trims to 64 chars. Empty
    input falls back to a timestamped placeholder.
    """
    if not run_id:
        return f"run_{int(time.time()*1000)}"
    sanitized = "".join(c if c in _SAFE_RUN_ID_CHARS else "_"
                        for c in run_id)
    sanitized = sanitized.lstrip("_")[:64]
    return sanitized or f"run_{int(time.time()*1000)}"


def start(source: str, *, run_id: str | None = None,
          model: str | None = None,
          thread_id: str | None = None,
          goal_id: str | None = None,
          extra: dict[str, Any] | None = None) -> TrajectoryRecorder | None:
    """Begin a new trajectory recording.

    Returns a ``TrajectoryRecorder`` to feed events into, or ``None``
    when recording is disabled. Callers can safely chain
    ``rec = trajectory.start(...)``; subsequent ``if rec is not None``
    guards are NOT required — every method on a None-coalescing wrapper
    is a no-op when off.

    For ergonomic call sites that don't want None guards, use the
    ``recording(...)`` context manager which returns a no-op stub
    when disabled.
    """
    if not is_enabled():
        return None
    rid = _sanitize_run_id(run_id or f"{source}_{int(time.time()*1000)}")
    path = _trajectory_dir() / f"{rid}.jsonl"
    rec = TrajectoryRecorder(run_id=rid, source=source, path=path)
    payload = {
        "source": source,
        "run_id": rid,
    }
    if model:
        payload["model"] = model
    if thread_id:
        payload["thread_id"] = thread_id
    if goal_id:
        payload["goal_id"] = goal_id
    if extra:
        payload.update(extra)
    rec.event("run_start", payload)
    return rec


# Sentinel: no-op recorder used when trajectory recording is disabled.
# Calls to its methods do nothing. Lets callers stop checking for None.
class _NullRecorder:
    """No-op stand-in for TrajectoryRecorder when recording is disabled."""
    run_id = ""
    source = ""
    path = None

    def event(self, *_a, **_kw) -> None:
        return None

    def tool_start(self, *_a, **_kw) -> None:
        return None

    def tool_end(self, *_a, **_kw) -> None:
        return None

    def content(self, *_a, **_kw) -> None:
        return None

    def thinking(self, *_a, **_kw) -> None:
        return None

    def turn(self, *_a, **_kw) -> None:
        return None

    def finish(self, *_a, **_kw) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_a) -> None:
        return None


def recording(source: str, **kwargs) -> "TrajectoryRecorder | _NullRecorder":
    """Context-manager form. Use inside ``with`` to auto-finalise on
    exit, even on exception. Returns a ``_NullRecorder`` when disabled
    so the calling code doesn't have to check.

        with trajectory.recording("chat", model="gpt-4o") as rec:
            rec.tool_start("read_file", {"path": "/etc/hostname"})
            rec.tool_end("read_file", "macbook", duration_ms=12)
    """
    rec = start(source, **kwargs)
    return rec if rec is not None else _NullRecorder()


# ── Replay / introspection ──────────────────────────────────────────────────


def list_runs(*, limit: int = 50) -> list[dict[str, Any]]:
    """List recorded trajectories, newest first.

    Returns dicts with ``{run_id, source, path, size_bytes, mtime}``.
    The full content stays on disk; callers read with ``load_run`` for
    one specific file.
    """
    try:
        d = _trajectory_dir()
    except Exception:
        return []
    files = []
    for p in d.glob("*.jsonl"):
        try:
            st = p.stat()
            files.append({
                "run_id": p.stem,
                "path": str(p),
                "size_bytes": st.st_size,
                "mtime": st.st_mtime,
            })
        except OSError:
            continue
    files.sort(key=lambda f: f["mtime"], reverse=True)
    return files[:limit]


def load_run(run_id: str) -> list[dict[str, Any]]:
    """Read a trajectory file back as a list of event dicts.

    Skips malformed lines silently (defensive — a half-written event
    from a crash shouldn't fail the read). Returns empty list if the
    file doesn't exist.
    """
    rid = _sanitize_run_id(run_id)
    path = _trajectory_dir() / f"{rid}.jsonl"
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        _log.debug(f"trajectory read failed: {e}")
    return events


def prune_old(days: int = 30) -> int:
    """Delete trajectory files older than ``days``. Returns count removed.

    Called on a schedule (separate job) — not from inside the recorder
    so a long-running agent never blocks on file deletes.
    """
    try:
        d = _trajectory_dir()
    except Exception:
        return 0
    cutoff = time.time() - (days * 86400)
    removed = 0
    for p in d.glob("*.jsonl"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ── Emitter integration ─────────────────────────────────────────────────────


def attach_to_emitter(emitter, recorder: TrajectoryRecorder) -> None:
    """Wire a recorder up to an ``agent_events.EventEmitter`` so every
    event it emits also lands in the trajectory file.

    The emitter event types map to trajectory events:
      tool_start    → tool_start (with name + args)
      tool_end      → tool_end (with name, result_preview, duration_ms)
      content_delta → content
      thinking_delta → thinking
      turn_end      → turn

    Other event types (status, error, budget_warning) are written as
    generic event entries with their ``data`` payload.
    """
    if recorder is None or isinstance(recorder, _NullRecorder):
        return

    def _on_event(ev) -> None:
        t = ev.type
        d = ev.data or {}
        if t == "tool_start":
            recorder.tool_start(d.get("name", "?"), d.get("args"))
        elif t == "tool_end":
            recorder.tool_end(d.get("name", "?"), d.get("result", ""),
                              d.get("duration_ms", 0))
        elif t == "content_delta":
            recorder.content(d.get("text", ""))
        elif t == "thinking_delta":
            recorder.thinking(d.get("text", ""))
        elif t == "turn_end":
            recorder.turn(d.get("turn", 0), d.get("finish_reason"))
        else:
            # Generic — keep the type as-is, attach the payload
            recorder.event(t, d)

    emitter.on_all(_on_event)

"""Server-broadcast notification WS events must NOT open a ghost
"generating" assistant bubble in the web UI.

User report: a chat was idle, the agent's last message had been sent,
everything looked done — and suddenly at "castor 09:39 PM" a phantom
"generating" status appeared. Root cause: server's scheduled-task
runner broadcasts a ``cron`` WS event to ALL clients regardless of
which thread is in view, and ``static/index.html::handleWsMessage``
had no short-circuit for it. The event fell through to the
streaming-message creation gate (``if (!state.streaming && t !==
'status')``) which opens a pending assistant bubble that NEVER closes
(no ``done`` event follows a broadcast notification — it isn't a turn).

Same class of bug fixed for ``task_update`` in v0.18.3 and for
``canvas_render`` shortly after. This pins the contract for all
remaining notification types so a future refactor can't lose any
of them.

JS contract test (per the existing pattern: ``test_api_helper_disables
_http_cache``, ``test_reload_path_runs_meta_files_through_splitfiles``):
reads static/index.html, anchors on the streaming gate, asserts that
every broadcast-only event type the server emits has a short-circuit
BEFORE the gate.
"""
from __future__ import annotations

from pathlib import Path


def _index_html() -> str:
    return (
        Path(__file__).resolve().parent.parent / "static" / "index.html"
    ).read_text(encoding="utf-8")


def _server_py() -> str:
    return (
        Path(__file__).resolve().parent.parent / "server.py"
    ).read_text(encoding="utf-8")


# The streaming-gate anchor — the line that opens a phantom assistant
# bubble when a non-chat event slips past the short-circuits. Any
# notification type emitted by the server MUST be handled (return)
# BEFORE this line.
_STREAMING_GATE_ANCHOR = "if (!state.streaming && t !== 'status') {"


def test_streaming_gate_anchor_exists():
    """Smoke: anchor still in handleWsMessage. If this fails, the JS
    was refactored and the rest of this file needs updating too."""
    src = _index_html()
    assert src.count(_STREAMING_GATE_ANCHOR) == 1, (
        "streaming-message creation gate moved or duplicated; update "
        "_STREAMING_GATE_ANCHOR in this test."
    )


def _short_circuits_before_gate() -> str:
    """Slice of handleWsMessage from `const handleWsMessage` to the
    streaming gate — everything in this window must handle a known
    notification type or fall through harmlessly."""
    src = _index_html()
    start = src.find("const handleWsMessage = (msg) => {")
    end = src.find(_STREAMING_GATE_ANCHOR)
    assert start >= 0 and end > start
    return src[start:end]


# ── Each notification type emitted by server.py to ALL WS clients ──
# must have an explicit short-circuit in handleWsMessage before the
# streaming gate. The list is sourced from the actual _broadcast call
# sites in server.py (grep -n '"type":' server.py).

# Notification types — broadcast to all clients, not a chat turn for
# any specific thread. Every one of these must short-circuit before
# the gate or it will produce a ghost "generating" bubble.
_NOTIFICATION_TYPES = [
    "task_update",          # background pipeline progress
    "cron",                 # scheduled task fired
    "compaction",           # memory compaction lifecycle
    "update_progress",      # self-update in progress
    "update_done",          # self-update finished
    "telegram",             # echo of a turn that came via Telegram bot
    "canvas_render",        # canvas skill render (has its own panel)
    "canvas_close",         # canvas skill close
    "knowledge_progress",   # KB ingestion progress
    "knowledge_gpu_warning",
    "knowledge_done",
    "get_frame",            # camera_capture frame request
    "frame_request",        # legacy alias
]


def test_every_notification_type_short_circuits_before_streaming_gate():
    """Pin: each broadcast notification type returns early from
    handleWsMessage BEFORE the streaming-message gate, so it doesn't
    open a ghost assistant bubble."""
    window = _short_circuits_before_gate()
    missing = []
    for t in _NOTIFICATION_TYPES:
        # The short-circuit takes the form `if (t === 'TYPE')` or
        # `if (msg.event === 'TYPE')` (interrupted_turn uses event).
        candidates = (
            f"t === '{t}'",
            f"msg.event === '{t}'",
            f't === "{t}"',
            f'msg.event === "{t}"',
        )
        if not any(c in window for c in candidates):
            missing.append(t)
    assert not missing, (
        "Notification WS types without a short-circuit in handleWsMessage: "
        f"{missing}. Each will create a phantom 'generating' assistant "
        "bubble that never closes (no `done` event follows a broadcast). "
        "Add an `if (t === '<type>') { ... return; }` block in "
        "static/index.html before the streaming-gate anchor "
        f"({_STREAMING_GATE_ANCHOR!r})."
    )


def test_cron_short_circuit_skips_system_internal_jobs():
    """The cron handler shouldn't toast for `__synthesis_continuous__`
    / `__heartbeat__` / other system internal jobs (names starting
    with `__`). Otherwise the user gets a toast every 15 min from
    their own background curator."""
    window = _short_circuits_before_gate()
    cron_branch_idx = window.find("if (t === 'cron'")
    assert cron_branch_idx >= 0
    cron_window = window[cron_branch_idx: cron_branch_idx + 400]
    assert "startsWith('__')" in cron_window, (
        "cron handler doesn't filter system-internal job names — toasts "
        "from synthesis_continuous / heartbeat will spam the user every "
        "few minutes."
    )


def test_server_notification_types_match_test_inventory():
    """Audit guard: if server.py adds a new ``_broadcast({"type":
    "..."})`` site, this test catches it so the client-side short-
    circuit list stays in sync.

    We grep server.py for all literal ``"type": "..."`` values inside
    payloads handed to ``_broadcast``. Compare against
    _NOTIFICATION_TYPES + the legitimate chat-turn types (which DO
    open the streaming gate).
    """
    import re
    src = _server_py()
    # Find all `_broadcast({...})` and `stream_queue.put({...})` calls.
    # The type literal is captured. This is best-effort — multi-line
    # payloads need a slightly different regex but the common shapes
    # in server.py are single-line ``"type": "name"`` after the brace.
    type_re = re.compile(r'"type":\s*"([a-z_]+)"')
    found = set(type_re.findall(src))

    # Chat-turn types — these legitimately open the streaming bubble.
    # The streaming gate is FOR them.
    chat_types = {
        "status", "thinking_delta", "content_delta", "tool_call",
        "recall", "reply", "error",
    }

    known = set(_NOTIFICATION_TYPES) | chat_types | {
        # Direct-send (per-client, not broadcast) types — these reach
        # specific clients via ``await ws.send_json`` rather than
        # ``_broadcast``. Inventoried here for completeness.
        "thinking",      # alias for thinking_delta in some paths
        "content",       # alias for content_delta
        # Telemetry-internal / not customer-visible:
        "dir", "file",   # directory/file listings inside /api/files
    }

    unknown = found - known
    assert not unknown, (
        f"Server emits these WS types that this test doesn't classify "
        f"as chat-turn or notification: {unknown}. Either add them to "
        f"_NOTIFICATION_TYPES (and add a short-circuit in "
        f"static/index.html) or to the `chat_types`/`known` set in "
        f"this test (if they legitimately belong to chat-turn flow)."
    )

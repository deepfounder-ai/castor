"""Anonymous telemetry — opt-in, transparent, audit-friendly.

# Privacy guarantees

1. **Default OFF.** Nothing leaves the machine until the user explicitly
   opts in via the first-run prompt or Settings → Privacy → Telemetry.
2. **No chat content.** No user input, no assistant replies, no thinking
   blocks, no thread titles, no message metadata that could contain
   user-typed text.
3. **No soul / personality.** Trait names, levels, custom traits — none
   of this is collected. The agent's persona is the user's design, not
   project metrics.
4. **No identifiers that could deanonymize.** No IP, hostname, username,
   API keys, file paths, exact model names (could be custom finetunes),
   provider URLs (could be internal corporate endpoints), specific
   skill names (user-created skills could leak company identity), or
   tool-call args / results.
5. **Anonymous user ID** is a random UUID generated once at opt-in,
   stored locally in `kv` table. Never derived from any PII. User can
   reset it any time without disabling telemetry.
6. **Allowed-events whitelist.** Every event name and its schema are
   declared in `ALLOWED_EVENTS` below. Unknown events are dropped with
   a warning. Schemas pin the type of every property so a future
   refactor can't accidentally add a string-valued field that smuggles
   chat text.
7. **All collection goes through `track_event()`.** Easy to audit by
   grepping the codebase for `telemetry.track_event`. There is no
   alternate path — no direct queue access from outside this module.
8. **Inert until endpoint configured.** If `telemetry_endpoint` setting
   is empty, the module collects events into the local queue but never
   sends anything over the network. Users who want self-hosted analytics
   can point this at their own collector (PostHog / Plausible / custom).
   The project doesn't ship a default endpoint until the privacy policy
   is signed off.

See `docs/PRIVACY.md` for the human-readable version of this contract.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable

import config

_log = logging.getLogger("qwe.telemetry")

# ── Whitelist of allowed events ──────────────────────────────────────
#
# Each entry: event name → {prop_name: prop_type}.
# Validation is type-strict — `int`, `str`, `bool`, `float`, `list`, `dict`.
# Lists / dicts are checked one level deep (no recursive type-check) but
# the OUTER schema lock prevents arbitrary fields from sneaking in.
#
# Adding an event here is a deliberate act. Code review should ask:
# - Could this prop value contain user-typed text? (Reject.)
# - Could it contain a path / URL / identifier that ties back to the
#   user's environment beyond OS + version? (Reject or anonymize.)
# - Is the cardinality bounded? (String values should be enums, not
#   free text.)

ALLOWED_EVENTS: dict[str, dict[str, type]] = {
    "session_start": {
        "qwe_version": str,        # e.g. "0.18.4" — already public on GitHub
        "python_version": str,     # e.g. "3.12.10"
        "os": str,                 # "linux" / "macos" / "windows"
        # Provider KIND only — never the URL (could be internal corp endpoint)
        # Allowed values: lmstudio / ollama / openai / azure / bedrock /
        # groq / openrouter / deepseek / together / unknown
        "provider_kind": str,
        # Bucketed model size — never the exact model id (could be a
        # custom finetune that uniquely identifies the org)
        # Allowed values: small (<=4B) / medium (4-13B) / large (>13B) /
        # unknown
        "model_size_bucket": str,
        # Boolean feature flags — what's *enabled*, not what's used
        "has_web_ui": bool,
        "has_telegram": bool,
        "has_voice": bool,
        "has_camera": bool,
        "has_scheduler": bool,
        "has_mcp": bool,
        # Counts only — never names. Three skills, not "acme_invoice_proc".
        "active_skills_count": int,
        "scheduled_jobs_count": int,
        "indexed_sources_count": int,
    },
    "turn_complete": {
        "duration_ms": int,
        "rounds": int,
        # CATEGORIES of tools used (memory / files / shell / browser /
        # http / vision / voice / automation / skills / orchestration),
        # never the specific tool names. Keeps cardinality bounded and
        # avoids leaking custom-skill names.
        "tool_categories_used": list,  # list[str], values from a fixed set
        "tool_calls_count": int,
        "tool_errors_count": int,
        "input_tokens": int,
        "output_tokens": int,
        "context_hits": int,           # number of memory-recall items injected
        # Surface where the turn came from
        "source": str,                  # "web" / "cli" / "telegram" / "scheduler"
    },
    "tool_error": {
        # Category, not specific tool name. Same set as
        # tool_categories_used above.
        "tool_category": str,
        # Error class, not the message text (which could include args /
        # paths / user content). Set: timeout / exception /
        # validation_failed / rate_limited / aborted / blocked
        "error_kind": str,
    },
    "skill_creator_pipeline": {
        # outcome: success / syntax_error / smoke_fail / validate_fail /
        # max_attempts_exhausted / aborted
        "outcome": str,
        "attempts": int,
        "duration_ms": int,
        # Tools count in the GENERATED skill — not their names
        "tools_count": int,
    },
    "feature_first_use": {
        # Tracks first-time activation of a feature in this session, so
        # we can see what users actually try. Single string value from a
        # fixed enum: camera_capture / live_voice / telegram_send /
        # scheduler_create / skill_create / browser_visible / mcp_add /
        # preset_activate / knowledge_index_url / knowledge_index_file
        "feature": str,
    },
}

# Categories used for tool_categories_used and tool_error.tool_category.
# Bound enum prevents free-text leakage of skill / tool names.
TOOL_CATEGORIES = frozenset({
    "memory", "files", "shell", "http", "browser", "vision", "voice",
    "automation", "skills", "orchestration", "vault", "rag", "other",
})

# Error kinds for tool_error.error_kind.
ERROR_KINDS = frozenset({
    "timeout", "exception", "validation_failed", "rate_limited",
    "aborted", "blocked", "not_found", "unauthorized", "other",
})

# Sources for turn_complete.source.
SOURCES = frozenset({"web", "cli", "telegram", "scheduler", "other"})

# Provider kinds.
PROVIDER_KINDS = frozenset({
    "lmstudio", "ollama", "openai", "azure", "bedrock", "groq",
    "openrouter", "deepseek", "together", "unknown",
})

# Model size buckets.
MODEL_SIZE_BUCKETS = frozenset({"small", "medium", "large", "unknown"})

# Outcomes for skill_creator_pipeline.
PIPELINE_OUTCOMES = frozenset({
    "success", "syntax_error", "smoke_fail", "validate_fail",
    "max_attempts_exhausted", "aborted",
})

# Features for feature_first_use.
FEATURES = frozenset({
    "camera_capture", "live_voice", "telegram_send",
    "scheduler_create", "skill_create", "browser_visible",
    "mcp_add", "preset_activate", "knowledge_index_url",
    "knowledge_index_file",
})

# Per-property enum constraints — additional check beyond type
_ENUM_CONSTRAINTS: dict[tuple[str, str], frozenset] = {
    ("session_start", "provider_kind"): PROVIDER_KINDS,
    ("session_start", "model_size_bucket"): MODEL_SIZE_BUCKETS,
    ("turn_complete", "source"): SOURCES,
    ("tool_error", "tool_category"): TOOL_CATEGORIES,
    ("tool_error", "error_kind"): ERROR_KINDS,
    ("skill_creator_pipeline", "outcome"): PIPELINE_OUTCOMES,
    ("feature_first_use", "feature"): FEATURES,
}

# ── Module state ─────────────────────────────────────────────────────

# Bounded queue — hard cap so a never-flushed install can't grow
# unbounded memory / disk usage.
_MAX_QUEUE = 1000
_queue: deque[dict] = deque(maxlen=_MAX_QUEUE)
_queue_lock = threading.Lock()

# Per-process session id, regenerated each start. Lets the receiver
# group events from one run without persisting any cross-session id
# beyond the user's anonymous_id.
_SESSION_ID = uuid.uuid4().hex

# ── Public API ───────────────────────────────────────────────────────


def enabled() -> bool:
    """Is telemetry enabled? Default False. Authoritative check used by
    track_event() to short-circuit before any work is done."""
    val = config.get("telemetry_enabled")
    return bool(val)


def anonymous_id() -> str:
    """Get the user's anonymous id, generating one on first call.

    Generated on first opt-in, persisted in `kv` table, never derived
    from any PII. User can call `reset_anonymous_id()` to rotate it
    without re-opting-in.
    """
    aid = config.get("telemetry_anonymous_id") or ""
    if not aid:
        aid = uuid.uuid4().hex
        config.set("telemetry_anonymous_id", aid)
    return aid


def session_id() -> str:
    """Per-process session id. Resets on every qwe-qwe start."""
    return _SESSION_ID


def opt_in() -> str:
    """Enable telemetry + ensure anonymous_id exists. Returns the id.

    Called by the first-run prompt or the Settings → Privacy toggle.
    Idempotent — safe to call repeatedly.
    """
    aid = anonymous_id()  # generates if missing
    config.set("telemetry_enabled", 1)
    _log.info("telemetry enabled (anonymous_id=%s)", aid[:8] + "...")
    return aid


def opt_out() -> None:
    """Disable telemetry and drop any queued events. Anonymous id is
    NOT deleted by default — keeping it lets a future re-opt-in stay
    consistent. Use `forget_me()` to also wipe the id."""
    config.set("telemetry_enabled", 0)
    with _queue_lock:
        dropped = len(_queue)
        _queue.clear()
    _log.info("telemetry disabled (%d queued events dropped)", dropped)


def forget_me() -> None:
    """Disable telemetry, drop queue, and wipe the anonymous id.

    Stronger than opt_out(): the next opt-in (if any) will get a fresh
    id, so nothing ties the two periods of opt-in together.
    """
    opt_out()
    config.set("telemetry_anonymous_id", "")
    _log.info("telemetry: anonymous_id wiped")


def reset_anonymous_id() -> str:
    """Generate a fresh anonymous id without changing the enabled flag.

    For users who want to "start over" without going through opt-out /
    opt-in. Useful if they fear correlation across long timeframes.
    """
    aid = uuid.uuid4().hex
    config.set("telemetry_anonymous_id", aid)
    _log.info("telemetry anonymous_id rotated")
    return aid


def track_event(name: str, props: dict | None = None) -> bool:
    """Add an event to the queue. Returns True if accepted.

    No-op (returns False) when:
    - telemetry is disabled (default)
    - event name is not in ALLOWED_EVENTS
    - any prop has a wrong type
    - any enum-constrained prop has a value outside its allowed set
    - tool_categories_used contains a category outside TOOL_CATEGORIES

    The strict validation is the audit point: if you're reading this
    code wondering "could a future bug leak chat content via this
    track_event call?", the answer is no — only declared props pass,
    only declared types match, and the enum constraints lock the
    string values down to bounded sets.
    """
    if not enabled():
        return False
    if name not in ALLOWED_EVENTS:
        _log.warning("telemetry: dropping unknown event %r", name)
        return False

    schema = ALLOWED_EVENTS[name]
    props = props or {}

    # Reject any extra keys
    extra = set(props.keys()) - set(schema.keys())
    if extra:
        _log.warning("telemetry: dropping event %r with extra keys %s", name, extra)
        return False

    # Type-check each declared prop
    cleaned: dict[str, Any] = {}
    for prop_name, prop_type in schema.items():
        if prop_name not in props:
            # Missing prop is allowed — schema is the upper bound, not
            # the requirement set. Skip and let the receiver handle.
            continue
        val = props[prop_name]
        if not isinstance(val, prop_type):
            _log.warning(
                "telemetry: dropping event %r — prop %r expected %s, got %s",
                name, prop_name, prop_type.__name__, type(val).__name__,
            )
            return False
        # Enum constraint
        constraint = _ENUM_CONSTRAINTS.get((name, prop_name))
        if constraint is not None and val not in constraint:
            _log.warning(
                "telemetry: dropping event %r — prop %r value %r not in allowed set",
                name, prop_name, val,
            )
            return False
        # List-of-strings check for tool_categories_used
        if isinstance(val, list) and prop_name == "tool_categories_used":
            if not all(isinstance(c, str) for c in val):
                _log.warning("telemetry: tool_categories_used must be list[str]")
                return False
            invalid = [c for c in val if c not in TOOL_CATEGORIES]
            if invalid:
                _log.warning(
                    "telemetry: dropping event — invalid categories %s",
                    invalid,
                )
                return False
        cleaned[prop_name] = val

    # Wrap with common metadata. anonymous_id is generated on first
    # access if missing — but we already checked enabled() above, and
    # opt_in() would have set it.
    event = {
        "event": name,
        "anonymous_id": anonymous_id(),
        "session_id": _SESSION_ID,
        "ts": time.time(),
        "props": cleaned,
    }

    with _queue_lock:
        _queue.append(event)
    return True


def get_pending_events() -> list[dict]:
    """Snapshot of the current queue. For UI inspection — lets the user
    see what's actually queued before they hit "send"."""
    with _queue_lock:
        return list(_queue)


def queue_size() -> int:
    """Cheap count without copying the queue."""
    with _queue_lock:
        return len(_queue)


def clear_queue() -> int:
    """Drop everything in the queue without sending. Returns dropped count."""
    with _queue_lock:
        n = len(_queue)
        _queue.clear()
    return n


def flush(send_fn: Callable[[list[dict]], bool] | None = None) -> int:
    """Send the queue to `telemetry_endpoint`. Returns the number of
    events successfully sent (0 if disabled, no endpoint, or send fails).

    `send_fn` parameter is for tests — production path uses the
    built-in HTTP POST. If endpoint is empty, returns 0 without doing
    anything. Queue is cleared only on a 2xx response.

    Currently always 0 in production: no endpoint default, no built-in
    sender. Wire-up lands in a follow-up commit so this PR ships
    foundation + opt-in only.
    """
    if not enabled():
        return 0
    endpoint = (config.get("telemetry_endpoint") or "").strip()
    if not endpoint:
        return 0  # No endpoint configured → silent no-op
    with _queue_lock:
        events = list(_queue)
    if not events:
        return 0
    sender = send_fn or _default_sender
    if sender(events):
        with _queue_lock:
            # Remove only the events we sent — newer events that
            # arrived during the network call stay in the queue
            for _ in range(min(len(events), len(_queue))):
                _queue.popleft()
        return len(events)
    return 0


def _default_sender(events: list[dict]) -> bool:
    """Built-in HTTP sender. Stub for now — real implementation lands
    in the wire-up commit. Returns False so the queue isn't cleared."""
    _log.debug("telemetry: would send %d events to %s",
               len(events), config.get("telemetry_endpoint"))
    return False


# ── Helpers for callers that need to bucket sensitive values ─────────


def bucket_model_size(param_count_b: float | None) -> str:
    """Map a model parameter count (in billions) to a coarse bucket.

    Use this anywhere you have an exact model id but don't want to send
    it. Cardinality of the output is fixed to 4 values, so it can't
    deanonymize.
    """
    if param_count_b is None:
        return "unknown"
    if param_count_b <= 4:
        return "small"
    if param_count_b <= 13:
        return "medium"
    return "large"


def os_kind() -> str:
    """OS string in the SOURCES enum format."""
    p = sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "macos"
    if p.startswith("win"):
        return "windows"
    return "other"


def python_version() -> str:
    """Python version as 'major.minor.patch' (no build / compiler info)."""
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"

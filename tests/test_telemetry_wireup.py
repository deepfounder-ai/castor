"""Wire-up tests for telemetry — assert each call site fires the right event
with valid props that pass the strict whitelist validator.

These tests pin the *integration* contract: that each of the five wire-up
sites in Stage 2 invokes `telemetry.track_event` with an allowed event name
and a payload that the validator accepts. They don't drive real LLM calls
or hit the network — every external dependency is mocked, and the queue is
inspected directly via `telemetry.get_pending_events()`.

If a future refactor moves a wire-up site or widens its props past the
schema, the matching test here fails and surfaces the regression before it
ships.
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture
def fresh_tel(qwe_temp_data_dir):
    """Reload telemetry against a clean CASTOR_DATA_DIR + opt the user in.

    Mirrors the pattern in tests/test_telemetry.py — every test starts
    with telemetry ENABLED so wire-up sites actually emit events. Without
    this fixture, the default-OFF guard short-circuits track_event() and
    no event ever lands in the queue.
    """
    if "telemetry" in sys.modules:
        importlib.reload(sys.modules["telemetry"])
    import telemetry as t
    t.clear_queue()
    # Reset session-level dedup so each test starts fresh
    t._FEATURES_USED_THIS_SESSION.clear()
    t.opt_in()
    return t


# ── session_start ─────────────────────────────────────────────────────


def test_session_start_event_passes_validator(fresh_tel, monkeypatch):
    """The session_start emitter (`server._emit_session_start_telemetry`)
    builds a payload that the validator accepts."""
    # Stub external state so the helper doesn't try real network / real
    # filesystem checks.
    import server
    monkeypatch.setattr(
        server.providers, "get_active_name", lambda: "lmstudio", raising=False
    )

    server._emit_session_start_telemetry(source="web")

    pending = fresh_tel.get_pending_events()
    matching = [e for e in pending if e["event"] == "session_start"]
    assert len(matching) == 1, f"expected 1 session_start, got {len(matching)}"
    props = matching[0]["props"]
    assert props["provider_kind"] == "lmstudio"
    assert props["model_size_bucket"] in {"small", "medium", "large", "unknown"}
    assert props["os"] in {"linux", "macos", "windows", "other"}
    # Counts must be ints and bounded sane (we're on an empty test DB)
    assert isinstance(props["active_skills_count"], int)
    assert isinstance(props["scheduled_jobs_count"], int)
    assert isinstance(props["indexed_sources_count"], int)
    # Boolean flags are real bools
    for flag in ("has_web_ui", "has_telegram", "has_voice", "has_camera",
                 "has_scheduler", "has_mcp"):
        assert isinstance(props[flag], bool), f"{flag} should be bool"


def test_session_start_unknown_provider_collapses_to_unknown(fresh_tel, monkeypatch):
    """A provider name outside PROVIDER_KINDS (e.g. user-added 'mycorp')
    must collapse to 'unknown' — it must NOT leak as a free-text value."""
    import server
    monkeypatch.setattr(
        server.providers, "get_active_name",
        lambda: "mycorp_internal", raising=False,
    )
    server._emit_session_start_telemetry(source="web")
    pending = [e for e in fresh_tel.get_pending_events()
               if e["event"] == "session_start"]
    assert pending and pending[0]["props"]["provider_kind"] == "unknown"


# ── turn_complete ─────────────────────────────────────────────────────


def test_turn_complete_emits_categories_not_tool_names(fresh_tel):
    """The agent turn_complete emitter maps tool *names* to bounded
    *categories*. A custom skill name like 'acme_invoicing' must never
    appear in tool_categories_used — it gets bucketed as 'skills'."""
    import agent
    agent._emit_turn_complete_telemetry(
        duration_ms=1234,
        rounds=3,
        tool_calls_made=["read_file", "write_file", "shell",
                         "memory_search", "acme_invoicing_custom"],
        tool_errors_count=0,
        input_tokens=500, output_tokens=120, context_hits=2,
        source="web",
    )
    pending = [e for e in fresh_tel.get_pending_events()
               if e["event"] == "turn_complete"]
    assert len(pending) == 1
    cats = pending[0]["props"]["tool_categories_used"]
    # Custom skill bucketed as "skills" — its raw name must NOT leak
    assert "acme_invoicing_custom" not in cats
    assert "skills" in cats
    # Built-ins map to known categories
    assert "files" in cats   # read_file + write_file
    assert "shell" in cats
    assert "memory" in cats
    # Categories are deduped — files appears once, not twice
    assert cats.count("files") == 1
    # All categories are in the bounded enum
    import telemetry
    for c in cats:
        assert c in telemetry.TOOL_CATEGORIES


def test_turn_complete_unknown_source_maps_to_other(fresh_tel):
    """A `source` outside SOURCES enum collapses to 'other' so the
    validator accepts it — never leaks free-text."""
    import agent
    agent._emit_turn_complete_telemetry(
        duration_ms=100, rounds=1, tool_calls_made=[],
        tool_errors_count=0, input_tokens=0, output_tokens=0,
        context_hits=0, source="my_secret_admin_panel",
    )
    pending = [e for e in fresh_tel.get_pending_events()
               if e["event"] == "turn_complete"]
    assert pending and pending[0]["props"]["source"] == "other"


def test_turn_complete_no_event_when_disabled(fresh_tel):
    """When telemetry is opted-out, the helper short-circuits cleanly."""
    fresh_tel.opt_out()
    import agent
    # Should not raise; should not emit anything (queue cleared by opt_out)
    agent._emit_turn_complete_telemetry(
        duration_ms=1, rounds=1, tool_calls_made=["read_file"],
        tool_errors_count=0, input_tokens=0, output_tokens=0,
        context_hits=0, source="web",
    )
    assert fresh_tel.queue_size() == 0


# ── tool_error ────────────────────────────────────────────────────────


def test_tool_error_classifies_timeout(fresh_tel):
    """subprocess.TimeoutExpired classifies as 'timeout', not 'exception'."""
    import subprocess
    import agent_loop
    kind = agent_loop._classify_tool_error(
        subprocess.TimeoutExpired(cmd="x", timeout=5)
    )
    assert kind == "timeout"


def test_tool_error_classifies_keyboard_interrupt_as_aborted(fresh_tel):
    import agent_loop
    assert agent_loop._classify_tool_error(KeyboardInterrupt()) == "aborted"


def test_tool_error_emits_category_not_tool_name(fresh_tel):
    """`_emit_tool_error_telemetry` sends the bounded category, not the
    raw tool name — even for custom skills."""
    import agent_loop
    agent_loop._emit_tool_error_telemetry("acme_corp_skill", "exception")
    agent_loop._emit_tool_error_telemetry("shell", "timeout")
    pending = [e for e in fresh_tel.get_pending_events()
               if e["event"] == "tool_error"]
    assert len(pending) == 2
    # Custom skill collapses to "skills" category — name doesn't leak
    cats = [e["props"]["tool_category"] for e in pending]
    assert "skills" in cats   # acme_corp_skill bucketed
    assert "shell" in cats
    # Error kinds preserved
    kinds = [e["props"]["error_kind"] for e in pending]
    assert "exception" in kinds
    assert "timeout" in kinds


# ── skill_creator_pipeline ───────────────────────────────────────────


def test_skill_creator_pipeline_success_emits_event(fresh_tel):
    """The success path of `_run_pipeline` emits outcome=success with
    the actual tools_count and a positive duration."""
    from skills import skill_creator
    start = time.time() - 1.0  # 1s ago
    skill_creator._emit_pipeline_telemetry(
        outcome="success", attempts=2, start_time=start, tools_count=5,
    )
    pending = [e for e in fresh_tel.get_pending_events()
               if e["event"] == "skill_creator_pipeline"]
    assert len(pending) == 1
    p = pending[0]["props"]
    assert p["outcome"] == "success"
    assert p["attempts"] == 2
    assert p["tools_count"] == 5
    assert p["duration_ms"] >= 1000  # at least the 1s sleep we faked


def test_skill_creator_pipeline_failure_outcomes_pass_validator(fresh_tel):
    """All declared failure outcomes (syntax_error / smoke_fail /
    validate_fail / max_attempts_exhausted) pass the validator."""
    from skills import skill_creator
    for outcome in ("syntax_error", "smoke_fail", "validate_fail",
                    "max_attempts_exhausted"):
        skill_creator._emit_pipeline_telemetry(
            outcome=outcome, attempts=3, start_time=time.time(),
            tools_count=0,
        )
    pending = [e for e in fresh_tel.get_pending_events()
               if e["event"] == "skill_creator_pipeline"]
    assert len(pending) == 4
    outcomes = {e["props"]["outcome"] for e in pending}
    assert outcomes == {"syntax_error", "smoke_fail", "validate_fail",
                        "max_attempts_exhausted"}


def test_skill_creator_pipeline_unknown_outcome_collapses(fresh_tel):
    """An outcome string outside PIPELINE_OUTCOMES collapses to
    'max_attempts_exhausted' so a typo can't smuggle a free-text value."""
    from skills import skill_creator
    skill_creator._emit_pipeline_telemetry(
        outcome="something_weird", attempts=1, start_time=time.time(),
        tools_count=0,
    )
    pending = [e for e in fresh_tel.get_pending_events()
               if e["event"] == "skill_creator_pipeline"]
    assert pending and pending[0]["props"]["outcome"] == "max_attempts_exhausted"


# ── feature_first_use ────────────────────────────────────────────────


def test_feature_first_use_fires_once_per_session(fresh_tel):
    """Calling `track_feature_first_use` repeatedly with the same feature
    only enqueues one event per process (until the session set is reset)."""
    assert fresh_tel.track_feature_first_use("camera_capture") is True
    assert fresh_tel.track_feature_first_use("camera_capture") is False
    assert fresh_tel.track_feature_first_use("camera_capture") is False
    pending = [e for e in fresh_tel.get_pending_events()
               if e["event"] == "feature_first_use"]
    assert len(pending) == 1
    assert pending[0]["props"]["feature"] == "camera_capture"


def test_feature_first_use_per_feature_dedup(fresh_tel):
    """Different features each fire once — they don't share dedup state."""
    assert fresh_tel.track_feature_first_use("camera_capture") is True
    assert fresh_tel.track_feature_first_use("live_voice") is True
    assert fresh_tel.track_feature_first_use("scheduler_create") is True
    pending = [e for e in fresh_tel.get_pending_events()
               if e["event"] == "feature_first_use"]
    assert len(pending) == 3
    features = {e["props"]["feature"] for e in pending}
    assert features == {"camera_capture", "live_voice", "scheduler_create"}


def test_feature_first_use_rejects_unknown_feature(fresh_tel):
    """A feature name outside FEATURES is rejected before reaching the
    queue — defense in depth so a buggy caller can't smuggle free text."""
    accepted = fresh_tel.track_feature_first_use("export_user_secrets")
    assert accepted is False
    pending = [e for e in fresh_tel.get_pending_events()
               if e["event"] == "feature_first_use"]
    assert len(pending) == 0


def test_feature_first_use_noop_when_disabled(fresh_tel):
    """opt_out makes track_feature_first_use a silent no-op."""
    fresh_tel.opt_out()
    assert fresh_tel.track_feature_first_use("camera_capture") is False


# ── helpers ──────────────────────────────────────────────────────────


def test_provider_kind_from_name_collapses_unknown_to_unknown(fresh_tel):
    """User-added custom provider names collapse to 'unknown' rather
    than leaking the raw name."""
    assert fresh_tel.provider_kind_from_name("mycorp_internal_llm") == "unknown"
    assert fresh_tel.provider_kind_from_name("openai") == "openai"
    assert fresh_tel.provider_kind_from_name(None) == "unknown"
    assert fresh_tel.provider_kind_from_name("") == "unknown"


def test_category_for_tool_unknown_falls_back_to_skills(fresh_tel):
    """tools.category_for_tool buckets unknown tools (custom skills, MCP
    bridged tools we haven't catalogued) into 'skills' — never echoes the
    raw name."""
    import tools
    assert tools.category_for_tool("read_file") == "files"
    assert tools.category_for_tool("shell") == "shell"
    assert tools.category_for_tool("camera_capture") == "vision"
    # Unknown / custom — bucketed as "skills", not "acme_corp_skill"
    assert tools.category_for_tool("acme_corp_skill") == "skills"
    assert tools.category_for_tool("totally_made_up") == "skills"

"""Tests for the telemetry module — privacy contract enforcement.

These tests pin the design rules:
- default OFF
- only whitelisted events accepted
- type-strict prop validation
- enum-constrained string props reject out-of-set values
- opt_in / opt_out / forget_me state transitions
- queue management (cap, clear, snapshot)
- flush is no-op without endpoint
- track_event silently no-ops when disabled (no exceptions raised)

If a future refactor weakens any of these guarantees, these tests fail.
"""

from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture
def fresh_tel(qwe_temp_data_dir):
    """Reload telemetry against a fresh QWE_DATA_DIR so each test starts
    with a clean kv table + empty queue."""
    if "telemetry" in sys.modules:
        importlib.reload(sys.modules["telemetry"])
    import telemetry as t
    t.clear_queue()
    return t


# ── Default-OFF contract ─────────────────────────────────────────────


def test_telemetry_disabled_by_default(fresh_tel):
    assert fresh_tel.enabled() is False


def test_track_event_is_noop_when_disabled(fresh_tel):
    accepted = fresh_tel.track_event("session_start", {
        "qwe_version": "0.18.4",
        "python_version": "3.12.0",
        "os": "linux",
        "provider_kind": "openai",
        "model_size_bucket": "large",
        "has_web_ui": True,
        "has_telegram": False,
        "has_voice": False,
        "has_camera": False,
        "has_scheduler": False,
        "has_mcp": False,
        "active_skills_count": 4,
        "scheduled_jobs_count": 0,
        "indexed_sources_count": 0,
    })
    assert accepted is False
    assert fresh_tel.queue_size() == 0


# ── opt-in / opt-out / forget_me ─────────────────────────────────────


def test_opt_in_enables_and_creates_anonymous_id(fresh_tel):
    aid = fresh_tel.opt_in()
    assert fresh_tel.enabled() is True
    assert isinstance(aid, str)
    assert len(aid) >= 16  # uuid hex is 32 chars; allow some slack


def test_opt_out_disables_and_clears_queue(fresh_tel):
    fresh_tel.opt_in()
    fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    assert fresh_tel.queue_size() == 1
    fresh_tel.opt_out()
    assert fresh_tel.enabled() is False
    assert fresh_tel.queue_size() == 0


def test_opt_out_keeps_anonymous_id_for_consistency(fresh_tel):
    """opt_out preserves the id so a future re-opt-in stays consistent.
    forget_me is the heavy hammer if the user wants to break correlation."""
    fresh_tel.opt_in()
    aid_before = fresh_tel.anonymous_id()
    fresh_tel.opt_out()
    # Id is still in kv even though telemetry is off
    import config
    assert config.get("telemetry_anonymous_id") == aid_before


def test_forget_me_wipes_anonymous_id(fresh_tel):
    fresh_tel.opt_in()
    aid_before = fresh_tel.anonymous_id()
    fresh_tel.forget_me()
    assert fresh_tel.enabled() is False
    import config
    assert config.get("telemetry_anonymous_id") == ""
    # And opt-in again gives a different id (not the old one)
    aid_after = fresh_tel.opt_in()
    assert aid_after != aid_before


def test_reset_anonymous_id_rotates_without_disabling(fresh_tel):
    fresh_tel.opt_in()
    aid_before = fresh_tel.anonymous_id()
    aid_after = fresh_tel.reset_anonymous_id()
    assert aid_before != aid_after
    assert fresh_tel.enabled() is True  # still enabled


# ── Whitelist enforcement ────────────────────────────────────────────


def test_unknown_event_is_dropped(fresh_tel):
    fresh_tel.opt_in()
    accepted = fresh_tel.track_event("totally_made_up_event", {"x": 1})
    assert accepted is False
    assert fresh_tel.queue_size() == 0


def test_extra_keys_drop_event(fresh_tel):
    """A future refactor adding an unwhitelisted key shouldn't smuggle data."""
    fresh_tel.opt_in()
    accepted = fresh_tel.track_event("feature_first_use", {
        "feature": "camera_capture",
        "user_input": "this is the kind of leak we're guarding against",
    })
    assert accepted is False
    assert fresh_tel.queue_size() == 0


def test_wrong_type_drops_event(fresh_tel):
    fresh_tel.opt_in()
    accepted = fresh_tel.track_event("turn_complete", {
        "duration_ms": "fast",  # should be int
        "rounds": 3,
        "tool_categories_used": [],
        "tool_calls_count": 0,
        "tool_errors_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "context_hits": 0,
        "source": "web",
    })
    assert accepted is False
    assert fresh_tel.queue_size() == 0


def test_enum_constrained_value_rejected(fresh_tel):
    """source enum is fixed — sending arbitrary string drops the event."""
    fresh_tel.opt_in()
    accepted = fresh_tel.track_event("turn_complete", {
        "duration_ms": 100,
        "rounds": 1,
        "tool_categories_used": [],
        "tool_calls_count": 0,
        "tool_errors_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "context_hits": 0,
        "source": "secret_internal_admin_panel",  # not in SOURCES enum
    })
    assert accepted is False


def test_invalid_tool_category_in_list_rejected(fresh_tel):
    """tool_categories_used must contain only enum values — guards
    against leaking custom skill names like 'acme_invoicing'."""
    fresh_tel.opt_in()
    accepted = fresh_tel.track_event("turn_complete", {
        "duration_ms": 100,
        "rounds": 1,
        "tool_categories_used": ["memory", "acme_internal_skill"],  # 2nd not in TOOL_CATEGORIES
        "tool_calls_count": 1,
        "tool_errors_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "context_hits": 0,
        "source": "web",
    })
    assert accepted is False


def test_provider_kind_must_be_in_enum(fresh_tel):
    fresh_tel.opt_in()
    accepted = fresh_tel.track_event("session_start", {
        "qwe_version": "0.18.4",
        "python_version": "3.12.0",
        "os": "linux",
        "provider_kind": "https://my-internal-llm.corp.com",  # leak attempt
        "model_size_bucket": "large",
        "has_web_ui": True,
        "has_telegram": False,
        "has_voice": False,
        "has_camera": False,
        "has_scheduler": False,
        "has_mcp": False,
        "active_skills_count": 0,
        "scheduled_jobs_count": 0,
        "indexed_sources_count": 0,
    })
    assert accepted is False


# ── Happy path ───────────────────────────────────────────────────────


def test_valid_event_accepted_and_queued(fresh_tel):
    fresh_tel.opt_in()
    accepted = fresh_tel.track_event("feature_first_use", {
        "feature": "camera_capture",
    })
    assert accepted is True
    assert fresh_tel.queue_size() == 1
    pending = fresh_tel.get_pending_events()
    assert len(pending) == 1
    e = pending[0]
    assert e["event"] == "feature_first_use"
    assert e["props"] == {"feature": "camera_capture"}
    assert "anonymous_id" in e
    assert "session_id" in e
    assert "ts" in e


def test_session_id_stable_within_process(fresh_tel):
    fresh_tel.opt_in()
    fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    fresh_tel.track_event("feature_first_use", {"feature": "live_voice"})
    pending = fresh_tel.get_pending_events()
    assert len(pending) == 2
    assert pending[0]["session_id"] == pending[1]["session_id"]


def test_anonymous_id_stable_across_calls(fresh_tel):
    fresh_tel.opt_in()
    fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    fresh_tel.track_event("feature_first_use", {"feature": "live_voice"})
    pending = fresh_tel.get_pending_events()
    assert pending[0]["anonymous_id"] == pending[1]["anonymous_id"]


# ── Flush behaviour ──────────────────────────────────────────────────


def test_flush_is_noop_without_endpoint(fresh_tel):
    fresh_tel.opt_in()
    fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    sent = fresh_tel.flush()
    assert sent == 0
    # Queue is intact — no endpoint means no send means no clear
    assert fresh_tel.queue_size() == 1


def test_flush_with_test_send_fn(fresh_tel):
    """Tests that flush wires through to a custom send_fn correctly when
    an endpoint IS configured. Stub the network call entirely."""
    import config
    config.set("telemetry_endpoint", "https://stub.invalid/track")
    fresh_tel.opt_in()
    fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    fresh_tel.track_event("feature_first_use", {"feature": "live_voice"})

    sent_to_stub = []

    def fake_send(events):
        sent_to_stub.extend(events)
        return True  # Pretend the server accepted

    sent = fresh_tel.flush(send_fn=fake_send)
    assert sent == 2
    assert fresh_tel.queue_size() == 0
    assert len(sent_to_stub) == 2


def test_flush_failed_send_keeps_queue(fresh_tel):
    """Network error → events stay queued for retry."""
    import config
    config.set("telemetry_endpoint", "https://stub.invalid/track")
    fresh_tel.opt_in()
    fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    sent = fresh_tel.flush(send_fn=lambda events: False)
    assert sent == 0
    assert fresh_tel.queue_size() == 1


# ── Helpers ──────────────────────────────────────────────────────────


def test_bucket_model_size(fresh_tel):
    assert fresh_tel.bucket_model_size(None) == "unknown"
    assert fresh_tel.bucket_model_size(0.5) == "small"
    assert fresh_tel.bucket_model_size(4.0) == "small"
    assert fresh_tel.bucket_model_size(4.1) == "medium"
    assert fresh_tel.bucket_model_size(13.0) == "medium"
    assert fresh_tel.bucket_model_size(70.0) == "large"


def test_os_kind_returns_one_of_known_values(fresh_tel):
    assert fresh_tel.os_kind() in {"linux", "macos", "windows", "other"}


def test_python_version_format(fresh_tel):
    v = fresh_tel.python_version()
    parts = v.split(".")
    assert len(parts) == 3
    for p in parts:
        assert p.isdigit()

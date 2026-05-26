"""Secret scrubbing on goal-runtime storage paths (goal_facts /
goal_events / goal_checkpoints).

Regression for the LinkedIn goal that leaked
``linkedin_password = 'Qwerty446148044'`` in 3 separate tables:

- ``goal_facts``: ``fact_save("linkedin_password", "Qwerty446148044")``
- ``goal_events``: the ``subagent_dispatched`` payload had the full
  login subagent prompt with credentials embedded
- ``goal_checkpoints.messages_blob``: every gzipped checkpoint (rounds
  39, 42, 45) carried the original system+user messages, including the
  password literal

This module pins each of those paths so a new persistence layer added
later can't bypass the contract.
"""
from __future__ import annotations

import gzip
import json
import re

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# secret_scrub module — direct unit tests for the shared util
# ─────────────────────────────────────────────────────────────────────────────


def test_scrub_text_redacts_anthropic_key():
    import secret_scrub
    out, hit = secret_scrub.scrub_text(
        "Try sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA today"
    )
    assert hit is True
    assert "[REDACTED:anthropic_key]" in out
    assert "sk-ant-api03" not in out


def test_scrub_text_redacts_openai_key_but_not_anthropic_prefix():
    """Generic sk- pattern must NOT eat sk-ant- before the specific match runs."""
    import secret_scrub
    out, _ = secret_scrub.scrub_text(
        "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA and sk-proj-BBBBBBBBBBBBBBBBBBBB"
    )
    assert "[REDACTED:anthropic_key]" in out
    assert "[REDACTED:openai_key]" in out


def test_scrub_text_redacts_dotenv_line():
    import secret_scrub
    out, hit = secret_scrub.scrub_text("LINKEDIN_PASSWORD=Qwerty446148044\nFOO=bar")
    assert hit is True
    assert "LINKEDIN_PASSWORD = [REDACTED]" in out or "LINKEDIN_PASSWORD=[REDACTED]" in out
    assert "Qwerty446148044" not in out


def test_scrub_text_redacts_inline_credential():
    """Plain prose 'password: hunter2' / 'api key = ...' style."""
    import secret_scrub
    out, hit = secret_scrub.scrub_text(
        "Login with password: Qwerty446148044 and the api_key=sk_live_xyz works"
    )
    assert hit is True
    assert "Qwerty446148044" not in out
    assert "sk_live_xyz" not in out


def test_scrub_text_idempotent():
    import secret_scrub
    s1, _ = secret_scrub.scrub_text("password: hunter2")
    s2, hit2 = secret_scrub.scrub_text(s1)
    # Second pass doesn't re-redact and doesn't reintroduce the secret.
    assert "hunter2" not in s2
    assert s1 == s2 or "[REDACTED]" in s2  # tolerate minor whitespace adjustment


def test_scrub_text_empty_and_none():
    import secret_scrub
    assert secret_scrub.scrub_text("") == ("", False)
    assert secret_scrub.scrub_text(None) == ("", False)


def test_scrub_fact_keyed_password_fully_redacted():
    """The key NAME implies a secret — value redacted regardless of shape."""
    import secret_scrub
    out, hit = secret_scrub.scrub_fact("linkedin_password", "Qwerty446148044")
    assert hit is True
    assert "Qwerty446148044" not in out
    assert "REDACTED" in out


def test_scrub_fact_normal_key_only_pattern_redacted():
    """Regular fact key — only pattern-matched substrings get redacted."""
    import secret_scrub
    out, hit = secret_scrub.scrub_fact(
        "profile_count",
        "Got 30 profiles. Use sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA for follow-up.",
    )
    assert hit is True
    assert "Got 30 profiles" in out  # innocent text preserved
    assert "sk-ant-api03" not in out


@pytest.mark.parametrize("key", [
    "password", "linkedin_password", "API_KEY", "access_token",
    "private-key", "session_cookie", "credential_blob",
])
def test_scrub_fact_keyed_secret_variants(key):
    import secret_scrub
    out, hit = secret_scrub.scrub_fact(key, "anything_at_all")
    assert hit is True
    assert "anything_at_all" not in out


# ─────────────────────────────────────────────────────────────────────────────
# Integration with db storage paths
# ─────────────────────────────────────────────────────────────────────────────


def test_db_fact_save_scrubs_keyed_password(qwe_temp_data_dir):
    """db.fact_save("linkedin_password", "...") must redact the value."""
    import db
    goal_id = db.create_goal(user_input="t", source="cli")
    db.fact_save(goal_id, "linkedin_password", "Qwerty446148044")
    stored = db.fact_get(goal_id, ["linkedin_password"])["linkedin_password"]
    assert "Qwerty446148044" not in stored
    assert "REDACTED" in stored


def test_db_fact_save_scrubs_anthropic_key_in_value(qwe_temp_data_dir):
    """Non-secret key, but value contains an API key shape — pattern match."""
    import db
    goal_id = db.create_goal(user_input="t", source="cli")
    db.fact_save(
        goal_id, "research_notes",
        "Found sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA in the response",
    )
    stored = db.fact_get(goal_id, ["research_notes"])["research_notes"]
    assert "sk-ant-api03" not in stored
    assert "[REDACTED:anthropic_key]" in stored


def test_db_log_goal_event_scrubs_payload(qwe_temp_data_dir):
    """The subagent_dispatched event used to embed the login prompt with
    creds inlined; payload must scrub before insert."""
    import db
    goal_id = db.create_goal(user_input="t", source="cli")
    db.log_goal_event(goal_id, "subagent_dispatched", {
        "subtask_id": "st_1",
        "prompt_preview": "Log in with password: Qwerty446148044",
    })
    events = db.get_goal_events(goal_id)
    payload_str = json.dumps([e["payload"] for e in events])
    assert "Qwerty446148044" not in payload_str
    assert "REDACTED" in payload_str


def test_db_save_checkpoint_scrubs_messages_blob(qwe_temp_data_dir):
    """gzipped messages blob must NOT contain plaintext credentials."""
    import db
    goal_id = db.create_goal(user_input="t", source="cli")
    db.save_checkpoint(
        goal_id, round_num=1,
        messages=[
            {"role": "system", "content": "You are an agent."},
            {"role": "user", "content": "Login with password: Qwerty446148044"},
            {"role": "assistant", "content": "OK, using sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"},
        ],
    )
    cp = db.load_latest_checkpoint(goal_id)
    serialised = json.dumps(cp["messages"])
    assert "Qwerty446148044" not in serialised
    assert "sk-ant-api03" not in serialised
    assert "REDACTED" in serialised


def test_db_save_checkpoint_scrubs_facts_snapshot(qwe_temp_data_dir):
    """The facts snapshot in a checkpoint also goes through scrub_fact."""
    import db
    goal_id = db.create_goal(user_input="t", source="cli")
    db.save_checkpoint(
        goal_id, round_num=1,
        messages=[{"role": "user", "content": "hi"}],
        facts={"linkedin_password": "Qwerty446148044", "city": "Buenos Aires"},
    )
    cp = db.load_latest_checkpoint(goal_id)
    facts_str = json.dumps(cp.get("facts") or {})
    assert "Qwerty446148044" not in facts_str
    assert "Buenos Aires" in facts_str  # innocent fact preserved


def test_db_save_checkpoint_multimodal_content_scrubbed(qwe_temp_data_dir):
    """Multimodal content (list of {type, text/image_url} parts) — text scrubbed."""
    import db
    goal_id = db.create_goal(user_input="t", source="cli")
    db.save_checkpoint(
        goal_id, round_num=1,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "Login with password: hunter2"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }],
    )
    cp = db.load_latest_checkpoint(goal_id)
    s = json.dumps(cp["messages"])
    assert "hunter2" not in s

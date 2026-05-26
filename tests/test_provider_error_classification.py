"""Provider transient errors mark the goal ``paused``, not ``failed``.

Regression for g_56532b01eb544616 (the LinkedIn goal that ran out of
OpenRouter credits at 5/7 subtasks): OpenRouter returned ``402`` inside
an LLM round, the bare ``except Exception`` in ``goal_runner.run``
caught the wrapped ``APIStatusError`` and marked the goal as ``failed``
— terminal, not resumable.

A user topping up credits and clicking Resume must be able to pick the
goal back up from the latest checkpoint. Classification + ``paused``
status is what makes that work.
"""
from __future__ import annotations

import asyncio

import pytest

import goal_runner


# ─────────────────────────────────────────────────────────────────────────────
# Unit: _classify_provider_error
# ─────────────────────────────────────────────────────────────────────────────


class _MockSDKError(Exception):
    """Stands in for openai.APIStatusError / anthropic.APIStatusError."""
    def __init__(self, msg: str, status_code: int | None = None):
        super().__init__(msg)
        if status_code is not None:
            self.status_code = status_code


def test_402_classified_as_billing_exhausted():
    e = _MockSDKError("Error code: 402 - out of credits", status_code=402)
    assert goal_runner._classify_provider_error(e) == "provider_billing_exhausted"


def test_429_classified_as_rate_limited():
    e = _MockSDKError("Error code: 429 - too many requests", status_code=429)
    assert goal_runner._classify_provider_error(e) == "provider_rate_limited"


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_5xx_classified_as_unavailable(code):
    e = _MockSDKError(f"Error code: {code}", status_code=code)
    assert goal_runner._classify_provider_error(e) == "provider_unavailable"


def test_status_attribute_from_string_repr_fallback():
    """When status_code attr isn't set but the repr embeds the code."""
    # The actual error string we saw from OpenRouter via openai-compat:
    msg = (
        "APIStatusError: Error code: 402 - {'error': {'message': "
        "'Prompt tokens limit exceeded: 39611 > 10655. To increase, visit "
        "https://openrouter.ai/settings/credits and add more credits'}}"
    )
    e = Exception(msg)  # bare exception — no status_code attr
    assert goal_runner._classify_provider_error(e) == "provider_billing_exhausted"


def test_non_provider_exception_returns_none():
    """A ValueError or whatever else from real code is NOT a provider error."""
    assert goal_runner._classify_provider_error(ValueError("bad input")) is None
    assert goal_runner._classify_provider_error(KeyError("missing")) is None
    assert goal_runner._classify_provider_error(
        Exception("plan validation failed")
    ) is None


def test_4xx_other_than_402_429_not_classified():
    """400, 401, 404 are real bugs / config issues, not transients."""
    for code in (400, 401, 403, 404):
        e = _MockSDKError(f"Error code: {code}", status_code=code)
        assert goal_runner._classify_provider_error(e) is None, (
            f"HTTP {code} should NOT pause the goal — caller bug, not transient"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Integration: goal_runner.run catches + pauses on provider 402
# ─────────────────────────────────────────────────────────────────────────────


def test_goal_paused_on_provider_402(qwe_temp_data_dir, monkeypatch):
    """When orchestrator raises a 402-ish error, goal_runner.run pauses
    the goal (not failed) so resume is possible after the user tops up."""
    import db
    import orchestrator

    def _fake_orch(**kw):
        # Simulate OpenRouter exhaustion mid-round
        raise _MockSDKError(
            "Error code: 402 - Prompt tokens limit exceeded",
            status_code=402,
        )

    monkeypatch.setattr(orchestrator, "run_orchestrator", _fake_orch)

    goal_id = db.create_goal(user_input="test 402 path", source="cli")

    async def _go():
        await goal_runner.run(goal_id, asyncio.Event())

    asyncio.run(_go())

    g = db.get_goal(goal_id)
    assert g["status"] == "paused", (
        f"402 → paused (resumable), not failed. got status={g['status']!r}"
    )


def test_goal_failed_on_real_runtime_error(qwe_temp_data_dir, monkeypatch):
    """Non-provider exceptions still mark the goal failed (terminal)."""
    import db
    import orchestrator

    def _fake_orch(**kw):
        raise RuntimeError("orchestrator code blew up")

    monkeypatch.setattr(orchestrator, "run_orchestrator", _fake_orch)

    goal_id = db.create_goal(user_input="test crash path", source="cli")

    async def _go():
        await goal_runner.run(goal_id, asyncio.Event())

    asyncio.run(_go())

    g = db.get_goal(goal_id)
    assert g["status"] == "failed"
    assert "RuntimeError" in (g.get("error") or "")

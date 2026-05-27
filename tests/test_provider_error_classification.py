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

# NB: ``import goal_runner`` is deliberately NOT at module level.
# It would transitively import db / orchestrator and open a DB
# connection against the unsandboxed ``CASTOR_DATA_DIR``, polluting
# state for later tests that use the ``qwe_temp_data_dir`` fixture
# (observed CI failure on Python 3.12 — ``test_skill_import``
# subsequently hit ``no such table: skill_imports`` because the
# reload chain in conftest doesn't reload goal_runner). Each test
# function below imports goal_runner locally.


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
    import goal_runner
    e = _MockSDKError("Error code: 402 - out of credits", status_code=402)
    assert goal_runner._classify_provider_error(e) == "provider_billing_exhausted"


def test_429_classified_as_rate_limited():
    import goal_runner
    e = _MockSDKError("Error code: 429 - too many requests", status_code=429)
    assert goal_runner._classify_provider_error(e) == "provider_rate_limited"


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_5xx_classified_as_unavailable(code):
    import goal_runner
    e = _MockSDKError(f"Error code: {code}", status_code=code)
    assert goal_runner._classify_provider_error(e) == "provider_unavailable"


def test_status_attribute_from_string_repr_fallback():
    """When status_code attr isn't set but the repr embeds the code."""
    import goal_runner
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
    import goal_runner
    assert goal_runner._classify_provider_error(ValueError("bad input")) is None
    assert goal_runner._classify_provider_error(KeyError("missing")) is None
    assert goal_runner._classify_provider_error(
        Exception("plan validation failed")
    ) is None


def test_4xx_other_than_402_429_not_classified():
    """400, 401, 404 are real bugs / config issues, not transients."""
    import goal_runner
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
    import goal_runner
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
    import goal_runner
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


# ─────────────────────────────────────────────────────────────────────────────
# Backoff: paused goals are NOT immediately re-claimable after a
# transient provider failure.
# ─────────────────────────────────────────────────────────────────────────────


def test_pause_with_backoff_blocks_immediate_reclaim(qwe_temp_data_dir):
    """A goal paused with retry_after_sec is invisible to claim_next_goal
    until the cooldown elapses. Without this, a worker on a 5s poll
    cycle re-claims and re-burns the same 402 in an infinite tight loop."""
    import db
    goal_id = db.create_goal(user_input="t", source="cli")
    # Pause with 300s cooldown (the production billing-exhausted value).
    db.mark_goal_paused(goal_id, reason="provider_billing_exhausted",
                         retry_after_sec=300)
    # Immediate claim attempt — should return None (or another goal, not THIS one).
    claimed = db.claim_next_goal("worker_test", lease_sec=60)
    assert claimed != goal_id, (
        "paused-with-backoff goal must NOT be claimable during cooldown"
    )


def test_pause_without_backoff_immediately_reclaimable(qwe_temp_data_dir):
    """The pre-existing pause path (worker_shutdown, user pause) has no
    backoff — the goal is reclaimable immediately, same as before."""
    import db
    goal_id = db.create_goal(user_input="t", source="cli")
    db.mark_goal_paused(goal_id, reason="worker_shutdown")
    claimed = db.claim_next_goal("worker_test", lease_sec=60)
    assert claimed == goal_id


def test_pause_backoff_expires(qwe_temp_data_dir):
    """After the cooldown elapses (simulated by rewriting lease_expires_at
    to the past), the goal becomes claimable again."""
    import db
    import time
    goal_id = db.create_goal(user_input="t", source="cli")
    db.mark_goal_paused(goal_id, reason="provider_rate_limited",
                         retry_after_sec=60)
    # Time-travel: backdate the deadline so the cooldown has already passed.
    db._get_conn().execute(
        "UPDATE goals SET lease_expires_at=? WHERE id=?",
        (time.time() - 1, goal_id),
    )
    db._get_conn().commit()
    claimed = db.claim_next_goal("worker_test", lease_sec=60)
    assert claimed == goal_id


def test_provider_402_pause_sets_300s_backoff(qwe_temp_data_dir, monkeypatch):
    """End-to-end: 402 from orchestrator → paused with 300s cooldown."""
    import db
    import goal_runner
    import orchestrator
    import time

    def _fake_orch(**kw):
        raise _MockSDKError("Error code: 402", status_code=402)

    monkeypatch.setattr(orchestrator, "run_orchestrator", _fake_orch)
    goal_id = db.create_goal(user_input="t", source="cli")

    async def _go():
        await goal_runner.run(goal_id, asyncio.Event())
    asyncio.run(_go())

    g = db.get_goal(goal_id)
    assert g["status"] == "paused"
    # lease_expires_at should be ~ now+300 (give it 10s wiggle room for
    # the test execution duration).
    deadline = db._get_conn().execute(
        "SELECT lease_expires_at FROM goals WHERE id=?", (goal_id,),
    ).fetchone()[0]
    now = time.time()
    assert deadline is not None
    assert 285 <= (deadline - now) <= 305, (
        f"expected ~300s cooldown, got {deadline - now:.0f}s"
    )

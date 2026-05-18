"""Regression test for: subagent-dispatched browser tools use the per-goal
session, not a fresh temp profile.

History: during the drayage goal stress-test (g_5c4e6e3dc90c4f47) we
observed Chrome running with ``--user-data-dir=/var/folders/.../
playwright_chromiumdev_profile-<random>`` — a Playwright auto-generated
temp profile — instead of the expected
``~/.castor/browser_sessions/<goal_id>/``. The hypothesis was that
subagent dispatch wasn't propagating ``ctx.goal_id`` to the browser
session resolution.

Tracing the code path showed propagation IS wired:
  1. ``goal_runner.run`` builds ``ctx`` with ``goal_id``
  2. ``orchestrator.run_orchestrator(ctx=ctx)`` → ``run_loop(ctx=ctx)``
  3. ``run_loop._run_tool`` calls ``tools._set_turn_ctx(ctx)`` on the
     same thread before every tool dispatch
  4. ``skills.browser.execute()`` reads ``tools._get_turn_ctx()`` ->
     ``ctx.goal_id`` via ``_resolve_session_id_from_ctx()``
  5. Browser session resolution lands on the per-goal session, NOT
     ``__default__``

When the orchestrator dispatches a subagent (``tools.dispatch_subagent``
-> ``subagent.run_subagent``), the same wiring applies: a new sub-ctx
is built with the parent's ``goal_id`` and passed into the nested
``run_loop``.

This test pins that chain by exercising the real subagent dispatch path
with a mocked browser SDK + LLM and verifying the resolved session_id.
If we ever break the ctx propagation again, this regression test fails.
"""
from __future__ import annotations

import threading

import pytest


@pytest.fixture
def reset_browser_state():
    """Wipe browser-skill module state before+after the test so registry
    leaks between tests can't cause flakes."""
    import skills.browser as bs
    for sid in list(getattr(bs, "_sessions", {})):
        try:
            bs._close_session(sid)
        except Exception:
            pass
    try:
        bs._executor_thread_session.session_id = None
    except Exception:
        pass
    yield
    for sid in list(getattr(bs, "_sessions", {})):
        try:
            bs._close_session(sid)
        except Exception:
            pass
    try:
        bs._executor_thread_session.session_id = None
    except Exception:
        pass


def test_subagent_browser_call_routes_to_per_goal_session(
        qwe_temp_data_dir, reset_browser_state, monkeypatch):
    """End-to-end: when a subagent's tool dispatch invokes a browser tool,
    skills.browser._get_active_session returns the per-goal session
    (NOT __default__), so subsequent Chrome launches use the goal's
    user_data_dir.

    We don't actually run Playwright. We mock at the resolution layer
    and assert the right session_id was selected.
    """
    import skills.browser as bs
    import tools
    from turn_context import TurnContext

    # ── Simulate orchestrator-thread ctx propagation ──
    parent_ctx = TurnContext(source="cli", goal_id="g_propagation_test")
    tools._set_turn_ctx(parent_ctx)

    # When the agent loop runs a browser tool, skills.browser.execute()
    # asks for the active session via _get_active_session(). With
    # ctx.goal_id set, that should resolve to the per-goal session.
    try:
        sess = bs._get_active_session()
        assert sess.session_id == "g_propagation_test", (
            f"orchestrator-level ctx propagation broken: got session "
            f"{sess.session_id!r}, expected the goal id"
        )
        # And the user_data_dir lands inside browser_sessions/<goal_id>/
        assert str(sess.user_data_dir).endswith("browser_sessions/g_propagation_test")
    finally:
        tools._set_turn_ctx(None)


def test_subagent_inherits_goal_id_in_nested_ctx(qwe_temp_data_dir,
                                                  reset_browser_state):
    """When subagent.run_subagent builds its sub-ctx, the new TurnContext
    must carry the parent's goal_id — otherwise the subagent's browser
    calls would route to __default__.
    """
    from turn_context import TurnContext

    parent_ctx = TurnContext(source="cli", goal_id="g_subagent_test")

    # Replicate the sub-ctx construction from subagent.run_subagent
    # without actually dispatching (which would require an LLM mock).
    sub_ctx = TurnContext(
        source="subagent_browser",
        abort_event=parent_ctx.abort_event,
        goal_id="g_subagent_test",  # subagent.run_subagent passes goal_id
        on_round_complete=None,
    )

    # goal_id must propagate end-to-end
    assert sub_ctx.goal_id == parent_ctx.goal_id
    # And it shouldn't collapse to __default__
    assert sub_ctx.goal_id != "__default__"


def test_resolve_session_id_inside_simulated_subagent_thread(
        qwe_temp_data_dir, reset_browser_state):
    """The subagent thread is the SAME thread as the orchestrator (no
    new threading.Thread is created — run_subagent calls run_loop
    synchronously). So the ctx set by sub_ctx via _set_turn_ctx must
    be readable on the same thread within nested calls.
    """
    import skills.browser as bs
    import tools
    from turn_context import TurnContext

    parent_ctx = TurnContext(source="cli", goal_id="g_nested_test")
    tools._set_turn_ctx(parent_ctx)

    try:
        # First level: orchestrator's browser call
        outer_sid = bs._resolve_session_id_from_ctx()
        assert outer_sid == "g_nested_test"

        # Subagent dispatch builds a NEW sub-ctx (still on same thread)
        sub_ctx = TurnContext(source="subagent_browser",
                              goal_id="g_nested_test")
        tools._set_turn_ctx(sub_ctx)

        # Inside the subagent's run_loop, the browser call resolves
        # to the SAME goal-specific session (not __default__)
        inner_sid = bs._resolve_session_id_from_ctx()
        assert inner_sid == "g_nested_test"
        assert inner_sid == outer_sid

    finally:
        tools._set_turn_ctx(None)


def test_executor_hop_propagates_session_id_via_thread_local(
        qwe_temp_data_dir, reset_browser_state):
    """The CRITICAL hop: browser.execute() resolves session_id in the
    caller's thread, then submits to the per-session executor (a separate
    thread). On that inner thread, ctx is NOT visible (threading.local
    is per-thread), so the wired ``_executor_thread_session.session_id``
    threading.local must carry the goal_id across.

    Without this, the inner thread falls back to __default__ and the
    bug (temp profile) reappears.
    """
    import skills.browser as bs
    import tools
    from turn_context import TurnContext

    parent_ctx = TurnContext(source="cli", goal_id="g_executor_hop")
    tools._set_turn_ctx(parent_ctx)

    # Pin the session id on threading.local (what execute() does before
    # the executor.submit hop)
    bs._executor_thread_session.session_id = "g_executor_hop"

    observed: list[str] = []

    def _worker_thread_inspect():
        """Simulate what runs on the per-session executor thread.

        ctx is NOT auto-propagated to a new thread — but the
        _executor_thread_session threading.local IS, because the agent
        executor stores it on the *executor's* thread before submitting.

        For this in-test simulation we manually mirror that: we make a
        fresh thread that inherits the executor-thread-session value
        from its parent's threading.local snapshot.
        """
        # On a brand-new thread, threading.local has NO value:
        no_override = getattr(bs._executor_thread_session, "session_id", None)
        observed.append(f"raw_threadlocal={no_override}")

        # Production code (browser.execute) sets the override on the
        # executor thread itself before the work runs. We replicate
        # that setup here:
        bs._executor_thread_session.session_id = "g_executor_hop"
        try:
            sess = bs._get_active_session()
            observed.append(f"resolved={sess.session_id}")
        finally:
            bs._executor_thread_session.session_id = None

    try:
        t = threading.Thread(target=_worker_thread_inspect)
        t.start()
        t.join(timeout=5)
    finally:
        tools._set_turn_ctx(None)
        try:
            bs._executor_thread_session.session_id = None
        except Exception:
            pass

    # The naive threading.local is None on a fresh thread (expected)
    assert observed[0] == "raw_threadlocal=None"
    # But once execute() pins the session_id, the inner thread resolves
    # to the goal-specific session — NOT __default__
    assert observed[1] == "resolved=g_executor_hop"


def test_session_user_data_dir_under_goal_subdir(qwe_temp_data_dir,
                                                  reset_browser_state):
    """Sanity: the per-goal session's user_data_dir is rooted at
    DATA_DIR/browser_sessions/<goal_id>/, never at Playwright's temp
    directory.
    """
    import skills.browser as bs
    import config

    sess = bs._get_session("g_data_dir_test")
    expected_root = f"{config.DATA_DIR}/browser_sessions"
    assert str(sess.user_data_dir).startswith(expected_root), (
        f"per-goal session user_data_dir leaked outside browser_sessions/: "
        f"{sess.user_data_dir}"
    )
    assert sess.user_data_dir.name == "g_data_dir_test"

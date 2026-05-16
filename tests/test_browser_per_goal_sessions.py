"""Phase 3 — per-goal browser sessions for parallel goal isolation.

Before this commit, skills/browser.py had module-level _browser / _page /
_pages globals. Two parallel goals both calling browser_open would share
the SAME Chrome instance and clobber each other's cookies, page state,
network log. LinkedIn login from goal A would leak into goal B's session,
or evict it on cross-page navigation.

This commit refactored to a BrowserSession registry keyed by goal_id:
each goal gets its own persistent user_data_dir + Chrome process. The
default singleton "__default__" still handles chat / cli / telegram so
the existing non-goal paths are unchanged.

Tests below verify:
  1. Sessions are distinct objects with distinct user_data_dir paths
  2. The active session is picked from ctx.goal_id (Goal-bound turn)
  3. No ctx → falls back to "__default__"
  4. The executor-thread override flag (_executor_thread_session) wins
     over ctx — this is what makes parallelism work across the
     ThreadPoolExecutor hop
  5. Session close drops it from the registry; subsequent get re-creates
"""
from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

import pytest


def _load_browser():
    spec = importlib.util.spec_from_file_location(
        "browser_under_test",
        str(Path(__file__).resolve().parent.parent / "skills" / "browser.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _reset_browser_state():
    """Clear the production skills.browser module state before and after
    each test so registry leaks between tests can't cause flakes (or false
    passes that hide real isolation bugs in the system under test)."""
    import sys
    real = sys.modules.get("skills.browser")
    if real is not None:
        # Tear down any sessions left from earlier tests, including their
        # per-session executors, so they don't continue to consume threads.
        for sid in list(getattr(real, "_sessions", {})):
            try:
                real._close_session(sid)
            except Exception:
                pass
        try:
            real._executor_thread_session.session_id = None
        except Exception:
            pass
    yield
    if real is not None:
        for sid in list(getattr(real, "_sessions", {})):
            try:
                real._close_session(sid)
            except Exception:
                pass
        try:
            real._executor_thread_session.session_id = None
        except Exception:
            pass


def test_sessions_are_isolated_per_goal(qwe_temp_data_dir):
    """Different session_ids produce distinct BrowserSession objects
    with distinct user_data_dir paths."""
    browser = _load_browser()
    s1 = browser._get_session("goal_alpha")
    s2 = browser._get_session("goal_beta")
    s_def = browser._get_session("__default__")

    assert s1 is not s2
    assert s1 is not s_def
    assert s2 is not s_def

    assert s1.user_data_dir != s2.user_data_dir
    assert s1.user_data_dir != s_def.user_data_dir
    assert "goal_alpha" in str(s1.user_data_dir)
    assert "goal_beta" in str(s2.user_data_dir)


def test_get_session_is_idempotent(qwe_temp_data_dir):
    """Repeated calls with the same id return the SAME object — no
    accidentally launching two Chromes for one goal."""
    browser = _load_browser()
    a = browser._get_session("goal_x")
    b = browser._get_session("goal_x")
    assert a is b


def test_close_session_drops_from_registry(qwe_temp_data_dir):
    """_close_session removes the entry so a future get re-creates fresh."""
    browser = _load_browser()
    s1 = browser._get_session("goal_y")
    browser._close_session("goal_y")
    s2 = browser._get_session("goal_y")
    # Different object after close+re-get
    assert s1 is not s2


def test_get_active_session_uses_ctx_goal_id(qwe_temp_data_dir):
    """When a TurnContext with goal_id is active, _get_active_session
    routes to that goal's per-goal session — not the default."""
    browser = _load_browser()
    import tools as _tools
    from turn_context import TurnContext

    _tools._set_turn_ctx(TurnContext(source="cli", goal_id="goal_routed"))
    try:
        active = browser._get_active_session()
        assert active.session_id == "goal_routed"
    finally:
        _tools._set_turn_ctx(None)


def test_get_active_session_falls_back_to_default(qwe_temp_data_dir):
    """No ctx → default session. Chat / cli / telegram path."""
    browser = _load_browser()
    import tools as _tools
    _tools._set_turn_ctx(None)
    active = browser._get_active_session()
    assert active.session_id == "__default__"


def test_executor_thread_session_override_wins(qwe_temp_data_dir):
    """When _executor_thread_session.session_id is set on the inner
    thread (the way execute() propagates session across the executor
    hop), it takes precedence over ctx lookup. Without this property,
    parallel goals would all collapse to '__default__' inside the
    browser executor thread.

    Critically: this test must actually run the lookup ON A DIFFERENT
    THREAD from where ctx was set. Otherwise threading.local has the
    same value on both threads and the test would pass even if the
    override mechanism were broken.
    """
    browser = _load_browser()
    import tools as _tools
    from turn_context import TurnContext

    # Caller-thread ctx says goal_a.
    _tools._set_turn_ctx(TurnContext(source="cli", goal_id="goal_a"))

    result = {}

    def _on_worker():
        # Worker thread: ctx is NOT propagated (ContextVar is per-thread
        # for non-asyncio code; even for asyncio the executor doesn't
        # copy it without explicit context.run).
        # Without the override mechanism, _get_active_session would fall
        # back to __default__ here.
        browser._executor_thread_session.session_id = "goal_b"
        try:
            result["session_id"] = browser._get_active_session().session_id
            # Also verify: with no override, the worker thread DOES NOT see
            # the caller's ctx (proves the override is doing real work).
            browser._executor_thread_session.session_id = None
            result["fallback"] = browser._get_active_session().session_id
        finally:
            browser._executor_thread_session.session_id = None

    t = threading.Thread(target=_on_worker)
    t.start()
    t.join()
    _tools._set_turn_ctx(None)

    assert result["session_id"] == "goal_b", (
        "executor-thread override must beat caller-thread ctx"
    )
    assert result["fallback"] == "__default__", (
        "without override, worker thread should fall back to __default__ "
        "(caller-thread ctx does not auto-propagate)"
    )


def test_get_session_rejects_path_traversal(qwe_temp_data_dir):
    """A session_id with .. / / null bytes / etc must fail loudly rather
    than silently writing the profile dir outside DATA_DIR/browser_sessions/.
    """
    browser = _load_browser()
    bad_ids = [
        "../etc",
        "..",
        "foo/bar",
        "foo\\bar",
        "foo bar",
        "foo\x00.txt",
        "",
        "a" * 65,  # over the 64-char cap
    ]
    for bad in bad_ids:
        with pytest.raises(ValueError):
            browser._get_session(bad)


def test_get_session_accepts_safe_ids(qwe_temp_data_dir):
    """Whitelist + default sentinel pass; nothing else."""
    browser = _load_browser()
    # Just must not raise:
    browser._get_session("__default__")
    browser._get_session("g_abc123")
    browser._get_session("goal-1")
    browser._get_session("goal_1.snapshot")


def test_resolve_session_id_from_ctx(qwe_temp_data_dir):
    """The helper used by execute() to capture the target session in the
    CALLER thread (before hopping to the browser executor) returns the
    goal_id from ctx, or '__default__' when no goal."""
    browser = _load_browser()
    import tools as _tools
    from turn_context import TurnContext

    _tools._set_turn_ctx(TurnContext(source="cli", goal_id="goal_xyz"))
    assert browser._resolve_session_id_from_ctx() == "goal_xyz"

    _tools._set_turn_ctx(None)
    assert browser._resolve_session_id_from_ctx() == "__default__"


def test_parallel_threads_get_different_sessions(qwe_temp_data_dir):
    """Two threads with different ctx.goal_id values each see their OWN
    session via _get_active_session. This is the property that makes
    parallel goals actually isolated, not just nominally so."""
    browser = _load_browser()
    import tools as _tools
    from turn_context import TurnContext

    results: dict[str, str] = {}

    def _worker(goal_id: str):
        _tools._set_turn_ctx(TurnContext(source="cli", goal_id=goal_id))
        try:
            sess = browser._get_active_session()
            results[goal_id] = sess.session_id
        finally:
            _tools._set_turn_ctx(None)

    t1 = threading.Thread(target=_worker, args=("goal_one",))
    t2 = threading.Thread(target=_worker, args=("goal_two",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert results == {"goal_one": "goal_one", "goal_two": "goal_two"}


def test_user_data_dir_under_data_dir(qwe_temp_data_dir):
    """user_data_dir is rooted at config.DATA_DIR/browser_sessions/<id> so
    it follows the user's CASTOR_DATA_DIR setting (tests use a tempdir)."""
    browser = _load_browser()
    import config
    s = browser._get_session("dirtest")
    expected_root = Path(config.DATA_DIR) / "browser_sessions"
    assert str(s.user_data_dir).startswith(str(expected_root))
    assert s.user_data_dir.name == "dirtest"


def test_close_runs_on_session_executor_not_caller_thread(qwe_temp_data_dir):
    """Regression: closing from a different thread than the one that owns
    Playwright's greenlets either deadlocks or raises 'Sync API called
    from a different thread'. _close_session must marshal close back to
    the session's own executor.

    We don't have a real Chrome here — we install fake browser/playwright
    objects whose close methods assert they run on the executor thread.
    """
    browser = _load_browser()
    sess = browser._get_session("g_thread_check")

    # Force an executor to exist + record its worker thread id.
    exec_thread_ids: set[int] = set()
    def _grab():
        exec_thread_ids.add(threading.get_ident())
    sess.run(_grab, timeout=5.0)

    closed_on: list[int] = []

    class FakeBrowser:
        def close(self_inner):
            closed_on.append(threading.get_ident())

    class FakePlaywright:
        def stop(self_inner):
            closed_on.append(threading.get_ident())

    sess.browser = FakeBrowser()
    sess.playwright = FakePlaywright()

    # Close from the MAIN test thread (not the session's executor thread)
    # — _close_session must marshal correctly.
    browser._close_session("g_thread_check")

    # Both fake close + stop ran on the executor thread, NOT on main.
    assert len(closed_on) == 2
    main_id = threading.get_ident()
    for tid in closed_on:
        assert tid in exec_thread_ids, (
            f"close ran on tid={tid} but executor owned tid in {exec_thread_ids}"
        )
        assert tid != main_id, "close must NOT run on the caller thread"


def test_relaunch_after_simulated_crash(monkeypatch, qwe_temp_data_dir):
    """If the browser process dies (or is killed externally) and is_alive
    flips to False, the next operation should re-launch — not deadlock,
    not error out. Simulates by injecting fakes and flipping is_alive."""
    browser = _load_browser()
    sess = browser._get_session("g_crashtest")

    launch_calls: list[dict] = []

    class FakeContext:
        def __init__(self):
            self.pages = []
        def new_page(self):
            return object()
        def close(self):
            pass

    class FakeChromium:
        def launch_persistent_context(self, **kwargs):
            launch_calls.append(kwargs)
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()
        def stop(self):
            pass

    class _Starter:
        def start(self):
            return FakePlaywright()

    # Stub the `from playwright.sync_api import sync_playwright` inside
    # _launch_inline. types.ModuleType + sys.modules-monkeypatch is the
    # cleanest way.
    import sys
    import types
    fake_mod = types.ModuleType("playwright.sync_api")
    fake_mod.sync_playwright = lambda: _Starter()
    fake_pkg = types.ModuleType("playwright")
    monkeypatch.setitem(sys.modules, "playwright", fake_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_mod)
    # Stub _attach_page_listeners (would call page.on(...) on our fake).
    monkeypatch.setattr(browser, "_attach_page_listeners", lambda *a, **kw: None)

    sess.ensure_running()
    assert len(launch_calls) == 1
    first_browser = sess.browser

    # Simulate external crash: drop the browser handle. is_alive flips
    # to False, _launch_inline should fire again on the next call.
    sess.browser = None

    sess.ensure_running()
    assert len(launch_calls) == 2
    assert sess.browser is not first_browser


def test_session_cleaned_up_on_orchestrator_failure(monkeypatch, qwe_temp_data_dir):
    """Regression for the goal_runner cleanup-outside-finally bug: when
    the orchestrator raises, _close_session must STILL run so the
    Chrome process and its SingletonLock don't leak.

    We don't actually run an orchestrator here — we test the contract:
    invoking goal_runner.run on a crashing orchestrator must call
    skills.browser._close_session(goal_id) before returning.
    """
    import asyncio
    import goal_runner
    import skills.browser as bs

    # Seed a session so we can observe it being closed.
    bs._get_session("g_crashy")
    assert "g_crashy" in bs._sessions

    closed: list[str] = []
    original_close = bs._close_session

    def _track_close(sid):
        closed.append(sid)
        original_close(sid)

    monkeypatch.setattr(bs, "_close_session", _track_close)

    # Make orchestrator raise.
    import orchestrator
    def _boom(*a, **kw):
        raise RuntimeError("orchestrator exploded")
    monkeypatch.setattr(orchestrator, "run_orchestrator", _boom)

    # Minimal db stubs so goal_runner.run doesn't blow up before reaching
    # the orchestrator call.
    import db
    monkeypatch.setattr(db, "get_goal", lambda gid: {
        "id": gid, "status": "pending", "source": "cli", "user_input": "x",
    })
    monkeypatch.setattr(db, "load_latest_checkpoint", lambda gid: None)
    monkeypatch.setattr(db, "log_goal_event", lambda *a, **kw: None)
    monkeypatch.setattr(db, "mark_goal_failed", lambda *a, **kw: None)
    monkeypatch.setattr(db, "mark_goal_paused", lambda *a, **kw: None)
    monkeypatch.setattr(db, "get_goal_plan", lambda gid: None)

    async def _go():
        shutdown = asyncio.Event()
        await goal_runner.run("g_crashy", shutdown)

    asyncio.run(_go())

    assert "g_crashy" in closed, (
        "goal_runner must call _close_session in its finally block even "
        "when the orchestrator raises, otherwise Chrome processes leak"
    )

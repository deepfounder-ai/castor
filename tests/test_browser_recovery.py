"""Browser auto-recovery from dead-session errors.

Live test exposed a real autonomy bug: the agent gave up on the WHOLE goal
after seeing `TargetClosedError` on a subagent's browser_open. That's
infrastructure noise — Chrome process died externally or session went
stale — not a reason to abandon the user's task.

The fix in skills/browser.py wraps every tool call: if the result string
contains a known dead-session marker, we auto-close the broken browser,
relaunch a fresh one, and retry the operation ONCE. Only after a second
dead-session result do we surface an error — and the surfaced text tells
the LLM to fall back to non-browser tools (http_request) or alternative
data sources.

These tests don't launch real Chromium — they monkey-patch _execute_impl
to control what the first/second attempts return.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_browser():
    spec = importlib.util.spec_from_file_location(
        "browser_under_test",
        str(Path(__file__).resolve().parent.parent / "skills" / "browser.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dead_session_markers_recognized():
    """The error-string heuristic must match the strings Playwright actually emits."""
    browser = _load_browser()
    assert browser._looks_like_dead_session("Target page, context or browser has been closed")
    assert browser._looks_like_dead_session("TargetClosedError: navigation failed")
    assert browser._looks_like_dead_session("Connection closed by peer")
    assert browser._looks_like_dead_session("BrowserClosedError")
    # NOT recoverable — these are real errors the agent must handle itself
    assert not browser._looks_like_dead_session("Timeout 30000ms exceeded")
    assert not browser._looks_like_dead_session("net::ERR_NAME_NOT_RESOLVED")
    assert not browser._looks_like_dead_session("Element not found: #foo")
    assert not browser._looks_like_dead_session("")


def test_recovery_retries_on_dead_session(monkeypatch):
    """First call returns TargetClosedError → close+relaunch → second call succeeds.

    The LLM sees the [recovered ...] prefix and the successful result, NOT the
    raw error. Infrastructure heals itself, agent keeps going.
    """
    browser = _load_browser()

    calls = []
    def _fake_impl(name, args):
        calls.append((name, args))
        if len(calls) == 1:
            return "Browser error (TargetClosedError): page has been closed"
        return "Title: Example\nURL: https://example.com\n\nbody text"

    closes = []
    def _fake_close():
        closes.append(True)

    ensures = []
    def _fake_ensure():
        ensures.append(True)

    monkeypatch.setattr(browser, "_execute_impl", _fake_impl)
    monkeypatch.setattr(browser, "_close_browser", _fake_close)
    monkeypatch.setattr(browser, "_ensure_browser", _fake_ensure)

    result = browser._execute_with_recovery("browser_open", {"url": "https://example.com"})

    # Tried twice
    assert len(calls) == 2
    # Cleaned up between attempts
    assert closes == [True]
    assert ensures == [True]
    # Surface the recovered output with a prefix the LLM can recognise
    assert "[recovered from dead session" in result
    assert "Example" in result


def test_recovery_skips_for_browser_close(monkeypatch):
    """browser_close on a dead session is a no-op — no point reopening just to close."""
    browser = _load_browser()
    monkeypatch.setattr(
        browser, "_execute_impl",
        lambda n, a: "Browser error (TargetClosedError): closed",
    )
    closes = []
    monkeypatch.setattr(browser, "_close_browser", lambda: closes.append(True))
    monkeypatch.setattr(browser, "_ensure_browser", lambda: None)

    result = browser._execute_with_recovery("browser_close", {})
    # No recovery attempted for browser_close
    assert closes == []
    assert result.startswith("Browser error")


def test_recovery_surfaces_error_after_second_failure(monkeypatch):
    """If the SECOND attempt also dies, escalate with a clear fall-back hint."""
    browser = _load_browser()

    monkeypatch.setattr(
        browser, "_execute_impl",
        lambda n, a: "Browser error (TargetClosedError): again",
    )
    monkeypatch.setattr(browser, "_close_browser", lambda: None)
    monkeypatch.setattr(browser, "_ensure_browser", lambda: None)

    result = browser._execute_with_recovery("browser_open", {"url": "x"})
    assert "twice in a row" in result
    # The hint must point the LLM at http_request as an escape hatch — this is
    # what makes the agent autonomous (try a different tool, not give up).
    assert "http_request" in result or "alternative" in result.lower()


def test_recovery_passes_through_non_session_errors(monkeypatch):
    """A real timeout or 404 should reach the LLM as-is — these are signals the
    agent SHOULD reason about (different URL, wait + retry, etc.), not infra noise."""
    browser = _load_browser()
    monkeypatch.setattr(
        browser, "_execute_impl",
        lambda n, a: "Browser error (TimeoutError): 30000ms exceeded",
    )
    closes = []
    monkeypatch.setattr(browser, "_close_browser", lambda: closes.append(True))

    result = browser._execute_with_recovery("browser_open", {"url": "x"})
    assert "30000ms exceeded" in result
    # No recovery attempted — agent has to handle this itself
    assert closes == []


def test_recovery_handles_ensure_browser_failure(monkeypatch):
    """If recovery itself fails (Playwright won't even relaunch), return a clear
    escalation message instead of crashing."""
    browser = _load_browser()
    monkeypatch.setattr(
        browser, "_execute_impl",
        lambda n, a: "Browser error (TargetClosedError): boom",
    )
    monkeypatch.setattr(browser, "_close_browser", lambda: None)
    def _broken_ensure():
        raise RuntimeError("playwright install broken")
    monkeypatch.setattr(browser, "_ensure_browser", _broken_ensure)

    result = browser._execute_with_recovery("browser_open", {"url": "x"})
    assert "recovery failed" in result.lower()
    assert "escalate" in result.lower() or "stuck" in result.lower()

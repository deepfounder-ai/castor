"""Bot API 10.1 rich messages — sendRichMessage primary send path.

Bot API 10.1 added ``sendRichMessage`` / ``editMessageText(rich_message=...)``
accepting an ``InputRichMessage`` with a ``markdown`` OR ``html`` string.
Telegram parses the full rich dialect server-side (headings, tables, math,
ordered/unordered/task lists, dividers, block/pull quotations, footnotes,
marked text, sub/superscript, media, details/collage, auto-detected
entities). Castor sends the agent's reply verbatim through it as the PRIMARY
path, with the classic MarkdownV2/HTML converters as the pre-10.1 fallback.

These tests mock ``telegram_bot._api`` so they make no network calls.
"""
from __future__ import annotations

import pytest

import telegram_bot as tb


@pytest.fixture(autouse=True)
def _reset_rich_cache():
    # _rich_supported is a module global cached across calls; reset per test.
    tb._rich_supported = None
    yield
    tb._rich_supported = None


class _ApiRecorder:
    """Records _api calls and returns scripted results by method name."""

    def __init__(self, results: dict):
        self.results = results  # method -> result dict (or callable)
        self.calls = []  # list of (method, kwargs)

    def __call__(self, method, token, **kwargs):
        self.calls.append((method, kwargs))
        r = self.results.get(method, {"ok": False, "description": "unscripted"})
        return r(kwargs) if callable(r) else r

    def methods(self):
        return [m for m, _ in self.calls]


# ── payload selection ───────────────────────────────────────────────────────


def test_payload_markdown_for_plain_reply():
    assert tb._rich_message_payload("# H1\n**bold** ==marked== $x^2$") == {
        "markdown": "# H1\n**bold** ==marked== $x^2$"
    }


def test_payload_html_for_agent_html():
    assert tb._rich_message_payload("<b>bold</b> and <tg-spoiler>s</tg-spoiler>") == {
        "html": "<b>bold</b> and <tg-spoiler>s</tg-spoiler>"
    }


# ── _send_rich_safe ─────────────────────────────────────────────────────────


def test_send_rich_uses_sendrichmessage_with_markdown(monkeypatch):
    rec = _ApiRecorder({"sendRichMessage": {"ok": True, "result": {"message_id": 1}}})
    monkeypatch.setattr(tb, "_api", rec)
    res = tb._send_rich_safe(123, "## heading\n- item", "TOK")
    assert res["ok"] is True
    assert rec.methods() == ["sendRichMessage"]
    method, kwargs = rec.calls[0]
    assert kwargs["rich_message"] == {"markdown": "## heading\n- item"}
    assert kwargs["chat_id"] == 123
    assert tb._rich_supported is True


def test_send_rich_edit_uses_editmessagetext(monkeypatch):
    rec = _ApiRecorder({"editMessageText": {"ok": True}})
    monkeypatch.setattr(tb, "_api", rec)
    res = tb._send_rich_safe(123, "**x**", "TOK", edit_message_id=55)
    assert res["ok"] is True
    method, kwargs = rec.calls[0]
    assert method == "editMessageText"
    assert kwargs["message_id"] == 55
    assert kwargs["rich_message"] == {"markdown": "**x**"}


def test_send_rich_caches_unsupported(monkeypatch):
    rec = _ApiRecorder({"sendRichMessage": {"ok": False, "description": "Bad Request: method not found"}})
    monkeypatch.setattr(tb, "_api", rec)
    res = tb._send_rich_safe(123, "x", "TOK")
    assert res["ok"] is False
    assert tb._rich_supported is False
    # Second call short-circuits without hitting _api again.
    res2 = tb._send_rich_safe(123, "y", "TOK")
    assert res2["ok"] is False
    assert rec.methods() == ["sendRichMessage"]  # only the first attempt


def test_send_rich_content_error_not_cached(monkeypatch):
    # A content-level rejection (not an unsupported-method signal) must NOT
    # disable rich for later well-formed messages.
    rec = _ApiRecorder({"sendRichMessage": {"ok": False, "description": "Bad Request: message is too long"}})
    monkeypatch.setattr(tb, "_api", rec)
    tb._send_rich_safe(123, "x", "TOK")
    assert tb._rich_supported is None  # still unknown, not disabled


def test_method_unsupported_classifier():
    # 404 / exact "Not Found" = method missing → unsupported.
    assert tb._method_unsupported({"error_code": 404}) is True
    assert tb._method_unsupported({"description": "Not Found"}) is True
    assert tb._method_unsupported({"description": "Bad Request: unknown method"}) is True
    # Content errors that merely CONTAIN "not found" are NOT method-missing.
    assert tb._method_unsupported({"error_code": 400, "description": "Bad Request: chat not found"}) is False
    assert tb._method_unsupported({"description": "Bad Request: message to edit not found"}) is False
    assert tb._method_unsupported({"description": "Bad Request: user not found"}) is False


def test_send_rich_chat_not_found_does_not_latch(monkeypatch):
    # Regression: "chat not found" is a content error (bad chat_id), NOT an
    # unsupported method. It must not disable rich messages process-wide.
    rec = _ApiRecorder({"sendRichMessage": {"ok": False, "error_code": 400,
                                            "description": "Bad Request: chat not found"}})
    monkeypatch.setattr(tb, "_api", rec)
    tb._send_rich_safe(999, "x", "TOK")
    assert tb._rich_supported is None  # NOT latched off

    # But a real 404 (method missing on an old Bot API) does latch.
    tb._rich_supported = None
    rec404 = _ApiRecorder({"sendRichMessage": {"ok": False, "error_code": 404, "description": "Not Found"}})
    monkeypatch.setattr(tb, "_api", rec404)
    tb._send_rich_safe(123, "x", "TOK")
    assert tb._rich_supported is False


def test_send_rich_draft_chat_not_found_does_not_latch(monkeypatch):
    rec = _ApiRecorder({"sendRichMessageDraft": {"ok": False, "error_code": 400,
                                                 "description": "Bad Request: chat not found"}})
    monkeypatch.setattr(tb, "_api", rec)
    tb._send_rich_draft_safe(999, 1, "x", "TOK")
    assert tb._rich_draft_supported is None  # NOT latched off


def test_send_rich_empty_text_noop(monkeypatch):
    rec = _ApiRecorder({})
    monkeypatch.setattr(tb, "_api", rec)
    res = tb._send_rich_safe(123, "", "TOK")
    assert res["ok"] is False
    assert rec.calls == []


# ── send_message integration ────────────────────────────────────────────────


def test_send_message_rich_first_no_fallback(monkeypatch):
    rec = _ApiRecorder({"sendRichMessage": {"ok": True, "result": {"message_id": 9}}})
    monkeypatch.setattr(tb, "_api", rec)
    monkeypatch.setattr(tb, "get_token", lambda: "TOK")
    tb.send_message(123, "# title\n**bold**\n\n| a | b |\n|:--|--:|\n| 1 | 2 |")
    # Rich succeeded → no MarkdownV2/HTML fallback attempts.
    assert rec.methods() == ["sendRichMessage"]


def test_send_message_falls_back_when_rich_unsupported(monkeypatch):
    rec = _ApiRecorder({
        "sendRichMessage": {"ok": False, "description": "method not found"},
        "sendMessage": {"ok": True},
    })
    monkeypatch.setattr(tb, "_api", rec)
    monkeypatch.setattr(tb, "get_token", lambda: "TOK")
    tb.send_message(123, "**bold**")
    methods = rec.methods()
    assert methods[0] == "sendRichMessage"
    # Then it fell back to the classic chain (MarkdownV2 via sendMessage).
    assert "sendMessage" in methods


def test_send_message_html_reply_uses_html_field(monkeypatch):
    captured = {}

    def fake_api(method, token, **kwargs):
        captured.setdefault(method, kwargs)
        return {"ok": True} if method == "sendRichMessage" else {"ok": False}

    monkeypatch.setattr(tb, "_api", fake_api)
    monkeypatch.setattr(tb, "get_token", lambda: "TOK")
    tb.send_message(123, "<b>bold</b> <i>it</i>")
    assert captured["sendRichMessage"]["rich_message"] == {"html": "<b>bold</b> <i>it</i>"}


# ── streaming drafts + <tg-thinking> ────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_draft_cache():
    tb._rich_draft_supported = None
    yield
    tb._rich_draft_supported = None


def test_build_thinking_draft_body_both():
    out = tb._build_thinking_draft_body("reasoning here", "partial answer")
    assert "<tg-thinking>reasoning here</tg-thinking>" in out
    assert "partial answer" in out
    # thinking block comes first
    assert out.index("tg-thinking") < out.index("partial answer")


def test_build_thinking_draft_body_thinking_only():
    out = tb._build_thinking_draft_body("just thinking", "")
    assert out == "<tg-thinking>just thinking</tg-thinking>"


def test_build_thinking_draft_body_content_only():
    assert tb._build_thinking_draft_body("", "answer") == "answer"


def test_build_thinking_draft_body_empty():
    assert tb._build_thinking_draft_body("", "") == ""
    assert tb._build_thinking_draft_body("  ", "  ") == ""


def test_build_thinking_draft_body_caps_thinking():
    long_th = "x" * 5000
    out = tb._build_thinking_draft_body(long_th, "")
    # capped to the last 600 chars
    assert out.count("x") == 600


def test_send_rich_draft_passes_draft_id(monkeypatch):
    rec = _ApiRecorder({"sendRichMessageDraft": {"ok": True}})
    monkeypatch.setattr(tb, "_api", rec)
    ok = tb._send_rich_draft_safe(123, 42, "<tg-thinking>hm</tg-thinking>\n\ntext", "TOK")
    assert ok is True
    method, kwargs = rec.calls[0]
    assert method == "sendRichMessageDraft"
    assert kwargs["draft_id"] == 42
    assert kwargs["chat_id"] == 123
    assert "rich_message" in kwargs
    assert tb._rich_draft_supported is True


def test_send_rich_draft_caches_unsupported(monkeypatch):
    rec = _ApiRecorder({"sendRichMessageDraft": {"ok": False, "description": "method not found"}})
    monkeypatch.setattr(tb, "_api", rec)
    assert tb._send_rich_draft_safe(123, 1, "x", "TOK") is False
    assert tb._rich_draft_supported is False
    # second call short-circuits
    assert tb._send_rich_draft_safe(123, 2, "y", "TOK") is False
    assert rec.methods() == ["sendRichMessageDraft"]


def test_send_rich_draft_empty_noop(monkeypatch):
    rec = _ApiRecorder({})
    monkeypatch.setattr(tb, "_api", rec)
    monkeypatch.setattr(tb, "get_token", lambda: "TOK")
    assert tb.send_rich_draft(123, 1, "", "TOK") == {"ok": False}
    assert rec.calls == []

"""Outbound rich-message formatting — MarkdownV2 + HTML converters.

Castor's agent emits a standard-markdown dialect; ``telegram_bot._to_markdownv2``
and ``telegram_bot._to_html`` translate it into Telegram's two formatting
styles. This file pins the entity set documented at
https://core.telegram.org/bots/api#formatting-options including the newer
entities added through Bot API 10.1: spoiler, underline, block quotation,
expandable block quotation, and custom emoji.

The source dialect (what the LLM writes):
  **bold**          → bold
  *italic* _italic_ → italic
  __underline__     → underline           (NEW — safe: agent uses ** for bold)
  ~~strike~~        → strikethrough
  ||spoiler||       → spoiler              (NEW)
  `code` ```block``` → code
  [text](url)       → link
  ![👍](tg://emoji?id=N) → custom emoji    (NEW)
  > line            → block quotation      (NEW for MarkdownV2; HTML already had it)
  >! first line     → expandable block quotation (NEW)
"""
from __future__ import annotations

import telegram_bot as tb


# ── MarkdownV2 ──────────────────────────────────────────────────────────────


def test_md2_plain_text_escapes_reserved():
    # Sanity: reserved chars in plain text get backslash-escaped.
    out = tb._to_markdownv2("a.b-c!")
    assert out == "a\\.b\\-c\\!"


def test_md2_bold_italic_unchanged():
    assert tb._to_markdownv2("**hi**") == "*hi*"
    assert tb._to_markdownv2("*hi*") == "_hi_"


def test_md2_spoiler():
    # ||spoiler|| stays ||spoiler|| — the pipes around it must NOT be escaped.
    out = tb._to_markdownv2("a ||secret|| b")
    assert "||secret||" in out
    # The surrounding plain text is still escaped normally, but the spoiler
    # delimiters survive intact.
    assert "\\|" not in out.replace("||secret||", "")


def test_md2_spoiler_escapes_inner_reserved():
    out = tb._to_markdownv2("||a.b||")
    assert out == "||a\\.b||"


def test_md2_underline():
    out = tb._to_markdownv2("__under__")
    assert out == "__under__"


def test_md2_underline_distinct_from_bold():
    # ** is bold (*), __ is underline (__) — they must not collide.
    out = tb._to_markdownv2("**b** and __u__")
    assert "*b*" in out
    assert "__u__" in out


def test_md2_blockquote_parity():
    # Regression: blockquote existed in _to_html but was missing from
    # _to_markdownv2, so quoted lines rendered as escaped '>' literals.
    out = tb._to_markdownv2("> quoted line")
    assert out.startswith(">")
    # The '>' that starts a quote must NOT be backslash-escaped.
    assert not out.startswith("\\>")
    assert "quoted line" in out


def test_md2_blockquote_multiline():
    out = tb._to_markdownv2("> line one\n> line two")
    lines = out.split("\n")
    assert all(ln.startswith(">") and not ln.startswith("\\>") for ln in lines)


def test_md2_expandable_blockquote():
    # >! marks an expandable quote: MarkdownV2 wants **> on the first line
    # and a trailing || after the last line.
    out = tb._to_markdownv2(">! hidden long quote")
    assert out.startswith("**>")
    assert out.rstrip().endswith("||")
    assert "hidden long quote" in out


def test_md2_custom_emoji():
    out = tb._to_markdownv2("look ![👍](tg://emoji?id=5368324170671202286)")
    assert "![👍](tg://emoji?id=5368324170671202286)" in out


def test_md2_code_block_untouched():
    out = tb._to_markdownv2("```python\nx = 1\n```")
    assert "x = 1" in out
    assert "```" in out


# ── HTML ────────────────────────────────────────────────────────────────────


def test_html_bold_italic():
    assert tb._to_html("**hi**") == "<b>hi</b>"
    assert tb._to_html("*hi*") == "<i>hi</i>"


def test_html_spoiler():
    out = tb._to_html("a ||secret|| b")
    assert "<tg-spoiler>secret</tg-spoiler>" in out


def test_html_underline():
    out = tb._to_html("__under__")
    assert out == "<u>under</u>"


def test_html_blockquote():
    out = tb._to_html("> quoted")
    assert "<blockquote>quoted</blockquote>" in out


def test_html_expandable_blockquote():
    out = tb._to_html(">! hidden")
    assert "<blockquote expandable>" in out
    assert "hidden" in out


def test_html_custom_emoji():
    out = tb._to_html("![👍](tg://emoji?id=5368324170671202286)")
    assert '<tg-emoji emoji-id="5368324170671202286">👍</tg-emoji>' in out


def test_html_escapes_plain_angle_brackets():
    # Plain < > & still escaped so they don't break the HTML parse.
    out = tb._to_html("1 < 2 & 3 > 0")
    assert "&lt;" in out
    assert "&amp;" in out
    assert "&gt;" in out


# ── Agent-emitted raw HTML detection ────────────────────────────────────────
#
# When the agent answers with literal Telegram HTML (demonstrating formatting,
# or just preferring HTML), the send path must ship it verbatim with
# parse_mode=HTML — NOT route it through _to_markdownv2 (which renders the tags
# as the literal text "<b>bold</b>") or _to_html (which would double-escape the
# existing tags into "&lt;b&gt;"). _looks_like_html gates that fast-path.


def test_looks_like_html_detects_closing_tags():
    assert tb._looks_like_html("<b>bold</b>") is True
    assert tb._looks_like_html("<i>i</i> and <code>c</code>") is True
    assert tb._looks_like_html("<tg-spoiler>s</tg-spoiler>") is True
    assert tb._looks_like_html("<blockquote>q</blockquote>") is True
    assert tb._looks_like_html('<pre><code class="language-python">x</code></pre>') is True
    assert tb._looks_like_html('<a href="http://e.com">link</a>') is True


def test_looks_like_html_false_for_markdown():
    assert tb._looks_like_html("just **bold** and _italic_ markdown") is False
    assert tb._looks_like_html("> a quote\nand `code`") is False


def test_looks_like_html_false_for_unclosed_mention():
    # Talking ABOUT a tag (no closing tag) is not HTML content.
    assert tb._looks_like_html("use the <b> tag for bold") is False
    assert tb._looks_like_html("compare a < b and c > d") is False

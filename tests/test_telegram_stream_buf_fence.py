"""Regression shield for #11 — final Telegram message must use the
streamed buffer when present, not just the LLM's reply text.

When a skill calls ``ctx.emit_content(text)`` directly, that content
flows through the streaming buffer (``_stream_buf``) and is shown to the
user during the reply. But the final ``editMessageText`` call used to
overwrite that with ``response`` (= ``result.reply`` = LLM-only text),
so the user briefly saw the skill output, then it vanished.

The fix uses ``_stream_buf`` as the final message body when non-empty,
falling back to ``response`` only when nothing was streamed.

This isn't a behaviour test — patching the closures inside
``_handle_message`` would require ~200 lines of mocking. Instead it's
a *fence*: read the source and confirm the patch is in place. If
someone reverts or refactors away the logic without preserving it,
this test catches it at PR-review time.
"""

from __future__ import annotations

import inspect
import re

import telegram_bot


def test_handle_message_uses_stream_buf_in_final_parts():
    """``parts.append`` must take the streamed buffer when available."""
    src = inspect.getsource(telegram_bot)
    # `streamed` is derived from `_stream_buf` (now also run through the
    # tool-call-markup strip), `reply_text` falls back streamed -> response,
    # and the appended value prefers `reply_text`. Whitespace-tolerant.
    pat = re.compile(
        r"streamed\s*=\s*_strip_tc\(\(?_stream_buf"
        r".*?"
        r"reply_text\s*=\s*streamed\s+or\s+"
        r".*?"
        r"parts\.append\(\s*reply_text\s+if\s+reply_text",
        re.DOTALL,
    )
    assert pat.search(src), (
        "telegram_bot._handle_message must derive `streamed` from `_stream_buf`, "
        "fall back `reply_text = streamed or <stripped response>`, and append "
        "`reply_text if reply_text else ...` so direct skill emit_content() "
        "output isn't overwritten by the LLM-only reply text. See issue #11."
    )


def test_handle_message_does_not_unconditionally_append_response():
    """Guard against accidental revert to ``parts.append(response)`` solo."""
    src = inspect.getsource(telegram_bot)
    # The bad pattern — bare append of response with no streamed-buf branch
    # — should NOT appear immediately after the comment block that sets up
    # the parts list. We just check the canonical bad single-line form is
    # gone from the relevant section.
    bad = "parts = []\n                parts.append(response)\n"
    assert bad not in src, (
        "Found the pre-fix line `parts.append(response)` immediately after "
        "`parts = []`. This is the regression from #11 — final message "
        "would overwrite skill emit_content() output. Revert and apply "
        "the streamed-buf fallback instead."
    )

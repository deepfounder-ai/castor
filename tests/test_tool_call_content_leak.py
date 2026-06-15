"""Tool-call XML must never leak into the user-visible reply.

MiniMax-M2 / Anthropic-style models emit tool calls as CONTENT tokens
(<invoke>/<minimax:tool_call>/<parameter>) rather than native tool_calls. They
are executed via text-extraction, so the markup must be suppressed from the
streamed content AND the final reply — otherwise Telegram/web show raw
`document.querySelector(...) </minimax:tool_call>` fragments (the reported bug).
"""
from __future__ import annotations

import types

from utils import strip_tool_call_markup
from agent_events import EventEmitter
from agent_budget import BudgetLimits
from agent_loop import run_loop


# ── strip_tool_call_markup (final-reply safety net) ──────────────────────────

def test_strip_removes_complete_invoke_block_keeps_reply():
    t = 'Готово. <invoke name="browser_eval"><parameter name="code">x()</parameter></invoke>'
    assert strip_tool_call_markup(t) == "Готово."


def test_strip_removes_minimax_envelope():
    t = ('<minimax:tool_call><invoke name="browser_open">'
         '<parameter name="url">https://t.com</parameter></invoke></minimax:tool_call>')
    assert strip_tool_call_markup(t) == ""


def test_strip_truncates_at_stray_opening_marker():
    assert strip_tool_call_markup("Answer here <minimax:tool_call") == "Answer here"


def test_strip_plain_text_unchanged():
    assert strip_tool_call_markup("Just a normal reply.") == "Just a normal reply."


def test_strip_empty_and_none_safe():
    assert strip_tool_call_markup("") == ""
    assert strip_tool_call_markup(None) is None


# ── live streaming suppression + execution via run_loop ──────────────────────

def _fake_client(chunks):
    class _Delta:
        def __init__(self, content):
            self.content = content
            self.tool_calls = None
            self.role = "assistant"
            self.reasoning_content = None
            self.reasoning = None

    class _Chunk:
        def __init__(self, content, finish=None):
            self.choices = [types.SimpleNamespace(
                delta=_Delta(content), finish_reason=finish, message=_Delta(content))]
            self.usage = None
            self.id = "f"
            self.model = "fake"

    class _Completions:
        def __init__(self):
            self.n = 0

        def create(self, **kw):
            self.n += 1
            seq = chunks[0] if self.n == 1 else ["done."]

            def _gen():
                for c in seq:
                    yield _Chunk(c)
                yield _Chunk("", finish="stop")
            return _gen()

    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))


def test_tool_call_xml_suppressed_from_content_but_executed():
    recorded, executed = [], []
    emitter = EventEmitter()
    emitter.on("content_delta", lambda e: recorded.append(e.data["text"]))

    def _exec(name, args):
        executed.append((name, args))
        return "ok"

    # The model streams a preface + an inline tool call as content.
    chunks = ["Открываю. ",
              '<minimax:tool_call><invoke name="browser_open">',
              '<parameter name="url">https://t.com</parameter></invoke></minimax:tool_call>']
    run_loop(
        client=_fake_client([chunks]),
        model="fake", messages=[{"role": "user", "content": "go"}],
        tools=[{"type": "function", "function": {"name": "browser_open", "parameters": {}}}],
        emitter=emitter, budget=BudgetLimits.from_config(),
        tool_executor=_exec,
    )
    streamed = "".join(recorded)
    assert "<invoke" not in streamed and "minimax:tool_call" not in streamed and "<parameter" not in streamed
    assert streamed.strip().startswith("Открываю.")
    # The tool was actually executed via text-extraction.
    assert ("browser_open", {"url": "https://t.com"}) in executed


# ── Extended-tool calls without prior tool_search execute + auto-activate ────
# MiniMax calls extended tools (browser_wait_for, …) straight from training,
# without a tool_search first. The active set sent to the LLM doesn't contain
# them, but the main agent passes the FULL known-tool set for EXTRACTION so the
# call still runs, and auto-activates the tool for later turns.

def test_extended_tool_extracted_via_broader_name_set():
    recorded, executed, activated = [], [], []
    emitter = EventEmitter()
    emitter.on("content_delta", lambda e: recorded.append(e.data["text"]))

    def _exec(name, args):
        executed.append((name, args))
        return "true"

    chunks = ['<minimax:tool_call><invoke name="browser_wait_for">'
              '<parameter name="expression">document.querySelector(\'input\') !== null</parameter>'
              '</invoke></minimax:tool_call>']
    run_loop(
        client=_fake_client([chunks]),
        model="fake", messages=[{"role": "user", "content": "go"}],
        # Active set does NOT include browser_wait_for...
        tools=[{"type": "function", "function": {"name": "browser_open", "parameters": {}}}],
        emitter=emitter, budget=BudgetLimits.from_config(),
        tool_executor=_exec,
        # ...but the broader extraction set does, and we record activation.
        extraction_tool_names={"browser_open", "browser_wait_for"},
        on_extended_tool=lambda n: activated.append(n),
    )
    assert any(name == "browser_wait_for" for name, _ in executed), "inactive tool was not executed"
    assert "browser_wait_for" in activated, "inactive tool was not auto-activated"
    streamed = "".join(recorded)
    assert "<invoke" not in streamed and "minimax:tool_call" not in streamed


def test_inactive_tool_not_extracted_without_broader_set():
    # Subagents pass NO extraction_tool_names → the restricted whitelist stays
    # the gate, so a call to a tool outside it is NOT executed.
    executed = []
    emitter = EventEmitter()
    chunks = ['<invoke name="shell"><parameter name="command">rm -rf /</parameter></invoke>']
    run_loop(
        client=_fake_client([chunks]),
        model="fake", messages=[{"role": "user", "content": "go"}],
        tools=[{"type": "function", "function": {"name": "browser_open", "parameters": {}}}],
        emitter=emitter, budget=BudgetLimits.from_config(),
        tool_executor=lambda n, a: executed.append((n, a)) or "x",
    )
    assert not executed, "tool outside the active set must NOT run without extraction_tool_names"

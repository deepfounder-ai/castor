"""Regression: inline-thinking models must not lose the start of the reply.

When a model streams reasoning inline as ``<think>...</think>answer`` (rather
than via a separate ``reasoning_content`` field), the closing ``</think>`` tag
often shares a single streaming delta with the first chunk of the real answer
(e.g. ``"</think>Окей"``). The agent loop must emit the post-tag answer text to
``emitter.content`` — not silently drop it — otherwise Telegram's final message
(built from the streamed content buffer) shows a reply with its start cut off.
"""
from __future__ import annotations

import types

from agent_events import EventEmitter
from agent_budget import BudgetLimits
from agent_loop import run_loop


def _fake_client(chunk_texts: list[str]):
    """A streaming OpenAI-compatible fake that yields each string as one delta."""

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
            self.id = "fake"
            self.model = "fake-model"

    class _Completions:
        def create(self, **kw):
            def _gen():
                for t in chunk_texts:
                    yield _Chunk(t)
                yield _Chunk("", finish="stop")
            return _gen()

    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))


def _run(chunk_texts):
    recorded = []
    emitter = EventEmitter()
    emitter.on("content_delta", lambda e: recorded.append(e.data["text"]))
    result = run_loop(
        client=_fake_client(chunk_texts),
        model="fake-model",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        emitter=emitter,
        budget=BudgetLimits.from_config(),
    )
    return "".join(recorded), result["reply"]


def test_answer_after_closing_think_tag_is_streamed():
    # The answer text rides in the SAME delta as </think>.
    streamed, reply = _run(["<think>", "musing", "</think>Окей, без проблем."])
    assert streamed == "Окей, без проблем.", f"streamed lost the start: {streamed!r}"
    assert "Окей, без проблем." in reply


def test_answer_before_opening_think_tag_is_streamed():
    # Some models lead with a word, then open a think block.
    streamed, _ = _run(["Ответ: <think>", "musing", "</think> готово"])
    assert streamed.startswith("Ответ: ")
    assert "готово" in streamed
    assert "musing" not in streamed


def test_plain_answer_without_think_unaffected():
    streamed, _ = _run(["Окей, ", "без проблем."])
    assert streamed == "Окей, без проблем."

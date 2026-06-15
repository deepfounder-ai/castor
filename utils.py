"""Shared utilities used across agent, agent_loop, tasks, etc."""

import re


def strip_thinking(text: str) -> str:
    """Remove thinking blocks from model output.

    Handles:
    - <think>...</think> tags (Qwen, Llama)
    - <|channel>thought... (Gemma)
    - Stray special tokens <|...|>
    """
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    # Gemma <|channel>thought — extract reply after thought block
    if "<|channel>" in text:
        segments = re.split(r"<\|channel\>\w*\s*", text)
        reply_parts = [s.strip() for s in segments if s.strip() and len(s.strip()) > 5]
        text = reply_parts[-1] if reply_parts else ""
    text = re.sub(r"<\|[^>]+\>", "", text)
    return text.strip()


def extract_thinking(text: str) -> str:
    """Extract thinking content from <think> tags."""
    match = re.search(r"<think>(.*?)</think>", text, re.DOTALL)
    return match.group(1).strip() if match else ""


# Tool-call XML dialects some models (MiniMax-M2, Anthropic-style) emit as
# CONTENT instead of native tool_calls. They're executed via text-extraction
# (agent_loop._extract_tool_from_text), so they must never reach the user as
# reply text — otherwise the chat shows raw `<invoke>…</invoke>` /
# `</minimax:tool_call>` fragments. Mirror of strip_thinking for tool markup.
_TC_BLOCK_RE = [
    re.compile(r"<minimax:tool_call>.*?</minimax:tool_call>", re.DOTALL),
    re.compile(r"<function_calls>.*?</function_calls>", re.DOTALL),
    re.compile(r"<invoke\b.*?</invoke>", re.DOTALL),
    re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL),
]
# Partial / unbalanced markers — truncate the reply at the earliest one so a
# tool call split across stream chunks can't leave a dangling tag or argument.
_TC_TRAIL_MARKERS = (
    "<minimax:tool_call", "</minimax:tool_call", "<invoke", "</invoke",
    "<function_calls", "</function_calls", "<tool_call", "</tool_call",
    "!<function_call", "<parameter", "</parameter",
)


def strip_tool_call_markup(text: str) -> str:
    """Remove tool-call XML markup from model reply text. Returns clean text."""
    if not text:
        return text
    for rx in _TC_BLOCK_RE:
        text = rx.sub("", text)
    cut = None
    for mk in _TC_TRAIL_MARKERS:
        i = text.find(mk)
        if i != -1:
            cut = i if cut is None else min(cut, i)
    if cut is not None:
        text = text[:cut]
    return text.strip()

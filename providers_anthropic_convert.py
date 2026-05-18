"""Pure dict-in / dict-out converters between OpenAI- and Anthropic-shape
LLM request/response payloads.

This module is intentionally dependency-free: ``json`` + project ``logger``
only. The official ``anthropic`` SDK is never imported here — the
``providers_anthropic`` client owns SDK access. That separation lets the
converter be unit-tested without the SDK installed AND lets the stream
reassembler in ``providers_anthropic_stream`` reuse ``_map_stop_reason``
without taking a hard dependency on the client.

The contract is captured in ``docs/specs/2026-05-17-native-anthropic-adapter.md``
(workstream A). The function docstrings below restate the exact behavior
expected.

Note on tool-call arguments: Anthropic returns tool inputs as JSON
objects, OpenAI returns them as JSON strings. ``to_anthropic_request``
parses the OpenAI string into a dict for ``tool_use.input``; if the
string is not valid JSON we fall back to ``{"raw": <original>}`` so the
upstream model still sees the bytes rather than silently dropping the
argument.
"""

from __future__ import annotations

import json

import logger

_log = logger.get("providers_anthropic_convert")

# Anthropic rejects tool_result content above ~200 KB per block. We
# truncate proactively with a clear marker so the model can see what was
# dropped rather than receiving a generic API error.
_TOOL_RESULT_MAX_CHARS = 200 * 1024


# ── stop-reason mapping ──────────────────────────────────────────────────────

_STOP_REASON_MAP = {
    "end_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "stop_sequence": "stop",
    "pause_turn": "stop",
}


def _map_stop_reason(anthropic_stop):
    """Map an Anthropic stop_reason string to the OpenAI ``finish_reason``
    surface that ``agent_loop`` already understands.

    Mappings:
      ``"end_turn"`` → ``"stop"``
      ``"max_tokens"`` → ``"length"``
      ``"tool_use"`` → ``"tool_calls"``
      ``"stop_sequence"`` → ``"stop"``
      ``"pause_turn"`` → ``"stop"`` (rare — beta API feature)
      ``None`` → ``None`` (passed through, used by streaming mid-events)
      anything else → ``"stop"`` (safe default)

    Returned strings are exactly the values listed in the contract section
    of the spec (``"stop" | "tool_calls" | "length" | "content_filter"``).
    """
    if anthropic_stop is None:
        return None
    return _STOP_REASON_MAP.get(anthropic_stop, "stop")


# ── request converter ────────────────────────────────────────────────────────


def _convert_tool_schema(openai_tool):
    """Convert one OpenAI tool schema entry to the Anthropic shape.

    OpenAI:
        {"type": "function",
         "function": {"name": ..., "description": ..., "parameters": {...}}}
    Anthropic:
        {"name": ..., "description": ..., "input_schema": {...}}

    Tools that don't match the OpenAI ``{type: "function", function: {...}}``
    envelope (already Anthropic-shape, custom, etc.) pass through unchanged.
    """
    if not isinstance(openai_tool, dict):
        return openai_tool
    if openai_tool.get("type") != "function" or "function" not in openai_tool:
        # Already Anthropic-shape or unknown — leave it alone.
        return openai_tool
    fn = openai_tool["function"] or {}
    out = {"name": fn.get("name", "")}
    if "description" in fn:
        out["description"] = fn["description"]
    # OpenAI uses "parameters"; Anthropic uses "input_schema". Default to
    # an empty object schema when missing (Anthropic rejects tools with no
    # schema).
    out["input_schema"] = fn.get("parameters", {"type": "object", "properties": {}})
    return out


def _coerce_tool_input(arguments):
    """Parse an OpenAI tool-call ``arguments`` string into a dict for
    Anthropic's ``tool_use.input``.

    Anthropic expects an object, not a string. If the string is empty,
    return ``{}``. If it can't be parsed as JSON, fall back to
    ``{"raw": <original>}`` so the value is preserved verbatim.
    """
    if arguments is None or arguments == "":
        return {}
    if isinstance(arguments, dict):
        return arguments
    try:
        loaded = json.loads(arguments)
        if isinstance(loaded, dict):
            return loaded
        # Tool inputs that aren't objects (rare — list/string/number JSON
        # literals) still need to live under a key.
        return {"value": loaded}
    except (TypeError, ValueError):
        return {"raw": arguments}


def _truncate_tool_result_content(content):
    """Cap a tool_result content string at ``_TOOL_RESULT_MAX_CHARS``.

    Returns the (possibly truncated) string with an explicit truncation
    marker. Non-string content (list of content blocks, etc.) is returned
    unchanged — the caller decides how to handle multimodal blocks.
    """
    if not isinstance(content, str):
        return content
    if len(content) <= _TOOL_RESULT_MAX_CHARS:
        return content
    omitted = len(content) - _TOOL_RESULT_MAX_CHARS
    return (
        content[:_TOOL_RESULT_MAX_CHARS]
        + f"\n[truncated by adapter: {omitted} chars omitted]"
    )


def _assistant_to_anthropic(msg):
    """Translate one OpenAI assistant message to Anthropic shape.

    Two scenarios:

    1. Pure text reply: assistant has ``content`` (a string) and no
       ``tool_calls`` → emit ``{role: "assistant", content: "<text>"}``.

    2. Tool-use turn: assistant has ``tool_calls`` (possibly with a
       leading ``content`` string explaining the call) → emit
       ``{role: "assistant", content: [<text block>?, <tool_use blocks...>]}``.
       Anthropic content lists tolerate an empty array but reject ``None``;
       always emit a list when tool_calls are present.
    """
    raw_content = msg.get("content")
    tool_calls = msg.get("tool_calls")

    if not tool_calls:
        # Pure text. Pass content through (string, list of content
        # blocks, or None — Anthropic accepts string or list).
        return {"role": "assistant", "content": raw_content if raw_content is not None else ""}

    blocks = []
    if isinstance(raw_content, str) and raw_content:
        blocks.append({"type": "text", "text": raw_content})
    elif isinstance(raw_content, list):
        # Pre-existing content blocks (rare with OpenAI shape, but pass
        # them through so multimodal callers aren't surprised).
        blocks.extend(raw_content)

    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        tool_input = _coerce_tool_input(fn.get("arguments"))
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "input": tool_input,
            }
        )

    return {"role": "assistant", "content": blocks}


def _tool_to_anthropic(msg):
    """Translate one OpenAI tool message to Anthropic shape.

    OpenAI:
        {"role": "tool", "tool_call_id": "...", "content": "..."}
    Anthropic:
        {"role": "user", "content": [
            {"type": "tool_result",
             "tool_use_id": "...",
             "content": "..."}
        ]}

    Tool-result content longer than 200 KB is truncated with a marker so
    Anthropic doesn't reject the request.
    """
    content = msg.get("content", "")
    content = _truncate_tool_result_content(content)
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": content,
            }
        ],
    }


def to_anthropic_request(
    *,
    model,
    messages,
    tools,
    max_tokens=4096,
    temperature=None,
    stream=False,
):
    """Translate an OpenAI-style request payload into the Anthropic
    ``messages.create()`` kwargs shape.

    Returns a dict ready to splat into
    ``anthropic.Anthropic().messages.create(**kwargs)``.

    Translation rules (each covered by a test):
      * ``{role: "system", content: ...}`` → top-level ``system=`` kwarg.
        Multiple system messages are concatenated with two newlines in
        the order they appeared.
      * ``{role: "user" | "assistant"}`` → Anthropic messages list.
      * ``{role: "tool", tool_call_id, content}`` → user message with
        ``[{type: "tool_result", tool_use_id, content: <str>}]`` content.
      * Assistant ``{tool_calls: [...]}`` → assistant message with
        ``[{type: "tool_use", id, name, input}]`` content. The OpenAI
        arguments string is parsed via ``_coerce_tool_input``.
      * OpenAI tool schemas → ``{name, description, input_schema}``.

    Edge cases:
      * Empty messages list → ``ValueError``.
      * First non-system message is assistant → prepend a synthetic
        ``(continuing from prior context)`` user message and log a
        warning. Anthropic rejects assistant-first turns; this is the
        same workaround OpenRouter uses today.
      * Tool-result content > 200 KB → truncated with marker.
      * ``temperature=None`` → key omitted from the result so the
        Anthropic default applies.
      * ``stream=True`` → key included as ``stream=True`` in the result.
    """
    if not messages:
        raise ValueError("messages cannot be empty")

    # Pass 1: pull system messages out, preserve order.
    system_parts = []
    rest = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "system":
            sys_content = m.get("content")
            if isinstance(sys_content, str):
                system_parts.append(sys_content)
            elif isinstance(sys_content, list):
                # Concatenate text blocks; pass other shapes verbatim
                # (json-serialized) to avoid silently losing data.
                for blk in sys_content:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        system_parts.append(blk.get("text", ""))
                    else:
                        system_parts.append(json.dumps(blk, ensure_ascii=False))
            elif sys_content is not None:
                system_parts.append(str(sys_content))
        else:
            rest.append(m)

    # Pass 2: translate each non-system message.
    out_messages = []
    if rest and rest[0].get("role") == "assistant":
        _log.warning(
            "first non-system message is assistant; prepending synthetic user "
            "turn to satisfy Anthropic's user-first requirement"
        )
        out_messages.append(
            {"role": "user", "content": "(continuing from prior context)"}
        )

    for m in rest:
        role = m.get("role")
        if role == "assistant":
            out_messages.append(_assistant_to_anthropic(m))
        elif role == "tool":
            out_messages.append(_tool_to_anthropic(m))
        elif role == "user":
            # Anthropic accepts string OR list-of-blocks for user.content.
            # Pass through verbatim so multimodal (image_url etc.) blocks
            # survive — converting them is the caller's responsibility.
            content = m.get("content", "")
            out_messages.append({"role": "user", "content": content})
        else:
            # Unknown role — treat as user to avoid losing the turn.
            _log.warning("unknown role %r in messages; coercing to user", role)
            out_messages.append({"role": "user", "content": m.get("content", "")})

    # Pass 3: tools schema.
    anthropic_tools = None
    if tools:
        anthropic_tools = [_convert_tool_schema(t) for t in tools]

    req = {
        "model": model,
        "messages": out_messages,
        "max_tokens": max_tokens,
    }
    if system_parts:
        req["system"] = "\n\n".join(system_parts)
    if anthropic_tools is not None:
        req["tools"] = anthropic_tools
    if temperature is not None:
        req["temperature"] = temperature
    if stream:
        req["stream"] = True
    return req


# ── response converter ───────────────────────────────────────────────────────


def from_anthropic_response(resp):
    """Translate Anthropic's non-streaming response dict back to the
    OpenAI-shape dict that ``agent_loop`` and other callers expect.

    Anthropic response top-level keys (per their API spec):
      ``id``, ``type``, ``role``, ``model``, ``content`` (list of blocks),
      ``stop_reason``, ``stop_sequence``, ``usage``.

    Output structure (sufficient to feed an OpenAI ChatCompletion
    wrapper)::

        {
          "id": ...,
          "model": ...,
          "choices": [{
            "index": 0,
            "message": {
              "role": "assistant",
              "content": <concatenated text blocks, or None>,
              "tool_calls": [
                {"id": ..., "type": "function",
                 "function": {"name": ..., "arguments": json.dumps(input)}}
              ] or None,
              "reasoning": <concatenated thinking blocks, or None>
            },
            "finish_reason": _map_stop_reason(stop_reason)
          }],
          "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input + output,
            "cache_creation_input_tokens": ... or 0,
            "cache_read_input_tokens": ... or 0
          }
        }
    """
    content_blocks = resp.get("content") or []

    text_parts = []
    thinking_parts = []
    tool_calls = []

    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "thinking":
            # Anthropic exposes thinking blocks as {"type":"thinking",
            # "thinking": "...", "signature": "..."}; we surface the
            # text only — ``reasoning`` is consumed by agent_loop for
            # the same purpose Gemma's thinking channel serves.
            thinking_parts.append(block.get("thinking", ""))
        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(
                            block.get("input", {}), ensure_ascii=False
                        ),
                    },
                }
            )
        # Other block types (e.g. multimodal output, server_tool_use)
        # are ignored — they're not part of the contract surface.

    content = "".join(text_parts) if text_parts else None
    reasoning = "".join(thinking_parts) if thinking_parts else None
    tool_calls_out = tool_calls if tool_calls else None

    usage = resp.get("usage") or {}
    input_tokens = usage.get("input_tokens", 0) or 0
    output_tokens = usage.get("output_tokens", 0) or 0
    cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0

    return {
        "id": resp.get("id"),
        "model": resp.get("model"),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls_out,
                    "reasoning": reasoning,
                },
                "finish_reason": _map_stop_reason(resp.get("stop_reason")),
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
        },
    }

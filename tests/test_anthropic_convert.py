"""Tests for ``providers_anthropic_convert`` — pure dict-in/out converters.

These tests must run without the ``anthropic`` SDK installed and without
any network access. They pin the contract documented in
``docs/specs/2026-05-17-native-anthropic-adapter.md`` (workstream A).

The spec lists 23 named bullets; we cover all of them plus several
additional edge cases (>25 tests total) to keep the surface durable
against future changes to the request/response shape.
"""

from __future__ import annotations

import json

import pytest

from providers_anthropic_convert import (
    _map_stop_reason,
    from_anthropic_response,
    to_anthropic_request,
)


# ── to_anthropic_request — system hoisting ───────────────────────────────────


def test_system_message_extracted_to_system_kwarg():
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "hi"},
        ],
        tools=None,
    )
    assert req["system"] == "You are a helpful assistant."
    assert req["messages"] == [{"role": "user", "content": "hi"}]
    # And no system message survives inside the messages list.
    assert all(m.get("role") != "system" for m in req["messages"])


def test_multiple_system_messages_concatenated_with_two_newlines():
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[
            {"role": "system", "content": "First."},
            {"role": "system", "content": "Second."},
            {"role": "system", "content": "Third."},
            {"role": "user", "content": "hi"},
        ],
        tools=None,
    )
    assert req["system"] == "First.\n\nSecond.\n\nThird."


def test_system_block_list_with_text_blocks_flattened():
    """Some callers pass system content as a list of content blocks."""
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "Part A"},
                    {"type": "text", "text": "Part B"},
                ],
            },
            {"role": "user", "content": "hi"},
        ],
        tools=None,
    )
    assert req["system"] == "Part A\n\nPart B"


# ── to_anthropic_request — user / assistant alternation ──────────────────────


def test_user_assistant_alternation_preserved():
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[
            {"role": "user", "content": "1"},
            {"role": "assistant", "content": "2"},
            {"role": "user", "content": "3"},
            {"role": "assistant", "content": "4"},
        ],
        tools=None,
    )
    roles = [m["role"] for m in req["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert req["messages"][0]["content"] == "1"
    assert req["messages"][1]["content"] == "2"


# ── to_anthropic_request — tool messages ─────────────────────────────────────


def test_tool_message_converts_to_user_with_tool_result_block():
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[
            {"role": "user", "content": "search docs"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "search", "arguments": '{"q":"x"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result text"},
        ],
        tools=None,
    )
    tool_msg = req["messages"][-1]
    assert tool_msg["role"] == "user"
    assert isinstance(tool_msg["content"], list)
    assert tool_msg["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": "result text",
    }


# ── to_anthropic_request — assistant tool_calls ──────────────────────────────


def test_assistant_tool_calls_convert_to_tool_use_blocks():
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[
            {"role": "user", "content": "search"},
            {
                "role": "assistant",
                "content": "Let me search.",
                "tool_calls": [
                    {
                        "id": "call_42",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query": "Anthropic API"}',
                        },
                    }
                ],
            },
        ],
        tools=None,
    )
    asst = req["messages"][-1]
    assert asst["role"] == "assistant"
    blocks = asst["content"]
    assert isinstance(blocks, list)
    # Text block first, then tool_use.
    assert blocks[0] == {"type": "text", "text": "Let me search."}
    assert blocks[1]["type"] == "tool_use"
    assert blocks[1]["id"] == "call_42"
    assert blocks[1]["name"] == "web_search"
    # Arguments string is parsed into a dict for Anthropic's input field.
    assert blocks[1]["input"] == {"query": "Anthropic API"}


def test_assistant_tool_call_with_invalid_json_arguments_falls_back_to_raw():
    """The arguments string is parsed; if it isn't valid JSON we surface
    it under {"raw": ...} rather than silently dropping it."""
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_x",
                        "type": "function",
                        "function": {
                            "name": "do_thing",
                            "arguments": "not-json-at-all",
                        },
                    }
                ],
            },
        ],
        tools=None,
    )
    tool_use = req["messages"][-1]["content"][0]
    assert tool_use["type"] == "tool_use"
    assert tool_use["input"] == {"raw": "not-json-at-all"}


def test_assistant_tool_call_with_empty_arguments_string_becomes_empty_dict():
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_y",
                        "type": "function",
                        "function": {"name": "noop", "arguments": ""},
                    }
                ],
            },
        ],
        tools=None,
    )
    tool_use = req["messages"][-1]["content"][0]
    assert tool_use["input"] == {}


# ── to_anthropic_request — tool schema translation ───────────────────────────


def test_tool_schema_openai_to_anthropic_shape():
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "calc",
                    "description": "Do math.",
                    "parameters": {
                        "type": "object",
                        "properties": {"x": {"type": "number"}},
                        "required": ["x"],
                    },
                },
            }
        ],
    )
    assert req["tools"] == [
        {
            "name": "calc",
            "description": "Do math.",
            "input_schema": {
                "type": "object",
                "properties": {"x": {"type": "number"}},
                "required": ["x"],
            },
        }
    ]


def test_tool_schema_missing_parameters_gets_default_object_schema():
    """Anthropic rejects tools with no input_schema; we fill in an empty
    object schema so a sloppy caller doesn't trigger an API error."""
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "ping", "description": "."},
            }
        ],
    )
    assert req["tools"][0]["input_schema"] == {"type": "object", "properties": {}}


# ── to_anthropic_request — assistant-first synthetic prepend ─────────────────


def test_synthetic_user_prepended_when_first_message_after_system_is_assistant(caplog):
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "I think therefore I am."},
            {"role": "user", "content": "next?"},
        ],
        tools=None,
    )
    assert req["messages"][0] == {
        "role": "user",
        "content": "(continuing from prior context)",
    }
    assert req["messages"][1]["role"] == "assistant"
    assert req["messages"][1]["content"] == "I think therefore I am."


# ── to_anthropic_request — error / edge cases ────────────────────────────────


def test_empty_messages_raises_value_error():
    with pytest.raises(ValueError, match="messages cannot be empty"):
        to_anthropic_request(
            model="claude-sonnet-4-5",
            messages=[],
            tools=None,
        )


def test_oversized_tool_result_content_is_truncated_with_marker():
    huge = "x" * (200 * 1024 + 50)  # 50 chars past the cap
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_big",
                        "type": "function",
                        "function": {"name": "dump", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_big", "content": huge},
        ],
        tools=None,
    )
    tr_content = req["messages"][-1]["content"][0]["content"]
    assert "[truncated by adapter: 50 chars omitted]" in tr_content
    # And total stays at-or-near the cap + the marker length.
    assert len(tr_content) < len(huge)


def test_tool_result_content_at_or_under_cap_is_not_truncated():
    body = "x" * (200 * 1024)  # exactly at the cap
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_ok",
                        "type": "function",
                        "function": {"name": "dump", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_ok", "content": body},
        ],
        tools=None,
    )
    assert req["messages"][-1]["content"][0]["content"] == body


def test_stream_flag_passes_through_when_true():
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        stream=True,
    )
    assert req["stream"] is True


def test_stream_flag_omitted_when_false():
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        stream=False,
    )
    assert "stream" not in req


def test_temperature_passed_through_when_set():
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        temperature=0.7,
    )
    assert req["temperature"] == 0.7


def test_temperature_omitted_when_none():
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        temperature=None,
    )
    assert "temperature" not in req


def test_max_tokens_passed_through():
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=12345,
    )
    assert req["max_tokens"] == 12345


def test_multimodal_user_content_passes_through_unchanged():
    """Image / multimodal content blocks must survive the converter
    untouched — we don't translate Anthropic image format here."""
    image_blocks = [
        {"type": "text", "text": "What's in this image?"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAA"},
        },
    ]
    req = to_anthropic_request(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": image_blocks}],
        tools=None,
    )
    assert req["messages"][0]["content"] == image_blocks


def test_model_field_passed_through():
    req = to_anthropic_request(
        model="claude-opus-4-1",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
    )
    assert req["model"] == "claude-opus-4-1"


# ── from_anthropic_response ──────────────────────────────────────────────────


def test_text_only_response_populates_content_and_no_tool_calls():
    resp = {
        "id": "msg_1",
        "model": "claude-sonnet-4-5",
        "role": "assistant",
        "content": [{"type": "text", "text": "Hello there."}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }
    out = from_anthropic_response(resp)
    msg = out["choices"][0]["message"]
    assert msg["content"] == "Hello there."
    assert msg["tool_calls"] is None
    assert msg["role"] == "assistant"
    assert out["choices"][0]["finish_reason"] == "stop"


def test_tool_use_only_response_has_none_content_and_finish_reason_tool_calls():
    resp = {
        "id": "msg_2",
        "model": "claude-sonnet-4-5",
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_42",
                "name": "search",
                "input": {"q": "x"},
            }
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    out = from_anthropic_response(resp)
    msg = out["choices"][0]["message"]
    assert msg["content"] is None
    assert msg["tool_calls"] is not None
    assert msg["tool_calls"][0]["id"] == "toolu_42"
    assert msg["tool_calls"][0]["type"] == "function"
    assert msg["tool_calls"][0]["function"]["name"] == "search"
    # Arguments are JSON-dumped from the input dict.
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"q": "x"}
    assert out["choices"][0]["finish_reason"] == "tool_calls"


def test_mixed_text_and_tool_use_response_populates_both():
    resp = {
        "id": "msg_3",
        "model": "claude-sonnet-4-5",
        "content": [
            {"type": "text", "text": "Let me search. "},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "search",
                "input": {"q": "x"},
            },
            {"type": "text", "text": "Working on it."},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    out = from_anthropic_response(resp)
    msg = out["choices"][0]["message"]
    # Text blocks concatenated in order.
    assert msg["content"] == "Let me search. Working on it."
    assert msg["tool_calls"] is not None
    assert len(msg["tool_calls"]) == 1


def test_thinking_blocks_populate_reasoning_field():
    resp = {
        "id": "msg_4",
        "model": "claude-opus-4-1",
        "content": [
            {"type": "thinking", "thinking": "Let me reason. "},
            {"type": "thinking", "thinking": "And reason more."},
            {"type": "text", "text": "Here's the answer."},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    out = from_anthropic_response(resp)
    msg = out["choices"][0]["message"]
    assert msg["reasoning"] == "Let me reason. And reason more."
    assert msg["content"] == "Here's the answer."


def test_usage_block_propagates_cache_fields_and_totals():
    resp = {
        "id": "msg_5",
        "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 200,
            "output_tokens": 30,
            "cache_creation_input_tokens": 7,
            "cache_read_input_tokens": 18,
        },
    }
    out = from_anthropic_response(resp)
    assert out["usage"]["prompt_tokens"] == 200
    assert out["usage"]["completion_tokens"] == 30
    assert out["usage"]["total_tokens"] == 230
    assert out["usage"]["cache_creation_input_tokens"] == 7
    assert out["usage"]["cache_read_input_tokens"] == 18


def test_usage_block_missing_cache_fields_defaults_to_zero():
    resp = {
        "id": "msg_6",
        "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 25},
    }
    out = from_anthropic_response(resp)
    assert out["usage"]["cache_creation_input_tokens"] == 0
    assert out["usage"]["cache_read_input_tokens"] == 0


# ── _map_stop_reason — 5 mapping cases (+ None passthrough) ──────────────────


def test_map_stop_reason_end_turn_is_stop():
    assert _map_stop_reason("end_turn") == "stop"


def test_map_stop_reason_max_tokens_is_length():
    assert _map_stop_reason("max_tokens") == "length"


def test_map_stop_reason_tool_use_is_tool_calls():
    assert _map_stop_reason("tool_use") == "tool_calls"


def test_map_stop_reason_stop_sequence_is_stop():
    assert _map_stop_reason("stop_sequence") == "stop"


def test_map_stop_reason_pause_turn_is_stop():
    assert _map_stop_reason("pause_turn") == "stop"


def test_map_stop_reason_none_passes_through():
    assert _map_stop_reason(None) is None


def test_map_stop_reason_unknown_defaults_to_stop():
    assert _map_stop_reason("some_future_reason") == "stop"

"""Tests for ovos_tool_calling.llm — pure helpers only.

call_chat itself does HTTP and isn't covered here; the message-construction
helpers around it are pure and worth pinning down because the agent loop
depends on their exact format matching the OpenAI tool-calling protocol.
"""

from __future__ import annotations

import json

import pytest

from ovos_tool_calling.llm import (
    LLMConfig,
    LLMToolCall,
    assistant_message_for_tool_calls,
    build_initial_messages,
    tool_result_message,
)


# --- LLMConfig ----------------------------------------------------------------


def test_llmconfig_is_usable_when_all_fields_present():
    cfg = LLMConfig(api_url="https://example.com/v1", key="k", model="m")
    assert cfg.is_usable()


def test_llmconfig_unusable_with_missing_field():
    assert not LLMConfig(api_url="", key="k", model="m").is_usable()
    assert not LLMConfig(api_url="u", key="", model="m").is_usable()
    assert not LLMConfig(api_url="u", key="k", model="").is_usable()


# --- build_initial_messages ---------------------------------------------------


def test_initial_messages_has_system_then_user():
    cfg = LLMConfig(
        api_url="u", key="k", model="m", system_prompt="be terse"
    )
    msgs = build_initial_messages(cfg, "set a timer")
    assert len(msgs) == 2
    assert msgs[0] == {"role": "system", "content": "be terse"}
    assert msgs[1] == {"role": "user", "content": "set a timer"}


# --- assistant_message_for_tool_calls -----------------------------------------


def test_assistant_message_serializes_tool_calls_in_openai_format():
    tcs = [
        LLMToolCall(tool_name="tool_a", arguments={"x": 1}, tool_call_id="call_0"),
        LLMToolCall(tool_name="tool_b", arguments={}, tool_call_id="call_1"),
    ]
    msg = assistant_message_for_tool_calls(tcs)
    assert msg["role"] == "assistant"
    assert msg["content"] is None
    assert len(msg["tool_calls"]) == 2

    first = msg["tool_calls"][0]
    assert first["id"] == "call_0"
    assert first["type"] == "function"
    assert first["function"]["name"] == "tool_a"
    # Arguments are JSON-serialized per the OpenAI protocol.
    assert json.loads(first["function"]["arguments"]) == {"x": 1}


def test_assistant_message_serializes_empty_args_as_empty_object():
    tc = LLMToolCall(tool_name="t", arguments={}, tool_call_id="c0")
    msg = assistant_message_for_tool_calls([tc])
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {}


# --- tool_result_message ------------------------------------------------------


def test_tool_result_message_format():
    msg = tool_result_message("call_0", "ok\nDone.")
    assert msg["role"] == "tool"
    assert msg["tool_call_id"] == "call_0"
    assert msg["content"] == "ok\nDone."


def test_tool_result_message_supports_error_content():
    msg = tool_result_message("call_x", "error: skill not found")
    assert msg["content"].startswith("error: ")

"""Tests for ovos_tool_calling.llm — pure helpers and the call_chat HTTP wrapper.

The pure helpers are obvious unit tests. ``call_chat`` is exercised against a
monkeypatched ``requests.post`` so we cover the OpenAI response-shape parsing,
the failure modes (transport error, non-2xx, malformed JSON), and the way
malformed/missing fields in individual tool_calls are tolerated.
"""

from __future__ import annotations

import json

import pytest
import requests

from ovos_tool_calling import llm as llm_mod
from ovos_tool_calling.llm import (
    LLMConfig,
    LLMToolCall,
    assistant_message_for_tool_calls,
    build_initial_messages,
    call_chat,
    tool_result_message,
)


class _FakeResponse:
    """Minimal stand-in for requests.Response, only what call_chat reads."""

    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.ok = 200 <= status_code < 300

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    @property
    def text(self) -> str:
        try:
            return json.dumps(self._body)
        except (TypeError, ValueError):
            return str(self._body)


def _config() -> LLMConfig:
    return LLMConfig(
        api_url="https://example.com/v1",
        key="sk-test",
        model="test-model",
        max_tokens=200,
        temperature=0.1,
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


# --- call_chat: payload shape -------------------------------------------------


def test_call_chat_posts_expected_payload_and_headers(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = json
        captured["timeout"] = timeout
        return _FakeResponse(
            200,
            {"choices": [{"message": {"content": "hi"}}]},
        )

    monkeypatch.setattr(llm_mod.requests, "post", fake_post)
    cfg = _config()
    msgs = build_initial_messages(cfg, "hello")
    tools = [{"type": "function", "function": {"name": "noop"}}]

    result = call_chat(cfg, msgs, tools)

    assert result is not None
    assert captured["url"] == "https://example.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["payload"]["model"] == "test-model"
    assert captured["payload"]["messages"] == msgs
    assert captured["payload"]["tools"] == tools
    assert captured["payload"]["tool_choice"] == "auto"
    assert captured["payload"]["max_tokens"] == 200
    assert captured["payload"]["temperature"] == 0.1
    assert captured["timeout"] == cfg.timeout_seconds


def test_call_chat_strips_trailing_slash_from_api_url(monkeypatch):
    captured = {}

    def fake_post(url, **_kw):
        captured["url"] = url
        return _FakeResponse(200, {"choices": [{"message": {"content": "x"}}]})

    monkeypatch.setattr(llm_mod.requests, "post", fake_post)
    cfg = LLMConfig(api_url="https://example.com/v1/", key="k", model="m")
    call_chat(cfg, [], [])
    assert captured["url"] == "https://example.com/v1/chat/completions"


# --- call_chat: response parsing ----------------------------------------------


def test_call_chat_parses_text_only_response(monkeypatch):
    monkeypatch.setattr(
        llm_mod.requests, "post",
        lambda *a, **kw: _FakeResponse(
            200, {"choices": [{"message": {"content": "the time is 5pm"}}]},
        ),
    )
    tool_calls, text = call_chat(_config(), [], [])
    assert tool_calls == []
    assert text == "the time is 5pm"


def test_call_chat_parses_tool_calls_response(monkeypatch):
    monkeypatch.setattr(
        llm_mod.requests, "post",
        lambda *a, **kw: _FakeResponse(
            200,
            {
                "choices": [{
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_42",
                                "type": "function",
                                "function": {
                                    "name": "create_timer",
                                    "arguments": json.dumps({"duration": "5 minutes"}),
                                },
                            }
                        ],
                    }
                }]
            },
        ),
    )
    tool_calls, text = call_chat(_config(), [], [])
    assert text is None
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "create_timer"
    assert tool_calls[0].arguments == {"duration": "5 minutes"}
    assert tool_calls[0].tool_call_id == "call_42"


def test_call_chat_parses_mixed_tool_calls_and_text(monkeypatch):
    monkeypatch.setattr(
        llm_mod.requests, "post",
        lambda *a, **kw: _FakeResponse(
            200,
            {
                "choices": [{
                    "message": {
                        "content": "starting timer",
                        "tool_calls": [
                            {"id": "c0", "type": "function",
                             "function": {"name": "t", "arguments": "{}"}}
                        ],
                    }
                }]
            },
        ),
    )
    tool_calls, text = call_chat(_config(), [], [])
    assert len(tool_calls) == 1
    assert text == "starting timer"


def test_call_chat_synthesizes_tool_call_id_when_absent(monkeypatch):
    """OpenAI-compatible servers may omit `id`; we synthesise call_<idx>."""
    monkeypatch.setattr(
        llm_mod.requests, "post",
        lambda *a, **kw: _FakeResponse(
            200,
            {"choices": [{"message": {
                "tool_calls": [
                    {"function": {"name": "a", "arguments": "{}"}},
                    {"function": {"name": "b", "arguments": "{}"}},
                ]
            }}]},
        ),
    )
    tool_calls, _ = call_chat(_config(), [], [])
    assert [tc.tool_call_id for tc in tool_calls] == ["call_0", "call_1"]


def test_call_chat_skips_tool_call_with_malformed_arguments(monkeypatch):
    """A tool_call with broken JSON args should be dropped, others kept."""
    monkeypatch.setattr(
        llm_mod.requests, "post",
        lambda *a, **kw: _FakeResponse(
            200,
            {"choices": [{"message": {
                "tool_calls": [
                    {"id": "c0", "function": {"name": "good", "arguments": '{"x":1}'}},
                    {"id": "c1", "function": {"name": "bad", "arguments": "{not-json"}},
                ]
            }}]},
        ),
    )
    tool_calls, _ = call_chat(_config(), [], [])
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "good"


def test_call_chat_treats_empty_arguments_string_as_empty_dict(monkeypatch):
    monkeypatch.setattr(
        llm_mod.requests, "post",
        lambda *a, **kw: _FakeResponse(
            200,
            {"choices": [{"message": {
                "tool_calls": [
                    {"id": "c0", "function": {"name": "t", "arguments": ""}}
                ]
            }}]},
        ),
    )
    tool_calls, _ = call_chat(_config(), [], [])
    assert tool_calls[0].arguments == {}


# --- call_chat: failure modes -------------------------------------------------


def test_call_chat_returns_none_on_transport_error(monkeypatch):
    def boom(*a, **kw):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(llm_mod.requests, "post", boom)
    assert call_chat(_config(), [], []) is None


def test_call_chat_returns_none_on_non_2xx(monkeypatch):
    monkeypatch.setattr(
        llm_mod.requests, "post",
        lambda *a, **kw: _FakeResponse(500, {"error": "boom"}),
    )
    assert call_chat(_config(), [], []) is None


def test_call_chat_returns_none_on_malformed_json(monkeypatch):
    monkeypatch.setattr(
        llm_mod.requests, "post",
        lambda *a, **kw: _FakeResponse(200, ValueError("not json")),
    )
    assert call_chat(_config(), [], []) is None


def test_call_chat_returns_none_when_choices_missing(monkeypatch):
    monkeypatch.setattr(
        llm_mod.requests, "post",
        lambda *a, **kw: _FakeResponse(200, {}),
    )
    assert call_chat(_config(), [], []) is None


def test_call_chat_returns_none_when_choices_empty(monkeypatch):
    monkeypatch.setattr(
        llm_mod.requests, "post",
        lambda *a, **kw: _FakeResponse(200, {"choices": []}),
    )
    assert call_chat(_config(), [], []) is None

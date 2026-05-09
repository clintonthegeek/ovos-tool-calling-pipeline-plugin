"""Tests for the ToolCallingPipeline class.

We instantiate the pipeline with a FakeBus and stub the LLM (via
``ovos_tool_calling.llm.call_chat``) and the catalog (via the pipeline's
``build_catalog`` method) so each test exercises one decision branch in
``_try_llm_dispatch`` deterministically.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import pytest
from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus

from ovos_tool_calling import ToolCallingPipeline
from ovos_tool_calling import llm as llm_mod
from ovos_tool_calling.dispatch import SPEAK_MATCH_TYPE, SPEAK_SKILL_ID
from ovos_tool_calling.schemas import ToolEntry


def _entry(skill_id: str = "ovos-skill-alerts.openvoiceos",
           intent_name: str = "CreateTimer",
           matcher: str = "adapt") -> ToolEntry:
    name = f"{skill_id.replace('.', '_').replace('-', '-')}__{intent_name}"
    return ToolEntry(
        name=name,
        skill_id=skill_id,
        intent_name=intent_name,
        matcher=matcher,
        schema={"type": "function", "function": {"name": name}},
    )


def _make_pipeline(**overrides) -> ToolCallingPipeline:
    """Build a ToolCallingPipeline with FakeBus and minimal LLM config."""
    bus = FakeBus()
    config: Dict[str, Any] = {
        "enabled": True,
        "api_url": "https://x/v1",
        "key": "k",
        "model": "m",
        # Keep gate permissive by default so most tests reach the LLM path.
        "min_words": 1,
        "cache_size": 8,
        # Disable agent loop by default — most tests target the single-tool
        # synchronous path which is simpler. Tests that need it set True.
        "enable_agent_loop": False,
    }
    config.update(overrides)
    return ToolCallingPipeline(bus=bus, config=config)


def _stub_catalog(pipeline: ToolCallingPipeline,
                  entries: List[ToolEntry]) -> Dict[str, ToolEntry]:
    """Replace pipeline.build_catalog with a fixed (tools, name_index)."""
    name_index = {e.name: e for e in entries}
    tools = [e.schema for e in entries]
    pipeline.build_catalog = lambda: (tools, name_index)
    return name_index


def _stub_llm(monkeypatch, *, tool_calls=None, text=None, return_none=False):
    """Force call_chat to return a scripted result on its next invocation."""
    if return_none:
        monkeypatch.setattr(llm_mod, "call_chat", lambda *a, **kw: None)
        return
    monkeypatch.setattr(
        llm_mod, "call_chat",
        lambda *a, **kw: (list(tool_calls or []), text),
    )


# --- construction -------------------------------------------------------------


def test_pipeline_disabled_when_config_says_so():
    p = _make_pipeline(enabled=False)
    assert p.enabled is False
    assert p.llm_config is None


def test_pipeline_enabled_builds_llm_config():
    p = _make_pipeline()
    assert p.enabled is True
    assert p.llm_config is not None
    assert p.llm_config.is_usable()


def test_pipeline_disabled_returns_none_immediately(monkeypatch):
    """Disabled plugin must not call the LLM regardless of input."""
    p = _make_pipeline(enabled=False)
    called = {"n": 0}

    def fake_call_chat(*a, **kw):
        called["n"] += 1
        return ([], None)

    monkeypatch.setattr(llm_mod, "call_chat", fake_call_chat)
    result = p.match_low(["any utterance here"], "en-us", Message("x"))
    assert result is None
    assert called["n"] == 0


# --- gate skip / cache --------------------------------------------------------


def test_gate_skip_short_utterance_does_not_call_llm(monkeypatch):
    p = _make_pipeline(min_words=3)
    called = {"n": 0}
    monkeypatch.setattr(llm_mod, "call_chat",
                        lambda *a, **kw: (called.__setitem__("n", called["n"] + 1) or ([], None)))

    result = p.match_low(["hi there"], "en-us", Message("x"))
    assert result is None
    assert called["n"] == 0


def test_gate_cache_hit_returns_cached_match_without_llm(monkeypatch):
    """Pre-populating the gate's cache should short-circuit the LLM call."""
    p = _make_pipeline()
    entry = _entry()
    _stub_catalog(p, [entry])

    # Seed the cache by recording a prior dispatch.
    from ovos_tool_calling.dispatch import make_match
    prior_match = make_match(entry, {"utterance": "set a 5 minute timer"},
                             "set a 5 minute timer")
    p.gate.record("set a 5 minute timer", prior_match)

    called = {"n": 0}
    monkeypatch.setattr(llm_mod, "call_chat",
                        lambda *a, **kw: (called.__setitem__("n", called["n"] + 1) or ([], None)))

    # Same utterance — should hit cache.
    result = p.match_low(["set a 5 minute timer"], "en-us", Message("x"))
    assert result is prior_match
    assert called["n"] == 0


# --- LLM declined / no tools --------------------------------------------------


def test_returns_none_when_no_tools_registered(monkeypatch):
    """Empty catalog -> no LLM call -> None."""
    p = _make_pipeline()
    p.build_catalog = lambda: ([], {})
    called = {"n": 0}
    monkeypatch.setattr(llm_mod, "call_chat",
                        lambda *a, **kw: (called.__setitem__("n", called["n"] + 1) or ([], None)))

    result = p.match_low(["any utterance"], "en-us", Message("x"))
    assert result is None
    assert called["n"] == 0


def test_returns_none_when_llm_call_fails(monkeypatch):
    p = _make_pipeline()
    _stub_catalog(p, [_entry()])
    _stub_llm(monkeypatch, return_none=True)

    result = p.match_low(["any utterance"], "en-us", Message("x"))
    assert result is None


def test_returns_none_when_llm_declines_no_tool_no_text(monkeypatch):
    p = _make_pipeline()
    _stub_catalog(p, [_entry()])
    _stub_llm(monkeypatch, tool_calls=[], text=None)

    result = p.match_low(["any utterance"], "en-us", Message("x"))
    assert result is None


# --- text answer (v0.5 path) --------------------------------------------------


def test_text_answer_emits_speak_and_returns_sentinel(monkeypatch):
    """LLM text response -> bus emits 'speak' and we return a sentinel match."""
    p = _make_pipeline(speak_text_answers=True)
    _stub_catalog(p, [_entry()])
    _stub_llm(monkeypatch, tool_calls=[], text="It is five PM.")

    spoken: List[Message] = []
    p.bus.on("speak", lambda m: spoken.append(m))

    result = p.match_low(["what time is it"], "en-us", Message("x"))

    assert result is not None
    assert result.match_type == SPEAK_MATCH_TYPE
    assert result.skill_id == SPEAK_SKILL_ID

    assert len(spoken) == 1
    assert spoken[0].data["utterance"] == "It is five PM."
    assert spoken[0].data["meta"]["source"] == "tool-calling-pipeline"
    assert spoken[0].context.get("skill_id") == SPEAK_SKILL_ID


def test_text_answer_falls_through_when_disabled(monkeypatch):
    """speak_text_answers=False -> return None so downstream pipeline runs."""
    p = _make_pipeline(speak_text_answers=False)
    _stub_catalog(p, [_entry()])
    _stub_llm(monkeypatch, tool_calls=[], text="It is five PM.")

    spoken: List[Message] = []
    p.bus.on("speak", lambda m: spoken.append(m))

    result = p.match_low(["what time is it"], "en-us", Message("x"))
    assert result is None
    assert spoken == []  # we did not speak it ourselves


# --- single-tool dispatch (agent loop disabled) -------------------------------


def test_single_tool_dispatch_synthesizes_match(monkeypatch):
    """LLM picks a tool, agent_loop=False -> returns the dispatch match."""
    p = _make_pipeline(enable_agent_loop=False)
    entry = _entry()
    _stub_catalog(p, [entry])
    _stub_llm(monkeypatch, tool_calls=[
        llm_mod.LLMToolCall(entry.name, {"utterance": "set a 5 minute timer"}, "c0"),
    ])

    result = p.match_low(["set a 5 minute timer"], "en-us", Message("x"))

    assert result is not None
    assert result.match_type == f"{entry.skill_id}:{entry.intent_name}"
    assert result.skill_id == entry.skill_id
    assert result.utterance == "set a 5 minute timer"


def test_single_tool_dispatch_records_to_cache(monkeypatch):
    """A successful single-tool dispatch should populate the LRU cache."""
    p = _make_pipeline(enable_agent_loop=False)
    entry = _entry()
    _stub_catalog(p, [entry])
    _stub_llm(monkeypatch, tool_calls=[
        llm_mod.LLMToolCall(entry.name, {"utterance": "u"}, "c0"),
    ])

    cached_before, _ = p.gate.stats()
    p.match_low(["set a 5 minute timer"], "en-us", Message("x"))
    cached_after, _ = p.gate.stats()
    assert cached_after == cached_before + 1


def test_unknown_tool_picked_returns_none(monkeypatch):
    """LLM hallucinates a tool name not in the catalog."""
    p = _make_pipeline(enable_agent_loop=False)
    _stub_catalog(p, [_entry()])
    _stub_llm(monkeypatch, tool_calls=[
        llm_mod.LLMToolCall("not_a_real_tool", {}, "c0"),
    ])

    result = p.match_low(["something"], "en-us", Message("x"))
    assert result is None


# --- agent loop hand-off (v0.6) ------------------------------------------------


def test_agent_loop_handoff_returns_sentinel_immediately(monkeypatch):
    """agent_loop=True -> spawn worker thread, return sentinel synchronously."""
    p = _make_pipeline(enable_agent_loop=True, tool_timeout_seconds=0.05)
    entry = _entry()
    _stub_catalog(p, [entry])

    # The agent's *follow-up* call_chat must terminate the loop quickly.
    _stub_llm(monkeypatch, tool_calls=[
        llm_mod.LLMToolCall(entry.name, {}, "c0"),
    ])
    # Once the worker is spawned, it'll re-call call_chat for the follow-up;
    # since we monkeypatch the module-level reference, both calls hit the same
    # stub. That returns a tool_call again -> loop iterates -> dispatch times
    # out (no skill listening) -> next call_chat -> same. With max_iter=1 the
    # loop exits after one iteration.
    p.agent_config.max_tool_iterations = 1

    result = p.match_low(["set a 5 minute timer"], "en-us", Message("x"))

    assert result is not None
    assert result.match_type == SPEAK_MATCH_TYPE

    # Let the spawned worker thread finish so pytest doesn't see a leak.
    if p.agent_loop._current is not None:
        p.agent_loop._current._abort.set()
        p.agent_loop._current._thread.join(timeout=2.0)


# --- single-flight memo across tiers ------------------------------------------


def test_inflight_memo_returns_same_result_within_ttl(monkeypatch):
    """Same utterance hitting match_high then match_low within the TTL must
    re-use the first tier's result without a second LLM call."""
    p = _make_pipeline(enable_agent_loop=False)
    entry = _entry()
    _stub_catalog(p, [entry])

    call_count = {"n": 0}

    def counting_call_chat(*a, **kw):
        call_count["n"] += 1
        return [llm_mod.LLMToolCall(entry.name, {}, "c0")], None

    monkeypatch.setattr(llm_mod, "call_chat", counting_call_chat)

    r1 = p.match_high(["set a timer"], "en-us", Message("x"))
    r2 = p.match_medium(["set a timer"], "en-us", Message("x"))
    r3 = p.match_low(["set a timer"], "en-us", Message("x"))

    assert r1 is r2 is r3  # same object
    assert call_count["n"] == 1


def test_inflight_memo_expires_after_ttl(monkeypatch):
    """After the TTL expires, the same utterance should re-call the LLM."""
    p = _make_pipeline(enable_agent_loop=False, cache_size=0)  # disable LRU
    entry = _entry()
    _stub_catalog(p, [entry])

    call_count = {"n": 0}

    def counting_call_chat(*a, **kw):
        call_count["n"] += 1
        return [llm_mod.LLMToolCall(entry.name, {}, "c0")], None

    monkeypatch.setattr(llm_mod, "call_chat", counting_call_chat)
    p._inflight_ttl = 0.05  # tiny TTL for the test

    p.match_low(["set a timer"], "en-us", Message("x"))
    time.sleep(0.1)
    p.match_low(["set a timer"], "en-us", Message("x"))

    assert call_count["n"] == 2

"""Tests for ovos_tool_calling.agent — multi-tool agent loop.

The loop runs in a worker thread but FakeBus dispatches handlers synchronously,
so we can drive it deterministically. We monkeypatch ``call_chat`` per test to
script the LLM's responses across iterations.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

import pytest
from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus

from ovos_tool_calling import agent as agent_mod
from ovos_tool_calling.agent import (
    AgentConfig,
    AgentLoop,
    _DispatchOutcome,
    _LoopRun,
)
from ovos_tool_calling.llm import LLMConfig, LLMToolCall
from ovos_tool_calling.schemas import ToolEntry


# --- helpers ------------------------------------------------------------------


def _entry(skill_id: str = "ovos-skill-alerts.openvoiceos",
           intent_name: str = "CreateTimer",
           tool_name: str = "ovos-skill-alerts__CreateTimer") -> ToolEntry:
    return ToolEntry(
        name=tool_name,
        skill_id=skill_id,
        intent_name=intent_name,
        matcher="adapt",
        schema={"type": "function", "function": {"name": tool_name}},
    )


def _llm_config() -> LLMConfig:
    return LLMConfig(api_url="https://x/v1", key="k", model="m")


def _new_run(
    bus: FakeBus,
    *,
    name_index: Optional[Dict[str, ToolEntry]] = None,
    initial_tool_calls: Optional[List[LLMToolCall]] = None,
    initial_text: Optional[str] = None,
    agent_config: Optional[AgentConfig] = None,
) -> _LoopRun:
    return _LoopRun(
        bus=bus,
        agent_config=agent_config or AgentConfig(
            max_tool_iterations=3,
            tool_timeout_seconds=1.0,
            post_complete_grace_seconds=0.0,
        ),
        initial_messages=[{"role": "user", "content": "hi"}],
        initial_tool_calls=initial_tool_calls or [],
        initial_text=initial_text,
        utterance="hi",
        lang="en-us",
        original_message=None,
        llm_config=_llm_config(),
        tools=[],
        name_index=name_index or {},
        on_done=lambda r: None,
    )


# --- _DispatchOutcome ---------------------------------------------------------


def test_dispatch_outcome_ok_formats_with_speak_text():
    o = _DispatchOutcome(status="ok", captured_speak="set a timer for 5 minutes")
    assert o.as_tool_content() == "ok\nset a timer for 5 minutes"


def test_dispatch_outcome_ok_with_no_speak_strips_trailing_newline():
    o = _DispatchOutcome(status="ok", captured_speak="")
    assert o.as_tool_content() == "ok"


def test_dispatch_outcome_error_includes_message():
    o = _DispatchOutcome(status="error", error="boom")
    assert o.as_tool_content() == "error: boom"


def test_dispatch_outcome_error_default_message():
    o = _DispatchOutcome(status="error")
    assert "unknown error" in o.as_tool_content()


def test_dispatch_outcome_timeout_message():
    o = _DispatchOutcome(status="timeout")
    assert "timeout" in o.as_tool_content()


# --- _dispatch_one over FakeBus -----------------------------------------------


def test_dispatch_one_captures_speak_and_completes():
    """A skill that emits matching speak + handler.complete returns ok with text."""
    bus = FakeBus()
    entry = _entry()

    def fake_skill(msg: Message):
        bus.emit(Message("speak", {
            "utterance": "Timer set for 5 minutes.",
            "meta": {"skill": entry.skill_id},
        }))
        bus.emit(Message(
            "mycroft.skill.handler.complete",
            {},
            {"skill_id": entry.skill_id},
        ))

    bus.on(f"{entry.skill_id}:{entry.intent_name}", fake_skill)

    run = _new_run(bus, name_index={entry.name: entry})
    tc = LLMToolCall(tool_name=entry.name, arguments={}, tool_call_id="c0")
    outcome = run._dispatch_one(tc)
    assert outcome.status == "ok"
    assert outcome.captured_speak == "Timer set for 5 minutes."


def test_dispatch_one_unknown_tool_returns_error():
    bus = FakeBus()
    run = _new_run(bus, name_index={})  # empty index
    tc = LLMToolCall(tool_name="nonexistent_tool", arguments={}, tool_call_id="c0")
    outcome = run._dispatch_one(tc)
    assert outcome.status == "error"
    assert "unknown tool" in outcome.error


def test_dispatch_one_ignores_speak_from_other_skills():
    """A speak event whose meta.skill doesn't match must NOT be captured."""
    bus = FakeBus()
    entry = _entry()

    def fake_skill(msg: Message):
        bus.emit(Message("speak", {
            "utterance": "Unrelated",
            "meta": {"skill": "some.other.skill"},
        }))
        bus.emit(Message("speak", {
            "utterance": "Done.",
            "meta": {"skill": entry.skill_id},
        }))
        bus.emit(Message(
            "mycroft.skill.handler.complete",
            {},
            {"skill_id": entry.skill_id},
        ))

    bus.on(f"{entry.skill_id}:{entry.intent_name}", fake_skill)
    run = _new_run(bus, name_index={entry.name: entry})
    tc = LLMToolCall(tool_name=entry.name, arguments={}, tool_call_id="c0")
    outcome = run._dispatch_one(tc)
    assert outcome.status == "ok"
    assert outcome.captured_speak == "Done."
    assert "Unrelated" not in outcome.captured_speak


def test_dispatch_one_handler_error_returns_error():
    bus = FakeBus()
    entry = _entry()

    def fake_skill(msg: Message):
        bus.emit(Message(
            "mycroft.skill.handler.error",
            {"exception": "ValueError: bad input"},
            {"skill_id": entry.skill_id},
        ))

    bus.on(f"{entry.skill_id}:{entry.intent_name}", fake_skill)
    run = _new_run(bus, name_index={entry.name: entry})
    tc = LLMToolCall(tool_name=entry.name, arguments={}, tool_call_id="c0")
    outcome = run._dispatch_one(tc)
    assert outcome.status == "error"
    assert "ValueError" in outcome.error


def test_dispatch_one_times_out_when_skill_silent():
    """No handler.complete arrives -> timeout outcome."""
    bus = FakeBus()
    entry = _entry()
    run = _new_run(
        bus, name_index={entry.name: entry},
        agent_config=AgentConfig(
            max_tool_iterations=1,
            tool_timeout_seconds=0.2,
            post_complete_grace_seconds=0.0,
        ),
    )
    tc = LLMToolCall(tool_name=entry.name, arguments={}, tool_call_id="c0")

    t0 = time.monotonic()
    outcome = run._dispatch_one(tc)
    elapsed = time.monotonic() - t0

    assert outcome.status == "timeout"
    assert 0.15 <= elapsed < 1.0


def test_dispatch_one_aborts_when_abort_event_set():
    """Setting the abort event mid-wait should cut the dispatch short."""
    bus = FakeBus()
    entry = _entry()
    run = _new_run(
        bus, name_index={entry.name: entry},
        agent_config=AgentConfig(
            max_tool_iterations=1,
            tool_timeout_seconds=5.0,
            post_complete_grace_seconds=0.0,
        ),
    )
    tc = LLMToolCall(tool_name=entry.name, arguments={}, tool_call_id="c0")

    threading.Timer(0.15, run._abort.set).start()

    t0 = time.monotonic()
    outcome = run._dispatch_one(tc)
    elapsed = time.monotonic() - t0

    assert outcome.status == "error"
    assert outcome.error == "aborted"
    assert elapsed < 1.0


# --- AgentLoop slot management ------------------------------------------------


def test_agent_loop_cancels_prior_run_when_new_one_starts(monkeypatch):
    """Spawning a fresh loop while one is active aborts the previous."""
    bus = FakeBus()

    monkeypatch.setattr(agent_mod, "call_chat", lambda *a, **kw: ([], None))

    loop = AgentLoop(
        bus=bus,
        agent_config=AgentConfig(
            max_tool_iterations=1,
            tool_timeout_seconds=0.05,
            post_complete_grace_seconds=0.0,
        ),
    )

    common_kwargs = dict(
        initial_messages=[{"role": "user", "content": "hi"}],
        initial_text=None,
        utterance="hi",
        lang="en-us",
        original_message=None,
        llm_config=_llm_config(),
        tools=[],
        name_index={},
    )

    loop.start(initial_tool_calls=[LLMToolCall("nope", {}, "c0")], **common_kwargs)
    first_run = loop._current
    assert first_run is not None

    loop.start(initial_tool_calls=[LLMToolCall("nope2", {}, "c1")], **common_kwargs)
    second_run = loop._current
    assert second_run is not None and second_run is not first_run
    assert first_run._abort.is_set()

    first_run._thread.join(timeout=2.0)
    second_run._thread.join(timeout=2.0)


def test_agent_loop_release_clears_slot():
    bus = FakeBus()
    loop = AgentLoop(bus=bus, agent_config=AgentConfig())
    fake_run = object()
    loop._current = fake_run
    loop._release(fake_run)
    assert loop._current is None


def test_agent_loop_release_ignores_non_current_run():
    """Stale run reporting done after a newer run took the slot must not clear it."""
    bus = FakeBus()
    loop = AgentLoop(bus=bus, agent_config=AgentConfig())
    stale = object()
    current = object()
    loop._current = current
    loop._release(stale)
    assert loop._current is current


# --- _LoopRun.run termination logic (mocked LLM) ------------------------------


def test_loop_terminates_on_text_only_initial_response():
    """No tool_calls + initial text -> speaks once and exits."""
    bus = FakeBus()
    spoken: List[str] = []
    bus.on("speak", lambda m: spoken.append(m.data["utterance"]))

    run = _new_run(
        bus,
        initial_tool_calls=[],
        initial_text="It is 5 PM.",
    )
    run._run()
    assert spoken == ["It is 5 PM."]


def test_loop_suppresses_final_text_when_skill_already_spoke(monkeypatch):
    """If a skill spoke during the loop, the LLM's summary text is suppressed."""
    bus = FakeBus()
    entry = _entry()

    agent_speaks: List[str] = []

    def speak_listener(m: Message):
        if (m.data.get("meta") or {}).get("source") == "tool-calling-agent":
            agent_speaks.append(m.data["utterance"])

    bus.on("speak", speak_listener)

    def fake_skill(msg: Message):
        bus.emit(Message("speak", {
            "utterance": "Timer set.",
            "meta": {"skill": entry.skill_id},
        }))
        bus.emit(Message(
            "mycroft.skill.handler.complete", {}, {"skill_id": entry.skill_id},
        ))

    bus.on(f"{entry.skill_id}:{entry.intent_name}", fake_skill)

    monkeypatch.setattr(
        agent_mod, "call_chat",
        lambda *a, **kw: ([], "Your timer is set."),
    )

    run = _new_run(
        bus,
        name_index={entry.name: entry},
        initial_tool_calls=[LLMToolCall(entry.name, {}, "c0")],
    )
    run._run()
    assert agent_speaks == []


def test_loop_speaks_final_text_when_no_skill_spoke(monkeypatch):
    """If no skill spoke (e.g. tools timed out), final LLM text is spoken."""
    bus = FakeBus()
    entry = _entry()
    spoken: List[str] = []
    bus.on("speak", lambda m: spoken.append(m.data["utterance"]))

    monkeypatch.setattr(
        agent_mod, "call_chat",
        lambda *a, **kw: ([], "I couldn't set the timer."),
    )

    run = _new_run(
        bus,
        name_index={entry.name: entry},
        initial_tool_calls=[LLMToolCall(entry.name, {}, "c0")],
        agent_config=AgentConfig(
            max_tool_iterations=2,
            tool_timeout_seconds=0.1,
            post_complete_grace_seconds=0.0,
        ),
    )
    run._run()
    assert spoken == ["I couldn't set the timer."]


def test_loop_aborts_before_iteration_when_abort_set(monkeypatch):
    """A pre-iteration abort short-circuits before any dispatch."""
    bus = FakeBus()
    spoken: List[str] = []
    bus.on("speak", lambda m: spoken.append(m.data["utterance"]))

    monkeypatch.setattr(agent_mod, "call_chat", lambda *a, **kw: ([], "should not speak"))

    run = _new_run(
        bus,
        initial_tool_calls=[LLMToolCall("anything", {}, "c0")],
    )
    run._abort.set()
    run._run()
    assert spoken == []


def test_loop_stops_on_mycroft_stop_event():
    """A mycroft.stop received while subscribed sets the abort flag."""
    bus = FakeBus()
    run = _new_run(bus)
    run.start()
    bus.emit(Message("mycroft.stop"))
    assert run._abort.is_set()
    run._thread.join(timeout=2.0)


def test_loop_stops_on_new_utterance_event():
    """A different utterance arriving while we run aborts the loop."""
    bus = FakeBus()
    run = _new_run(bus)
    run.start()
    bus.emit(Message("recognizer_loop:utterance",
                     {"utterances": ["something different"]}))
    assert run._abort.is_set()
    run._thread.join(timeout=2.0)


def test_loop_ignores_re_dispatch_of_same_utterance():
    """A re-dispatched identical utterance shouldn't kill the running loop."""
    bus = FakeBus()
    run = _new_run(bus)
    run.start()
    bus.emit(Message("recognizer_loop:utterance", {"utterances": ["hi"]}))
    assert not run._abort.is_set()
    run._abort.set()
    run._thread.join(timeout=2.0)

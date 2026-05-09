"""Background-thread agent loop for multi-tool LLM dispatch.

When the LLM returns one or more ``tool_calls`` in response to a user
utterance, this module runs the iterative dispatch:

* dispatch each tool by emitting its ``<skill_id>:<intent>`` event on the bus
* listen for ``mycroft.skill.handler.complete`` (or ``.error``) plus any
  ``speak`` events the skill emitted while running
* feed the captured speech back to the LLM as ``{role: "tool", content: ...}``
* re-call the LLM; if it returns more tool_calls, iterate (up to
  ``max_tool_iterations``); if it returns text, speak the text and exit

The intent service is *not* blocked. The pipeline returns a sentinel
``tool-calling:speak`` IntentHandlerMatch immediately after the first LLM
round-trip identifies tool_calls; the loop runs in a worker thread.

Stop coordination: the worker subscribes to ``mycroft.stop`` and
``recognizer_loop:utterance`` while running. Either firing aborts the loop
before the next iteration. A module-level lock ensures at most one loop is
active; spawning a fresh loop signals the previous one to abort.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from ovos_bus_client.client import MessageBusClient
from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus
from ovos_utils.log import LOG

from ovos_tool_calling.dispatch import (
    SPEAK_SKILL_ID,
    build_dispatch_message,
)
from ovos_tool_calling.llm import (
    LLMConfig,
    LLMToolCall,
    assistant_message_for_tool_calls,
    call_chat,
    tool_result_message,
)
from ovos_tool_calling.schemas import ToolEntry


HANDLER_COMPLETE = "mycroft.skill.handler.complete"
HANDLER_ERROR = "mycroft.skill.handler.error"
SPEAK_EVENT = "speak"
STOP_EVENT = "mycroft.stop"
NEW_UTTERANCE_EVENT = "recognizer_loop:utterance"


@dataclass
class _DispatchOutcome:
    status: str  # "ok" | "error" | "timeout"
    captured_speak: str = ""
    error: str = ""

    def as_tool_content(self) -> str:
        if self.status == "ok":
            return f"ok\n{self.captured_speak}".rstrip()
        if self.status == "error":
            return f"error: {self.error or 'unknown error'}"
        return "error: skill did not respond within timeout"


@dataclass
class AgentConfig:
    max_tool_iterations: int = 3
    tool_timeout_seconds: float = 8.0
    # Brief grace period after handler.complete for trailing speak events
    # (skills sometimes speak immediately *after* the handler returns).
    post_complete_grace_seconds: float = 0.5


class AgentLoop:
    """Owns the at-most-one slot for the agent loop.

    Public API:
      * ``start(...)``: synchronously claim the slot (cancelling any prior
        loop) and spawn the worker thread.
      * The worker calls ``self._on_done`` when finished so the slot frees.

    Per-loop state lives on the worker thread; this object is the dispatcher.
    """

    def __init__(
        self,
        bus: Union[MessageBusClient, FakeBus],
        agent_config: AgentConfig,
    ):
        self.bus = bus
        self.agent_config = agent_config
        self._lock = threading.Lock()
        self._current: Optional["_LoopRun"] = None

    def start(
        self,
        initial_messages: List[Dict[str, Any]],
        initial_tool_calls: List[LLMToolCall],
        initial_text: Optional[str],
        utterance: str,
        lang: str,
        original_message: Optional[Message],
        llm_config: LLMConfig,
        tools: List[Dict[str, Any]],
        name_index: Dict[str, ToolEntry],
    ) -> None:
        """Spawn the worker. Returns immediately."""
        with self._lock:
            prior = self._current
            run = _LoopRun(
                bus=self.bus,
                agent_config=self.agent_config,
                initial_messages=initial_messages,
                initial_tool_calls=initial_tool_calls,
                initial_text=initial_text,
                utterance=utterance,
                lang=lang,
                original_message=original_message,
                llm_config=llm_config,
                tools=tools,
                name_index=name_index,
                on_done=self._release,
            )
            self._current = run
        if prior is not None:
            LOG.info("[tool-calling] agent: cancelling prior loop in favour of new utterance")
            prior.cancel()
        run.start()

    def cancel_active(self) -> None:
        with self._lock:
            run = self._current
        if run is not None:
            run.cancel()

    def _release(self, run: "_LoopRun") -> None:
        with self._lock:
            if self._current is run:
                self._current = None


class _LoopRun:
    """One instance of the agent loop, executed in its own thread."""

    def __init__(
        self,
        bus: Union[MessageBusClient, FakeBus],
        agent_config: AgentConfig,
        initial_messages: List[Dict[str, Any]],
        initial_tool_calls: List[LLMToolCall],
        initial_text: Optional[str],
        utterance: str,
        lang: str,
        original_message: Optional[Message],
        llm_config: LLMConfig,
        tools: List[Dict[str, Any]],
        name_index: Dict[str, ToolEntry],
        on_done,
    ):
        self.bus = bus
        self.agent_config = agent_config
        self.messages = list(initial_messages)
        self.next_tool_calls = initial_tool_calls
        self.initial_text = initial_text
        self.utterance = utterance
        self.lang = lang
        self.original_message = original_message
        self.llm_config = llm_config
        self.tools = tools
        self.name_index = name_index
        self._on_done = on_done

        self._abort = threading.Event()
        self._thread = threading.Thread(
            target=self._safe_run,
            name=f"tool-calling-agent-{id(self):x}",
            daemon=True,
        )

        # Stop / new-utterance subscriptions are bound for this run only.
        self._abort_handlers = [
            (STOP_EVENT, self._on_stop),
            (NEW_UTTERANCE_EVENT, self._on_new_utterance),
        ]

    def start(self) -> None:
        for event, handler in self._abort_handlers:
            self.bus.on(event, handler)
        self._thread.start()

    def cancel(self) -> None:
        self._abort.set()

    def _on_stop(self, _message: Message) -> None:
        LOG.info("[tool-calling] agent: mycroft.stop received -> abort loop")
        self._abort.set()

    def _on_new_utterance(self, message: Message) -> None:
        # Only abort if it's a *different* utterance — re-dispatches of the
        # same string (rare, but possible from the listener) shouldn't kill us.
        new_utts = (message.data or {}).get("utterances") or []
        if new_utts and new_utts[0] != self.utterance:
            LOG.info("[tool-calling] agent: new utterance %r -> abort loop", new_utts[0])
            self._abort.set()

    def _safe_run(self) -> None:
        try:
            self._run()
        except Exception as e:  # noqa: BLE001
            LOG.exception("[tool-calling] agent loop crashed: %s", e)
        finally:
            for event, handler in self._abort_handlers:
                try:
                    self.bus.remove(event, handler)
                except Exception:  # noqa: BLE001
                    pass
            self._on_done(self)

    def _run(self) -> None:
        tool_calls = self.next_tool_calls
        text = self.initial_text
        any_skill_spoke = False

        for iteration in range(1, self.agent_config.max_tool_iterations + 1):
            if self._abort.is_set():
                LOG.info("[tool-calling] agent: aborted before iteration %d", iteration)
                return
            if not tool_calls:
                # LLM returned text only on this turn. Speak it *unless* a
                # skill already spoke during the loop — the user already got
                # spoken feedback and the LLM's "summary" would be redundant.
                if text and not any_skill_spoke:
                    self._speak(text)
                elif text:
                    LOG.info(
                        "[tool-calling] agent: suppressing redundant text after skill speak: %s",
                        text[:80],
                    )
                LOG.info(
                    "[tool-calling] agent: loop done (no more tool_calls, "
                    "skill_spoke=%s, final_text=%r)",
                    any_skill_spoke, (text[:60] + "...") if text and len(text) > 60 else text,
                )
                return

            LOG.info(
                "[tool-calling] agent: iteration %d, %d tool_call(s)",
                iteration, len(tool_calls),
            )
            # Record the assistant turn that produced these tool_calls.
            self.messages.append(assistant_message_for_tool_calls(tool_calls))

            # Dispatch each tool sequentially, appending its result as we go.
            for tc in tool_calls:
                if self._abort.is_set():
                    LOG.info("[tool-calling] agent: aborted mid-iteration")
                    return
                outcome = self._dispatch_one(tc)
                if outcome.captured_speak:
                    any_skill_spoke = True
                LOG.info(
                    "[tool-calling] agent: tool result for %s -> status=%s, speak=%r",
                    tc.tool_name, outcome.status,
                    outcome.captured_speak[:80] if outcome.captured_speak else "",
                )
                self.messages.append(
                    tool_result_message(tc.tool_call_id, outcome.as_tool_content())
                )

            # Ask the LLM what to do next.
            if self._abort.is_set():
                return
            LOG.debug("[tool-calling] agent: follow-up LLM call (history=%d msgs)",
                     len(self.messages))
            result = call_chat(self.llm_config, self.messages, self.tools)
            if result is None:
                LOG.warning("[tool-calling] agent: follow-up LLM call failed; exiting loop")
                return
            tool_calls, text = result

        # Hit max iterations with tools still pending -> speak whatever we have
        # so the user isn't left in silence (only if no skill already spoke).
        LOG.info("[tool-calling] agent: max_tool_iterations reached")
        if text and not any_skill_spoke:
            self._speak(text)

    def _dispatch_one(self, tc: LLMToolCall) -> _DispatchOutcome:
        entry = self.name_index.get(tc.tool_name)
        if entry is None:
            LOG.warning(
                "[tool-calling] agent: unknown tool %r in iteration", tc.tool_name
            )
            return _DispatchOutcome(status="error", error=f"unknown tool {tc.tool_name}")

        full_name = f"{entry.skill_id}:{entry.intent_name}"
        msg = build_dispatch_message(
            entry=entry,
            args=tc.arguments,
            utterance=self.utterance,
            lang=self.lang,
            original_message=self.original_message,
        )

        complete_event = threading.Event()
        error_box: Dict[str, str] = {}
        captured: List[str] = []

        def on_speak(message: Message):
            data = message.data or {}
            if (data.get("meta") or {}).get("skill") == entry.skill_id:
                utt = data.get("utterance")
                if utt:
                    captured.append(utt)

        def on_complete(message: Message):
            ctx_skill = (message.context or {}).get("skill_id")
            if ctx_skill == entry.skill_id:
                complete_event.set()

        def on_error(message: Message):
            data = message.data or {}
            ctx_skill = (message.context or {}).get("skill_id")
            if ctx_skill == entry.skill_id:
                error_box["msg"] = data.get("exception") or data.get("error") or "handler error"
                complete_event.set()

        self.bus.on(SPEAK_EVENT, on_speak)
        self.bus.on(HANDLER_COMPLETE, on_complete)
        self.bus.on(HANDLER_ERROR, on_error)
        try:
            LOG.info("[tool-calling] agent: dispatching %s with args=%s", full_name, tc.arguments)
            self.bus.emit(msg)

            # Wait for handler completion or timeout. Abort flag short-circuits.
            timeout = self.agent_config.tool_timeout_seconds
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._abort.is_set():
                    return _DispatchOutcome(status="error", error="aborted")
                if complete_event.wait(timeout=0.1):
                    break

            # Brief grace to capture trailing speaks (skills sometimes speak
            # right after the handler returns).
            if complete_event.is_set():
                time.sleep(self.agent_config.post_complete_grace_seconds)
                if "msg" in error_box:
                    return _DispatchOutcome(
                        status="error",
                        error=error_box["msg"],
                        captured_speak=" ".join(captured),
                    )
                return _DispatchOutcome(status="ok", captured_speak=" ".join(captured))

            return _DispatchOutcome(status="timeout", captured_speak=" ".join(captured))
        finally:
            self.bus.remove(SPEAK_EVENT, on_speak)
            self.bus.remove(HANDLER_COMPLETE, on_complete)
            self.bus.remove(HANDLER_ERROR, on_error)

    def _speak(self, text: str) -> None:
        """Speak final text via the same path v0.5 uses."""
        data = {
            "utterance": text,
            "expect_response": False,
            "meta": {"skill": SPEAK_SKILL_ID, "source": "tool-calling-agent"},
            "lang": self.lang,
        }
        msg = (
            self.original_message.forward("speak", data)
            if self.original_message is not None
            else Message("speak", data)
        )
        msg.context["skill_id"] = SPEAK_SKILL_ID
        LOG.info("[tool-calling] agent: speaking final summary (%d chars): %s",
                 len(text), text[:120])
        self.bus.emit(msg)

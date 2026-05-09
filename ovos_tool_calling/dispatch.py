"""Build the IntentHandlerMatch that ovos-core will dispatch.

When the LLM picks one of our generated tools, we have to hand ovos-core a
match shaped the way Adapt or Padatious would have shaped it, so the skill's
``@intent_handler`` receives the message it expects:

* For Padatious intents the match_data is the slot dict (``{"location":
  "Tokyo"}``), and the match_type is the full intent name (``<skill_id>:<name>``).
* For Adapt intents the match_data carries the canonical fields Adapt sends:
  ``intent_type``, ``utterance``, ``confidence``, ``__tags__`` (we leave the
  tags empty since the LLM bypassed Adapt's tagger). Most Adapt skills only
  read ``utterance`` from the message and re-parse arguments themselves, so
  this is a faithful enough shape.

When the LLM responds with plain text instead of a tool pick, ``make_speak_match``
synthesizes a no-op ``tool-calling:speak`` match. Speaking the text is a side
effect performed by the pipeline (see ``__init__.py``); the match itself just
signals "handled" to ovos-core so no further pipeline plugins run.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ovos_bus_client.message import Message
from ovos_plugin_manager.templates.pipeline import IntentHandlerMatch

from ovos_tool_calling.schemas import ToolEntry


SPEAK_MATCH_TYPE = "tool-calling:speak"
SPEAK_SKILL_ID = "tool-calling.openvoiceos"


def _match_data_for(entry: ToolEntry, args: Dict[str, Any], utterance: str,
                    confidence: float = 0.99) -> Dict[str, Any]:
    """Build the message.data dict for a dispatch, in the shape Adapt or
    Padatious would have produced. Shared by ``make_match`` (returns an
    IntentHandlerMatch for ovos-core to dispatch) and ``build_dispatch_message``
    (returns a Message the agent emits itself)."""
    full_name = f"{entry.skill_id}:{entry.intent_name}"
    if entry.matcher == "padatious":
        data: Dict[str, Any] = dict(args or {})
        data.setdefault("utterance", utterance)
        data.setdefault("conf", confidence)
        data.setdefault("name", full_name)
        return data
    return {
        "intent_type": full_name,
        "utterance": utterance,
        "confidence": confidence,
        "target": None,
        "__tags__": [],
    }


def make_match(
    entry: ToolEntry,
    args: Dict[str, Any],
    utterance: str,
    confidence: float = 0.99,
) -> IntentHandlerMatch:
    """Build the IntentHandlerMatch for a chosen tool.

    Args:
        entry: ToolEntry for the picked tool.
        args: Arguments the LLM extracted (parsed from tool_call.arguments).
        utterance: The original user utterance.
        confidence: Reported confidence; LLM tool-calling has no calibrated
            score, but ovos-core expects something in match_data for Adapt.
    """
    full_name = f"{entry.skill_id}:{entry.intent_name}"
    return IntentHandlerMatch(
        match_type=full_name,
        match_data=_match_data_for(entry, args, utterance, confidence),
        skill_id=entry.skill_id,
        utterance=utterance,
    )


def build_dispatch_message(
    entry: ToolEntry,
    args: Dict[str, Any],
    utterance: str,
    lang: str = "en-us",
    original_message: Optional[Message] = None,
    confidence: float = 0.99,
) -> Message:
    """Build the bus Message the agent emits to dispatch a tool itself.

    Used by the agent loop (v0.6+) to dispatch follow-up tools directly,
    bypassing ``IntentHandlerMatch`` round-trips through the intent service
    (we already returned a sentinel match for the original utterance).

    The Message type is ``<skill_id>:<intent_name>``; data shape mirrors
    what Adapt or Padatious would have produced. When ``original_message``
    is provided we use ``message.forward`` so session/context propagates.
    """
    full_name = f"{entry.skill_id}:{entry.intent_name}"
    data = _match_data_for(entry, args, utterance, confidence)
    data.setdefault("lang", lang)
    if original_message is not None:
        msg = original_message.forward(full_name, data)
    else:
        msg = Message(full_name, data, {"lang": lang})
    msg.context["skill_id"] = entry.skill_id
    return msg


def make_speak_match(
    utterance: str,
    text: str,
    lang: str = "en-us",
) -> IntentHandlerMatch:
    """Build the IntentHandlerMatch for the LLM-text answer path.

    The match itself is a sentinel — ovos-core will emit ``tool-calling:speak``
    on the bus, but no skill listens for it, so it's a no-op. The actual
    user-facing speech happens via a separate ``speak`` bus emission performed
    by the pipeline before returning this match. We need *some* match to stop
    further pipeline plugins (e.g. ovos-persona-low) from running and making a
    second redundant LLM call.
    """
    return IntentHandlerMatch(
        match_type=SPEAK_MATCH_TYPE,
        match_data={
            "utterance": utterance,
            "answer": text,
            "lang": lang,
        },
        skill_id=SPEAK_SKILL_ID,
        utterance=utterance,
    )

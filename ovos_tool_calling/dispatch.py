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
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ovos_plugin_manager.templates.pipeline import IntentHandlerMatch

from ovos_tool_calling.schemas import ToolEntry


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

    if entry.matcher == "padatious":
        # Padatious skills receive the slot dict directly in message.data.
        # Padatious also includes the original utterance and confidence; mirror that.
        match_data: Dict[str, Any] = dict(args or {})
        match_data.setdefault("utterance", utterance)
        match_data.setdefault("conf", confidence)
        match_data.setdefault("name", full_name)
        return IntentHandlerMatch(
            match_type=full_name,
            match_data=match_data,
            skill_id=entry.skill_id,
            utterance=utterance,
        )

    # Adapt path. Most Adapt handlers only read ``utterance`` and re-parse.
    # Fill in the canonical Adapt fields so handlers that *do* introspect
    # message.data don't choke on missing keys.
    match_data = {
        "intent_type": full_name,
        "utterance": utterance,
        "confidence": confidence,
        "target": None,
        "__tags__": [],
    }
    return IntentHandlerMatch(
        match_type=full_name,
        match_data=match_data,
        skill_id=entry.skill_id,
        utterance=utterance,
    )

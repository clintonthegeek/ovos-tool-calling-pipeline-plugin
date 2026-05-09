"""Tests for ovos_tool_calling.dispatch — match shapes and bus message synth."""

from __future__ import annotations

import pytest
from ovos_bus_client.message import Message

from ovos_tool_calling.dispatch import (
    SPEAK_MATCH_TYPE,
    SPEAK_SKILL_ID,
    build_dispatch_message,
    make_match,
    make_speak_match,
)
from ovos_tool_calling.schemas import ToolEntry


def _adapt_entry(skill_id="ovos-skill-alerts.openvoiceos", intent_name="CreateTimer"):
    return ToolEntry(
        name=f"{skill_id}__{intent_name}",
        skill_id=skill_id,
        intent_name=intent_name,
        matcher="adapt",
        schema={"function": {"name": f"{skill_id}__{intent_name}"}},
    )


def _padatious_entry(
    skill_id="ovos-skill-wikipedia.openvoiceos", intent_name="wiki.intent"
):
    return ToolEntry(
        name=f"{skill_id}__{intent_name}",
        skill_id=skill_id,
        intent_name=intent_name,
        matcher="padatious",
        schema={"function": {"name": f"{skill_id}__{intent_name}"}},
    )


# --- make_match ---------------------------------------------------------------


def test_make_match_adapt_shape():
    entry = _adapt_entry()
    m = make_match(entry, {"utterance": "set a 5 minute timer"}, "set a 5 minute timer")
    assert m.match_type == "ovos-skill-alerts.openvoiceos:CreateTimer"
    assert m.skill_id == entry.skill_id
    assert m.utterance == "set a 5 minute timer"
    # Adapt-shape match_data carries the canonical fields handlers may inspect.
    d = m.match_data
    assert d["intent_type"] == "ovos-skill-alerts.openvoiceos:CreateTimer"
    assert d["utterance"] == "set a 5 minute timer"
    assert d["confidence"] == 0.99
    assert d["__tags__"] == []


def test_make_match_padatious_shape_uses_slot_dict():
    entry = _padatious_entry()
    m = make_match(entry, {"query": "Pluto"}, "tell me about Pluto")
    assert m.match_type == "ovos-skill-wikipedia.openvoiceos:wiki.intent"
    d = m.match_data
    assert d["query"] == "Pluto"
    assert d["utterance"] == "tell me about Pluto"
    assert d["conf"] == 0.99
    assert d["name"] == "ovos-skill-wikipedia.openvoiceos:wiki.intent"


def test_make_match_padatious_does_not_clobber_explicit_args():
    """If args already include 'utterance' or 'conf', keep the caller's value."""
    entry = _padatious_entry()
    m = make_match(
        entry,
        {"query": "X", "utterance": "explicit", "conf": 0.42},
        "raw",
    )
    assert m.match_data["utterance"] == "explicit"
    assert m.match_data["conf"] == 0.42


# --- make_speak_match ---------------------------------------------------------


def test_make_speak_match_uses_sentinel_constants():
    m = make_speak_match("what is X", "X is Y", "en-US")
    assert m.match_type == SPEAK_MATCH_TYPE
    assert m.skill_id == SPEAK_SKILL_ID
    assert m.utterance == "what is X"
    assert m.match_data["answer"] == "X is Y"
    assert m.match_data["lang"] == "en-US"


# --- build_dispatch_message ---------------------------------------------------


def test_build_dispatch_message_adapt():
    entry = _adapt_entry()
    msg = build_dispatch_message(
        entry=entry,
        args={"utterance": "set a 7 minute timer"},
        utterance="set a 7 minute timer",
        lang="en-US",
    )
    assert isinstance(msg, Message)
    assert msg.msg_type == "ovos-skill-alerts.openvoiceos:CreateTimer"
    assert msg.context["skill_id"] == "ovos-skill-alerts.openvoiceos"
    # Adapt match_data shape preserved on the bus message.
    assert msg.data["intent_type"] == "ovos-skill-alerts.openvoiceos:CreateTimer"
    assert msg.data["utterance"] == "set a 7 minute timer"
    assert msg.data["lang"] == "en-US"


def test_build_dispatch_message_padatious_carries_slots():
    entry = _padatious_entry()
    msg = build_dispatch_message(
        entry=entry,
        args={"query": "Saturn"},
        utterance="tell me about Saturn",
        lang="en-US",
    )
    assert msg.msg_type == "ovos-skill-wikipedia.openvoiceos:wiki.intent"
    assert msg.data["query"] == "Saturn"
    assert msg.data["utterance"] == "tell me about Saturn"


def test_build_dispatch_message_forwards_from_original():
    """When given an originating Message, the new one inherits its context."""
    original = Message(
        "recognizer_loop:utterance",
        {"utterances": ["set a timer"]},
        {"session": {"session_id": "S-123"}, "lang": "en-US"},
    )
    entry = _adapt_entry()
    msg = build_dispatch_message(
        entry=entry,
        args={"utterance": "set a timer"},
        utterance="set a timer",
        lang="en-US",
        original_message=original,
    )
    # Forwarded messages carry the original's session context.
    assert msg.context.get("session", {}).get("session_id") == "S-123"
    # And our explicit skill_id.
    assert msg.context["skill_id"] == "ovos-skill-alerts.openvoiceos"

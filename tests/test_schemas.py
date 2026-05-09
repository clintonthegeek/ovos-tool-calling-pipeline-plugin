"""Tests for ovos_tool_calling.schemas — pure functions only, no bus needed."""

from __future__ import annotations

import pytest

from ovos_tool_calling import AdaptIntent, PadatiousIntent, SkillRecord
from ovos_tool_calling.schemas import (
    TOOL_NAME_MAXLEN,
    adapt_intent_to_schema,
    build_tool_catalog,
    extract_slots,
    padatious_intent_to_schema,
    sanitize_tool_name,
)


# --- sanitize_tool_name -------------------------------------------------------


def test_sanitize_basic_skill_id_intent_name():
    name = sanitize_tool_name("ovos-skill-alerts.openvoiceos", "CreateTimer")
    assert name == "ovos-skill-alerts_openvoiceos__CreateTimer"
    assert len(name) <= TOOL_NAME_MAXLEN


def test_sanitize_replaces_dots_and_colons():
    name = sanitize_tool_name("foo.bar:baz", "Intent")
    # Both '.' and ':' are not in the allowed [A-Za-z0-9_-] set.
    assert ":" not in name and "." not in name


def test_sanitize_truncates_when_too_long():
    long_skill = "x" * 80
    name = sanitize_tool_name(long_skill, "Intent")
    assert len(name) <= TOOL_NAME_MAXLEN
    # The intent name should still be at the tail.
    assert name.endswith("__Intent")


def test_sanitize_truncates_when_intent_is_huge():
    name = sanitize_tool_name("s", "I" * 200)
    assert len(name) <= TOOL_NAME_MAXLEN


# --- extract_slots ------------------------------------------------------------


def test_extract_slots_dedupes_and_preserves_order():
    samples = [
        "tell me about {topic}",
        "what is the weather in {city}",
        "weather in {city} please",
        "remind me to {task} at {time}",
    ]
    assert extract_slots(samples) == ["topic", "city", "task", "time"]


def test_extract_slots_no_slots():
    assert extract_slots(["hello", "what time is it"]) == []


# --- adapt_intent_to_schema ---------------------------------------------------


def _no_vocab(_vid):
    return []


def test_adapt_schema_has_passthrough_utterance_param():
    intent = AdaptIntent(
        name="CreateTimer",
        skill_id="ovos-skill-alerts.openvoiceos",
        required=["create", "timer"],
    )
    entry = adapt_intent_to_schema(intent, _no_vocab)

    assert entry.matcher == "adapt"
    assert entry.skill_id == "ovos-skill-alerts.openvoiceos"
    assert entry.intent_name == "CreateTimer"
    fn = entry.schema["function"]
    assert fn["name"] == entry.name
    params = fn["parameters"]
    assert params["properties"]["utterance"]["type"] == "string"
    assert params["required"] == ["utterance"]


def test_adapt_description_uses_vocab_resolver():
    intent = AdaptIntent(
        name="CreateTimer",
        skill_id="ovos-skill-alerts.openvoiceos",
        required=["create", "timer"],
    )
    vocabs = {"create": ["set", "start", "make"], "timer": ["timer", "alarm"]}
    entry = adapt_intent_to_schema(intent, lambda vid: vocabs.get(vid, []))
    desc = entry.schema["function"]["description"]
    # Expect the resolved trigger phrases to be in the description.
    assert "set" in desc and "start" in desc
    assert "timer" in desc


# --- padatious_intent_to_schema -----------------------------------------------


def test_padatious_schema_extracts_slots_as_string_params():
    intent = PadatiousIntent(
        name="wiki.intent",
        skill_id="ovos-skill-wikipedia.openvoiceos",
        samples=[
            "tell me about {query}",
            "what is {query}",
            "search wikipedia for {query}",
        ],
    )
    entry = padatious_intent_to_schema(intent)

    assert entry.matcher == "padatious"
    fn = entry.schema["function"]
    assert "query" in fn["parameters"]["properties"]
    assert fn["parameters"]["properties"]["query"]["type"] == "string"


def test_padatious_schema_has_no_required_slots():
    """Slots are best-effort — Padatious itself tolerates missing matches."""
    intent = PadatiousIntent(
        name="example",
        skill_id="skill",
        samples=["do {action} now"],
    )
    entry = padatious_intent_to_schema(intent)
    assert entry.schema["function"]["parameters"]["required"] == []


def test_padatious_description_includes_samples():
    intent = PadatiousIntent(
        name="joke.intent",
        skill_id="ovos-skill-icanhazdadjokes.openvoiceos",
        samples=["tell me a joke", "say something funny"],
    )
    entry = padatious_intent_to_schema(intent)
    desc = entry.schema["function"]["description"]
    assert "tell me a joke" in desc


# --- build_tool_catalog -------------------------------------------------------


def test_build_catalog_indexes_both_matchers():
    """A skill with both adapt and padatious intents should produce two tools."""
    rec = SkillRecord(skill_id="ovos-skill-alerts.openvoiceos")
    rec.adapt_intents["CreateTimer"] = AdaptIntent(
        name="CreateTimer",
        skill_id="ovos-skill-alerts.openvoiceos",
        required=["create", "timer"],
    )
    rec.padatious_intents["snooze.intent"] = PadatiousIntent(
        name="snooze.intent",
        skill_id="ovos-skill-alerts.openvoiceos",
        samples=["snooze for {duration}"],
    )
    skills = {"ovos-skill-alerts.openvoiceos": rec}

    tools, index = build_tool_catalog(skills, _no_vocab)

    assert len(tools) == 2
    assert len(index) == 2
    matchers = {entry.matcher for entry in index.values()}
    assert matchers == {"adapt", "padatious"}


def test_build_catalog_index_keys_match_schema_names():
    rec = SkillRecord(skill_id="skill.x")
    rec.adapt_intents["Foo"] = AdaptIntent(name="Foo", skill_id="skill.x", required=["a"])
    tools, index = build_tool_catalog({"skill.x": rec}, _no_vocab)
    # The tool name in the schema must equal the index key.
    schema_name = tools[0]["function"]["name"]
    assert schema_name in index
    assert index[schema_name].name == schema_name


def test_build_catalog_empty_when_no_skills():
    tools, index = build_tool_catalog({}, _no_vocab)
    assert tools == []
    assert index == {}

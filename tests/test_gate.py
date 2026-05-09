"""Tests for ovos_tool_calling.gate — admission control + LRU cache.

These tests don't touch the bus; the Gate is pure with respect to its config
and the utterance string.
"""

from __future__ import annotations

import pytest
from ovos_plugin_manager.templates.pipeline import IntentHandlerMatch

from ovos_tool_calling.gate import Gate


def _fake_match(name: str = "test:Intent") -> IntentHandlerMatch:
    return IntentHandlerMatch(
        match_type=name,
        match_data={"utterance": "ignored"},
        skill_id="test.skill",
        utterance="ignored",
    )


# --- skip rules ---------------------------------------------------------------


def test_skip_empty_utterance():
    g = Gate({"min_words": 2})
    d = g.consider("")
    assert d.action == "skip"
    assert "empty" in d.reason


def test_skip_whitespace_only():
    g = Gate({"min_words": 2})
    assert g.consider("   ").action == "skip"


def test_skip_too_short():
    g = Gate({"min_words": 3})
    d = g.consider("hello there")
    assert d.action == "skip"
    assert "too short" in d.reason


def test_skip_blocklist_match():
    g = Gate({"min_words": 1, "blocklist_patterns": [r"\bhey mycroft\b"]})
    d = g.consider("hey mycroft what time is it")
    assert d.action == "skip"
    assert "blocklist" in d.reason


def test_blocklist_invalid_regex_does_not_crash():
    """Bad regex is logged and ignored — Gate still works."""
    g = Gate({"min_words": 1, "blocklist_patterns": ["[unclosed"]})
    # No crash; the bad pattern should simply not be applied.
    d = g.consider("any utterance")
    assert d.action == "proceed"


# --- proceed + cache ----------------------------------------------------------


def test_proceed_when_no_skip_rule_matches():
    g = Gate({"min_words": 2})
    d = g.consider("set a five minute timer")
    assert d.action == "proceed"


def test_cache_hit_after_record():
    g = Gate({"min_words": 2, "cache_size": 8})
    m = _fake_match("ovos-skill-alerts:CreateTimer")

    # First time: proceed.
    d1 = g.consider("set a five minute timer")
    assert d1.action == "proceed"

    # Caller records the dispatch.
    g.record("set a five minute timer", m)

    # Second time: cache hit returning the same match.
    d2 = g.consider("set a five minute timer")
    assert d2.action == "cached"
    assert d2.cached_match is m


def test_cache_normalization_handles_whitespace_and_case():
    g = Gate({"min_words": 2, "cache_size": 8})
    m = _fake_match()
    g.record("Set A Timer", m)

    # Different case + extra spaces should still hit the cache.
    d = g.consider("set   a   timer")
    assert d.action == "cached"
    assert d.cached_match is m


# --- numeric-word normalization (digit <-> word equivalence in cache keys) ----


def test_cache_normalization_digit_word_equivalence_simple():
    """'five' and '5' should hit the same cache entry."""
    g = Gate({"min_words": 2, "cache_size": 8})
    m = _fake_match("ovos-skill-alerts:CreateTimer")
    g.record("set a 5 minute timer", m)

    d = g.consider("set a five minute timer")
    assert d.action == "cached"
    assert d.cached_match is m


def test_cache_normalization_digit_word_equivalence_reverse():
    """'5' should hit a cache entry recorded under 'five'."""
    g = Gate({"min_words": 2, "cache_size": 8})
    m = _fake_match()
    g.record("set a five minute timer", m)

    d = g.consider("set a 5 minute timer")
    assert d.action == "cached"
    assert d.cached_match is m


def test_cache_normalization_teen_words():
    """Teens (thirteen..nineteen) should normalize to their digits."""
    g = Gate({"min_words": 2, "cache_size": 8})
    m = _fake_match()
    g.record("set a 15 minute timer", m)

    assert g.consider("set a fifteen minute timer").action == "cached"


def test_cache_normalization_tens_words():
    """Round tens (twenty..ninety) should normalize to digits."""
    g = Gate({"min_words": 2, "cache_size": 8})
    m = _fake_match()
    g.record("set a 30 minute timer", m)

    assert g.consider("set a thirty minute timer").action == "cached"


def test_cache_normalization_compound_tens():
    """'twenty five' and '25' should hit the same cache key."""
    g = Gate({"min_words": 2, "cache_size": 8})
    m = _fake_match()
    g.record("set a 25 minute timer", m)

    assert g.consider("set a twenty five minute timer").action == "cached"


def test_cache_normalization_hyphenated_compound():
    """'twenty-five' (hyphenated, as STT may transcribe) should also normalize."""
    g = Gate({"min_words": 2, "cache_size": 8})
    m = _fake_match()
    g.record("set a 25 minute timer", m)

    assert g.consider("set a twenty-five minute timer").action == "cached"


def test_cache_normalization_does_not_mangle_non_numbers():
    """Words that look numeric-adjacent must not be coerced (e.g. 'won' != 'one')."""
    g = Gate({"min_words": 2, "cache_size": 8})
    m = _fake_match()
    g.record("we won the game", m)

    # Different sentence with the digit '1' substituted should NOT hit cache.
    assert g.consider("we 1 the game").action == "proceed"


def test_cache_normalization_idempotent_on_digits():
    """Already-digit utterances should still cache-hit themselves."""
    g = Gate({"min_words": 2, "cache_size": 8})
    m = _fake_match()
    g.record("set a 5 minute timer", m)

    assert g.consider("set a 5 minute timer").action == "cached"


def test_record_none_does_not_cache():
    """A None result is worth re-trying next time, not caching."""
    g = Gate({"min_words": 2, "cache_size": 8})
    g.record("some utterance", None)
    d = g.consider("some utterance")
    # Still proceeds — nothing was cached.
    assert d.action == "proceed"


def test_cache_size_zero_disables_caching():
    g = Gate({"min_words": 2, "cache_size": 0})
    m = _fake_match()
    g.record("foo bar", m)
    assert g.consider("foo bar").action == "proceed"


def test_lru_evicts_oldest():
    g = Gate({"min_words": 1, "cache_size": 2})
    g.record("first", _fake_match("first"))
    g.record("second", _fake_match("second"))
    g.record("third", _fake_match("third"))  # should evict "first"

    assert g.consider("first").action == "proceed"
    assert g.consider("second").action == "cached"
    assert g.consider("third").action == "cached"


def test_lru_touch_on_hit_keeps_entry_warm():
    g = Gate({"min_words": 1, "cache_size": 2})
    g.record("a", _fake_match("a"))
    g.record("b", _fake_match("b"))
    # Hit "a" so it becomes most-recent.
    assert g.consider("a").action == "cached"
    # Adding "c" should now evict "b" (the oldest after the touch).
    g.record("c", _fake_match("c"))

    assert g.consider("b").action == "proceed"  # evicted
    assert g.consider("a").action == "cached"   # kept (was touched)
    assert g.consider("c").action == "cached"


# --- stats --------------------------------------------------------------------


def test_stats_reports_cache_size_and_blocklist():
    g = Gate({"min_words": 1, "cache_size": 4, "blocklist_patterns": ["foo", "bar"]})
    cached, blocked = g.stats()
    assert cached == 0
    assert blocked == 2

    g.record("hello world", _fake_match())
    cached, _ = g.stats()
    assert cached == 1

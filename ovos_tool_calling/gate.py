"""Pre-LLM admission control + dispatch caching.

The LLM round-trip is the most expensive thing this plugin does. Most
utterances reaching us at the ``*-low`` tier are still cases where calling
an LLM is wasteful: empty transcriptions, single-token noise, repeated
queries we just answered, or known false-trigger phrases.

The Gate runs *before* any catalog build or HTTP call and answers a single
question: should we even try the LLM for this utterance? It also keeps a
small LRU of recent (utterance -> IntentHandlerMatch) decisions so a
repeated query returns instantly without another LLM round-trip.

Configuration (all under the plugin's ``intents`` block, all optional):

    "min_words":           2,        # skip if utterance has < N whitespace-tokens
    "blocklist_patterns":  [],       # regexes; if any matches, skip
    "cache_size":          32,       # LRU; 0 disables caching
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple

from ovos_plugin_manager.templates.pipeline import IntentHandlerMatch
from ovos_utils.log import LOG


_NUM_WORDS: Dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_TENS_WORDS: Dict[str, int] = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}


def _words_to_digits(text: str) -> str:
    """Replace English number-words 0..99 with their digit form.

    Handles bare units ("five" → "5"), teens, round tens, and compound
    tens-plus-units ("twenty five" → "25"). Other tokens pass through
    untouched. The output preserves single-space separation between tokens.
    """
    tokens = text.split()
    out: List[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in _TENS_WORDS:
            base = _TENS_WORDS[t]
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if nxt in _NUM_WORDS and 1 <= _NUM_WORDS[nxt] <= 9:
                out.append(str(base + _NUM_WORDS[nxt]))
                i += 2
                continue
            out.append(str(base))
        elif t in _NUM_WORDS:
            out.append(str(_NUM_WORDS[t]))
        else:
            out.append(t)
        i += 1
    return " ".join(out)


@dataclass(frozen=True)
class GateDecision:
    """Result of consulting the gate before doing LLM work.

    Exactly one of ``cached_match`` or ``proceed`` is meaningful per the
    ``action`` field:

    - action="skip":  do not call the LLM. Return None to ovos-core, let the
      remaining pipeline tiers run.
    - action="cached":  return ``cached_match`` immediately. Same utterance
      was successfully dispatched recently.
    - action="proceed":  call the LLM. The caller must report the resulting
      match (or None) back via ``Gate.record(...)`` so the cache stays warm.
    """

    action: str  # "skip" | "cached" | "proceed"
    reason: str
    cached_match: Optional[IntentHandlerMatch] = None


class Gate:
    """Decide whether (and how) to invoke the LLM for an utterance."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        config = config or {}
        self.min_words: int = int(config.get("min_words", 2))
        self.cache_size: int = int(config.get("cache_size", 32))
        patterns = config.get("blocklist_patterns") or []
        self._blocklist: List[re.Pattern] = []
        for p in patterns:
            try:
                self._blocklist.append(re.compile(p, re.IGNORECASE))
            except re.error as e:
                LOG.warning("[tool-calling/gate] bad blocklist regex %r: %s", p, e)

        self._lock = RLock()
        self._cache: "OrderedDict[str, IntentHandlerMatch]" = OrderedDict()

    @staticmethod
    def _normalize(utterance: str) -> str:
        """Cache-key normalization: trim, collapse whitespace, lowercase, and
        coerce English number-words 0..99 into digits so e.g. "set a five
        minute timer" and "set a 5 minute timer" share a cache entry.
        """
        text = re.sub(r"\s+", " ", utterance.strip().lower())
        # Treat hyphenated compounds ("twenty-five") as two tokens so they
        # match the spaced form. Done after lowercasing so we don't disturb
        # other punctuation logic.
        text = text.replace("-", " ")
        return _words_to_digits(text)

    def consider(self, utterance: str) -> GateDecision:
        """Run the gate for an utterance. Does not call the LLM itself."""
        if not utterance or not utterance.strip():
            return GateDecision("skip", "empty utterance")

        words = utterance.split()
        if len(words) < self.min_words:
            return GateDecision(
                "skip", f"too short ({len(words)} < min_words={self.min_words})"
            )

        for pat in self._blocklist:
            if pat.search(utterance):
                return GateDecision("skip", f"blocklist match: /{pat.pattern}/")

        if self.cache_size > 0:
            key = self._normalize(utterance)
            with self._lock:
                cached = self._cache.get(key)
                if cached is not None:
                    # LRU touch.
                    self._cache.move_to_end(key)
                    return GateDecision(
                        "cached", "cache hit", cached_match=cached
                    )

        return GateDecision("proceed", "no skip rule matched")

    def record(self, utterance: str, match: Optional[IntentHandlerMatch]) -> None:
        """Update the cache with the final dispatch result.

        Only successful dispatches (non-None) are cached; a None result is
        worth re-trying next time (the LLM may have been transiently
        unavailable, or the catalog may have grown).
        """
        if match is None or self.cache_size <= 0:
            return
        key = self._normalize(utterance)
        with self._lock:
            self._cache[key] = match
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)

    def stats(self) -> Tuple[int, int]:
        """Return (cached_entries, blocklist_size) for observability."""
        with self._lock:
            return len(self._cache), len(self._blocklist)

from collections import defaultdict
from dataclasses import dataclass, field
from threading import RLock
from typing import Dict, List, Optional, Union

from ovos_bus_client.client import MessageBusClient
from ovos_bus_client.message import Message
from ovos_plugin_manager.templates.pipeline import (
    ConfidenceMatcherPipeline,
    IntentHandlerMatch,
)
from ovos_utils.fakebus import FakeBus
from ovos_utils.log import LOG


@dataclass
class AdaptIntent:
    """Keyword-based intent (Adapt). The skill matches when all `required`
    vocab IDs are present in the utterance."""

    name: str
    skill_id: str
    required: List[str] = field(default_factory=list)
    at_least_one: List[List[str]] = field(default_factory=list)
    optional: List[str] = field(default_factory=list)


@dataclass
class PadatiousIntent:
    """Fuzzy-trained intent (Padatious). The skill registers a list of example
    utterances; matching is done via a small classifier."""

    name: str
    skill_id: str
    samples: List[str] = field(default_factory=list)
    lang: str = "en-us"


@dataclass
class SkillRecord:
    """Aggregate of everything one skill has registered."""

    skill_id: str
    adapt_intents: Dict[str, AdaptIntent] = field(default_factory=dict)
    padatious_intents: Dict[str, PadatiousIntent] = field(default_factory=dict)


class SkillRegistry:
    """Accumulates skill/intent registrations seen on the bus.

    Subscribes to `register_intent`, `register_vocab`, `padatious:register_intent`,
    `padatious:register_entity`, `detach_intent`, and `detach_skill`. Builds a
    `{skill_id: SkillRecord}` view that the pipeline can later turn into LLM
    tool-call schemas.
    """

    def __init__(self, bus: Union[MessageBusClient, FakeBus]):
        self.bus = bus
        self._lock = RLock()
        self._skills: Dict[str, SkillRecord] = {}
        # vocab_id -> set of literal phrases (e.g. 'create' -> {'set', 'start', 'make'})
        self._vocab: Dict[str, set] = defaultdict(set)
        self._wire()

    def _wire(self):
        self.bus.on("register_intent", self._on_adapt_intent)
        self.bus.on("register_vocab", self._on_vocab)
        self.bus.on("padatious:register_intent", self._on_padatious_intent)
        self.bus.on("detach_intent", self._on_detach_intent)
        self.bus.on("detach_skill", self._on_detach_skill)

    @staticmethod
    def _split_intent_name(intent_name: str) -> tuple:
        """Intent names are usually `<skill_id>:<IntentName>`. Returns
        `(skill_id, intent_name)`; if the form is unrecognized, returns
        `("anonymous", intent_name)`."""
        if ":" in intent_name:
            skill_id, _, name = intent_name.partition(":")
            return skill_id, name
        return "anonymous", intent_name

    def _record(self, skill_id: str) -> SkillRecord:
        rec = self._skills.get(skill_id)
        if rec is None:
            rec = SkillRecord(skill_id=skill_id)
            self._skills[skill_id] = rec
        return rec

    def _on_adapt_intent(self, message: Message):
        data = message.data or {}
        full_name = data.get("name") or ""
        skill_id, name = self._split_intent_name(full_name)
        # `requires` is a list of [vocab_id, vocab_alias] pairs.
        required = [pair[0] for pair in (data.get("requires") or []) if pair]
        optional = [pair[0] for pair in (data.get("optional") or []) if pair]
        # `at_least_one` is a list of groups of vocab_ids (any one suffices).
        at_least_one = list(data.get("at_least_one") or [])
        intent = AdaptIntent(
            name=name,
            skill_id=skill_id,
            required=required,
            at_least_one=at_least_one,
            optional=optional,
        )
        with self._lock:
            self._record(skill_id).adapt_intents[name] = intent
        LOG.debug("[tool-calling] +adapt %s :: %s", skill_id, name)

    def _on_vocab(self, message: Message):
        data = message.data or {}
        vocab_id = data.get("entity_type")
        phrase = data.get("entity_value")
        if not vocab_id or not phrase:
            return
        # OVOS munges vocab IDs differently in two places: vocab files emit
        # `<alnum_skill_id><Title>` while IntentBuilder.require() keeps the
        # author's case. Adapt's matcher lowercases at runtime, so we do too
        # at registration time so vocab.lookup() works either way.
        with self._lock:
            self._vocab[vocab_id.lower()].add(phrase)

    def _on_padatious_intent(self, message: Message):
        data = message.data or {}
        full_name = data.get("name") or ""
        skill_id = data.get("skill_id") or message.context.get("skill_id")
        if not skill_id:
            skill_id, name = self._split_intent_name(full_name)
        else:
            _, name = self._split_intent_name(full_name)
        intent = PadatiousIntent(
            name=name,
            skill_id=skill_id,
            samples=list(data.get("samples") or []),
            lang=data.get("lang") or "en-us",
        )
        with self._lock:
            self._record(skill_id).padatious_intents[name] = intent
        LOG.debug(
            "[tool-calling] +padatious %s :: %s (%d samples)",
            skill_id,
            name,
            len(intent.samples),
        )

    def _on_detach_intent(self, message: Message):
        full_name = (message.data or {}).get("intent_name") or ""
        skill_id, name = self._split_intent_name(full_name)
        with self._lock:
            rec = self._skills.get(skill_id)
            if rec:
                rec.adapt_intents.pop(name, None)
                rec.padatious_intents.pop(name, None)

    def _on_detach_skill(self, message: Message):
        skill_id = (message.data or {}).get("skill_id")
        if not skill_id:
            return
        with self._lock:
            self._skills.pop(skill_id, None)

    def snapshot(self) -> Dict[str, SkillRecord]:
        """Thread-safe shallow copy for read-only inspection."""
        with self._lock:
            return dict(self._skills)

    def vocab(self, vocab_id: str) -> List[str]:
        """Resolve a vocab id (e.g. 'create') to its literal phrases
        (e.g. ['set', 'start', 'make']). Returns [] if unknown.
        Lookup is case-insensitive (see _on_vocab for why)."""
        with self._lock:
            return sorted(self._vocab.get(vocab_id.lower(), ()))

    def summary(self) -> str:
        """Human-readable summary, useful for logging."""
        with self._lock:
            lines = []
            for skill_id in sorted(self._skills):
                rec = self._skills[skill_id]
                a, p = len(rec.adapt_intents), len(rec.padatious_intents)
                lines.append(f"  {skill_id}: adapt={a} padatious={p}")
            return (
                f"SkillRegistry: {len(self._skills)} skills, "
                f"{sum(len(r.adapt_intents) for r in self._skills.values())} adapt intents, "
                f"{sum(len(r.padatious_intents) for r in self._skills.values())} padatious intents, "
                f"{len(self._vocab)} vocabs\n" + "\n".join(lines)
            )


class ToolCallingPipeline(ConfidenceMatcherPipeline):
    """LLM-orchestrator pipeline plugin.

    v0.3: when ``enabled`` in config, calls the configured LLM with the tool
    catalog and dispatches the picked tool by returning the matching
    IntentHandlerMatch. If the LLM answers in plain text instead of picking
    a tool, we currently return None and let the rest of the pipeline run.
    (v0.4 will speak the plain answer.)

    Place us where you want in the pipeline:
      - At ``*-high`` for full LLM-orchestrator mode (LLM sees every utterance
        first; remaining matchers only run if no tool fits and we returned
        None)
      - At ``*-low`` for fallback mode (only utterances that no other matcher
        caught reach us)

    Bus inspection helpers:
      - emit ``tool-calling.registry.dump`` to log the registry summary
      - emit ``tool-calling.schemas.dump`` to log a catalog summary
      - emit ``tool-calling.schemas.dump`` with ``data={"full": True}`` for
        the full JSON catalog
    """

    DUMP_REGISTRY_EVENT = "tool-calling.registry.dump"
    DUMP_SCHEMAS_EVENT = "tool-calling.schemas.dump"

    def __init__(
        self,
        bus: Optional[Union[MessageBusClient, FakeBus]] = None,
        config: Optional[Dict] = None,
    ):
        super().__init__(bus=bus, config=config)
        self.registry = SkillRegistry(self.bus)
        self.bus.on(self.DUMP_REGISTRY_EVENT, self._handle_dump_registry)
        self.bus.on(self.DUMP_SCHEMAS_EVENT, self._handle_dump_schemas)

        from ovos_tool_calling.gate import Gate
        from ovos_tool_calling.llm import build_config

        self.enabled = bool(self.config.get("enabled", False))
        self.llm_config = build_config(self.config) if self.enabled else None
        self.gate = Gate(self.config)
        # v0.5: when the LLM answers in text (no tool pick), speak the text
        # ourselves and claim the utterance as handled, so the rest of the
        # pipeline (notably ovos-persona-low) doesn't trigger a second LLM
        # round-trip on the same utterance. Set to False to fall through to
        # downstream pipeline plugins instead.
        self.speak_text_answers = bool(self.config.get("speak_text_answers", True))
        # When ConfidenceMatcherPipeline.match() falls through high → medium → low,
        # or when a config registers us at multiple tiers, we'd otherwise call
        # the LLM once per tier. Short-lived memo (1s) dedupes those same-tick
        # calls without persisting across separate user queries.
        self._inflight_utterance: Optional[str] = None
        self._inflight_result: Optional[IntentHandlerMatch] = None
        self._inflight_at: float = 0.0
        self._inflight_ttl: float = 1.0

        status = "ENABLED" if self.enabled and (self.llm_config and self.llm_config.is_usable()) else "disabled"
        model = self.llm_config.model if self.llm_config else "(none)"
        cache_size, blocklist_size = self.gate.stats()
        LOG.info(
            "ToolCallingPipeline loaded (v0.5: speak text answers) — %s, model=%s, "
            "speak_text=%s, gate(min_words=%d, cache_size=%d, blocklist=%d)",
            status, model, self.speak_text_answers,
            self.gate.min_words, self.gate.cache_size, blocklist_size,
        )

    def build_catalog(self):
        """Snapshot the registry and return ``(tools, name_index)``.

        Imported lazily so the schemas module can keep importing names from
        this module without risking a circular import at startup.
        """
        from ovos_tool_calling.schemas import build_tool_catalog

        return build_tool_catalog(self.registry.snapshot(), self.registry.vocab)

    def _handle_dump_registry(self, message: Message):
        LOG.info("[tool-calling] %s", self.registry.summary())

    def _handle_dump_schemas(self, message: Message):
        import json

        tools, index = self.build_catalog()
        # Catalog summary (always small).
        adapt = sum(1 for e in index.values() if e.matcher == "adapt")
        pada = sum(1 for e in index.values() if e.matcher == "padatious")
        LOG.info(
            "[tool-calling] catalog: %d tools (%d adapt + %d padatious)",
            len(tools),
            adapt,
            pada,
        )
        if (message.data or {}).get("full"):
            # Full catalog requested; log as JSON. Can be very long.
            LOG.info(
                "[tool-calling] catalog JSON:\n%s",
                json.dumps(tools, indent=2, ensure_ascii=False),
            )
            return
        # Otherwise, log a few representative entries: first adapt + first padatious.
        first_adapt = next(
            (e for e in index.values() if e.matcher == "adapt"), None
        )
        first_pada = next(
            (e for e in index.values() if e.matcher == "padatious"), None
        )
        for label, entry in (("adapt example", first_adapt), ("padatious example", first_pada)):
            if entry is None:
                continue
            LOG.info(
                "[tool-calling] %s: %s\n%s",
                label,
                entry.name,
                json.dumps(entry.schema, indent=2, ensure_ascii=False),
            )

    def _try_llm_dispatch(
        self,
        utterances: List[str],
        tier: str,
        lang: str = "en-us",
        message: Optional[Message] = None,
    ) -> Optional[IntentHandlerMatch]:
        """Build the catalog, ask the LLM, and dispatch its tool pick.

        Returns the IntentHandlerMatch if a tool was picked, or None if the
        LLM declined / failed / answered in text. The same logic runs at all
        three confidence tiers; the user controls which tier reaches us via
        pipeline placement in mycroft.conf.
        """
        if not self.enabled or self.llm_config is None or not self.llm_config.is_usable():
            return None
        if not utterances:
            return None
        utterance = utterances[0]

        # Reuse the previous tier's result if we just computed it (within ttl).
        import time as _time

        now = _time.monotonic()
        if (
            self._inflight_utterance == utterance
            and (now - self._inflight_at) < self._inflight_ttl
        ):
            return self._inflight_result
        self._inflight_utterance = utterance
        self._inflight_result = None
        self._inflight_at = now

        # Gate: cheap pre-LLM admission control + LRU cache.
        decision = self.gate.consider(utterance)
        if decision.action == "skip":
            LOG.info("[tool-calling] %s: gate skip (%s)", tier, decision.reason)
            return None
        if decision.action == "cached":
            LOG.info(
                "[tool-calling] %s: gate cache hit -> %s",
                tier, decision.cached_match.match_type,
            )
            self._inflight_result = decision.cached_match
            return decision.cached_match

        from ovos_tool_calling.dispatch import make_match
        from ovos_tool_calling.llm import call_chat

        tools, name_index = self.build_catalog()
        if not tools:
            LOG.debug("[tool-calling] %s: no tools registered yet", tier)
            return None

        result = call_chat(self.llm_config, utterance, tools)
        if result is None:
            return None
        tool_calls, text = result

        if not tool_calls:
            if text and self.speak_text_answers:
                speak_match = self._handle_text_answer(
                    utterance=utterance,
                    text=text,
                    lang=lang,
                    message=message,
                    tier=tier,
                )
                self._inflight_result = speak_match
                self._inflight_at = _time.monotonic()
                return speak_match
            if text:
                LOG.info(
                    "[tool-calling] %s: LLM answered in text but speak_text_answers=False: %s",
                    tier, text[:120],
                )
            else:
                LOG.debug("[tool-calling] %s: LLM declined (no tool, no text)", tier)
            return None

        # Pick the first tool call; v0.5 will support multi-step.
        call = tool_calls[0]
        entry = name_index.get(call.tool_name)
        if entry is None:
            LOG.warning(
                "[tool-calling] %s: LLM picked unknown tool %r (catalog has %d entries)",
                tier, call.tool_name, len(name_index),
            )
            return None

        LOG.info(
            "[tool-calling] %s: dispatching %s (matcher=%s) with args=%s",
            tier, f"{entry.skill_id}:{entry.intent_name}", entry.matcher, call.arguments,
        )
        match = make_match(entry, call.arguments, utterance)
        self._inflight_result = match
        self._inflight_at = _time.monotonic()
        self.gate.record(utterance, match)
        return match

    def _handle_text_answer(
        self,
        utterance: str,
        text: str,
        lang: str,
        message: Optional[Message],
        tier: str,
    ) -> IntentHandlerMatch:
        """Speak ``text`` and synthesize a sentinel match to claim the utterance.

        We emit ``speak`` on the bus *here* (side effect) and return a
        ``tool-calling:speak`` IntentHandlerMatch so ovos-core stops running
        further pipeline plugins. No skill listens for the match_type — it's
        just a sentinel to short-circuit the pipeline.
        """
        from ovos_tool_calling.dispatch import (
            SPEAK_SKILL_ID,
            make_speak_match,
        )

        LOG.info(
            "[tool-calling] %s: speaking LLM text answer (%d chars): %s",
            tier, len(text), text[:120],
        )

        speak_data = {
            "utterance": text,
            "expect_response": False,
            "meta": {"skill": SPEAK_SKILL_ID, "source": "tool-calling-pipeline"},
            "lang": lang,
        }
        # Forward from the originating utterance message when we have it, so
        # session/context (session_id, destination, etc.) propagates to ovos-audio.
        speak_msg = (
            message.forward("speak", speak_data)
            if message is not None
            else Message("speak", speak_data)
        )
        speak_msg.context["skill_id"] = SPEAK_SKILL_ID
        self.bus.emit(speak_msg)

        return make_speak_match(utterance=utterance, text=text, lang=lang)

    def match_high(
        self, utterances: List[str], lang: str, message: Message
    ) -> Optional[IntentHandlerMatch]:
        return self._try_llm_dispatch(utterances, tier="high", lang=lang, message=message)

    def match_medium(
        self, utterances: List[str], lang: str, message: Message
    ) -> Optional[IntentHandlerMatch]:
        return self._try_llm_dispatch(utterances, tier="medium", lang=lang, message=message)

    def match_low(
        self, utterances: List[str], lang: str, message: Message
    ) -> Optional[IntentHandlerMatch]:
        return self._try_llm_dispatch(utterances, tier="low", lang=lang, message=message)

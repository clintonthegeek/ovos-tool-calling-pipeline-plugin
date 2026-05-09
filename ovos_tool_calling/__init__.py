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
        with self._lock:
            self._vocab[vocab_id].add(phrase)

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
        (e.g. ['set', 'start', 'make']). Returns [] if unknown."""
        with self._lock:
            return sorted(self._vocab.get(vocab_id, ()))

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

    v0.1: builds a live registry of every skill intent registered on the bus.
    Still returns no match from match_*; subsequent versions will turn the
    registry into LLM tool schemas and dispatch tool calls.

    Bus inspection helpers (intended for development):
      - emit `tool-calling.registry.dump` to log the current registry summary
    """

    DUMP_EVENT = "tool-calling.registry.dump"

    def __init__(
        self,
        bus: Optional[Union[MessageBusClient, FakeBus]] = None,
        config: Optional[Dict] = None,
    ):
        super().__init__(bus=bus, config=config)
        self.registry = SkillRegistry(self.bus)
        self.bus.on(self.DUMP_EVENT, self._handle_dump)
        LOG.info(
            "ToolCallingPipeline loaded (v0.1: skill discovery) — "
            "dump with: bus.emit(Message('%s'))",
            self.DUMP_EVENT,
        )

    def _handle_dump(self, message: Message):
        LOG.info("[tool-calling] %s", self.registry.summary())

    def match_high(
        self, utterances: List[str], lang: str, message: Message
    ) -> Optional[IntentHandlerMatch]:
        return None

    def match_medium(
        self, utterances: List[str], lang: str, message: Message
    ) -> Optional[IntentHandlerMatch]:
        return None

    def match_low(
        self, utterances: List[str], lang: str, message: Message
    ) -> Optional[IntentHandlerMatch]:
        return None

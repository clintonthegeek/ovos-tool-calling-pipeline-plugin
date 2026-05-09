"""Convert OVOS skill intent registrations into OpenAI-style tool schemas.

The strategy differs by matcher:

- Adapt intents are keyword-driven; the skill almost always re-parses the raw
  utterance to extract semantic arguments. So we expose them as tools with a
  single ``utterance`` string parameter (passthrough), and we synthesize a
  human-readable description from the resolved trigger words.

- Padatious intents are sample-driven and carry first-class slot markers
  (``{location}``, ``{num}`` …). We extract those slots as real ``string``
  parameters and quote a few representative samples in the description so the
  LLM has concrete behavior cues.

Tool names must match ``[a-zA-Z0-9_-]{1,64}``. The original ``skill_id`` and
``intent_name`` are kept in a reverse-lookup table so the pipeline can later
synthesize the correct bus message to dispatch the matched tool back through
ovos-core.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from ovos_tool_calling import AdaptIntent, PadatiousIntent

SLOT_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
TOOL_NAME_INVALID_RE = re.compile(r"[^A-Za-z0-9_-]")
TOOL_NAME_MAXLEN = 64

# Separator used between skill_id and intent_name when building a tool name.
# Two underscores so it can be split unambiguously even when the skill_id has
# its own underscores. We also normalise '.', ':', '-' inside skill_id to '_'.
TOOL_NAME_SEP = "__"


@dataclass(frozen=True)
class ToolEntry:
    """One LLM-callable tool plus the metadata we need to dispatch it later."""

    name: str  # sanitized, OpenAI-safe tool name
    skill_id: str  # original OVOS skill id (e.g. "ovos-skill-alerts.openvoiceos")
    intent_name: str  # original short intent name (e.g. "CreateTimer")
    matcher: str  # "adapt" or "padatious"
    schema: Dict  # OpenAI tools[].function entry


def sanitize_tool_name(skill_id: str, intent_name: str) -> str:
    """Build a tool name that satisfies OpenAI's ``[A-Za-z0-9_-]{1,64}``.

    >>> sanitize_tool_name("ovos-skill-alerts.openvoiceos", "CreateTimer")
    'ovos-skill-alerts_openvoiceos__CreateTimer'
    """
    sk = TOOL_NAME_INVALID_RE.sub("_", skill_id)
    nm = TOOL_NAME_INVALID_RE.sub("_", intent_name)
    full = f"{sk}{TOOL_NAME_SEP}{nm}"
    if len(full) <= TOOL_NAME_MAXLEN:
        return full
    # Tail-truncate the skill id, keep the intent name intact when possible.
    keep = TOOL_NAME_MAXLEN - len(TOOL_NAME_SEP) - len(nm)
    if keep < 8:
        # Intent name itself too long; truncate that instead.
        return full[:TOOL_NAME_MAXLEN]
    return f"{sk[:keep]}{TOOL_NAME_SEP}{nm}"


def extract_slots(samples: List[str]) -> List[str]:
    """Return the unique ``{slot}`` names appearing in ``samples``, preserving
    first-seen order so the schema is deterministic across runs."""
    seen: Dict[str, None] = {}
    for s in samples:
        for m in SLOT_RE.findall(s):
            seen.setdefault(m, None)
    return list(seen)


def _format_adapt_description(
    intent: AdaptIntent, vocab_resolver: Callable[[str], List[str]]
) -> str:
    """Build a one-paragraph description from an Adapt intent's vocab constraints.

    ``vocab_resolver`` resolves a vocab id (e.g. ``'create'``) to its literal
    trigger phrases (e.g. ``['add', 'create', 'make', 'set', 'start']``).
    """

    def phrases(vocab_id: str) -> str:
        words = vocab_resolver(vocab_id) or [vocab_id]
        # Cap so we don't produce a 200-word description.
        if len(words) > 8:
            words = words[:8] + [f"... ({len(words) - 8} more)"]
        return ", ".join(words)

    parts = [
        f"OVOS skill intent '{intent.skill_id}:{intent.name}' (Adapt keyword matcher)."
    ]
    if intent.required:
        clauses = [f"[{phrases(v)}]" for v in intent.required]
        parts.append("Trigger requires all of: " + " AND ".join(clauses) + ".")
    if intent.at_least_one:
        clauses = [
            "[" + ", ".join(phrases(v) for v in group) + "]"
            for group in intent.at_least_one
        ]
        parts.append("Plus at least one phrase from each: " + ", ".join(clauses) + ".")
    if intent.optional:
        clauses = ", ".join(phrases(v) for v in intent.optional[:6])
        parts.append(f"May also reference: {clauses}.")
    parts.append(
        "Call this tool with the user's full utterance; the skill parses "
        "duration, time, and other arguments itself."
    )
    return " ".join(parts)


def adapt_intent_to_schema(
    intent: AdaptIntent, vocab_resolver: Callable[[str], List[str]]
) -> ToolEntry:
    """Convert one Adapt intent into a ToolEntry."""
    name = sanitize_tool_name(intent.skill_id, intent.name)
    description = _format_adapt_description(intent, vocab_resolver)
    schema = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "utterance": {
                        "type": "string",
                        "description": (
                            "The user's full original utterance, verbatim. "
                            "The skill will parse any arguments itself."
                        ),
                    }
                },
                "required": ["utterance"],
            },
        },
    }
    return ToolEntry(
        name=name,
        skill_id=intent.skill_id,
        intent_name=intent.name,
        matcher="adapt",
        schema=schema,
    )


def _pick_representative_samples(samples: List[str], n: int = 6) -> List[str]:
    """Pick up to ``n`` samples, biased toward those carrying slot markers."""
    with_slots = [s for s in samples if SLOT_RE.search(s)]
    without = [s for s in samples if not SLOT_RE.search(s)]
    # Prefer slot samples first, then fill with sloth-less ones.
    out = with_slots[:n]
    if len(out) < n:
        out += without[: n - len(out)]
    return out


def _format_padatious_description(intent: PadatiousIntent) -> str:
    examples = _pick_representative_samples(intent.samples)
    parts = [
        f"OVOS skill intent '{intent.skill_id}:{intent.name}' (Padatious fuzzy matcher)."
    ]
    if examples:
        quoted = "; ".join(f"'{s}'" for s in examples)
        parts.append(f"Trained on samples like: {quoted}.")
    parts.append(
        "Call this tool when the user's intent matches the same idea, "
        "filling slot parameters extracted from the request."
    )
    return " ".join(parts)


def padatious_intent_to_schema(intent: PadatiousIntent) -> ToolEntry:
    """Convert one Padatious intent into a ToolEntry."""
    name = sanitize_tool_name(intent.skill_id, intent.name)
    description = _format_padatious_description(intent)
    slot_names = extract_slots(intent.samples)
    properties = {
        slot: {
            "type": "string",
            "description": f"Value for the {{{slot}}} slot in the user's request.",
        }
        for slot in slot_names
    }
    parameters: Dict = {"type": "object", "properties": properties, "required": []}
    schema = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }
    return ToolEntry(
        name=name,
        skill_id=intent.skill_id,
        intent_name=intent.name,
        matcher="padatious",
        schema=schema,
    )


def build_tool_catalog(
    skills: Dict[str, "object"],
    vocab_resolver: Callable[[str], List[str]],
) -> Tuple[List[Dict], Dict[str, ToolEntry]]:
    """Walk a registry snapshot and return ``(tools, name_index)``.

    ``tools`` is the list to hand to an OpenAI-compatible chat-completion call.
    ``name_index`` maps the sanitized tool name back to the ToolEntry, so when
    the LLM picks tool ``X``, the pipeline can look up the matcher and the
    original ``skill_id:intent_name`` to synthesize a dispatch.
    """
    tools: List[Dict] = []
    index: Dict[str, ToolEntry] = {}
    for skill_id in sorted(skills):
        rec = skills[skill_id]
        for intent in rec.adapt_intents.values():
            entry = adapt_intent_to_schema(intent, vocab_resolver)
            tools.append(entry.schema)
            index[entry.name] = entry
        for intent in rec.padatious_intents.values():
            entry = padatious_intent_to_schema(intent)
            tools.append(entry.schema)
            index[entry.name] = entry
    return tools, index

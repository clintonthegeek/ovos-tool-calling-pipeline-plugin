# Roadmap

The development trajectory for `ovos-tool-calling-pipeline-plugin`. Each milestone names what shipped, why, and what's still open.

Update this file as milestones complete. The TODO file (`docs/TODO.md`) holds the active short-term work; this one is the long-term arc.

---

## Mission

Replace OVOS's pre-LLM, rule-based intent dispatch with an LLM orchestrator that exposes existing OVOS skills as function-call tools. Skills change zero. Pipeline plugin sits inside `ovos-core` and synthesizes the same dispatch messages Adapt or Padatious would have produced.

This is opposed to the existing `ovos-persona-pipeline-plugin`, which uses an LLM as a passthrough text-in/text-out fallback after rule-based matchers fail. Here the LLM is the *router*, and rule-based matchers either gate the LLM (when placed before us in the pipeline) or remain as fallbacks (when placed after).

---

## Status

| Version | Status | Description |
|---|---|---|
| 0.1 | ✅ shipped | Skill discovery via bus listening |
| 0.2 | ✅ shipped | Tool schema generation (Adapt + Padatious) |
| 0.3 | ✅ shipped | Tool dispatch via LLM with synthesized IntentHandlerMatch |
| 0.4 | ✅ shipped | Latency gate + dispatch cache |
| 0.5 | 🟡 next | Speak plain text answers when no tool fits |
| 0.6 | 🟡 planned | Multi-tool agent loop (sequential calls) |
| 0.7 | 🟡 planned | Predictive gate (peek at Adapt/Padatious before LLM call) |
| 0.8 | 🟡 planned | Conversational state respect (defer to converse pipeline) |
| 1.0 | 🟡 planned | Stable API; PyPI release |

---

## v0.1 — Skill discovery (shipped)

**What**: subscribe to `register_intent`, `register_vocab`, `padatious:register_intent`, `detach_intent`, `detach_skill` on the bus. Build a `{skill_id: SkillRecord}` registry.

**Why**: Need to know what skills exist before we can expose them as tools. Listening at startup is the same pattern Adapt and Padatious use themselves.

**Limits / known issues**: 
- Plugin loads early in ovos-core startup, before skills load. There's a 5-30s window where the registry is incomplete. Snapshot queries (`intent.service.adapt.manifest.get` etc.) could backfill but aren't currently used.

**Code**: `ovos_tool_calling.SkillRegistry` in `__init__.py`.

---

## v0.2 — Tool schema generation (shipped)

**What**: walk the registry and emit OpenAI-style tool schemas:

- Adapt → tool with `utterance: string` passthrough param + vocab-resolved description.
- Padatious → tool with `{slot}` parameters extracted from sample markers + sample-utterance description.
- Reverse-lookup index `name → ToolEntry(skill_id, intent_name, matcher)` so we can dispatch when the LLM picks a tool.

**Why**: The LLM needs a tool catalog to choose from. Different schema shapes per matcher because Adapt skills self-parse arguments; Padatious skills receive structured slot dicts.

**Limits**: 
- Adapt vocab IDs case-mismatch between `register_vocab` (Title Case from filename) and `register_intent` requires (lowercase from author code). Resolved by lowercasing both sides.
- Tool name length capped at 64 chars (OpenAI limit). Pathological `skill_id` lengths get truncated.
- Description text grows linearly with vocab size; for skills with 100+-line `.voc` files we cap at 8 phrases per vocab.

**Code**: `ovos_tool_calling/schemas.py`. Pure functions — testable without a bus.

---

## v0.3 — Tool dispatch (shipped)

**What**: when `enabled` in config, every utterance reaching one of our `match_*` methods is forwarded to the configured LLM along with the live tool catalog. If the LLM picks a tool, we synthesize an `IntentHandlerMatch` whose `match_type` is the same `<skill_id>:<intent_name>` Adapt or Padatious would have emitted, and `match_data` is shaped per matcher.

Config sources:
- `persona: "<name>"` reuses credentials from `~/.config/ovos_persona/<persona>.json`.
- Inline `api_url`, `key`, `model`, `system_prompt`, `max_tokens`, `temperature` override individual fields.

**Why**: Closes the loop. Skills receive what looks like a normal Adapt or Padatious dispatch and run unchanged.

**Limits**:
- Reasoning models (deepseek-v4-pro, glm-5p1) emit output as `reasoning_content` not `content`; OVOS plugin reads `content` only. Document `INSTALL_NOTES.md` lists which models work.
- LLM round-trip blocks the intent service for ~1-2s per call.
- No tool-call validation — if the LLM picks a tool name we don't know, we log and return None.
- No multi-tool support — we take only the first tool call.
- Plain-text answers from the LLM are logged but ignored (v0.5).

**Code**: `ovos_tool_calling/llm.py` (HTTP), `ovos_tool_calling/dispatch.py` (match synthesis).

---

## v0.4 — Latency gate (shipped)

**What**: `ovos_tool_calling/gate.py` runs *before* any LLM work and decides:

- `skip` — empty utterance / below `min_words` threshold / matches a `blocklist_patterns` regex
- `cached` — LRU hit, return prior IntentHandlerMatch instantly  
- `proceed` — call the LLM, then `Gate.record(...)` the result for caching

Config:
- `min_words: 2`
- `cache_size: 32`
- `blocklist_patterns: []`

Single-flight memo with 1s TTL absorbs `ConfidenceMatcherPipeline.match()`'s tier fall-through without persisting across queries.

**Why**: Make repeated commands free, drop noise (single-token mistranscriptions of the wake word) without a round-trip.

**Limits**:
- Cache by exact utterance string — no semantic equivalence ("set a five minute timer" vs "set a 5 minute timer" are different keys).
- No predictive gate yet — we don't peek at what Adapt/Padatious would have done; that's v0.7.

**Code**: `ovos_tool_calling/gate.py`. Wired in at the top of `_try_llm_dispatch`.

---

## v0.5 — Speak plain answers (next)

**What**: when the LLM responds with `content` instead of `tool_calls`, dispatch a `speak` event to `ovos-audio` and return an IntentHandlerMatch that flags the utterance as handled.

**Why**: Today, plain-text answers from the LLM are logged and ignored, so the rest of the pipeline runs (typically reaching `ovos-persona-pipeline-plugin-low`, which then makes a *second* LLM call). v0.5 closes the loop by letting our orchestrator handle the no-tool case directly.

**Design notes**:
- Need to choose a sensible `match_type` for the dispatch; `persona:query` is one option (keeps everything compatible with existing fallback handlers), but a custom one (`tool-calling:speak`) is cleaner.
- Need to decide whether to claim a `skill_id`. The persona pipeline uses `persona.openvoiceos`. For now, mimic that.
- This bypasses `match_data["utterance"]` re-parsing — the LLM's `content` is the answer text. We `speak()` it and call it done.

**Estimated**: ~50 lines.

---

## v0.6 — Multi-tool agent loop (planned)

**What**: when the LLM returns multiple `tool_calls`, dispatch them sequentially. After each tool's response, feed back to the LLM via a follow-up message round.

**Why**: "set a five minute timer and tell me a joke" → two tool calls. Today we take only the first.

**Design notes**:
- Iterate up to a configurable `max_tool_iterations` (default 3).
- After dispatching tool N, capture the speak/response from the bus, append as an assistant message, and ask the LLM if more tools are needed.
- Risk: skill responses arrive asynchronously via bus events; we'd need to listen for the speak event after dispatch.
- Probably involves restructuring the dispatch flow to be agent-like (loop until LLM declines further tools).

**Estimated**: bigger than the others; the agent loop is non-trivial. ~200 lines.

---

## v0.7 — Predictive gate (planned)

**What**: peek at what Padatious and Adapt would say *before* calling the LLM. If either has a high-confidence match, abstain (return None) and let the rule-based matchers handle it.

**Why**: this is what unlocks safely placing the plugin at `-high` priority — pure-orchestrator mode where the LLM gets first crack but doesn't waste round-trips on trivial commands.

**Design notes**:
- Querying the matchers via bus (`intent.service.padatious.get`) is async and adds bus round-trips.
- Could instead embed a copy of the Padatious classifier locally — train on the same registered intents — and run inference in-process. This is more code but faster.
- Or: use a simpler heuristic — shorter utterances are more likely to be keyword commands; pass them to Adapt-style heuristic checks.
- May be unnecessary if users are content with pipeline placement (us at `-low`).

**Estimated**: depends on approach; ~100 lines for bus-query, ~500 for embedded classifier.

---

## v0.8 — Conversational state (planned)

**What**: respect when a skill is mid-conversation (via OVOS's `converse` pipeline) and abstain.

**Why**: the converse pipeline runs early in the pipeline list specifically so an active skill (e.g. alerts skill asking "which alarm to cancel?") gets first crack at the user's response. We should not preempt this.

**Design notes**:
- The `converse` pipeline already runs before us, so this might be a no-op as long as we're at `-low`.
- For `-high` placement, we'd need to query session state to see if a skill has converse claims.

**Estimated**: small, mostly testing.

---

## v1.0 — Stable API + PyPI release (planned)

**What**: a public release suitable for community use. Stable config schema, semantic versioning, tagged release, PyPI upload.

**Prereqs**:
- Test coverage (currently zero).
- Document supported model providers (OpenAI, Anthropic via OpenAI-compatible proxy, Fireworks, Ollama for local).
- Migration guide from `ovos-persona-pipeline-plugin`.
- A demo video / blog post.

---

## Out of scope (probably forever)

- **Skill *generation*** from natural language. We use existing skills; we don't manufacture them.
- **Multi-LLM ensemble**. Pick one model and tune it.
- **Direct PyPI competition with `ovos-persona`**. Different architecture, different goals; coexistence is fine.
- **A custom GUI**. The user already has `ovos-gui` (rarely used) and `mycroft.conf`; we don't need another config surface.

---

## Design principles

When in doubt, fall back to these:

1. **Skills don't change.** Zero modifications required for skills to be callable as tools. The dispatch contract is sacred.
2. **Bus is the source of truth.** Don't maintain duplicate state. Listen, snapshot, dispatch, but don't shadow OVOS state.
3. **Gracefully degrade.** Any LLM/network failure should fall back to "no match" so the rest of the pipeline runs.
4. **Plugin disabled by default.** Installing the plugin must not change behavior until config opts in.
5. **Pure functions where possible.** Schema generation, gate decisions, dispatch synthesis — all testable without a bus.
6. **Observability is a feature.** Every gate decision and every dispatch is logged with a reason. No silent magic.

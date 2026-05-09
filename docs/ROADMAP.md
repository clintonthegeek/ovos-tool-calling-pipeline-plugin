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
| 0.5 | ✅ shipped | Speak plain text answers when no tool fits |
| 0.6 | ✅ shipped | Multi-tool agent loop (background thread, stop coord) |
| 0.7 | ⏸ deferred | Predictive gate — original `-high` motivation gone (we stay at `-low`) |
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
- Plain-text answers from the LLM are logged but ignored. (Resolved in v0.5.)

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

## v0.5 — Speak plain answers (shipped)

**What**: when the LLM responds with `content` instead of `tool_calls`, the pipeline emits a `speak` bus event with the text and returns a sentinel `IntentHandlerMatch(match_type="tool-calling:speak", skill_id="tool-calling.openvoiceos")`. The match flags the utterance as handled so no further pipeline plugins (notably `ovos-persona-pipeline-plugin-low`) run on the same utterance.

**Why**: Before v0.5, plain-text answers were logged and ignored, so the persona-low pipeline made a *second* LLM round-trip on the same utterance. Now our orchestrator closes the loop directly.

**Implementation**:
- `dispatch.make_speak_match(utterance, text, lang)` synthesizes the sentinel match (pure function).
- `__init__._handle_text_answer` performs the side effect — `bus.emit(message.forward("speak", {...}))` so session/destination context propagates to ovos-audio — then returns the sentinel match.
- `match_high/medium/low` thread `lang` and the originating `message` into `_try_llm_dispatch`.
- Config flag `speak_text_answers: true` (default) gates the behavior; set False to fall through to downstream pipeline plugins.

**Design choices**:
- `match_type` is `tool-calling:speak`, a custom event name. No skill listens for it; ovos-core's dispatch is a harmless no-op since the user-facing speech already happened on the bus emit.
- `skill_id` is `tool-calling.openvoiceos`, mirroring how `ovos-persona` claims `persona.openvoiceos`.
- We deliberately do **not** call `gate.record(...)` on the text path. Cached tool dispatches are deterministic; cached text answers risk replaying stale answers as the LRU ages.
- The `speak` message uses `message.forward("speak", data)` when the originating message is available, so session_id and destination propagate correctly.

**Limits**:
- The text answer's quality depends entirely on the LLM's `system_prompt` for this pipeline. Long, verbose answers happen if the persona's prompt invites them.
- Repeated identical questions pay the LLM round-trip every time (no cache).

**Code**: `ovos_tool_calling/dispatch.py` (`make_speak_match`), `ovos_tool_calling/__init__.py` (`_handle_text_answer`).

---

## v0.6 — Multi-tool agent loop (shipped)

**What**: when the LLM returns one or more `tool_calls`, dispatch them sequentially in a background thread, capture each skill's speak output, feed the output back to the LLM, and iterate until the LLM stops calling tools or `max_tool_iterations` (default 3) is reached. Final assistant text (if any) is spoken via the v0.5 path.

**Why**: "set a five minute timer and tell me a joke" → two tool calls. Today (v0.5) we take only the first. v0.6 dispatches both, and additionally enables flows like "what's the weather, and if it's cold set a heater alarm" where the second decision depends on the first tool's result.

**Design**:

- **Background-thread the loop.** The intent service's `handle_utterance` iterates pipelines synchronously on a single thread. Blocking it for ~24s (3 iterations × 8s tool timeout) would queue every other utterance — including stop, snooze, volume — behind us. Instead: do the *first* LLM call synchronously in `_try_llm_dispatch` (~1-2s, same as v0.5), and if it returns tool_calls, hand off to `agent.AgentLoop.start(...)` which runs the loop in its own thread. We return the v0.5 sentinel `IntentHandlerMatch` immediately, freeing the intent service.
- **Stop coordination.** The agent thread subscribes to `mycroft.stop` and `recognizer_loop:utterance` for the duration of its run. Either firing aborts the loop before the next iteration. Skills currently dispatched aren't killed by us — the stop pipeline already handles that. We just stop dispatching new ones.
- **At-most-one loop.** A module-level lock guards the slot. If a fresh loop starts (via a new utterance) while a previous one is still iterating, the previous one is signaled to abort and the new one takes over.
- **Response capture.** For each dispatched tool, listen for `speak` events scoped by the dispatched skill's `skill_id`, plus `mycroft.skill.handler.complete` and `.error`. Wait up to `tool_timeout_seconds` (default 8s). Captured speaks are concatenated and fed back to the LLM as `{role: "tool", tool_call_id: ..., content: "ok\n<speak text>"}` (or `"error: <msg>"` on failure / timeout).
- **Dispatch via bus emit.** Since we don't return another `IntentHandlerMatch` for the second/third tool (we already returned the sentinel), the agent thread emits the `<skill_id>:<intent_name>` event itself with the same `match_data` shape `dispatch.make_match` produces. New helper: `dispatch.build_dispatch_message(...)`.
- **Final speak.** When the LLM eventually returns text instead of more tool_calls (or we hit max iterations), the agent speaks that text via `bus.emit(Message("speak", {...}))` — same as v0.5's text path.

**Configuration** (with defaults):

```jsonc
{
  "ovos-tool-calling-pipeline-plugin": {
    "enable_agent_loop": true,
    "max_tool_iterations": 3,
    "tool_timeout_seconds": 8.0
  }
}
```

`enable_agent_loop: false` reverts to v0.5 behavior (first tool only).

**Module additions**:

- `agent.py` (new) — `AgentLoop` class: `start()`, `_run()` (worker), `_dispatch_one()`, `_should_abort()`, `cancel()`. Background thread.
- `llm.py` — `call_chat` refactored to accept a full message list (system + user + tool + assistant turns) instead of just a single utterance. `LLMToolCall` gains `tool_call_id`.
- `dispatch.py` — `build_dispatch_message(entry, args, utterance, lang, original_message)` returns a `Message` for the agent to emit.

**Limits**:

- Per-tool 8s wait may truncate slow skills (Wikipedia search). User-tunable via config.
- `mycroft.skill.handler.complete` is reliable for `@intent_handler`-decorated handlers but custom event handlers don't emit it; those tools will hit the timeout and be reported as "timed out" to the LLM.
- The cache (`gate.py`) is bypassed during agent loops — caching multi-tool sequences is volatile and not worth the complexity.

**Verified end-to-end**:
- Single-tool dispatch ("set a thirteen minute timer") — CreateTimer dispatched, handler.complete observed, LLM follow-up summary suppressed because skill spoke. Loop exited cleanly.
- Multi-tool sequential ("set a 14 minute timer and tell me a joke") — both skills dispatched (timer + joke), both spoke, redundant LLM summary suppressed.
- Stop interruption — emitted `mycroft.stop` mid-loop during a triple-wiki query. Loop logged `mycroft.stop received -> abort loop` and `aborted before iteration 2`; remaining wiki queries (Venus, Earth) were not dispatched.

---

## v0.7 — Predictive gate (motivation revisited)

**Status**: motivation weakened. The original rationale was "this is what unlocks safely placing the plugin at `-high` priority" — but the user has decided to keep us at `-low` permanently as a fallback pipeline. At `-low`, rule-based matchers (Adapt, Padatious, m2v, OCP) have already had first crack at the utterance. A predictive gate would be re-asking "could a rule-based matcher handle this?" when the answer is already "no, they tried and didn't claim it." Mostly redundant.

**Possible repurposes** (if we keep this milestone at all):
- Skip the LLM call for utterances that look like obvious keyword commands the rule-based matchers happened to miss due to vocab gaps (cost-saving, not correctness).
- Heuristics-only: short utterances + presence of an Adapt-required vocab → abstain.

**Decision**: defer. Re-open if we observe a meaningful slice of `-low` traffic where the LLM is dispatching utterances a rule-based matcher should have handled (not just things rule-based matchers can't reach).

**Design notes** (for reference if reopened):
- Querying the matchers via bus (`intent.service.padatious.get`) is async and adds bus round-trips.
- Could instead embed a copy of the Padatious classifier locally — train on the same registered intents — and run inference in-process. More code but faster.
- Or: use a simpler heuristic — shorter utterances are more likely to be keyword commands; pass them to Adapt-style heuristic checks.

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

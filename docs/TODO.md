# Active TODO

Short-term task list, the working scratch. AI sessions and humans should keep this current: add new items as they arise, mark done when complete, prune stale items aggressively.

For the long-term arc, see `ROADMAP.md`.

## Format

- `[ ]` open
- `[x]` done (leave a couple of recent ones for context, prune older)
- `[~]` in progress (don't leave more than one or two)
- `[?]` blocked / needs decision

---

## Now

_(no active items)_

## Soon

- [ ] Revisit v0.7 (predictive gate) motivation. Its stated rationale was "unlocks safely placing us at `-high`", but the user has confirmed we stay at `-low` permanently as a fallback pipeline. A predictive gate at `-low` is mostly redundant (rule-based matchers already had first crack). Either deprioritise, repurpose (e.g. as a cost-saving measure that aborts an obviously-trivial utterance before the LLM call), or close.

## Open questions / [?] blocked

- [?] How should we handle the `tutubo`/`ovos-classifiers` failures for news/somafm/youtube-music skills? They fail to load entirely. Skipping them means no LLM tool for those skills. Logging an exclusion in the catalog would be cleaner than silently missing tools.
- [?] Should we cache per-language? Currently the cache is language-blind. Probably fine until we support a non-English deployment.
- [?] Is `m2v` worth keeping at all for users of this plugin? The over-eager false-matches are a known issue. Document or override?

## Recently done (keep last ~10)

- [x] **Pipeline placement decision** — confirmed we stay at `-low` (fallback mode) permanently. v0.7's predictive-gate motivation needs revisiting.
- [x] **OCP dispatch shape investigation** — concluded "no shape needed". OCP registers via a separate bus channel (`ovos.common_play.announce` / `register_keyword`) and dispatches its own `ocp:*` matches; our SkillRegistry doesn't subscribe to those events, so OCP intents never appear in our tool catalog. With OCP at `-high` and us at `-low`, OCP utterances never reach us. LLM-orchestrated playback would be a separate feature, not a bugfix. Documented in `docs/INSTALL_NOTES.md`.
- [x] **Test coverage expansion** — 99 tests total (was 41). Added: `tests/test_llm.py` HTTP-mocked `call_chat` coverage (12 new), `tests/test_agent.py` FakeBus + threading coverage of `_DispatchOutcome`, `_dispatch_one`, `AgentLoop` slot management, and `_LoopRun` termination logic (21 tests), `tests/test_pipeline.py` covering `_try_llm_dispatch` branches (16 tests). `tests/test_gate.py` extended for digit↔word normalization (8 new tests).
- [x] **Cache key digit↔word normalization** — `Gate._normalize` now coerces English number-words 0..99 (incl. compound tens like "twenty five" and hyphenated "twenty-five") to digits, so "set a 5 minute timer" and "set a five minute timer" share a cache entry.
- [x] **`time` import cleanup** — removed inline `import time as _time` aliasing in `_try_llm_dispatch`; now using a top-level `import time`.
- [x] **Initial test suite** (41 tests, all pass): `schemas.py`, `gate.py`, `dispatch.py`, `llm.py` helpers. pytest configured via pyproject. Test extra installable via `pip install -e .[test]`.
- [x] **v0.6 multi-tool agent loop** committed `f62bd05`. Background-thread loop dispatches sequential tool_calls via bus emit, captures speak text via `mycroft.skill.handler.complete` + `speak` listeners, feeds results back to LLM, iterates up to 3. Aborts on `mycroft.stop` or new utterance. Suppresses redundant LLM summary if any skill already spoke. Config: `enable_agent_loop: true`, `max_tool_iterations: 3`, `tool_timeout_seconds: 8.0`.
- [x] **v0.5 speak text answers** committed `f1642cc`. LLM-text path emits `speak` and returns a `tool-calling:speak` sentinel match so `ovos-persona-low` doesn't run a second LLM call. Config flag `speak_text_answers: true`.
- [x] **v0.4 latency gate** committed `b73e202`. Min-words filter, blocklist, LRU, in-flight memo TTL.
- [x] **v0.3 tool dispatch** committed `afd62cf`. End-to-end working: voice → STT → pipeline → LLM → dispatch → skill → TTS.
- [x] **v0.2 tool schema generation** committed `cde5b37`. 81 tools generated for the user's install (36 adapt + 45 padatious).
- [x] **v0.1 skill discovery** committed `7373ae3`. Bus listener for register_intent / register_vocab / padatious:register_intent.
- [x] **Initial skeleton** committed `269adca`. Stub plugin verifying entry-point discovery and pipeline registration.
- [x] **Comprehensive docs** committed (this commit). Architecture, intent matchers, pipeline protocol, install notes, dev loop, roadmap, todo, repo CLAUDE.md, user-env CLAUDE.md.

## Stretch / nice-to-have

- [ ] Web inspector: a small HTTP endpoint on the bus that returns the live registry + catalog as JSON. Easier than emitting bus dump events.
- [ ] Tool-call tracing: log a structured event per dispatch with utterance, tool, args, latency. Goes into a JSONL file for offline analysis.
- [ ] PyPI release prep (after v1.0 milestone).
- [ ] Pluggable LLM clients: today we hardcode an OpenAI-compatible client. Could abstract `LLMClient` so Anthropic/Google/Ollama have first-class support without depending on OpenAI-shape proxies.

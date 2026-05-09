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

- [ ] **v0.5: speak plain text answers when LLM doesn't pick a tool.** Choose `match_type` (custom `tool-calling:speak` recommended) and skill_id. Emit the speak event. Return an IntentHandlerMatch so the rest of the pipeline doesn't run again. Estimated ~50 lines.

## Soon (after v0.5)

- [ ] Decide whether to bump plugin to `*-high` in the user's `mycroft.conf`. Pre-req: collect a few days of `-low` data on what queries the LLM is currently catching, to predict cost/quality if we move to `-high`.
- [ ] Add tests. None exist. Start with `schemas.py` (pure functions, easiest target).
- [ ] Strip the unused `time` import re-aliasing from `_try_llm_dispatch` — currently uses `import time as _time` inline, would be cleaner at the top of the file.
- [ ] Verify the dispatch shape for OCP (Open Common Play) intents. They're a different category and we haven't tested them. Probably needs custom `match_data` shape.
- [ ] Audit the cache key — `_normalize` lowercases and collapses whitespace, but "set a 5 minute timer" vs "set a five minute timer" still hit different keys. Numeric normalization could improve hit rate.

## Open questions / [?] blocked

- [?] How should we handle the `tutubo`/`ovos-classifiers` failures for news/somafm/youtube-music skills? They fail to load entirely. Skipping them means no LLM tool for those skills. Logging an exclusion in the catalog would be cleaner than silently missing tools.
- [?] Should we cache per-language? Currently the cache is language-blind. Probably fine until we support a non-English deployment.
- [?] Is `m2v` worth keeping at all for users of this plugin? The over-eager false-matches are a known issue. Document or override?

## Recently done (keep last ~10)

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

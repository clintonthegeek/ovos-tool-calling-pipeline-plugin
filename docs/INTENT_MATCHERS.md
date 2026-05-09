# Adapt vs Padatious: How OVOS's Two Intent Matchers Actually Work

OVOS skills route utterances to handlers using two coexisting matchers, both inherited from Mycroft. Knowing them in detail matters for this plugin because we need to expose every intent (regardless of which matcher claims it) as a callable LLM tool, and we need to synthesize the dispatch shape the matcher would have produced.

This is the ground truth as of OVOS 2026 (verified against ovos-core 2.1.x, ovos-adapt, ovos-padatious in the user's venv).

---

## Adapt

### Model

A keyword-tagger plus an intent-resolver. **No ML.** Build a trie of known phrases, find them in the utterance, then check which intent's required-keyword constraints are satisfied. Trace it to 2014 Mycroft.

### Author-side artifacts

Two things per intent:

1. **`*.voc` files** — one phrase per line, file basename = vocab id.

   ```
   # ovos_skill_alerts/locale/en-us/vocab/create.voc
   set
   start
   make
   add
   schedule
   ```

   Every line registers as an `Entity` in the engine's trie, sharing the entity_type `create`. Phrases are case-insensitive at runtime.

2. **`IntentBuilder` declarations** — declarative constraints over vocab ids:

   ```python
   IntentBuilder("CreateTimer")
       .require("create")        # at least one phrase from create.voc must be present
       .require("timer")         # AND at least one from timer.voc
       .one_of("alarm", "timer", "reminder")  # exactly one of these vocabs (any phrase)
       .optionally("question")   # extracts but doesn't gate matching
       .exclude("stop")          # if any phrase from stop.voc is present, no match
   ```

That's it. No training, no examples, no embeddings. The skill author hand-picks which vocab files matter.

### Algorithm at runtime

```
utterance: "set a 2 minute timer"

Step 1: tokenize -> ["set", "a", "2", "minute", "timer"]

Step 2: tag against the entity trie
  "set"   -> matches vocab "create"  (confidence 1.0)
  "timer" -> matches vocab "timer"   (confidence 1.0)
  "2"     -> matches regex entity for {duration} if registered
  Others: untagged

Step 3: for every IntentBuilder ever registered, validate constraints
  CreateTimer requires {create, timer} -> both tagged -> CANDIDATE
  CreateAlarm requires {create, alarm} -> alarm not tagged -> reject
  ...

Step 4: score candidates
  confidence = sum_of_entity_confidences * coverage_fraction
  CreateTimer: 2 tags / 5 tokens covered = ~0.4

Step 5: return the highest-scoring candidate (sorted by confidence)
```

### Match data emitted to the skill

When `CreateTimer` wins, Adapt emits a bus message named `<skill_id>:CreateTimer` whose `data` is the parse result:

```python
{
  "intent_type": "ovos-skill-alerts.openvoiceos:CreateTimer",
  "utterance": "set a 2 minute timer",
  "confidence": 0.4,
  "target": None,
  "__tags__": [
    {"key": "create", "match": "set", "start_token": 0, ...},
    {"key": "timer", "match": "timer", "start_token": 4, ...}
  ],
  "ovos_skill_alerts_openvoiceoscreate": "set",
  "ovos_skill_alerts_openvoiceostimer": "timer"
}
```

The semantic argument (the `2 minute` duration) is **not** extracted by Adapt — the skill's handler reaches for `message.data["utterance"]` and calls its own duration parser. This is the part most Adapt skills do.

### Strengths

- Deterministic. Matches or it doesn't, no probability surface.
- Instant. No training, no embeddings.
- Easy to debug. The trigger words are right there in `.voc` files.
- Friendly to non-English: swap the `.voc` files, intents still work.

### Limits

- No paraphrase tolerance. "begin a two-minute countdown" → won't match `CreateTimer` because "begin" and "countdown" aren't in the relevant vocab files.
- No semantic argument extraction.
- Vocab collisions. The word "set" appears across many skills' `create.voc` files; the highest-coverage intent wins, sometimes wrongly.
- No compositional understanding. "set a timer and turn down the volume" gets one intent only.

### What this plugin does with Adapt intents

Tool name: `<sanitized_skill_id>__<IntentName>`. Description: the resolved trigger words from `.voc` files. Parameters: a single `utterance` string passthrough — the skill will re-parse the raw utterance for arguments.

---

## Padatious

### Model

**One small neural network per intent**, plus one extra pair of edge classifiers per `{slot}` for argument extraction. Built on FANN (Fast Artificial Neural Network), a 1990s C library. Per-intent: a 3-layer fully-connected net, ~10 hidden units, sigmoid stepwise activations. Inference is `net.run(vectorize(utterance))[0]`; trains to a `bit_fail_limit` of 0.1.

Where Adapt is "do these specific words appear?", Padatious is "does this look like the things I was trained on?"

### Author-side artifacts

1. **`*.intent` files** — one sample per line. Filename (minus `.intent`) is the intent name.

   ```
   # ovos_skill_weather/locale/en-us/intents/N_days_forecast.intent
   What is the 2 day forecast
   What is the 2 day forecast in {location}
   What is the {num} day forecast in {location}
   give me the {num} day forecast
   ```

   Each line is a positive training sample. Tokens in `{curlies}` are slot markers — placeholders for runtime entity extraction.

2. **Bracket expansion** for compact authoring:

   ```
   (what | which) (alarm | timer) did i miss
   ```

   Expands at load time to 4 enumerated samples (`bracket_expansion.py` in ovos_padatious).

3. **`*.entity` files** (optional) — vocab files for slot type constraints (e.g. `location.entity` of city names). When extracting a `{location}` slot, Padatious can validate against this list. Most skills omit them and let the runtime parser handle slot values directly.

### Training procedure (per intent)

For each `.intent` file:

```
INPUTS                                              OUTPUT (target confidence)
"what is the 2 day forecast"                  →     1.0   (positive sample)
"what is the {num} day forecast in {location}" →    1.0   (positive sample)
":null: :null: what is the 2 day forecast"    →     0.6   (polluted: leading noise)
"what"                                        →     0.04  (single-word, weighted by len³)
"forecast"                                    →     0.51  (single-word, weighted by len³)
"set a timer"                                 →     0.0   (negative, from another intent)
"what time is it"                             →     0.0   (negative, from another intent)
```

The `pollute()` step injects `:null:` tokens at the front and back of positive samples to teach noise tolerance. The `weight()` step trains single-word inputs with a target proportional to word-length cubed (longer words = stronger evidence). Negative samples are taken from every *other* intent's positive samples so the net learns to discriminate.

This happens **at every ovos-core startup** (or first-run; subsequent runs cached). Total training time for ~60 intents is 5-30s.

### Slot extraction

For each `{slot}` token in any sample, Padatious creates a `PosIntent` with two `EntityEdge` classifiers:

- **Left edge net**: trained on the token immediately before the slot (and surrounding context). Predicts "does a slot start here?"
- **Right edge net**: trained on the token immediately after. Predicts "does a slot end here?"

At match time, for each candidate intent that scored above threshold:

```
utterance: "what is the 4 day forecast in tokyo"

1. Run intent classifier → 0.92 confidence for N_days_forecast
2. For slot {location}:
   - Score every position with left edge net  → high score after "in"
   - Score every position with right edge net → high score after "tokyo"
   - Find compatible (l_pos, r_pos) pairs not crossing other slots
   - Extracted: ["tokyo"]
3. For slot {num}:
   - Left edge after "the" → high
   - Right edge before "day" → high
   - Extracted: ["4"]

Final match data:
  {name: "N_days_forecast", confidence: 0.92,
   matches: {"location": "tokyo", "num": "4"}}
```

### Match data emitted to the skill

```python
{
  "name": "ovos-skill-weather.openvoiceos:N_days_forecast",
  "utterance": "what is the 4 day forecast in tokyo",
  "conf": 0.92,
  "location": "tokyo",   # extracted slot
  "num": "4"             # extracted slot
}
```

The skill handler reads `message.data["location"]` and `message.data["num"]` directly. **Slots are first-class arguments**, unlike Adapt's "tags" which are mostly for which trigger words fired.

### Confidence tiers

Pipeline placement matters:

- `ovos-padatious-pipeline-plugin-high` returns matches with confidence ≥ ~0.85.
- `-medium` returns ≥ 0.6.
- `-low` accepts anything (rarely placed; would be too noisy).

### Strengths

- Paraphrase tolerance: "hey, what's the forecast going to look like" matches "what is the forecast" even though the literal sample wasn't seen.
- Real slot extraction with first-class arguments.
- Per-intent isolation: adding a new intent doesn't degrade other intents (separate net).

### Limits

- Training pass at startup, ~5-30s for 60+ intents.
- Sample-quality dependent. If your samples brute-force enumerate ("did i miss a alarm" × 100 variations) without bracket expansion, the intent is brittle around phrasings the author didn't anticipate.
- No semantic understanding. Still bag-of-tokens. "Tell me about the day on which I was born" doesn't generalize from "what's the date".
- Slot edge nets need surrounding-context examples. If all your `{location}` samples have it after "in", "give me Tokyo's forecast" won't extract it.
- FANN is a 1990s C library. Maintenance liability.

### What this plugin does with Padatious intents

Tool name: `<sanitized_skill_id>__<intent_name>`. Description: a few representative samples from the `.intent` file, biased toward samples carrying slot markers. Parameters: every `{slot}` extracted from samples becomes a `string` parameter in the schema. The LLM fills them, and we pass the dict directly as `match_data` to the skill — same shape Padatious would have produced.

---

## Pipeline ordering: which one runs first?

In a typical OVOS install, pipelines run in this rough priority:

```
stop-high → converse → ocp-high → persona-high → padatious-high
  → adapt-high → fallback-high → adapt-medium → fallback-medium
  → tool-calling-low → persona-low → fallback-low
```

So Padatious gets first crack (at high confidence), then Adapt at high, then Adapt at medium, then various fallbacks. **First match wins** — once any plugin returns a non-None `IntentHandlerMatch`, the rest never run for that utterance.

This means in normal operation our plugin (at `-low`) only sees utterances that **everything else gave up on**. That's the design choice for safety. To go full LLM-orchestrator, move us to `-high`, but expect every utterance to pay a ~1-2s LLM round-trip.

---

## A real-world quirk: case-mismatch in vocab IDs

If you build a tool that introspects the registry, watch out:

- **Vocab files are loaded with `entity_type = alphanumeric_skill_id + filename[:-4].title()`.**
  So `wakeup.voc` becomes `ovos_skill_naptime_openvoiceosWakeup` (capital W). See `ovos_workshop/resource_files.py:802`.

- **Adapt intent `requires` is built from whatever the developer wrote in `IntentBuilder.require("wakeup")`.**
  After munging: `ovos_skill_naptime_openvoiceoswakeup` (lowercase w). See `ovos_workshop/intents.py:341` (`munge_intent_parser`).

These two strings should match for vocab-resolution to work but **don't**, because of the `.title()` call. Adapt itself sidesteps the problem because its tagger lowercases everything at match time. But a registry inspector that does string-key lookup against `register_vocab` data and `register_intent` data will see the mismatch.

This plugin handles it by lowercasing both sides at registration time (see `ovos_tool_calling/__init__.py: SkillRegistry._on_vocab` and the `vocab()` accessor).

---

## A real-world quirk: m2v overrules everything when it shouldn't

`ovos-m2v-pipeline-plugin` is a sentence-embedding classifier that tries to match utterances to skill intents based on registered example utterances. It's prone to high-confidence false matches. We saw "Set a 2 minute timer" classified as `ChangeProperties` (changing alarm/timer properties) at 0.74 confidence — wrong intent, but the m2v plugin returned a "match" so Adapt never got a turn.

Recommendation: keep m2v out of `-high` priority, or remove it entirely. The Adapt match for that query is unambiguous.

---

## What this means for the LLM-orchestrator

A short list of decisions:

- **Tool name**: always `<skill_id>:<intent_name>`. Sanitize for OpenAI's `[A-Za-z0-9_-]{1,64}`. We use `__` as the separator.
- **Tool description**: derive from artifacts. For Adapt, vocab phrases via `register_vocab`. For Padatious, sample utterances via `padatious:register_intent`.
- **Tool parameters**: For Padatious, extract `{slot}` markers — these are real schema. For Adapt, declare a single `utterance: string` and let the skill re-parse — that's already the contract Adapt skills expect.
- **Dispatch**: synthesize an `IntentHandlerMatch` with `match_type = <skill_id>:<intent_name>` and `match_data` shaped per matcher type. The skill receives what looks like a normal Adapt or Padatious dispatch and runs unchanged.

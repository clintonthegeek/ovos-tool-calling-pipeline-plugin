# Tool-Calling Pipeline Plugin — Install Notes

Plugin-specific gotchas. For general OVOS install/runtime quirks (installer relocation, plugin-manager v2, TTS/STT, listener tuning, KDE notifications, log locations, persona config types, etc.), see `../../docs/INSTALL_NOTES.md` at the project root.

## Vocab id case mismatch

**Symptom**: A registry inspector finds vocab keys but lookups against an intent's `requires` list return empty.

**Root cause**: see `../../docs/INTENT_MATCHERS.md` § "case-mismatch quirk". TL;DR: vocab files register as `<alphanumeric_skill_id><Title>` but `IntentBuilder.require("foo")` registers as `<alphanumeric_skill_id>foo`.

**Fix**: lowercase both sides at storage and lookup time. We do this in `SkillRegistry._on_vocab` and `SkillRegistry.vocab()`.

## OCP intents are on a separate bus channel

**Symptom**: A user expects "play some jazz" to be dispatched by us as a tool call, but it never is — even when OCP is at `-low` and we're at `-high`.

**Root cause**: OCP (Open Common Play) skills register their intents and keywords through dedicated bus events that our `SkillRegistry` does not subscribe to:

- `ovos.common_play.announce` — skill announces itself to OCP
- `ovos.common_play.register_keyword` — vocab registration
- `ovos.common_play.deregister_keyword` — vocab removal

OCP also dispatches its own match shapes — `match_type="ocp:play"`, `match_type="ocp:legacy_cps"`, etc. — with `match_data` carrying `media_type` / `query` / `conf`. Adapt and Padatious never see OCP intents either; OCP is its own pipeline plugin.

**Implication**: our tool catalog never contains OCP-registered intents. The LLM cannot pick "play" as a tool. In the user's recommended layout (OCP at `-high`, us at `-low`), OCP claims its own utterances before they reach us, so this is a no-op in practice.

**Fix**: none required for current behavior. To add LLM-orchestrated playback as a feature, we'd need to:

1. Subscribe to `ovos.common_play.announce` / `register_keyword` in `SkillRegistry`.
2. Add a "play" tool to the catalog (single string-query parameter).
3. Synthesize an `IntentHandlerMatch(match_type="ocp:play", match_data={"media_type": ..., "query": ..., "conf": ...}, skill_id="ovos.common_play")` when the LLM picks it.

This is a feature, not a bugfix — track in ROADMAP if/when it becomes a priority.

## Reasoning model incompatibility

**Symptom**: User asks an LLM-fallback question. Mycroft says "A technical issue arose while speaking with OVOS Installer LLM." No specific API error in logs.

**Root cause**: Modern reasoning models (DeepSeek's `deepseek-v4-pro`, GLM-5p1, etc.) put their chain-of-thought in `delta.reasoning_content` during streaming, **not** `delta.content`. The OVOS OpenAI plugin only reads `delta.content`, so it sees an empty stream and falls back to the error message.

In non-streaming mode the same models often emit reasoning verbatim into `content` instead of producing a clean answer ("1. Analyze the request: ...").

**Fix**: use a non-reasoning instruction-tuned model. On Fireworks:

- ✅ `accounts/fireworks/models/gpt-oss-120b` — clean function calling, recommended
- ✅ `accounts/fireworks/models/llama-v3p3-70b-instruct` — solid baseline
- ✅ `accounts/fireworks/models/kimi-k2-instruct` — long-context, conversational
- ❌ `accounts/fireworks/models/deepseek-v4-pro` — reasoning model, breaks streaming
- ❌ `accounts/fireworks/models/glm-5p1` — reasoning model, breaks streaming

Verify with a streaming curl before committing to a model:

```bash
curl -s -N https://api.fireworks.ai/inference/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -d '{"model":"accounts/fireworks/models/<model>","messages":[...],"stream":true}' \
  | head -5
```

If the deltas have `"reasoning_content"` instead of `"content"`, it's incompatible.

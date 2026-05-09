# OVOS Install Notes & Real-World Gotchas

A grab bag of things that bit us during initial bring-up and plugin development. Capturing them here so future sessions don't redo the diagnosis.

## OVOS-installer relocation bug (May 2026)

**Symptom**: `uv pip install` against an OVOS venv fails with:

```
Could not find platform independent libraries <prefix>
sys.base_prefix = '/install'
ModuleNotFoundError: No module named 'encodings'
```

**Root cause**: The OVOS installer creates the venv with `python3 -m venv` (system Python 3.12) but then adds a `python3.11` symlink that points into uv's standalone Python distribution at `~/.local/share/uv/python/cpython-3.11.13-linux-x86_64-gnu/`. That standalone Python was compiled with `--prefix=/install` and uses path-relative tricks to find its stdlib — tricks that break when invoked from a venv symlink in another tree.

**Fix**: Recreate the venv directly with uv:

```bash
rm -rf ~/.venvs/ovos
uv venv --python 3.11 ~/.venvs/ovos
# Then re-run the OVOS installer playbook
```

uv's own `venv` command knows how to relocate its standalone Python correctly.

## OVOS plugin-manager v2 entry-point change

**Symptom**: An installed VAD/STT/wake-word plugin doesn't show up in `find_*_plugins()`. Manual import works, registry returns empty.

**Root cause**: ovos-plugin-manager v2.x changed entry-point group names from `ovos.plugin.<TYPE>` to `opm.<TYPE>`. Older plugins (e.g. `ovos-vad-plugin-silero` 0.0.5) still register under the old group. Some plugin types have a fallback shim that looks at both groups; **VAD does not**.

**Fix**: upgrade the affected plugin to a version that uses the new group:

```bash
pip install -U ovos-vad-plugin-silero  # 0.1.0+ uses opm.VAD
```

If no such version exists, the plugin is incompatible with current OVOS.

## Vocab id case mismatch

**Symptom**: A registry inspector finds vocab keys but lookups against an intent's `requires` list return empty.

**Root cause**: see `docs/INTENT_MATCHERS.md` § "case-mismatch quirk". TL;DR: vocab files register as `<alphanumeric_skill_id><Title>` but `IntentBuilder.require("foo")` registers as `<alphanumeric_skill_id>foo`.

**Fix**: lowercase both sides at storage and lookup time. We do this in `SkillRegistry._on_vocab` and `SkillRegistry.vocab()`.

## m2v over-eager matching

**Symptom**: Adapt skill `IntentBuilder.require("set").require("timer")` should match "set a 2 minute timer" but doesn't; instead the m2v pipeline matches a different intent (often `ChangeProperties` for the alerts skill).

**Root cause**: `ovos-m2v-pipeline-plugin` is a sentence-embedding classifier. It matches utterances to the closest skill's example utterances by embedding distance. With wide thresholds and many similar examples across skills, it produces high-confidence false positives.

**Fix**: Either remove m2v from the pipeline or move it to `-low`. Recommended pipeline order has Adapt-high before m2v, so Adapt's precise keyword match wins:

```json
"pipeline": [
  ...
  "ovos-padatious-pipeline-plugin-high",
  "ovos-adapt-pipeline-plugin-high",
  // "ovos-m2v-pipeline-high",   // skip or move below
  "ovos-fallback-pipeline-plugin-high",
  ...
]
```

## TTS plugin server outages

**Symptom**: `ovos-audio` logs `FileNotFoundError: None does not exist` in `execute_tts`. No audio output.

**Root cause**: Default `ovos-tts-plugin-server` (configured by the OVOS installer) reaches a hosted Piper server (e.g. `pipertts.ziggyai.online`). When that server is unreachable or returns errors, the plugin returns `None` for the audio path, and the audio service tries to play `None`.

**Fix**: switch to local Piper:

```json
"tts": {
  "module": "ovos-tts-plugin-piper",
  "ovos-tts-plugin-piper": {
    "voice": "alan-low"
  }
}
```

Piper auto-downloads voice models from huggingface on first use to `~/.local/share/piper_tts/`. Default English voice `alan-low` is Alan Pope's (Mycroft's classic voice), small, fast, runs on CPU.

## Listener wake-word leakage into STT

**Symptom**: User says "Hey Mycroft, what time is it" but STT transcribes "Hey, Minecraft, what time is it" or just "Hey, Minecraft" — the wake-word audio bleeds into the recorded utterance.

**Root cause**: Two settings interact:

- `listener.utterance_chunks_to_rewind`: chunks of audio kept *before* the wake-word event. Default 2. Used to capture the start of speech that began just before wake-word detection.
- `listener.silence_end`: silence threshold for end-of-utterance. Default 0.7s.

If the user pauses briefly after "Hey Mycroft" before the actual command, the buffer rewind catches the wake-word tail and VAD ends recording mid-sentence.

**Fix**: tighter rewind, longer silence:

```json
"listener": {
  "utterance_chunks_to_rewind": 0,
  "silence_end": 1.5,
  "VAD": {
    "module": "ovos-vad-plugin-silero",
    "silence_seconds": 1.2,
    "min_seconds": 2,
    "ovos-vad-plugin-silero": { "threshold": 0.4 }
  }
}
```

## STT misrecognition fixes

The default `ovos-stt-plugin-server` uses a hosted Vosk-equivalent service. It mistranscribes proper nouns ("Mycroft" → "Microsoft" / "Minecraft" / "My Croft") and is slow.

**Fix**: switch to local Whisper:

```bash
pip install ovos-stt-plugin-fasterwhisper
```

```json
"stt": {
  "module": "ovos-stt-plugin-fasterwhisper",
  "ovos-stt-plugin-fasterwhisper": {
    "model": "small",          // ~470MB; 'tiny' = ~75MB but worse
    "use_cuda": false,
    "compute_type": "int8",
    "beam_size": 5,
    "cpu_threads": 4
  }
}
```

Model auto-downloads on first use. The "small" model gives clean transcription of proper nouns and uncommon words.

## KDE volume notification on every wake-word

**Symptom**: Each "Hey Mycroft" pops up a Plasma volume OSD showing volume lowered to 30%, then restored. Annoying.

**Root cause**: `listener.fake_barge_in` (default `true`) lowers system volume during wake-word detection so the assistant can hear over playing audio. Touches PulseAudio, which triggers KDE's notification.

**Fix**: disable if you don't routinely talk over audio playback:

```json
"listener": { "fake_barge_in": false }
```

## Persona plugin config types

**Symptom**: Persona LLM call fails with `Request body field 'max_tokens' is of type 'string', expected 'int'`.

**Root cause**: The OVOS installer writes `~/.config/ovos_persona/<persona>.json` with **string** values for `max_tokens`, `temperature`, `top_p`. The OpenAI plugin streams them through to the API as-is.

**Fix**: edit the persona file to use proper types:

```json
{
  "name": "OVOS Installer LLM",
  "ovos-solver-openai-plugin": {
    "max_tokens": 300,        // int, not "300"
    "temperature": 0.2,       // float, not "0.2"
    "top_p": 0.2              // float, not "0.2"
  }
}
```

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

## Where logs live

- **Per-service journal**: `journalctl --user -u ovos-core.service` (and friends)
- **File logs** (sometimes lag the journal): `~/.local/state/mycroft/`
  - `skills.log` — intent service, skills
  - `voice.log` — listener
  - `audio.log` — TTS, audio backends
  - `phal.log` — PHAL plugins
  - `ggwave.log` — ggwave listener
  - `bus.log` — message bus

The journal is usually fresher than the file logs because of buffer flushing. Both are useful.

## OCP/news/youtube skill failures

**Symptom**: `ModuleNotFoundError: No module named 'ovos_classifiers'` for the news, somafm, or youtube-music skills.

**Root cause**: `ovos-classifiers` was deprecated/removed somewhere along the line, but the OCP-using skills still import it.

**Fix**: not yet diagnosed; these skills fail to load but don't impact other functionality. Can ignore unless you specifically need news/somafm/youtube-music.

## Persona's config default detection bug

**Symptom**: Logs show `system prompt not set in config! defaulting to 'You are a helpful assistant.'` even though the persona config has a `system_prompt` field.

**Root cause**: The error log fires for every solver in the persona loader, but only the *user* persona ("OVOS Installer LLM") has the system prompt set. The error refers to other personas in the loader (Wikipedia/Wolfram/WikiHow plugins also use the OpenAI persona engine internally).

**Fix**: ignore. The user persona uses its configured system prompt correctly; the warning is for unrelated plugin personas.

# ovos-tool-calling-pipeline-plugin

An [OpenVoiceOS](https://github.com/OpenVoiceOS) pipeline plugin that uses an LLM as the **primary intent router**, exposing existing OVOS skills as **function-call tools**.

Where the stock OVOS pipeline puts the LLM at the end (as a fallback when keyword/fuzzy matchers miss), this plugin inverts the flow: the LLM sees every utterance first, picks a skill via tool calling (with arguments already extracted), or answers directly when no skill fits.

## Documentation

For contributors and AI assistants working on the plugin:

| Doc | What it covers |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | OVOS process topology, message bus, skill model, dispatch flow |
| [`docs/INTENT_MATCHERS.md`](docs/INTENT_MATCHERS.md) | Adapt and Padatious deep dives, with case-mismatch quirks documented |
| [`docs/PIPELINE_PROTOCOL.md`](docs/PIPELINE_PROTOCOL.md) | The pipeline plugin contract: discovery, loading, IntentHandlerMatch shape, bus topics |
| [`docs/INSTALL_NOTES.md`](docs/INSTALL_NOTES.md) | Real-world OVOS install gotchas (vocab case, reasoning models, TTS plugin failures, etc.) |
| [`docs/DEV_LOOP.md`](docs/DEV_LOOP.md) | Editable install, systemctl restart, log tailing, test-utterance recipes |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Versions shipped (v0.1–v0.4), planned (v0.5+), design rationale |
| [`docs/TODO.md`](docs/TODO.md) | Active short-term task list |
| [`CLAUDE.md`](CLAUDE.md) | Instructions for AI assistants working on this repo |

## Status

**v0.4 — latency gate.** Cheap pre-LLM admission control + dispatch caching. Empty / single-token / blocklisted utterances skip the LLM entirely; repeated utterances hit a small LRU and return the previous dispatch immediately. Every decision (skip / cached / proceed) is logged with a reason.

### Configuration

Add to ``~/.config/mycroft/mycroft.conf`` under ``intents``:

```json
"intents": {
  "ovos-tool-calling-pipeline-plugin": {
    "enabled": true,
    "persona": "OVOS Installer LLM",
    "model": "accounts/fireworks/models/gpt-oss-120b",

    "min_words": 2,
    "cache_size": 32,
    "blocklist_patterns": []
  }
}
```

The ``persona`` field reuses the API URL, key, and (optional) system prompt from ``~/.config/ovos_persona/<persona>.json``. Override individual fields inline (``api_url``, ``key``, ``model``, ``system_prompt``, ``max_tokens``, ``temperature``) as needed.

Gate config (all optional):

| Field | Default | Effect |
|---|---|---|
| ``min_words`` | 2 | Skip LLM if utterance has fewer than N whitespace-separated tokens. |
| ``cache_size`` | 32 | LRU of recent (utterance → dispatch). 0 disables caching. |
| ``blocklist_patterns`` | ``[]`` | List of regexes; if any matches, skip LLM. |

Also add the plugin to your pipeline list. Place at ``-high`` for full LLM-orchestrator mode, ``-low`` for fallback after the keyword/fuzzy matchers:

```json
"pipeline": [
  "ovos-stop-pipeline-plugin-high",
  "ovos-converse-pipeline-plugin",
  "ovos-tool-calling-pipeline-plugin-high",
  ...
]
```

### Tested with

- Fireworks ``accounts/fireworks/models/gpt-oss-120b`` (recommended; clean function calling, no reasoning artefacts).
- Reasoning models (``deepseek-v4-pro``, ``glm-5p1``) emit their chain of thought in ``reasoning_content`` rather than ``content`` and are not currently supported.

### Inspection

Once running, trigger a registry summary:

```bash
python -c "from ovos_bus_client import MessageBusClient, Message; \
import time; b = MessageBusClient(); b.run_in_thread(); time.sleep(1); \
b.emit(Message('tool-calling.registry.dump')); time.sleep(2); b.close()"
```

Or dump example schemas (one Adapt + one Padatious):

```bash
python -c "from ovos_bus_client import MessageBusClient, Message; \
import time; b = MessageBusClient(); b.run_in_thread(); time.sleep(1); \
b.emit(Message('tool-calling.schemas.dump')); time.sleep(2); b.close()"
```

For the full JSON catalog, pass `{"full": true}` as the message data.

Logs land in `~/.local/state/mycroft/skills.log` under the `ovos_tool_calling` logger.

## Install (editable, into an existing OVOS venv)

```bash
source ~/.venvs/ovos/bin/activate
pip install -e /path/to/ovos-tool-calling-pipeline-plugin
```

## Enable

Add to `~/.config/mycroft/mycroft.conf` near the top of the pipeline list:

```json
"intents": {
  "pipeline": [
    "ovos-tool-calling-pipeline-plugin-high",
    "ovos-stop-pipeline-plugin-high",
    "ovos-converse-pipeline-plugin",
    ...
  ]
}
```

Then restart `ovos-core.service`.

## Roadmap

- [x] **v0.1** — Discover registered skill intents (Adapt + Padatious) by listening to the bus
- [x] **v0.2** — Convert the registry into OpenAI-style tool schemas
- [x] **v0.3** — Call configured LLM with the tool list, dispatch the picked tool by emitting the same bus message Adapt/Padatious would emit
- [x] **v0.4** — Latency gate (skip LLM for empty/short/blocklisted utterances; cache repeated dispatches)
- [ ] **v0.5** — Speak plain text answers when no tool fits (currently returns no match)
- [ ] **v0.6** — Multi-tool agent loop (sequential calls)
- [ ] **v0.7** — Conversational state respect (defer to `converse` pipeline)

## License

Apache-2.0

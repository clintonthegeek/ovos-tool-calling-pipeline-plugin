# ovos-tool-calling-pipeline-plugin

An [OpenVoiceOS](https://github.com/OpenVoiceOS) pipeline plugin that uses an LLM as the **primary intent router**, exposing existing OVOS skills as **function-call tools**.

Where the stock OVOS pipeline puts the LLM at the end (as a fallback when keyword/fuzzy matchers miss), this plugin inverts the flow: the LLM sees every utterance first, picks a skill via tool calling (with arguments already extracted), or answers directly when no skill fits.

## Status

**v0.2 — schema generation.** Builds a live registry of every Adapt and Padatious intent registered on the bus, then converts that registry into OpenAI-style tool schemas. Still returns no match; v0.3 will call the LLM with the catalog and dispatch the picked tool.

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
- [ ] **v0.3** — Call configured LLM with the tool list, dispatch the picked tool by emitting the same bus message Adapt/Padatious would emit
- [ ] **v0.4** — Pass through plain answers via persona/speak when no tool fits
- [ ] **v0.5** — Multi-tool agent loop (sequential calls)
- [ ] **v0.6** — Conversational state respect (defer to `converse` pipeline)
- [ ] **v0.7** — Latency gate (skip LLM for trivially keyword-matchable utterances)

## License

Apache-2.0

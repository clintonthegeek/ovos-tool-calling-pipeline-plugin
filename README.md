# ovos-tool-calling-pipeline-plugin

An [OpenVoiceOS](https://github.com/OpenVoiceOS) pipeline plugin that uses an LLM as the **primary intent router**, exposing existing OVOS skills as **function-call tools**.

Where the stock OVOS pipeline puts the LLM at the end (as a fallback when keyword/fuzzy matchers miss), this plugin inverts the flow: the LLM sees every utterance first, picks a skill via tool calling (with arguments already extracted), or answers directly when no skill fits.

## Status

**v0 — stub.** Loads, logs every utterance at all confidence tiers, returns no match. Use it to verify plugin discovery and pipeline wiring.

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

- [ ] Discover registered skill intents and build OpenAI-style tool schemas
- [ ] Call configured LLM with the tool list, dispatch the picked tool by emitting the same bus message Adapt/Padatious would emit
- [ ] Pass through plain answers via persona/speak when no tool fits
- [ ] Multi-tool agent loop (sequential calls)
- [ ] Conversational state respect (defer to `converse` pipeline)
- [ ] Latency gate (skip LLM for trivially keyword-matchable utterances)

## License

Apache-2.0

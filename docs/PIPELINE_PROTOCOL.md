# Pipeline Plugin Protocol

How an OVOS pipeline plugin is loaded, configured, called, and how its return value reaches the target skill. This is the contract this plugin implements; understanding it is necessary for any change to the dispatch layer.

## 1. Plugin discovery

Pipeline plugins are pip-installable packages that declare an entry point in the `opm.pipeline` group:

```toml
# pyproject.toml
[project.entry-points."opm.pipeline"]
ovos-tool-calling-pipeline-plugin = "ovos_tool_calling:ToolCallingPipeline"
```

The key on the left is the **plugin id**. It must match the names referenced in `mycroft.conf`'s `intents.pipeline` list (without the tier suffix).

`ovos-core` finds plugins via `find_pipeline_plugins()` in `ovos_plugin_manager.pipeline`. This scans `importlib.metadata.entry_points(group='opm.pipeline')`. Any plugin reachable on `sys.path` is discoverable; install with `pip install -e .` to develop.

## 2. Loading and configuration

When `ovos-core` reloads pipelines, it calls `OVOSPipelineFactory.load_plugin(pipe_id, bus, config)` for each discovered plugin. The factory:

```python
config = config or Configuration().get("intents", {}).get(pipe_id, {})
clazz = find_pipeline_plugins()[pipe_id]
plugin_instance = clazz(bus, config)
```

Two consequences:

- **Each plugin is instantiated exactly once** per ovos-core process. There is no per-utterance instance.
- **Config is read from `intents.<pipe_id>` of the merged `mycroft.conf`** at load time. Changes require a restart.

Constructor signature must accept `(bus, config)`:

```python
class ToolCallingPipeline(ConfidenceMatcherPipeline):
    def __init__(self, bus=None, config=None):
        super().__init__(bus=bus, config=config)
        self.bus     # ovos_bus_client.client.MessageBusClient or FakeBus
        self.config  # the dict from intents.<pipe_id>
        # ... your init ...
```

## 3. The base class

`ConfidenceMatcherPipeline` (from `ovos_plugin_manager.templates.pipeline`):

```python
class ConfidenceMatcherPipeline(PipelinePlugin):
    @abstractmethod
    def match_high(self, utterances: List[str], lang: str, message: Message
                  ) -> Optional[IntentHandlerMatch]:
        ...
    @abstractmethod
    def match_medium(self, utterances, lang, message) -> Optional[IntentHandlerMatch]:
        ...
    @abstractmethod
    def match_low(self, utterances, lang, message) -> Optional[IntentHandlerMatch]:
        ...

    def match(self, utterances, lang, message) -> Optional[IntentHandlerMatch]:
        # Default: try high, then medium, then low. Return first non-None.
        return (self.match_high(...) or self.match_medium(...) or self.match_low(...))
```

`utterances` is a `List[str]` — usually just `[transcript]`, but listener utterance transformers may produce alternate forms (e.g. punctuated and unpunctuated). Most plugins use only `utterances[0]`.

`lang` is BCP-47 (`en-US`).

`message` is the original `recognizer_loop:utterance` Message. You may reach `message.context` for things like `session_id` or `skill_id`.

## 4. The IntentHandlerMatch return type

```python
@dataclass
class IntentHandlerMatch:
    match_type: str               # bus topic the intent service will emit
    match_data: Optional[Dict]    # merged into the message data on dispatch
    skill_id: Optional[str]       # for skill activation tracking
    utterance: Optional[str]      # the transcribed utterance
    updated_session: Optional[Session] = None
```

When you return one, `ovos-core._emit_match_message()` does roughly:

```python
data = dict(original_message.data)        # carries forward session info, etc
data.update(match.match_data)             # plugin's overrides win
data["utterance"] = match.utterance       # canonicalize
data["lang"] = lang
reply = original_message.reply(match.match_type, data)
self.bus.emit(reply)                      # fires the skill handler
```

**`match_type` is the bus topic to fire.** For routing to a skill's `@intent_handler`, this must be exactly `<skill_id>:<intent_name>` — the same string Adapt or Padatious would have used.

**`match_data` is what the skill handler reads.** Shape it to match the matcher you're impersonating. See `ovos_tool_calling/dispatch.py: make_match()` for our reference shapes:

For Padatious:
```python
{
  "name": "<skill_id>:<intent_name>",
  "utterance": "<original utterance>",
  "conf": 0.99,
  "<slot1>": "<value>",
  "<slot2>": "<value>",
}
```

For Adapt:
```python
{
  "intent_type": "<skill_id>:<IntentName>",
  "utterance": "<original utterance>",
  "confidence": 0.99,
  "target": None,
  "__tags__": [],
}
```

Most Adapt skills only read `message.data["utterance"]` and re-parse arguments themselves, so empty `__tags__` works.

## 5. Pipeline placement (tier suffixes)

`mycroft.conf` references plugins by `<pipe_id>-<tier>`:

```json
"intents": {
  "pipeline": [
    "ovos-tool-calling-pipeline-plugin-high",   // calls match_high
    "ovos-padatious-pipeline-plugin-high",
    ...
    "ovos-adapt-pipeline-plugin-medium",        // calls match_medium
    ...
    "ovos-tool-calling-pipeline-plugin-low",    // calls match_low
    "ovos-fallback-pipeline-plugin-low"
  ]
}
```

Internally, `IntentService.get_pipeline_matcher()` strips the tier suffix to find the plugin instance, then dispatches to the right `match_*` method:

```python
matcher_id = "ovos-tool-calling-pipeline-plugin-low"
pipe_id = re.sub(r'-(high|medium|low)$', '', matcher_id)
plugin = self.pipeline_plugins[pipe_id]
return plugin.match_low
```

You may register at multiple tiers (we list ourselves only at `-low` by default; users can move us to `-high` for pure-orchestrator mode).

## 6. Bus topics this plugin care about

### Listened to (incoming)

| Topic | Source | Purpose for us |
|---|---|---|
| `register_intent` | skills (Adapt) | Build adapt-side tool catalog entries |
| `register_vocab` | skills (Adapt) | Map vocab id → trigger phrases for tool descriptions |
| `padatious:register_intent` | skills (Padatious) | Build padatious-side tool catalog entries |
| `detach_intent`, `detach_skill` | skills | Remove from catalog |
| `tool-calling.registry.dump` | dev | Print registry summary to log |
| `tool-calling.schemas.dump` | dev | Print schema catalog to log; data `{"full": true}` for full JSON |

### Emitted (outgoing)

We emit an `IntentHandlerMatch` *return value*, not a bus message. ovos-core does the actual emission. The downstream effect is a `<skill_id>:<intent_name>` event being fired on the bus, which the skill picks up.

## 7. Snapshot vs streaming

Two ways to learn the registered intents:

### Streaming (what we do)

Subscribe to `register_intent` and friends on the bus. As skills load, messages stream in. **Caveat**: the pipeline plugin loads early in `ovos-core` startup, *before* skills load. There's a window of seconds-to-tens-of-seconds where the registry is empty or partial. If you snapshot the registry too early (e.g. immediately after construction), expect zero results.

### Snapshot query

Adapt and Padatious also expose snapshot endpoints:

```python
intent.service.adapt.manifest.get
intent.service.adapt.vocab.manifest.get
intent.service.padatious.manifest.get
intent.service.padatious.entities.manifest.get
```

Send a Message on the topic, register a one-shot handler for the response (typically `<topic>.response` but check the implementation in `ovos_adapt/opm.py` and `ovos_padatious/opm.py`).

We don't currently use these — streaming is sufficient because we load with the intent service. We could combine both for robustness if a future deployment loads us late.

## 8. Threading and latency

The intent service iterates the pipeline list **synchronously** for each utterance. While our `match_*` is running, no other intent matching happens.

- A 1.7s LLM round-trip blocks the entire intent service for 1.7s.
- If a second utterance arrives during that time, it queues.

For now this is acceptable (we're at `-low` and only triggered when nothing else matched). If we move to `-high` we should consider:
- Returning `None` quickly when the gate decides to skip
- Spawning a background thread for the LLM call and using the converse pipeline / bus events to deliver the response asynchronously
- Cutting tool-call latency with a smaller/local model

## 9. Failure modes

- **Plugin raises an exception during `match_*`**: ovos-core catches and logs, then continues with the next pipeline plugin. Our plugin returns `None` on any LLM error to fail open.
- **Plugin returns a malformed `IntentHandlerMatch`**: the bus emit may not match any skill handler, and the user hears nothing. Symptom: query goes nowhere.
- **`match_type` references an unregistered intent**: skill handler never fires. Same symptom.
- **Plugin times out (no timeout enforcement in OVOS)**: blocks the intent service indefinitely. Mitigate via `requests.post(..., timeout=N)` in the LLM client.

The Gate (v0.4) absorbs most of the noise-utterance failure modes by skipping the LLM entirely when the input is bad.

# OVOS Architecture (as it concerns this plugin)

This is a working reference for anyone (human or AI) implementing on top of OVOS. It describes the runtime topology, the message-bus contract, the skill loading model, and the dispatch flow that any pipeline plugin sits inside. It's deliberately specific — every claim here was verified against a running OVOS install in May 2026.

## 1. Process topology

OVOS is six long-running processes glued together by one websocket bus.

```
                ┌───────────────────────────────────────────────────────┐
                │           ovos-messagebus (ws://127.0.0.1:8181)        │
                └───────────────────────────────────────────────────────┘
                  ▲     ▲       ▲       ▲       ▲       ▲       ▲
                  │     │       │       │       │       │       │
       ┌──────────┘     │       │       │       │       │       └─────┐
  ovos-listener   ovos-core   ovos-audio   ovos-phal   ovos-gui   skills
   mic + STT +    skill mgr +  TTS +       system     touch     N child
   wake word      intent svc   playback +  hooks      screen    procs
                  + pipeline   OCP media              (optional) (one per
                                                                  installed
                                                                  plugin
                                                                  skill)
```

All inter-process communication goes through the bus. There are no direct calls between services. A pipeline plugin (like this project) lives **inside `ovos-core`** as an in-process Python class — it's not a separate process — but everything it does eventually flows through the bus.

systemd unit names (user-scope, on this install):

```
ovos-messagebus.service     # the websocket bus broker
ovos-listener.service       # ovos-dinkum-listener: mic, VAD, STT, wake word
ovos-core.service           # skill manager + intent service (we live here)
ovos-audio.service          # TTS engine + audio backends + OCP media
ovos-phal.service           # platform/hardware abstraction layer
ovos-ggwave-listener.service# audio-codes listener (optional)
ovos.service                # meta-target that activates the others
```

`systemctl --user list-units 'ovos*'` enumerates them. `systemctl --user restart ovos-core.service` is the one we care about during plugin development.

## 2. The bus

The websocket bus is a JSON message router. Every component connects, subscribes to topics by name, and emits topics by name. The most relevant topics for a pipeline plugin:

| Topic | Direction | Meaning |
|---|---|---|
| `recognizer_loop:utterance` | listener → bus | STT finished; here is the transcribed text |
| `register_intent` | skill → bus | A skill registered an Adapt-style intent |
| `register_vocab` | skill → bus | A vocab id ↔ phrase mapping |
| `padatious:register_intent` | skill → bus | A skill registered a Padatious-style intent |
| `padatious:register_entity` | skill → bus | A Padatious entity (slot type) |
| `detach_intent`, `detach_skill` | skill → bus | A skill is being unloaded |
| `intent.service.adapt.manifest.get` | any → adapt | Snapshot query of the Adapt manifest |
| `intent.service.padatious.manifest.get` | any → padatious | Snapshot query of the Padatious manifest |
| `<skill_id>:<IntentName>` | intent svc → skill | Dispatch — the actual intent invocation |
| `speak` | skill → audio | "Say this text via TTS" |
| `mycroft.skills.train` | core → padatious | Trigger Padatious training pass |

The most important message for our purposes is the dispatch one. When the intent service decides an utterance maps to a skill+intent, it emits a message named `<skill_id>:<IntentName>` (e.g. `ovos-skill-alerts.openvoiceos:CreateTimer`) with the matched data. The skill's `@intent_handler` for that name picks it up. **Our pipeline plugin's job is to make sure the right such message gets emitted.**

## 3. Where things live on disk

A working OVOS install has files in several places. On this user's box (Manjaro KDE, OVOS installer-managed):

```
~/.venvs/ovos/                    # Python venv all OVOS packages live in
├── bin/python                    # Python 3.11 (uv-managed)
├── bin/ovos-config              # CLI config tool
└── lib/python3.11/site-packages/
    ├── ovos_core/                # intent service, skill manager
    ├── ovos_workshop/            # OVOSSkill base class, intent helpers
    ├── ovos_bus_client/          # MessageBusClient, Message
    ├── ovos_plugin_manager/      # plugin discovery via entry points
    ├── ovos_adapt/               # the Adapt pipeline plugin
    ├── ovos_padatious/           # the Padatious pipeline plugin
    ├── ovos_persona/             # the Persona pipeline plugin (LLM passthrough)
    ├── ovos_dinkum_listener/     # the listener service
    ├── ovos_audio/               # the audio service
    ├── ovos_skill_*/             # individual skills (one per plugin skill)
    └── ovos_tool_calling/        # this plugin (editable install)

~/.config/mycroft/mycroft.conf    # per-user OVOS config (the big one)
~/.config/ovos_persona/*.json     # persona definitions (LLM solver configs)
~/.config/ovos-installer/         # installer scenario files
~/.config/systemd/user/ovos*.service   # user-scope systemd units
~/.local/state/mycroft/           # logs (see DEV_LOOP.md)
~/.local/share/piper_tts/         # downloaded Piper voice models
~/.local/share/vosk/              # Vosk wake-word/STT models
/tmp/tts/                         # TTS audio file cache
```

## 4. Skills

A skill is a Python class that:

- Subclasses `OVOSSkill` (from `ovos_workshop.skills.ovos`).
- Lives in its own pip-installable package, e.g. `ovos-skill-alerts`.
- Registers itself via the `opm.skill` (or legacy `ovos.plugin.skill`) entry-point group.
- Auto-loads `.voc`, `.intent`, `.dialog`, and `.entity` resource files from its `locale/<lang>/` directory.
- Declares intent handlers via decorators on methods.

Skill discovery happens at `ovos-core` startup. Each skill is loaded into its own subprocess (or in-process plugin, depending on installation method) and connects to the bus. When loading, the skill emits its `register_intent` / `register_vocab` / `padatious:register_intent` messages, which Adapt and Padatious consume to populate their matchers. This is also how this plugin builds its tool catalog — see `INTENT_MATCHERS.md`.

Two intent handler styles:

```python
# Adapt: keyword-based
@intent_handler(IntentBuilder("CreateTimer")
                .require("create").require("timer")
                .optionally("question"))
def handle_create_timer(self, message):
    duration = self._parse_duration(message.data["utterance"])
    ...

# Padatious: sample-based, fuzzy
@intent_handler("wiki.intent")          # filename in locale/<lang>/intents/
def handle_wiki(self, message):
    query = message.data["query"]      # extracted slot
    ...
```

Skills can mix both styles in the same class. Choice is per-handler, not per-skill.

## 5. The intent service and pipeline plugins

`ovos-core` runs an **intent service** that owns a list of pipeline plugins. The list is configured in `mycroft.conf` under `intents.pipeline` and is processed in order, first match wins.

Each pipeline plugin is a Python class that:

- Subclasses `ConfidenceMatcherPipeline` (from `ovos_plugin_manager.templates.pipeline`).
- Implements `match_high()`, `match_medium()`, `match_low()`. Each returns `Optional[IntentHandlerMatch]`.
- Is registered via the `opm.pipeline` entry-point group with a single name. Tier suffixes (`-high`, `-medium`, `-low`) are appended in the pipeline list to choose which method to call.

Stock pipeline plugins:

| Plugin | What it does |
|---|---|
| `ovos-stop-pipeline-plugin` | Catches "stop", "cancel", etc. |
| `ovos-converse-pipeline-plugin` | Continues active skill conversations |
| `ovos-ocp-pipeline-plugin` | Open Common Play (media) routing |
| `ovos-padatious-pipeline-plugin` | Fuzzy-trained intent matching |
| `ovos-adapt-pipeline-plugin` | Keyword-based intent matching |
| `ovos-m2v-pipeline-plugin` | Sentence-embedding intent classifier (turned off in this install — over-eager) |
| `ovos-common-query-pipeline-plugin` | Q&A solvers (Wikipedia, Wolfram, DDG) |
| `ovos-fallback-pipeline-plugin` | Last-resort skill-based fallbacks |
| `ovos-persona-pipeline-plugin` | LLM passthrough (text in, text out) |
| `ovos-tool-calling-pipeline-plugin` | **This project** — LLM as primary router |

A typical pipeline list (the one this install uses):

```json
"intents": {
  "pipeline": [
    "ovos-stop-pipeline-plugin-high",
    "ovos-converse-pipeline-plugin",
    "ovos-ocp-pipeline-plugin-high",
    "ovos-persona-pipeline-plugin-high",
    "ovos-padatious-pipeline-plugin-high",
    "ovos-adapt-pipeline-plugin-high",
    "ovos-fallback-pipeline-plugin-high",
    "ovos-stop-pipeline-plugin-medium",
    "ovos-adapt-pipeline-plugin-medium",
    "ovos-fallback-pipeline-plugin-medium",
    "ovos-tool-calling-pipeline-plugin-low",
    "ovos-persona-pipeline-plugin-low",
    "ovos-fallback-pipeline-plugin-low"
  ]
}
```

## 6. End-to-end dispatch flow

A typical voice query, fully traced:

```
1. Microphone → ovos-listener
   (ALSA/sounddevice plugin yields 16 kHz PCM)

2. Wake-word detector triggers
   (Vosk small grammar; matches "hey mycroft" + variants)

3. Recording mode + Silero VAD
   (records until silence_seconds of silence; trims wake-word audio)

4. STT plugin transcribes
   (fasterwhisper local "small" model in this install)

5. Listener emits `recognizer_loop:utterance`
   { "utterances": ["set a five minute timer"] }

6. ovos-core's intent service receives the bus message
   - Walks the configured pipeline list
   - Calls match_high / match_medium / match_low on each plugin in order
   - First non-None IntentHandlerMatch wins

7. The winning plugin returns IntentHandlerMatch(...)

8. ovos-core._emit_match_message() turns it into a bus message
   - topic = match.match_type (e.g. "ovos-skill-alerts.openvoiceos:CreateTimer")
   - data merges original message.data with match.match_data

9. The skill (subscribed to its own intent topic) receives the message
   - @intent_handler runs
   - Skill speaks via Message("speak", {"utterance": "..."})

10. ovos-audio receives the speak message
    - TTS plugin synthesizes WAV (Piper alan-low locally in this install)
    - Audio backend plays it (mpv autodetected)
```

For our tool-calling plugin, step 7 is where we call the LLM, parse the tool-call response, and return a synthesized `IntentHandlerMatch` whose `match_type` is the same `<skill_id>:<IntentName>` Adapt or Padatious would have produced. From step 8 onward, the skill has no idea it was called by an LLM.

## 7. Configuration system

`ovos-config` (CLI tool) operates over a layered config:

- **Default config**: shipped with each OVOS package (`ovos_config/mycroft.conf` and friends).
- **System config**: `/etc/mycroft/mycroft.conf` (rare on user installs).
- **User config**: `~/.config/mycroft/mycroft.conf` (the one we edit).
- **Remote config**: pulled from a backend if configured (rarely used in OVOS).

The `Configuration()` class (in `ovos_config.config`) returns the merged view. Pipeline plugins receive their config block from `intents.<pipe-id>` of the merged config.

## 8. Things that frequently confuse people

- **Skills aren't always isolated processes.** The "plugin skill" loader runs them in-process inside `ovos-core` for installer-managed installs. Don't assume they have their own PID.
- **Bus messages are fire-and-forget by default.** No reliability guarantees. If you need a reply, register a temporary handler for a response topic before emitting.
- **The intent service is single-threaded for matching**, but skills handle their dispatched intents on their own threads. A long-running pipeline plugin (e.g. one that calls an LLM) blocks the entire intent service for its duration. Expensive plugins should consider running their own thread or returning fast and dispatching async.
- **Configuration changes require a service restart.** OVOS does not hot-reload `mycroft.conf` for pipeline plugins.
- **Skill loading is asynchronous.** A pipeline plugin loaded at intent-service startup will see register_intent messages stream in over the next 5-30 seconds as skills load. Plan your snapshot/query logic accordingly (see `PIPELINE_PROTOCOL.md`).

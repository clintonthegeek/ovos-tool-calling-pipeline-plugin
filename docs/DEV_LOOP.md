# Development Loop

How to make a change, test it against a live OVOS install, and verify the result. Works on the user's specific install at `~/.venvs/ovos/`.

## Setup (one-time)

```bash
# 1. Repo lives outside the OVOS venv to keep it portable.
mkdir -p ~/dev/ovos
git clone git@github.com:clintonthegeek/ovos-tool-calling-pipeline-plugin.git \
    ~/dev/ovos/ovos-tool-calling-pipeline-plugin

# 2. Editable install into the OVOS venv.
~/.venvs/ovos/bin/pip install -e ~/dev/ovos/ovos-tool-calling-pipeline-plugin

# 3. Add to mycroft.conf pipeline (see PIPELINE_PROTOCOL.md for placement).
$EDITOR ~/.config/mycroft/mycroft.conf

# 4. Optional: clone reference repos for reading.
git clone https://github.com/OpenVoiceOS/ovos-persona.git ~/dev/ovos/ovos-persona
git clone https://github.com/OpenVoiceOS/ovos-padatious.git ~/dev/ovos/ovos-padatious
git clone https://github.com/OpenVoiceOS/ovos-adapt.git ~/dev/ovos/ovos-adapt
```

## The inner loop

```bash
# 1. Edit code.
$EDITOR ~/dev/ovos/ovos-tool-calling-pipeline-plugin/ovos_tool_calling/__init__.py

# 2. Restart ovos-core to pick up changes.
systemctl --user restart ovos-core.service

# 3. Wait until skills finish loading (~10-30s).
until journalctl --user -u ovos-core.service --since "30 seconds ago" \
        | grep -q "ovos-core is ready"; do sleep 2; done

# 4. Tail logs in another shell.
tail -f ~/.local/state/mycroft/skills.log | grep tool-calling

# 5. Trigger a test (see "Sending test utterances" below).
```

The editable install means `pip` won't reinstall anything; the venv just imports from your working tree. Any `.py` change applies on next service restart.

## Sending test utterances

Bypass the listener and STT to inject a synthetic utterance directly onto the bus:

```bash
~/.venvs/ovos/bin/python -c "
from ovos_bus_client import MessageBusClient, Message
import time
bus = MessageBusClient()
bus.run_in_thread()
time.sleep(1)
bus.emit(Message('recognizer_loop:utterance', {'utterances': ['set a five minute timer']}))
time.sleep(5)   # wait for skill response and any TTS
bus.close()
"
```

This is fast and reproducible. Use real voice via "Hey Mycroft, ..." for end-to-end verification.

## Triggering plugin debug events

The plugin exposes two bus events for inspection:

```bash
# Dump registry summary (skill / intent / vocab counts).
~/.venvs/ovos/bin/python -c "
from ovos_bus_client import MessageBusClient, Message
import time
bus = MessageBusClient(); bus.run_in_thread(); time.sleep(1)
bus.emit(Message('tool-calling.registry.dump'))
time.sleep(2); bus.close()"

# Dump schema catalog summary plus one example each (adapt + padatious).
~/.venvs/ovos/bin/python -c "
from ovos_bus_client import MessageBusClient, Message
import time
bus = MessageBusClient(); bus.run_in_thread(); time.sleep(1)
bus.emit(Message('tool-calling.schemas.dump'))
time.sleep(2); bus.close()"

# Dump the FULL schema catalog as JSON (large; ~tens of KB).
~/.venvs/ovos/bin/python -c "
from ovos_bus_client import MessageBusClient, Message
import time
bus = MessageBusClient(); bus.run_in_thread(); time.sleep(1)
bus.emit(Message('tool-calling.schemas.dump', {'full': True}))
time.sleep(2); bus.close()"
```

Output goes to `~/.local/state/mycroft/skills.log` under the `ovos_tool_calling` logger.

## Reading logs

The journal is generally fresher than file logs:

```bash
# Live tail of core
journalctl --user -u ovos-core.service -f

# Filter to our plugin only
journalctl --user -u ovos-core.service -f | grep -E "tool-calling|ovos_tool_calling"

# Show the last query's full trace across services
journalctl --user -u ovos-listener.service -u ovos-core.service \
           -u ovos-audio.service --since "2 minutes ago"

# Find the last actual voice query
journalctl --user -u ovos-listener.service --since "10 minutes ago" \
           | grep -E "Wakeword detected|Raw transcription"
```

For digging into a specific query, knowing the timestamps is the trick. Find the wake-word timestamp, then grep that minute across all services.

## Validating mycroft.conf changes

```bash
# Quick JSON syntax check
~/.venvs/ovos/bin/python -c "import json; json.load(open('$HOME/.config/mycroft/mycroft.conf'))" \
  && echo "JSON OK"

# Preview the merged effective config
~/.venvs/ovos/bin/ovos-config show -u                      # user-only
~/.venvs/ovos/bin/ovos-config get -k /listener/silence_end # specific key
~/.venvs/ovos/bin/ovos-config get -k stt                   # entire stt block
```

## Verifying a model works for tool calling

Before committing to an LLM, sanity-check it supports tool-call function calling **and** doesn't put output in `reasoning_content`:

```bash
curl -s -X POST https://api.fireworks.ai/inference/v1/chat/completions \
  -H "Authorization: Bearer fw_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model":"accounts/fireworks/models/<model>",
    "messages":[
      {"role":"system","content":"Pick a tool to satisfy the request."},
      {"role":"user","content":"set a 5 minute timer"}
    ],
    "tools":[{
      "type":"function","function":{
        "name":"create_timer",
        "description":"Start a countdown timer",
        "parameters":{"type":"object",
          "properties":{"duration_seconds":{"type":"integer"}},
          "required":["duration_seconds"]}
      }
    }],
    "max_tokens":200
  }' | python3 -m json.tool
```

Look for `choices[0].message.tool_calls[0].function.name == "create_timer"`. If you see content like `"1. Analyze the request..."` or a non-empty `reasoning_content`, the model is reasoning-style and won't work. See `docs/INSTALL_NOTES.md` § "Reasoning model incompatibility".

For streaming compatibility, repeat with `"stream": true` and inspect the deltas:

```bash
curl -s -N -X POST https://api.fireworks.ai/inference/v1/chat/completions \
  ... \
  -d '{... "stream": true}' \
  | head -10
```

If deltas use `"reasoning_content"` instead of `"content"`, the model breaks streaming output.

## Quick references

```bash
# All OVOS systemd units
systemctl --user list-units 'ovos*'

# Restart the whole stack (rarely needed)
systemctl --user restart ovos.service

# Check plugin entry-point discovery
~/.venvs/ovos/bin/python -c "
from importlib.metadata import entry_points
for ep in entry_points(group='opm.pipeline'):
    print(ep.name, '->', ep.value)
"

# Get the active git revision of the plugin
git -C ~/dev/ovos/ovos-tool-calling-pipeline-plugin rev-parse --short HEAD
```

## When something is broken

A diagnostic checklist for "the plugin doesn't seem to be running":

1. **Service alive?** `systemctl --user is-active ovos-core.service` → `active`
2. **Plugin loaded?** `grep "ToolCallingPipeline loaded" ~/.local/state/mycroft/skills.log | tail -1`
3. **Plugin enabled?** Same log line — does it say "ENABLED" or "disabled"?
4. **In the pipeline list?** `grep tool-calling ~/.config/mycroft/mycroft.conf`
5. **Config valid?** `python -c "import json; json.load(open('$HOME/.config/mycroft/mycroft.conf'))"`
6. **Reaching `match_*`?** Tail and emit a test utterance; expect a "tool-calling" log line.
7. **LLM reachable?** `curl https://api.fireworks.ai/...` test.
8. **Right model?** Verify it returns `tool_calls`, not `reasoning_content`.

For "the plugin runs but doesn't dispatch":

1. Look for `dispatching <skill>:<intent>` in skill.log.
2. Look for the dispatched bus message: `grep "<skill_id>:<IntentName>" ~/.local/state/mycroft/skills.log`.
3. Verify the skill's handler ran: usually a logged INFO from inside the skill.
4. Verify TTS spoke: `grep Speak ~/.local/state/mycroft/audio.log`.

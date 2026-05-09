# Working on this plugin (instructions for AI sessions)

This file is loaded automatically by Claude Code when working in this repo. Read it in full before making changes.

## Read first

Before any non-trivial work, consult these in order:

1. **`docs/ROADMAP.md`** — the long-term plan and current milestone. Don't propose work that contradicts it without flagging.
2. **`docs/TODO.md`** — active short-term tasks. Pick from "Now" if no specific user request.
3. **`docs/ARCHITECTURE.md`** — OVOS topology. Necessary background for any change to dispatch logic.
4. **`docs/PIPELINE_PROTOCOL.md`** — the contract this plugin implements. Necessary for any change to `__init__.py`.
5. **`docs/INTENT_MATCHERS.md`** — Adapt and Padatious deep dives. Necessary for any change to schema generation or dispatch shapes.
6. **`docs/INSTALL_NOTES.md`** — known gotchas. Check this *first* when something seems broken — most surprises here are documented.
7. **`docs/DEV_LOOP.md`** — how to test and verify. Use the canonical commands; don't reinvent.

## Update these as you work

- **`docs/TODO.md`**: when you start a task, mark it `[~]` (in progress). When done, move to "Recently done" with the commit hash.
- **`docs/ROADMAP.md`**: only update when a milestone changes status (✅ shipped) or when a planned milestone needs revision.
- **`docs/INSTALL_NOTES.md`**: append a new section every time you diagnose a non-obvious OVOS quirk. Keep future sessions from re-discovering.
- **`docs/INTENT_MATCHERS.md` / `PIPELINE_PROTOCOL.md` / `ARCHITECTURE.md`**: these are reference docs; update when you discover something they got wrong, but don't bloat them with day-to-day work.

## Conventions

- **Editable install only.** The plugin lives at `~/dev/ovos/ovos-tool-calling-pipeline-plugin` and is `pip install -e`'d into `~/.venvs/ovos`. Never edit files under `~/.venvs/ovos/lib/.../site-packages/ovos_tool_calling/` — those are symlinks to here.
- **Restart after every change.** `systemctl --user restart ovos-core.service`. Wait until `"ovos-core is ready"` appears in the journal before testing.
- **Test by emitting on the bus.** Don't make the user say things into a microphone for round-trip tests. See `docs/DEV_LOOP.md` § "Sending test utterances".
- **Pure functions where possible.** Schema generation, gate decisions, dispatch synthesis — all live in submodules that take dataclasses and return dicts. Keep `__init__.py` thin.
- **Logger is `LOG` from `ovos_utils.log`.** Prefix all our log lines with `[tool-calling]` so they're greppable.
- **Bus debug events.** When adding a debug feature, emit a bus event named `tool-calling.<thing>.dump` and document it in `docs/DEV_LOOP.md`. Don't add HTTP servers or read stdin.
- **Defensive defaults.** Any LLM/network failure must return `None` so the rest of the pipeline runs. Never raise into the intent service.
- **Plugin disabled by default.** `enabled: false` until the user opts in. Don't change this default casually.

## Coding style

- Black-formatted (line length ~88).
- Type hints on public functions.
- Dataclasses for structured data (no plain dicts in interfaces).
- Single-responsibility modules:
  - `__init__.py` — pipeline plugin class, bus wiring
  - `schemas.py` — turn registry → tool schemas
  - `dispatch.py` — turn LLM tool call → IntentHandlerMatch
  - `llm.py` — HTTP client + config resolution
  - `gate.py` — admission control + cache

## Git

- Commit per milestone, not per file. Each commit message has a header describing what the version does plus a short bullet list of what changed.
- The author email is `clinton@concernednetizen.com` and the name is `Clinton`. Use these for commits unless the user is in their own Git context.
- Push to `origin/main` after each milestone. CI is not yet set up.
- Don't `--amend` published commits.

## Constraints from the upstream OVOS install

The user's environment uses these specific choices. **Don't change them as a side effect of plugin work; they're stable user decisions.**

- STT: `ovos-stt-plugin-fasterwhisper` with `model: small`
- TTS: `ovos-tts-plugin-piper` with voice `alan-low`
- LLM persona: `~/.config/ovos_persona/ovos-installer-llm.json` (Fireworks credentials)
- Plugin LLM model: `accounts/fireworks/models/gpt-oss-120b` (verified for tool calling)
- Pipeline placement: this plugin at `-low` priority (fallback mode, not orchestrator mode)
- Wake word: `hey_mycroft` via `ovos-ww-plugin-vosk`

## What NOT to do

- Don't suggest installing competing solutions (Home Assistant Voice, Willow, etc.) as a side-effect of plugin work. The user knows about those.
- Don't edit the user's `mycroft.conf` casually. Changes there require a service restart and can break the install.
- Don't run `pip install -U` or `pip install ovos-*` blindly. Many OVOS packages have tightly coupled dependencies (especially `ovos-workshop` major versions). See `docs/INSTALL_NOTES.md`.
- Don't claim work is done until you've verified the dispatch flow end-to-end (LLM call → match emitted → skill responds → TTS speaks).
- Don't add features outside the roadmap without flagging it as a roadmap update.

## Useful starting commands

```bash
# Status check
systemctl --user is-active ovos-core.service ovos-listener.service ovos-audio.service

# Tail relevant logs in another shell
journalctl --user -u ovos-core.service -f | grep -E "tool-calling|ovos_tool_calling"

# Latest git state
git -C ~/dev/ovos/ovos-tool-calling-pipeline-plugin log --oneline -5

# Active version banner
grep "ToolCallingPipeline loaded" ~/.local/state/mycroft/skills.log | tail -1

# Trigger a test dispatch
~/.venvs/ovos/bin/python -c "
from ovos_bus_client import MessageBusClient, Message
import time
b = MessageBusClient(); b.run_in_thread(); time.sleep(1)
b.emit(Message('recognizer_loop:utterance', {'utterances': ['set a five minute timer']}))
time.sleep(5); b.close()
"
```

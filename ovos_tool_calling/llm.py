"""Minimal OpenAI-compatible client for tool-calling chat completions.

We deliberately avoid the ``openai`` SDK so this plugin has no extra
dependency footprint beyond ``requests``. Streaming is *not* used here:
tool-calling output is a single response object, not a stream of deltas.

For the OVOS Installer LLM persona path, ``load_persona_credentials()``
reads ``~/.config/ovos_persona/<persona>.json`` and pulls the
``ovos-solver-openai-plugin`` block (api_url, key, system_prompt). The
plugin's own config can override individual fields (e.g. model).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from ovos_utils.log import LOG


@dataclass
class LLMConfig:
    api_url: str
    key: str
    model: str
    system_prompt: str = (
        "You are a voice assistant. The user has just spoken a request. "
        "If a tool fits, call exactly one tool with the appropriate arguments. "
        "If no tool fits, answer briefly in plain spoken English (one or two short sentences, no markdown)."
    )
    max_tokens: int = 300
    temperature: float = 0.2
    timeout_seconds: float = 15.0

    def is_usable(self) -> bool:
        return bool(self.api_url and self.key and self.model)


def _slugify_persona(name: str) -> str:
    """Match the slug ovos-persona uses on disk for a persona name."""
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


def load_persona_credentials(persona_name: str) -> Optional[Dict[str, Any]]:
    """Read ``ovos-solver-openai-plugin`` config from a persona file, if present.

    Looks at ``~/.config/ovos_persona/<slug>.json`` first, then a couple of
    historical locations. Returns the inner solver config block, or None.
    """
    slug = _slugify_persona(persona_name)
    candidates = [
        os.path.expanduser(f"~/.config/ovos_persona/{persona_name}.json"),
        os.path.expanduser(f"~/.config/ovos_persona/{slug}.json"),
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            LOG.warning("[tool-calling] failed reading persona %s: %s", path, e)
            continue
        # Persona file: {"name": ..., "<plugin-name>": {...}, "solvers": [...]}
        for key in data:
            if key.lower().startswith("ovos-solver-openai") or key.lower().startswith(
                "ovos-openai"
            ):
                return data[key]
    return None


def build_config(plugin_config: Dict[str, Any]) -> LLMConfig:
    """Resolve an ``LLMConfig`` from the plugin's own config block.

    Resolution order, with later entries overriding earlier ones:
      1. Defaults
      2. Persona file (if ``persona`` is set)
      3. The plugin config block itself (api_url/key/model/...)
    """
    cfg: Dict[str, Any] = {}

    persona_name = plugin_config.get("persona")
    if persona_name:
        creds = load_persona_credentials(persona_name) or {}
        if creds:
            cfg.update(
                {
                    "api_url": creds.get("api_url"),
                    "key": creds.get("key"),
                    "model": creds.get("model"),
                    "system_prompt": creds.get("system_prompt"),
                    "max_tokens": creds.get("max_tokens"),
                    "temperature": creds.get("temperature"),
                }
            )
        else:
            LOG.warning(
                "[tool-calling] persona '%s' not found; falling back to inline config",
                persona_name,
            )

    # Plugin-level overrides.
    for k in ("api_url", "key", "model", "system_prompt", "max_tokens", "temperature"):
        if k in plugin_config:
            cfg[k] = plugin_config[k]

    # Coerce numerics — mycroft.conf sometimes stores them as strings.
    if "max_tokens" in cfg and cfg["max_tokens"] is not None:
        cfg["max_tokens"] = int(cfg["max_tokens"])
    if "temperature" in cfg and cfg["temperature"] is not None:
        cfg["temperature"] = float(cfg["temperature"])

    # Drop None values so dataclass defaults apply.
    cfg = {k: v for k, v in cfg.items() if v is not None}
    return LLMConfig(**cfg)


@dataclass
class LLMToolCall:
    tool_name: str
    arguments: Dict[str, Any]


@dataclass
class LLMTextAnswer:
    text: str


def call_chat(
    config: LLMConfig,
    utterance: str,
    tools: List[Dict],
) -> Optional[Tuple[List[LLMToolCall], Optional[str]]]:
    """Call the chat-completions endpoint with tools.

    Returns ``(tool_calls, text)`` where:
      - ``tool_calls`` is a list of LLMToolCall (possibly empty)
      - ``text`` is the assistant's plain content if it returned one
    Returns None on transport / HTTP failure.
    """
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": config.system_prompt},
            {"role": "user", "content": utterance},
        ],
        "tools": tools,
        "tool_choice": "auto",
        "max_tokens": config.max_tokens,
        "temperature": config.temperature,
    }
    url = config.api_url.rstrip("/") + "/chat/completions"
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {config.key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=config.timeout_seconds,
        )
    except requests.RequestException as e:
        LOG.error("[tool-calling] LLM transport error: %s", e)
        return None
    if not resp.ok:
        LOG.error("[tool-calling] LLM HTTP %d: %s", resp.status_code, resp.text[:300])
        return None
    try:
        body = resp.json()
        message = body["choices"][0]["message"]
    except (ValueError, KeyError, IndexError) as e:
        LOG.error("[tool-calling] LLM response parse error: %s; body=%s", e, resp.text[:300])
        return None

    tool_calls: List[LLMToolCall] = []
    for tc in message.get("tool_calls") or []:
        try:
            fn = tc["function"]
            args_str = fn.get("arguments") or "{}"
            args = json.loads(args_str) if isinstance(args_str, str) else dict(args_str)
            tool_calls.append(LLMToolCall(tool_name=fn["name"], arguments=args))
        except (KeyError, json.JSONDecodeError) as e:
            LOG.warning("[tool-calling] failed parsing tool_call %r: %s", tc, e)

    text = message.get("content") or None
    return tool_calls, text

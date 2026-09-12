"""Provider-agnostic chat client over free-tier, OpenAI-compatible endpoints.

Gemini (Google AI Studio free key), Groq and OpenRouter free models all speak the
OpenAI chat-completions protocol, so one client covers them. Providers are tried in
order; a rate-limited or failing provider is cooled down and the next one is used.
If none work, callers fall back to the offline rule-based agents.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import openai
from openai import OpenAI

from app import config

log = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str
    cooldown_until: float = 0.0
    client: OpenAI | None = field(default = None, repr = False)

    def get_client(self) -> OpenAI:
        if self.client is None:
            self.client = OpenAI(api_key = self.api_key, base_url = self.base_url,
                                 timeout = config.LLM_TIMEOUT_SECONDS, max_retries = 1)
        return self.client


def _configured() -> list[Provider]:
    if config.LLM_MODE == "offline":
        return []
    out = []
    if config.GEMINI_API_KEY:
        out.append(Provider("gemini", config.GEMINI_BASE_URL, config.GEMINI_API_KEY, config.GEMINI_MODEL))
    if config.GROQ_API_KEY:
        out.append(Provider("groq", config.GROQ_BASE_URL, config.GROQ_API_KEY, config.GROQ_MODEL))
    if config.OPENROUTER_API_KEY:
        out.append(Provider("openrouter", config.OPENROUTER_BASE_URL, config.OPENROUTER_API_KEY,
                            config.OPENROUTER_MODEL))
    if config.OLLAMA_BASE_URL:
        out.append(Provider("ollama", config.OLLAMA_BASE_URL, "ollama", config.OLLAMA_MODEL))
    return out


PROVIDERS = _configured()
_lock = threading.Lock()


def status() -> dict[str, Any]:
    if not PROVIDERS:
        return {"mode": "offline", "providers": []}
    return {"mode": "llm", "providers": [{"name": p.name, "model": p.model,
                                          "cooling_down": p.cooldown_until > time.time()} for p in PROVIDERS]}


def _sanitize(messages: list[dict[str, Any]], provider: str) -> list[dict[str, Any]]:
    """Strip provider-specific extras (e.g. Gemini thought signatures) when switching providers."""
    out = []
    for m in messages:
        if m.get("role") != "assistant":
            out.append({k: v for k, v in m.items() if not k.startswith("_")})
            continue
        same = m.get("_provider") == provider
        clean: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
        if same and m.get("extra_content"):
            clean["extra_content"] = m["extra_content"]
        if m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                call = {"id": tc["id"], "type": "function",
                        "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}
                if same and tc.get("extra_content"):
                    call["extra_content"] = tc["extra_content"]
                calls.append(call)
            clean["tool_calls"] = calls
        out.append(clean)
    return out


def chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], str]:
    """Return (assistant_message_dict, provider_label). Raises LLMUnavailable if every provider fails."""
    errors = []
    for p in PROVIDERS:
        if p.cooldown_until > time.time():
            continue
        kwargs: dict[str, Any] = {"model": p.model, "messages": _sanitize(messages, p.name), "temperature": 0.2}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        try:
            resp = p.get_client().chat.completions.create(**kwargs)
            msg = resp.choices[0].message.model_dump(exclude_none = True)
            msg["_provider"] = p.name
            for i, tc in enumerate(msg.get("tool_calls") or []):
                tc.setdefault("id", f"call_{i}")
                if not tc.get("id"):
                    tc["id"] = f"call_{i}"
            return msg, f"{p.name}:{p.model}"
        except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError,
                openai.InternalServerError, openai.APIStatusError) as exc:
            with _lock:
                p.cooldown_until = time.time() + config.PROVIDER_COOLDOWN_SECONDS
            errors.append(f"{p.name}: {type(exc).__name__}: {str(exc)[:200]}")
            log.warning("provider %s failed: %s", p.name, exc)
    raise LLMUnavailable("; ".join(errors) or "no LLM provider configured")

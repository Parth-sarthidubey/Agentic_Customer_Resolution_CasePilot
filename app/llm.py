"""Provider-agnostic chat client over free-tier, OpenAI-compatible endpoints.

Gemini (Google AI Studio free key), Groq and OpenRouter free models all speak the
OpenAI chat-completions protocol, so one client covers them. Providers are tried in
order; a rate-limited or failing provider is cooled down and the next one is used.
If none work, callers fall back to the offline rule-based agents.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import openai
from openai import OpenAI

from app import bedrock, config

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
                                 timeout = config.LLM_TIMEOUT_SECONDS, max_retries = 2)
        return self.client


def _configured() -> list[Provider]:
    if config.LLM_MODE == "offline":
        return []
    out = []
    # Ordered by what a five-agent tool-calling pipeline actually needs: speed first, then
    # headroom. Groq answers in well under a second; Mistral's free tier is generous but takes
    # ~30s a call; Gemini is fast but allows only about ten requests a minute - one case.
    if config.GROQ_API_KEY:
        out.append(Provider("groq", config.GROQ_BASE_URL, config.GROQ_API_KEY, config.GROQ_MODEL))
    if config.MISTRAL_API_KEY:
        out.append(Provider("mistral", config.MISTRAL_BASE_URL, config.MISTRAL_API_KEY, config.MISTRAL_MODEL))
    if config.GEMINI_API_KEY:
        out.append(Provider("gemini", config.GEMINI_BASE_URL, config.GEMINI_API_KEY, config.GEMINI_MODEL))
    if config.OPENROUTER_API_KEY:
        out.append(Provider("openrouter", config.OPENROUTER_BASE_URL, config.OPENROUTER_API_KEY,
                            config.OPENROUTER_MODEL))
    if config.OLLAMA_BASE_URL:
        out.append(Provider("ollama", config.OLLAMA_BASE_URL, "ollama", config.OLLAMA_MODEL))
    return out


PROVIDERS = _configured()
_lock = threading.Lock()
_bedrock_state: bool | None = None


def _bedrock_ready() -> bool:
    global _bedrock_state
    if bedrock.capped():
        return False
    if _bedrock_state is None:
        _bedrock_state = config.LLM_MODE != "offline" and bedrock.available()
        if _bedrock_state:
            log.info("bedrock enabled: %s in %s", config.BEDROCK_MODEL, config.BEDROCK_REGION)
    return _bedrock_state


def status() -> dict[str, Any]:
    chain = []
    if _bedrock_ready():
        chain.append({"name": "bedrock", "model": config.BEDROCK_MODEL, "cooling_down": False})
    chain += [{"name": p.name, "model": p.model, "cooling_down": p.cooldown_until > time.time()}
              for p in PROVIDERS]
    if not chain:
        return {"mode": "offline", "providers": []}
    return {"mode": "llm", "providers": chain}


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


def _salvage_tool_use_failure(exc: Exception) -> str | None:
    """Recover the answer from a 'tool_use_failed' 400.

    Some models (notably gpt-oss on Groq) return their final JSON as a tool call named `json`,
    which the API rejects because no such tool was offered - but it echoes the generation back
    in `failed_generation`. That text is the answer we asked for, so use it instead of failing.
    """
    body = getattr(exc, "body", None) or {}
    if not isinstance(body, dict):
        return None
    # Providers differ: some nest the detail under "error", others return it at the top level.
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    if err.get("code") != "tool_use_failed":
        return None
    raw = err.get("failed_generation")
    if not isinstance(raw, str):
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    args = obj.get("arguments") if isinstance(obj, dict) else None
    return json.dumps(args) if isinstance(args, dict) else raw


def _usable() -> list[Provider]:
    """Providers to try, in order. If every one is cooling down, still try the closest to ready:
    degrading to a slow or flaky model beats silently dropping the whole case to offline rules."""
    now = time.time()
    ready = [p for p in PROVIDERS if p.cooldown_until <= now]
    return ready or sorted(PROVIDERS, key = lambda p: p.cooldown_until)[:1]


def chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], str]:
    """Return (assistant_message_dict, provider_label). Raises LLMUnavailable if every provider fails."""
    errors = []

    # Bedrock first when it is configured: it is the only option here without a
    # tokens-per-minute ceiling that a five-agent tool-calling pipeline blows through.
    if _bedrock_ready():
        try:
            return bedrock.chat(messages, tools), f"bedrock:{config.BEDROCK_MODEL}"
        except Exception as exc:
            errors.append(f"bedrock: {str(exc)[:200]}")
            log.warning("bedrock failed, falling through to the API providers: %s", str(exc)[:300])

    for p in _usable():
        kwargs: dict[str, Any] = {"model": p.model, "messages": _sanitize(messages, p.name), "temperature": 0.2}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        for attempt in range(1, config.TRANSIENT_RETRIES + 1):
            try:
                resp = p.get_client().chat.completions.create(**kwargs)
                msg = resp.choices[0].message.model_dump(exclude_none = True)
                msg["_provider"] = p.name
                for i, tc in enumerate(msg.get("tool_calls") or []):
                    tc.setdefault("id", f"call_{i}")
                    if not tc.get("id"):
                        tc["id"] = f"call_{i}"
                return msg, f"{p.name}:{p.model}"
            except openai.RateLimitError as exc:
                # A real quota signal - stop asking this provider for a while.
                with _lock:
                    p.cooldown_until = time.time() + config.RATE_LIMIT_COOLDOWN_SECONDS
                errors.append(f"{p.name}: rate limited")
                log.warning("provider %s rate limited, cooling down", p.name)
                break
            except openai.BadRequestError as exc:
                salvaged = _salvage_tool_use_failure(exc)
                if salvaged is not None:
                    log.info("provider %s returned its answer as a rejected tool call; recovered it", p.name)
                    return {"role": "assistant", "content": salvaged, "_provider": p.name}, f"{p.name}:{p.model}"
                # A malformed request will fail identically on retry - move to the next provider.
                errors.append(f"{p.name}: BadRequest: {str(exc)[:200]}")
                log.warning("provider %s rejected the request: %s", p.name, str(exc)[:300])
                break
            except (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError,
                    openai.APIStatusError) as exc:
                if attempt < config.TRANSIENT_RETRIES:
                    delay = config.TRANSIENT_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.4)
                    log.info("provider %s transient error (attempt %d/%d), retrying in %.1fs: %s",
                             p.name, attempt, config.TRANSIENT_RETRIES, delay, str(exc)[:120])
                    time.sleep(delay)
                    continue
                with _lock:
                    p.cooldown_until = time.time() + config.TRANSIENT_COOLDOWN_SECONDS
                errors.append(f"{p.name}: {type(exc).__name__}: {str(exc)[:200]}")
                log.warning("provider %s failed after %d attempts: %s", p.name, attempt, exc)
    raise LLMUnavailable("; ".join(errors) or "no LLM provider configured")

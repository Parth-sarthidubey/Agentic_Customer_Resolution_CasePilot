"""AWS Bedrock provider, spoken through the Converse API.

The rest of CasePilot passes messages and tools in OpenAI's chat-completions shape, because
every free-tier provider understands it. Bedrock does not - it has its own Converse format -
so this module translates in both directions and hands back an OpenAI-shaped assistant
message, letting `app/llm.py` treat Bedrock like any other provider.

Bedrock matters because the free tiers meter tokens per minute (Groq allows 8k/min, which a
five-agent tool-calling pipeline exhausts in one case). Bedrock on-demand does not, so the
agents can actually run.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from app import config

_client_lock = threading.Lock()
_client: Any = None


class BedrockUnavailable(RuntimeError):
    pass


def available() -> bool:
    """True when Bedrock is switched on and its own credentials are present."""
    if not (config.BEDROCK_ENABLED and config.BEDROCK_ACCESS_KEY_ID and config.BEDROCK_SECRET_ACCESS_KEY):
        return False
    try:
        get_client()
        return True
    except Exception:
        return False


def get_client() -> Any:
    """Bedrock client built from CasePilot's own credentials only.

    Deliberately does NOT use boto3's default credential chain. That chain would silently pick up
    whatever AWS identity happens to be configured on the machine - a work profile, an SSO session,
    an instance role - and bill somebody else's account for this app's traffic. Credentials must be
    given to CasePilot explicitly, or Bedrock stays off.
    """
    global _client
    with _client_lock:
        if _client is None:
            if not (config.BEDROCK_ACCESS_KEY_ID and config.BEDROCK_SECRET_ACCESS_KEY):
                raise BedrockUnavailable("BEDROCK_ACCESS_KEY_ID / BEDROCK_SECRET_ACCESS_KEY are not set")
            import boto3  # imported lazily so the app runs without AWS installed/configured
            _client = boto3.client(
                "bedrock-runtime",
                region_name = config.BEDROCK_REGION,
                aws_access_key_id = config.BEDROCK_ACCESS_KEY_ID,
                aws_secret_access_key = config.BEDROCK_SECRET_ACCESS_KEY,
                aws_session_token = config.BEDROCK_SESSION_TOKEN or None,
            )
        return _client


def _to_converse(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """OpenAI-shaped messages -> (Converse messages, system blocks)."""
    system: list[dict[str, str]] = []
    out: list[dict[str, Any]] = []

    for m in messages:
        role = m.get("role")
        if role == "system":
            system.append({"text": m.get("content") or ""})
            continue

        if role == "tool":
            # Converse carries tool results as a user turn holding toolResult blocks.
            block = {"toolResult": {"toolUseId": m.get("tool_call_id") or m.get("name") or "call",
                                    "content": [{"text": str(m.get("content") or "")}]}}
            if out and out[-1]["role"] == "user" and any("toolResult" in c for c in out[-1]["content"]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue

        if role == "assistant":
            content: list[dict[str, Any]] = []
            if m.get("content"):
                content.append({"text": m["content"]})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                content.append({"toolUse": {"toolUseId": tc.get("id") or fn.get("name") or "call",
                                            "name": fn.get("name"), "input": args}})
            if not content:
                content = [{"text": "."}]  # Converse rejects an empty turn
            out.append({"role": "assistant", "content": content})
            continue

        out.append({"role": "user", "content": [{"text": m.get("content") or ""}]})

    # Converse requires the conversation to open with a user turn.
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"text": "Begin."}]})
    return out, system


def _to_tool_config(tools: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not tools:
        return None
    specs = []
    for t in tools:
        fn = t.get("function") or {}
        specs.append({"toolSpec": {
            "name": fn.get("name"),
            "description": (fn.get("description") or fn.get("name") or "")[:512],
            "inputSchema": {"json": fn.get("parameters") or {"type": "object", "properties": {}}},
        }})
    return {"tools": specs}


def chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Call Bedrock and return an OpenAI-shaped assistant message."""
    conv, system = _to_converse(messages)
    params: dict[str, Any] = {
        "modelId": config.BEDROCK_MODEL,
        "messages": conv,
        "inferenceConfig": {"maxTokens": 2048, "temperature": 0.2},
    }
    if system:
        params["system"] = system
    tool_config = _to_tool_config(tools)
    if tool_config:
        params["toolConfig"] = tool_config

    resp = get_client().converse(**params)
    content = (resp.get("output") or {}).get("message", {}).get("content", []) or []

    text_parts, tool_calls = [], []
    for i, block in enumerate(content):
        if "text" in block:
            text_parts.append(block["text"])
        elif "toolUse" in block:
            tu = block["toolUse"]
            tool_calls.append({"id": tu.get("toolUseId") or f"call_{i}", "type": "function",
                               "function": {"name": tu.get("name"),
                                            "arguments": json.dumps(tu.get("input") or {})}})

    msg: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts), "_provider": "bedrock"}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg

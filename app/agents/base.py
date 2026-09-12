"""Minimal tool-calling agent loop with live trace events and validated JSON output."""

from __future__ import annotations

import inspect
import json
import re
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from app import config, db, llm
from app.tools.read_tools import Tool

M = TypeVar("M", bound = BaseModel)


class Tracer:
    """Writes agent activity to the ticket's event stream (rendered live in the UI)."""

    def __init__(self, ticket_id: int, agent: str):
        self.ticket_id, self.agent = ticket_id, agent

    def emit(self, type_: str, **payload: Any) -> None:
        db.add_event(self.ticket_id, self.agent, type_, payload)


def load_prompt(name: str) -> str:
    return (config.PROMPTS_DIR / f"{name}.md").read_text(encoding = "utf-8")


def _extract_json(text: str) -> dict[str, Any] | None:
    text = re.sub(r"```(?:json)?", "", text or "")
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None


def call_tool(tool: Tool, args: dict[str, Any], tracer: Tracer) -> Any:
    accepted = inspect.signature(tool.fn).parameters
    clean = {k: v for k, v in args.items() if k in accepted}
    tracer.emit("tool_call", tool = tool.name, args = clean)
    try:
        result = tool.fn(**clean)
    except Exception as exc:  # tools must never crash the agent
        result = {"error": f"{type(exc).__name__}: {exc}"}
    tracer.emit("tool_result", tool = tool.name, result = result)
    return result


def run_llm_agent(tracer: Tracer, system: str, user: str, tools: list[Tool], output_model: type[M]) -> M:
    """ReAct-style loop: the model calls tools until it replies with JSON matching output_model."""
    by_name = {t.name: t for t in tools}
    schema_hint = json.dumps(output_model.model_json_schema().get("properties", {}), indent = 1)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": f"{system}\n\n## Output\nWhen finished, reply with ONLY one JSON object "
                                      f"with these fields (no prose, no markdown):\n{schema_hint}"},
        {"role": "user", "content": user},
    ]
    specs = [t.spec() for t in tools] or None
    for _step in range(config.MAX_AGENT_STEPS):
        msg, provider = llm.chat(messages, specs)
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if msg.get("content") and calls:
            tracer.emit("thought", text = msg["content"][:1200], provider = provider)
        if calls:
            for tc in calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name in by_name:
                    result = call_tool(by_name[name], args, tracer)
                else:
                    result = {"error": f"unknown tool '{name}'"}
                content = json.dumps(result, default = str)[:config.TOOL_RESULT_MAX_CHARS]
                messages.append({"role": "tool", "tool_call_id": tc["id"], "name": name, "content": content})
            continue
        data = _extract_json(msg.get("content") or "")
        if data is not None:
            try:
                out = output_model.model_validate(data)
                tracer.emit("output", data = out.model_dump(), provider = provider)
                return out
            except ValidationError as exc:
                problem = str(exc)[:800]
        else:
            problem = "no JSON object found"
        messages.append({"role": "user", "content": f"Your reply was not valid ({problem}). Reply with ONLY "
                                                    f"the JSON object in the required shape."})
    raise llm.LLMUnavailable("agent did not produce a valid answer within the step limit")


def run_agent(ticket_id: int, agent: str, system: str, user: str, tools: list[Tool],
              output_model: type[M], offline: Callable[[Tracer], M]) -> M:
    """Run with the LLM; transparently fall back to the offline rule engine if no model is usable."""
    tracer = Tracer(ticket_id, agent)
    if llm.PROVIDERS:
        try:
            return run_llm_agent(tracer, system, user, tools, output_model)
        except llm.LLMUnavailable as exc:
            tracer.emit("info", text = f"LLM unavailable ({str(exc)[:240]}). Falling back to offline rules.")
    out = offline(tracer)
    tracer.emit("output", data = out.model_dump(), provider = "offline-rules")
    return out

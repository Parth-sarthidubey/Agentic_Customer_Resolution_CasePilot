"""Minimal tool-calling agent loop with live trace events and validated JSON output."""

from __future__ import annotations

import inspect
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
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


def _invoke(tool: Tool, args: dict[str, Any]) -> Any:
    accepted = inspect.signature(tool.fn).parameters
    clean = {k: v for k, v in args.items() if k in accepted}
    try:
        return tool.fn(**clean)
    except Exception as exc:  # tools must never crash the agent
        return {"error": f"{type(exc).__name__}: {exc}"}


def _clean_args(tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
    accepted = inspect.signature(tool.fn).parameters
    return {k: v for k, v in args.items() if k in accepted}


def call_tool(tool: Tool, args: dict[str, Any], tracer: Tracer) -> Any:
    tracer.emit("tool_call", tool = tool.name, args = _clean_args(tool, args))
    result = _invoke(tool, args)
    tracer.emit("tool_result", tool = tool.name, result = result)
    return result


def call_tools(requests: list[tuple[str, Tool | None, dict[str, Any]]], tracer: Tracer) -> list[Any]:
    """Run one round of tool calls concurrently.

    A model routinely asks for several independent reads at once (order, customer, tracking,
    policy). They do not depend on each other, so running them in parallel removes most of a
    round's latency. Every call is announced before the batch starts, so the work log shows the
    agent reaching for all of them at once rather than trickling.
    """
    for name, tool, args in requests:
        tracer.emit("tool_call", tool = name, args = _clean_args(tool, args) if tool else args)

    results: list[Any] = [None] * len(requests)
    runnable = [(i, t, a) for i, (_, t, a) in enumerate(requests) if t is not None]
    for i, (name, tool, _) in enumerate(requests):
        if tool is None:
            results[i] = {"error": f"unknown tool '{name}'. Do NOT call actions (like '{name}') as tool calls! Actions are not executable tools for you; place them strictly inside the 'actions' list of your final JSON response plan."}

    if runnable:
        with ThreadPoolExecutor(max_workers = min(len(runnable), config.TOOL_PARALLELISM)) as pool:
            futures = {pool.submit(_invoke, t, a): i for i, t, a in runnable}
            for fut in as_completed(futures):
                results[futures[fut]] = fut.result()

    for (name, _, _), result in zip(requests, results):
        tracer.emit("tool_result", tool = name, result = result)
    return results


def _schema_hint(model: type[BaseModel]) -> str:
    """Compact field list for the prompt.

    The full JSON schema is resent on every call, so it is one of the largest fixed costs in a
    run. Pretty-printing it and repeating pydantic's boilerplate buys nothing - the model needs
    the field names, their types and any fixed choices.
    """
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})

    def describe(spec: dict[str, Any]) -> str:
        if "$ref" in spec:
            spec = defs.get(spec["$ref"].rsplit("/", 1)[-1], {})
        if spec.get("enum"):
            return "|".join(str(v) for v in spec["enum"])
        for key in ("anyOf", "oneOf"):
            if key in spec:
                parts = [describe(s) for s in spec[key] if s.get("type") != "null"]
                return (parts[0] if parts else "string") + "?"
        t = spec.get("type", "string")
        if t == "array":
            return f"[{describe(spec.get('items') or {})}]"
        if t == "object":
            return "object"
        return t

    lines = []
    for name, spec in (schema.get("properties") or {}).items():
        note = spec.get("description")
        lines.append(f"{name}: {describe(spec)}" + (f"  # {note}" if note else ""))
    return "\n".join(lines)


def run_llm_agent(tracer: Tracer, system: str, user: str, tools: list[Tool], output_model: type[M]) -> M:
    """ReAct-style loop: the model calls tools until it replies with JSON matching output_model."""
    by_name = {t.name: t for t in tools}
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": f"{system}\n\nToday is {date.today().isoformat()}.\n\n## Output\n"
                                      f"When finished, reply with ONLY one JSON object with these fields "
                                      f"(no prose, no markdown):\n{_schema_hint(output_model)}"},
        {"role": "user", "content": user},
    ]
    specs = [t.spec() for t in tools] or None
    for _step in range(config.MAX_AGENT_STEPS):
        # If the last message was a JSON fix request, remove tool specs to force JSON output
        current_specs = None if (messages and messages[-1]["role"] == "user" and "not valid" in messages[-1].get("content", "")) else specs
        msg, provider = llm.chat(messages, current_specs)
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if msg.get("content") and calls:
            tracer.emit("thought", text = msg["content"][:1200], provider = provider)
        if calls:
            requests = []
            for tc in calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                requests.append((name, by_name.get(name), args))
            for tc, result in zip(calls, call_tools(requests, tracer)):
                content = json.dumps(result, default = str)[:config.TOOL_RESULT_MAX_CHARS]
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "name": tc["function"]["name"], "content": content})
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

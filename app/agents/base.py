"""Minimal tool-calling agent loop with live trace events and validated JSON output."""

from __future__ import annotations

import inspect
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from app import config, db, llm
from app.tools.read_tools import Tool

log = logging.getLogger(__name__)

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
    if not text:
        return None
    clean = re.sub(r"```(?:json)?", "", text)
    clean = re.sub(r"</?json>", "", clean)
    try:
        data = json.loads(clean.strip())
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(clean):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(clean[i:])
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


def run_llm_agent(tracer: Tracer, system: str, user: str, tools: list[Tool], output_model: type[M],
                  salvage: Callable[[str], dict[str, Any]] | None = None) -> M:
    """ReAct-style loop: the model calls tools until it replies with JSON matching output_model.

    `salvage` is for the agent whose prose is already the answer. The Communicator's job is to
    write a message to a customer; when it writes one and forgets the JSON envelope, throwing that
    away and sending a templated line instead makes the product worse, not safer. Agents whose
    output drives actions get no salvage - there, a malformed answer must fail.
    """
    by_name = {t.name: t for t in tools}
    last_text = ""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": f"{system}\n\nToday is {date.today().isoformat()}.\n\n## Output\n"
                                      f"When finished, reply with ONLY one JSON object with these fields "
                                      f"(no prose, no markdown):\n{_schema_hint(output_model)}"},
        {"role": "user", "content": user},
    ]
    specs = [t.spec() for t in tools] or None
    seen: dict[tuple[str, str], Any] = {}
    forced = False
    problem = "no answer attempted"
    for _step in range(config.MAX_AGENT_STEPS):
        # Two reasons to take the tools away and demand the answer:
        #   - the last reply was invalid JSON, so offering tools invites another detour;
        #   - the step budget is nearly spent. Running it to zero means falling back to the rule
        #     engine, which is far worse than answering from what has already been gathered.
        retrying_json = bool(messages and messages[-1]["role"] == "user"
                             and "not valid" in (messages[-1].get("content") or ""))
        out_of_road = _step >= config.MAX_AGENT_STEPS - 2
        if out_of_road and not forced:
            forced = True
            messages.append({"role": "user", "content":
                             "Stop calling tools. Answer now with the JSON object, using only what "
                             "you have already gathered. If something is still unknown, say so in "
                             "the fields rather than looking it up."})
        current_specs = None if (retrying_json or out_of_road) else specs
        msg, provider = llm.chat(messages, current_specs)
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if msg.get("content") and calls:
            tracer.emit("thought", text = msg["content"][:1200], provider = provider)
        if calls:
            requests, cached = [], {}
            for i, tc in enumerate(calls):
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                sig = (name, json.dumps(args, sort_keys = True, default = str))
                if sig in seen:
                    # A model that repeats a call it already made will keep repeating it until the
                    # step budget is gone and the agent falls back to the rule engine. Answer from
                    # what it already has, and say so, so it moves on.
                    cached[i] = {"note": "identical call already made in this run - reusing the "
                                         "earlier result; move on and answer", "result": seen[sig]}
                else:
                    requests.append((i, name, by_name.get(name), args, sig))

            results: dict[int, Any] = dict(cached)
            if requests:
                fresh = call_tools([(n, t, a) for _, n, t, a, _ in requests], tracer)
                for (i, _, _, _, sig), res in zip(requests, fresh):
                    results[i] = res
                    seen[sig] = res

            for tc, result in zip(calls, [results[i] for i in range(len(calls))]):
                content = json.dumps(result, default = str)[:config.TOOL_RESULT_MAX_CHARS]
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "name": tc["function"]["name"], "content": content})
            continue
        text = msg.get("content") or ""
        if text.strip():
            last_text = text
        data = _extract_json(text)
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
    if salvage and last_text.strip():
        try:
            out = output_model.model_validate(salvage(last_text))
            tracer.emit("output", data = out.model_dump(), provider = f"{provider} (prose salvaged)")
            return out
        except ValidationError:
            pass
    raise llm.LLMUnavailable(f"no valid answer within the step limit - last problem: {problem[:300]}")


def run_agent(ticket_id: int, agent: str, system: str, user: str, tools: list[Tool],
              output_model: type[M], offline: Callable[[Tracer], M],
              salvage: Callable[[str], dict[str, Any]] | None = None) -> M:
    """Run with the LLM; transparently fall back to the offline rule engine if no model is usable."""
    tracer = Tracer(ticket_id, agent)
    if llm.PROVIDERS or llm._bedrock_ready():
        try:
            return run_llm_agent(tracer, system, user, tools, output_model, salvage)
        except llm.LLMUnavailable as exc:
            # Loud on purpose. A silent fallback produces plausible output from hardcoded rules,
            # which hides a broken model path for as long as nobody reads the small print.
            reason = str(exc)[:240]
            log.warning("%s fell back to offline rules: %s", agent, reason)
            tracer.emit("fallback", agent = agent, reason = reason,
                        text = f"No model could answer for the {agent} ({reason}). "
                               f"This step ran on deterministic rules, not an LLM.")
    out = offline(tracer)
    tracer.emit("output", data = out.model_dump(), provider = "offline-rules")
    return out

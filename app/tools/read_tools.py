"""Read-only tools agents can call against the enterprise sandbox. None of them change state."""

from __future__ import annotations

import base64
import inspect
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app import config, db, llm
from app.sandbox import policy, services
from app.sandbox.services import ServiceError


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., Any]

    def spec(self) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


def _safe(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Service refusals are information for the agent, not crashes."""
    def wrapper(**kwargs: Any) -> Any:
        try:
            return fn(**kwargs)
        except ServiceError as exc:
            return exc.as_dict()
    wrapper.__signature__ = inspect.signature(fn)
    return wrapper


S, N = {"type": "string"}, {"type": "number"}

T_FIND_CUSTOMER = Tool("find_customer", "Find customers by email, customer id or name.",
                       _obj({"query": S}, ["query"]), _safe(services.find_customer))
T_GET_CUSTOMER = Tool("get_customer", "Customer profile: tier, claims in last 90 days, last goodwill credit, "
                      "order history, store credit issued.", _obj({"customer_id": S}, ["customer_id"]),
                      _safe(services.get_customer))
T_GET_ORDER = Tool("get_order", "Order details: status, dates, days_since_delivery, items (price, category, "
                   "final_sale), payment (captured/refunded), refunds, shipments.", _obj({"order_id": S}, ["order_id"]),
                   _safe(services.get_order))
T_INVENTORY = Tool("check_inventory", "Available stock of a SKU per warehouse, with transit days and estimated "
                   "arrival for the customer's region (east/west/central).",
                   _obj({"sku": S, "region": S}, ["sku"]), _safe(services.check_inventory))
T_TRACK = Tool("track_shipment", "Carrier tracking for a shipment: status, events, ETA, days_past_eta, proof.",
               _obj({"shipment_id": S}, ["shipment_id"]), _safe(services.track_shipment))
T_GATEWAY = Tool("payment_gateway_status", "Current health of the payment gateway.", _obj({}),
                 _safe(services.gateway_status))
T_POLICY = Tool("search_policy", "Search the customer-service policy handbook. Returns policy ids and text.",
                _obj({"query": S}, ["query"]), lambda query: policy.search_policy(query))
T_TODAY = Tool("get_today", "Today's date (YYYY-MM-DD).", _obj({}), lambda: {"today": services.today()})


def _past_cases(query: str) -> list[dict[str, Any]]:
    words = {w for w in query.lower().split() if len(w) > 3}
    scored = []
    for k in db.list_kb():
        text = f"{k['title']} {k['situation']} {k['tags']}".lower()
        score = sum(w in text for w in words)
        if score:
            scored.append((score, k))
    scored.sort(key = lambda s: s[0], reverse = True)
    return [{"case": k["case_number"], "title": k["title"], "resolution": k["resolution"]}
            for _, k in scored[:config.KB_TOP_K]]


T_PAST = Tool("search_past_cases", "Search resolved past cases (learned memory) for similar situations.",
              _obj({"query": S}, ["query"]), _past_cases)

# Tool schemas are resent on every call, so each agent carries only what its job needs.
# Today's date is injected into every system prompt instead of costing a tool and a round trip.
INTAKE_TOOLS = [T_FIND_CUSTOMER, T_GET_CUSTOMER, T_GET_ORDER]
INVESTIGATION_TOOLS = [T_GET_CUSTOMER, T_GET_ORDER, T_TRACK, T_INVENTORY, T_POLICY, T_PAST]
PLANNING_TOOLS = [T_GET_ORDER, T_INVENTORY, T_POLICY]
AUDIT_TOOLS = [T_POLICY, T_GET_ORDER, T_INVENTORY]


# --- Case-scoped tools (conversation + attachments) ---------------------------------------

def _describe_image(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    data = base64.b64encode(path.read_bytes()).decode()
    msg = [{"role": "user", "content": [
        {"type": "text", "text": "You are checking evidence for a retail support claim. Describe what this image "
                                 "shows in 2 sentences: the product, and any visible damage, defect or mismatch."},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}]}]
    try:
        out, provider = llm.chat(msg, None)
        return f"{out.get('content', '').strip()} (vision: {provider})"
    except llm.LLMUnavailable:
        return "Image received; automatic visual analysis unavailable (no vision-capable model reachable)."


def case_tools(case_id: int) -> list[Tool]:
    def conversation() -> dict[str, Any]:
        return {"messages": [{"from": m["author"], "sender": m["sender"], "body": m["body"]}
                             for m in db.messages(case_id, include_internal = False)],
                "attachments": [{"attachment_id": a["id"], "filename": a["filename"], "type": a["content_type"],
                                 "size": a["size"]} for a in db.attachments(case_id)]}

    def inspect_attachment(attachment_id: int) -> dict[str, Any]:
        a = db.query_one("SELECT * FROM attachments WHERE id = ? AND case_id = ?", (int(attachment_id), case_id))
        if not a:
            return {"error": "attachment not found on this case"}
        path = Path(a["path"])
        if (a["content_type"] or "").startswith("image/"):
            return {"filename": a["filename"], "analysis": _describe_image(path)}
        text = path.read_text(encoding = "utf-8", errors = "replace")
        return {"filename": a["filename"], "content": text[:4000], "truncated": len(text) > 4000}

    return [
        Tool("get_case_conversation", "The customer's messages on this case and the list of attachments.",
             _obj({}), conversation),
        Tool("inspect_attachment", "Read a text attachment or visually analyse an image attachment (e.g. damage photo).",
             _obj({"attachment_id": {"type": "integer"}}, ["attachment_id"]), inspect_attachment),
    ]

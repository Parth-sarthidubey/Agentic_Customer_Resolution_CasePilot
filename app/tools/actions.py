"""State-changing action catalogue. Agents propose these; only the Executor runs them."""

from __future__ import annotations

from typing import Any, Callable

from app.sandbox import services

CATALOG: dict[str, dict[str, Any]] = {
    "refund": {
        "fn": services.refund, "params": {"order_id": "order id", "amount": "amount to refund", "reason": "short reason"},
        "description": "Refund to the original payment method. Never more than captured minus already refunded.",
    },
    "create_replacement": {
        "fn": services.create_replacement,
        "params": {"order_id": "order id", "sku": "sku to replace", "warehouse_id": "warehouse to ship from"},
        "description": "Reserve stock and create a replacement shipment (also used to re-ship lost packages).",
    },
    "create_return_label": {
        "fn": services.create_return_label, "params": {"order_id": "order id", "sku": "sku being returned"},
        "description": "Issue a prepaid return label for an item.",
    },
    "cancel_order": {
        "fn": services.cancel_order, "params": {"order_id": "order id", "reason": "short reason"},
        "description": "Cancel an unshipped order (full refund is automatic). Fails if the order already shipped.",
    },
    "issue_store_credit": {
        "fn": services.issue_store_credit,
        "params": {"customer_id": "customer id", "amount": "credit amount", "reason": "short reason"},
        "description": "Issue goodwill store credit (policy POL-GW-1 limits apply).",
    },
}


def catalog_for_prompt() -> str:
    lines = []
    for name, a in CATALOG.items():
        params_str = ", ".join(f'"{k}": "{v}"' for k, v in a['params'].items())
        lines.append(f"- action: \"{name}\" (params: {{{params_str}}}) - {a['description']}")
    return "\n".join(lines)


def validate(actions: list[dict[str, Any]]) -> list[str]:
    errors = []
    for i, a in enumerate(actions, 1):
        name = a.get("action")
        if name not in CATALOG:
            errors.append(f"action #{i}: unknown action '{name}'")
            continue
        missing = [k for k in CATALOG[name]["params"] if k not in (a.get("params") or {})]
        if missing:
            errors.append(f"action #{i} ({name}): missing params {missing}")
    return errors


def execute(name: str, params: dict[str, Any], case_ref: str, attempt_key: str) -> dict[str, Any]:
    fn: Callable[..., dict[str, Any]] = CATALOG[name]["fn"]
    kwargs = {k: params[k] for k in CATALOG[name]["params"]}
    if name == "refund":
        kwargs["idempotency_key"] = attempt_key
    if name == "issue_store_credit":
        kwargs["case_ref"] = case_ref
    return fn(**kwargs)

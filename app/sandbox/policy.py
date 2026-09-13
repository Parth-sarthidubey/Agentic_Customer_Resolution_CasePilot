"""Policy handbook retrieval and the deterministic approval gate.

The LLM may *read* policy, but whether a plan needs a human is decided here, in code.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from functools import lru_cache
from typing import Any, Callable

from app import config

_WORD = re.compile(r"[a-z0-9]{3,}")


@lru_cache(maxsize = 1)
def _docs() -> list[dict[str, str]]:
    return json.loads((config.SEED_DIR / "policies.json").read_text(encoding = "utf-8"))


def search_policy(query: str, top_k: int = 3) -> list[dict[str, str]]:
    terms = Counter(_WORD.findall(query.lower()))
    scored = []
    for d in _docs():
        body = f"{d['title']} {d['text']}".lower()
        score = sum((3 if t in d["tags"] else 0) + (1 if t in body else 0) for t in terms)
        if score:
            scored.append((score, d))
    scored.sort(key = lambda s: s[0], reverse = True)
    return [{"policy_id": d["id"], "title": d["title"], "text": d["text"]} for _, d in scored[:top_k]]


def get_policy(policy_id: str) -> dict[str, str] | None:
    return next(({"policy_id": d["id"], "title": d["title"], "text": d["text"]} for d in _docs()
                 if d["id"] == policy_id), None)


def approval_reasons(actions: list[dict[str, Any]], risk_flags: list[str],
                     customer: dict[str, Any] | None = None, order: dict[str, Any] | None = None,
                     intake: dict[str, Any] | None = None) -> list[str]:
    """Deterministic guardrail: which parts of a plan need a human before execution.

    `risk_flags` is the Investigator's prose and is deliberately *not* what decides this. It used
    to be: any flag containing "fraud" or "claims" tripped POL-FRAUD-1, so the flag "No explicit
    delivery dispute or fraud indicators" - which says the opposite - held up a correct
    re-shipment for human approval. Negation is exactly what substring matching cannot see.
    POL-FRAUD-1 states two objective conditions, and both are in the records.
    """
    reasons = []
    refund_total = sum(float(a["params"].get("amount", 0)) for a in actions if a["action"] == "refund")
    credit_total = sum(float(a["params"].get("amount", 0)) for a in actions if a["action"] == "issue_store_credit")
    if refund_total > config.AUTO_REFUND_LIMIT:
        reasons.append(f"refund total {refund_total:.2f} exceeds auto-approval limit {config.AUTO_REFUND_LIMIT:.2f} (POL-REF-1)")
    if credit_total > config.AUTO_CREDIT_LIMIT:
        reasons.append(f"store credit {credit_total:.2f} exceeds {config.AUTO_CREDIT_LIMIT:.2f} (POL-GW-1)")
    # POL-FRAUD-1, first condition: three or more claims in the last 90 days.
    claims = int((customer or {}).get("claims_90d") or 0)
    if claims >= 3:
        reasons.append(f"POL-FRAUD-1: {claims} claims in the last 90 days")

    # POL-FRAUD-1, second condition: the customer disputes a delivery the carrier marked
    # delivered with photo proof.
    goal = (intake or {}).get("goal") or ""
    # Only a non-delivery claim is a delivery dispute. Asking where a refund has got to for an
    # item you returned is not, even though the order is also marked delivered with proof.
    if goal == "where_is_my_order" and order:
        for sh in order.get("shipments") or []:
            if sh.get("kind") == "outbound" and sh.get("status") == "delivered" and sh.get("proof"):
                reasons.append(f"POL-FRAUD-1: non-delivery claim against {sh['id']}, which the "
                               f"carrier marked delivered ({sh['proof']})")
                break
    return reasons


# --- Deterministic plan review (replaces the LLM Policy Auditor) -----------------------
#
# The handbook rules that decide whether a plan may run are arithmetic and date comparisons,
# not judgement: a window, a ledger balance, a stock level, an ETA. Asking a model to re-derive
# them each time was slower, cost a third of the LLM calls in a case, duplicated the reads the
# Resolver had already done - and deadlocked, because a weak model kept objecting to plans that
# broke no rule. Doing it in code is faster, cannot be talked out of a rule by a prompt, and
# gives the Resolver a precise, actionable objection instead of an opinion.

_ELECTRONICS = {"electronics"}
_GOODWILL_CAP = {"platinum": 50.0, "gold": 25.0}


def review_plan(plan: dict[str, Any], order: dict[str, Any], customer: dict[str, Any],
                intake: dict[str, Any], inventory: Callable[[str, str], dict[str, Any]] | None = None,
                ) -> list[str]:
    """Return a list of concrete policy breaches. Empty means the plan may proceed."""
    issues: list[str] = []
    actions = plan.get("actions") or []
    goal = (intake or {}).get("goal") or ""
    pay = order.get("payment") or {}
    captured = float(pay.get("captured") or order.get("total") or 0)
    refunded = float(pay.get("refunded") or 0)
    remaining = round(captured - refunded, 2)
    days = order.get("days_since_delivery")
    items = {i["sku"]: i for i in order.get("items") or []}

    refund_total = sum(_f(a, "amount") for a in actions if a.get("action") == "refund")
    credit_total = sum(_f(a, "amount") for a in actions if a.get("action") == "issue_store_credit")
    has_label = any(a.get("action") == "create_return_label" for a in actions)

    # POL-REF-1 - never refund more than the payment can still return.
    if refund_total > remaining + 0.001:
        issues.append(f"POL-REF-1: refund {refund_total:.2f} exceeds the {remaining:.2f} still refundable "
                      f"on {order.get('id')} ({refunded:.2f} of {captured:.2f} already returned)")

    # POL-RET-1 / POL-DMG-1 - the window the refund is being claimed under.
    if refund_total > 0 and days is not None:
        damaged = goal in ("damaged_item", "wrong_item")
        electronic = any((items.get(s) or {}).get("category") in _ELECTRONICS for s in items)
        limit = 14 if damaged else (15 if electronic else 30)
        rule = "POL-DMG-1" if damaged else "POL-RET-1"
        if days > limit:
            issues.append(f"{rule}: delivered {days} days ago, outside the {limit}-day window - "
                          f"a refund is not eligible; consider goodwill credit under POL-GW-1")

    # POL-RET-2 - final sale, unless it arrived damaged or wrong.
    if refund_total > 0 and goal not in ("damaged_item", "wrong_item"):
        for sku, item in items.items():
            if item.get("final_sale"):
                issues.append(f"POL-RET-2: {sku} is final sale and cannot be refunded unless it arrived "
                              f"damaged or was the wrong item")

    # POL-DMG-1 - a return is required for items priced 40.00 or more, and for wrong items.
    if goal in ("damaged_item", "wrong_item") and not has_label:
        sku = (intake or {}).get("sku")
        price = float((items.get(sku) or {}).get("unit_price") or 0) if sku else 0.0
        if goal == "wrong_item" or price >= 40:
            issues.append(f"POL-DMG-1: a prepaid return label is required for {sku or 'the item'} "
                          f"({'wrong item' if goal == 'wrong_item' else f'priced {price:.2f}'})")

    # When the item was faulty or wrong, a return label on its own is not a resolution - it
    # collects the item and gives the customer nothing back. Caught on the live model chain: a
    # 749.00 wrong-item case closed with a label alone, which verified clean because the plan
    # only ever claimed the label. Change-of-mind returns are different and stay exempt: there
    # the label goes out first and the refund follows when the item arrives.
    if goal in ("damaged_item", "wrong_item") and has_label             and not (refund_total > 0 or credit_total > 0
                     or any(a.get("action") == "create_replacement" for a in actions)):
        issues.append("POL-DMG-1: the plan takes the item back but gives nothing in return - a "
                      "prepaid return label must be paired with a refund, a replacement or store "
                      "credit")

    # POL-CAN-1 - cancellation only before dispatch.
    if any(a.get("action") == "cancel_order" for a in actions) and order.get("status") not in ("placed", "paid"):
        issues.append(f"POL-CAN-1: {order.get('id')} is {order.get('status')} and can no longer be "
                      f"cancelled - issue a prepaid return label instead")

    # POL-GW-1 - tier caps and the 90-day frequency limit.
    if credit_total > 0:
        tier = (customer.get("tier") or "standard").lower()
        cap = _GOODWILL_CAP.get(tier, 0.0)
        if cap == 0:
            issues.append(f"POL-GW-1: {tier} tier customers are not eligible for goodwill credit")
        elif credit_total > cap + 0.001:
            issues.append(f"POL-GW-1: goodwill credit {credit_total:.2f} exceeds the {cap:.2f} cap for {tier} tier")
        if customer.get("last_goodwill_at"):
            issues.append("POL-GW-1: this customer already received goodwill credit within the last 90 days")

    # POL-SLA-1 - a replacement must be in stock and actually arrive in time.
    need_by = (intake or {}).get("need_by")
    for a in actions:
        if a.get("action") != "create_replacement":
            continue
        p = a.get("params") or {}
        sku, wh = p.get("sku"), p.get("warehouse_id")
        if not inventory or not sku:
            continue
        try:
            inv = inventory(sku, order.get("region") or "")
        except Exception:
            continue
        row = next((w for w in inv.get("warehouses", []) if w.get("warehouse_id") == wh), None)
        if row is None:
            issues.append(f"POL-SLA-1: {wh} does not stock {sku}")
        elif row.get("available", 0) <= 0:
            better = [w for w in inv.get("warehouses", []) if w.get("available", 0) > 0]
            issues.append(f"POL-SLA-1: {sku} has no available stock at {wh}"
                          + (f" - stock is at {', '.join(w['warehouse_id'] for w in better)}" if better
                             else " - no warehouse has stock, so refund instead"))
        elif need_by and row.get("est_arrival") and row["est_arrival"] > need_by:
            issues.append(f"POL-SLA-1: a replacement from {wh} arrives {row['est_arrival']}, after the "
                          f"customer's need-by date {need_by} - refund instead and explain why")

    if issues:
        viable = _viable_remedies(order, customer, intake, remaining, days)
        if viable:
            issues.append("Remedies that would pass the gate right now: " + "; ".join(viable))
    return issues


def _viable_remedies(order: dict[str, Any], customer: dict[str, Any], intake: dict[str, Any],
                     remaining: float, days: int | None) -> list[str]:
    """Say what *would* be allowed, not only what was not.

    The gate was already precise about the breach and the models still answered a blocked plan by
    reaching for another blocked one - on a stockout, a replacement that missed the need-by date,
    then a goodwill credit over the tier cap, and out of revisions. Naming the amounts that clear
    every rule costs nothing here and is the same move that turned OUT_OF_STOCK from a dead end
    into the next step.
    """
    out: list[str] = []
    goal = (intake or {}).get("goal") or ""
    items = {i["sku"]: i for i in order.get("items") or []}
    damaged = goal in ("damaged_item", "wrong_item")
    electronic = any((items.get(s) or {}).get("category") in _ELECTRONICS for s in items)
    limit = 14 if damaged else (15 if electronic else 30)
    final_sale = any(i.get("final_sale") for i in items.values())

    window_open = days is None or days <= limit
    if remaining > 0 and window_open and (damaged or not final_sale):
        out.append(f"refund up to {remaining:.2f} (within the {limit}-day window"
                   + (f", and at or under {config.AUTO_REFUND_LIMIT:.2f} it needs no approval)"
                      if remaining <= config.AUTO_REFUND_LIMIT else
                      f", though over {config.AUTO_REFUND_LIMIT:.2f} needs a supervisor)"))

    cap = _GOODWILL_CAP.get((customer.get("tier") or "standard").lower(), 0.0)
    if cap > 0:
        out.append(f"goodwill store credit up to {cap:.2f} for this tier")
    return out


def _f(action: dict[str, Any], key: str) -> float:
    try:
        return float((action.get("params") or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0.0

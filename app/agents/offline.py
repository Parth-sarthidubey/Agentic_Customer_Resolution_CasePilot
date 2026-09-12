"""Deterministic fallback reasoning for every agent (no LLM needed).

Used when no free-tier model is configured or all are rate limited. It calls the same
tools, so the live trace looks the same, and it re-reads the systems on every attempt,
so it adapts to changed conditions exactly like the LLM agents.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from app.agents.base import Tracer, call_tool
from app.agents.schemas import ActionStep, Expected, Facts, Intake, Option, Plan, Reply, Review
from app.tools import read_tools as rt

ITEM_WORDS = {"KT-220": ["kettle"], "HP-510": ["headphone"], "LMP-33": ["lamp", "shade"], "CHR-14": ["chair"],
              "ESP-900": ["espresso", "machine"], "GRD-120": ["grinder"], "THR-08": ["throw", "blanket"],
              "CND-02": ["candle"], "MUG-06": ["mug"]}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _customer_text(tr: Tracer, case: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    conv = call_tool(rt.case_tools(case["id"])[0], {}, tr)
    text = " ".join(m["body"] for m in conv["messages"] if m["sender"] == "customer")
    return f"{case['subject']} {text}".lower(), conv["attachments"]


def _goal(t: str) -> str:
    if "cancel" in t:
        return "cancel"
    if any(w in t for w in ("wrong item", "instead of", "contained a", "wrong product", "not what i ordered")):
        return "wrong_item"
    if any(w in t for w in ("broken", "cracked", "damaged", "torn", "defective", "shattered")):
        return "damaged_item"
    if any(w in t for w in ("never arrived", "never came", "hasn't arrived", "not arrived", "not received")):
        return "where_is_my_order"
    if "where is" in t and "refund" not in t:
        return "where_is_my_order"
    if "refund" in t:
        return "refund"
    if "return" in t:
        return "return"
    return "other"


def _need_by(t: str) -> str | None:
    m = re.search(r"\b(20\d\d-\d\d-\d\d)\b", t)
    if m:
        return m.group(1)
    for i, day in enumerate(WEEKDAYS):
        if f"by {day}" in t or f"before {day}" in t:
            ahead = (i - date.today().weekday()) % 7 or 7
            return (date.today() + timedelta(days = ahead)).isoformat()
    return None


def intake(tr: Tracer, case: dict[str, Any]) -> Intake:
    t, _atts = _customer_text(tr, case)
    goal = _goal(t)
    found = call_tool(rt.T_FIND_CUSTOMER, {"query": case.get("customer_email") or ""}, tr)
    if not found:
        return Intake(goal = goal, priority = "P3", missing_info = ["customer account"],
                      clarifying_question = "Could you share the email address you used for your order?",
                      summary = "Customer could not be identified.")
    cust = call_tool(rt.T_GET_CUSTOMER, {"customer_id": found[0]["id"]}, tr)
    order_id = (re.search(r"ord-\d{5}", t) or [None])[0]
    order_id = order_id.upper() if order_id else None
    candidates = []
    if not order_id:
        recent = [o for o in cust["orders"] if o["placed_at"] >= (date.today() - timedelta(days = 60)).isoformat()]
        for o in recent:
            full = call_tool(rt.T_GET_ORDER, {"order_id": o["id"]}, tr)
            if any(w in t for i in full["items"] for w in ITEM_WORDS.get(i["sku"], [])):
                candidates.append(o["id"])
        if len(candidates) == 1:
            order_id = candidates[0]
        elif len(recent) == 1:
            order_id = recent[0]["id"]
    if not order_id:
        return Intake(goal = goal, customer_id = cust["id"], priority = "P3", missing_info = ["order", "item"],
                      clarifying_question = "Sorry to hear that! You have a few recent orders with us - which item "
                                            "is this about, and would you prefer a replacement or a refund?",
                      summary = f"{cust['name']} reported an issue but the order/item is unclear.")
    order = call_tool(rt.T_GET_ORDER, {"order_id": order_id}, tr)
    sku = next((i["sku"] for i in order["items"] if any(w in t for w in ITEM_WORDS.get(i["sku"], []))), None)
    if not sku and len(order["items"]) == 1:
        sku = order["items"][0]["sku"]
    prio = "P1" if order["total"] > 500 else "P2" if goal in ("damaged_item", "wrong_item", "where_is_my_order") else "P3"
    return Intake(goal = goal, customer_id = cust["id"], order_id = order_id, sku = sku, need_by = _need_by(t),
                  priority = prio, sentiment = "frustrated" if goal in ("where_is_my_order", "wrong_item") else "neutral",
                  summary = f"{cust['name']} ({cust['tier']}) - {goal.replace('_', ' ')} on {order_id}"
                            + (f" ({sku})" if sku else "") + ".")


def _price(order: dict[str, Any], sku: str | None) -> float:
    return next((i["unit_price"] for i in order["items"] if i["sku"] == sku), order["total"])


def investigate(tr: Tracer, case: dict[str, Any], it: Intake) -> Facts:
    order = call_tool(rt.T_GET_ORDER, {"order_id": it.order_id}, tr)
    cust = call_tool(rt.T_GET_CUSTOMER, {"customer_id": it.customer_id}, tr)
    findings = [f"{order['id']}: status {order['status']}, total {order['total']:.2f}, "
                f"captured {order['payment']['captured']:.2f}, refunded {order['payment']['refunded']:.2f}",
                f"Customer {cust['name']}: tier {cust['tier']}, claims in last 90 days {cust['claims_90d']}"]
    if order.get("days_since_delivery") is not None:
        findings.append(f"Delivered {order['days_since_delivery']} days ago")
    risk, options = [], []
    for s in order["shipments"]:
        tracked = call_tool(rt.T_TRACK, {"shipment_id": s["id"]}, tr)
        findings.append(f"{s['kind']} shipment {s['id']}: {tracked['status']}"
                        + (f", {tracked['days_past_eta']} days past ETA" if tracked.get("days_past_eta") else "")
                        + (f", proof: {tracked['proof']}" if tracked.get("proof") else ""))
    if cust["claims_90d"] >= 3:
        risk.append(f"claims_90d={cust['claims_90d']} - claims review required")
    outbound = next((s for s in order["shipments"] if s["kind"] == "outbound"), None)
    if it.goal == "where_is_my_order" and outbound and outbound["status"] == "delivered" and outbound.get("proof"):
        risk.append(f"carrier marked delivered with proof ({outbound['proof']}) - delivery dispute")
    pols = call_tool(rt.T_POLICY, {"query": it.goal.replace("_", " ") + " refund replacement"}, tr)
    price = _price(order, it.sku)

    if risk:
        options.append(Option(option = "escalate to Trust & Safety", eligible = True, policy_ref = "POL-FRAUD-1",
                              reason = "claims review conditions met"))
        rec = "escalate"
    elif it.goal in ("damaged_item", "wrong_item"):
        ok = (order.get("days_since_delivery") or 0) <= 14
        if it.goal == "damaged_item" and it.sku:
            inv = call_tool(rt.T_INVENTORY, {"sku": it.sku, "region": order["region"]}, tr)
            timely = [w for w in inv["warehouses"] if w["available"] > 0 and (not it.need_by or w["est_arrival"] <= it.need_by)]
            options.append(Option(option = "replacement", eligible = ok and bool(timely), policy_ref = "POL-SLA-1",
                                  reason = f"timely stock at {[w['warehouse_id'] for w in timely]}" if timely
                                  else "no warehouse can deliver by the need-by date"))
        options.append(Option(option = f"refund {price:.2f}", eligible = ok, policy_ref = "POL-DMG-1",
                              reason = "reported within 14 days of delivery" if ok else "reported too late"))
        rec = "replacement" if any(o.option == "replacement" and o.eligible for o in options) else f"refund {price:.2f}"
    elif it.goal == "where_is_my_order":
        late = (outbound or {}).get("status") == "in_transit" and \
            call_tool(rt.T_TRACK, {"shipment_id": outbound["id"]}, tr).get("days_past_eta", 0) >= 5
        options.append(Option(option = "re-ship", eligible = late, policy_ref = "POL-LOST-1",
                              reason = "5+ days past ETA without delivery scan" if late else "not yet considered lost"))
        rec = "re-ship" if late else "share tracking"
    elif it.goal == "cancel":
        can = order["status"] in ("placed", "paid")
        options.append(Option(option = "cancel", eligible = can, policy_ref = "POL-CAN-1", reason = f"status {order['status']}"))
        rec = "cancel" if can else "return label"
    else:
        returned = any(s["kind"] == "return" and s["status"] == "delivered" for s in order["shipments"])
        window = 15 if any(i["category"] == "electronics" for i in order["items"]) else 30
        in_window = (order.get("days_since_delivery") or 0) <= window
        final = any(i["final_sale"] for i in order["items"] if i["sku"] == it.sku)
        options.append(Option(option = f"refund {price:.2f}", eligible = returned or (in_window and not final),
                              policy_ref = "POL-RET-1", reason = "return already received" if returned else
                              f"{order.get('days_since_delivery')} days since delivery, window {window}"))
        goodwill_ok = cust["tier"] in ("gold", "platinum") and (
            not cust["last_goodwill_at"] or cust["last_goodwill_at"] < (date.today() - timedelta(days = 90)).isoformat())
        options.append(Option(option = "goodwill store credit", eligible = goodwill_ok, policy_ref = "POL-GW-1",
                              reason = f"tier {cust['tier']}"))
        rec = next((o.option for o in options if o.eligible), "decline politely")
    findings.append("Relevant policies: " + ", ".join(p["policy_id"] for p in pols))
    return Facts(findings = findings, options = options, risk_flags = risk, recommended = rec, confidence = 0.85)


def resolve(tr: Tracer, case: dict[str, Any], it: Intake, facts: Facts, history: list[dict[str, Any]]) -> Plan:
    order = call_tool(rt.T_GET_ORDER, {"order_id": it.order_id}, tr)
    t = f"{case['subject']} {case['description']}".lower()
    price, sku = _price(order, it.sku), it.sku
    done = {a for h in history for a in h.get("completed", [])}
    label = [] if "create_return_label" in done else \
        [ActionStep(action = "create_return_label", params = {"order_id": order["id"], "sku": sku},
                    rationale = "Return required for items 40.00+ / wrong items (POL-DMG-1)")]

    if facts.risk_flags:
        return Plan(decision = "escalate", resolution_type = "claims_review", summary = "Escalate to Trust & Safety.",
                    escalate_to = "Trust & Safety", policy_refs = ["POL-FRAUD-1"],
                    escalation_reason = "; ".join(facts.risk_flags))
    def refund_plan(why: str, with_label: bool) -> Plan:
        return Plan(
            decision = "execute", resolution_type = "refund", summary = f"Refund {price:.2f} for {sku}. {why}",
            actions = ([] if "refund" in done else
                       [ActionStep(action = "refund", params = {"order_id": order["id"], "amount": price,
                                                                "reason": it.goal}, rationale = why)])
                      + (label if with_label else []),
            expected = Expected(refund_amount = price, return_label = with_label),
            policy_refs = ["POL-DMG-1", "POL-REF-1"])

    needs_label = price >= 40 or it.goal == "wrong_item"

    if it.goal == "cancel":
        if order["status"] in ("placed", "paid"):
            return Plan(decision = "execute", resolution_type = "cancellation", summary = f"Cancel {order['id']}.",
                        actions = [ActionStep(action = "cancel_order", params = {"order_id": order["id"], "reason": "customer request"},
                                              rationale = "Order not shipped yet (POL-CAN-1)")],
                        expected = Expected(cancelled = True), policy_refs = ["POL-CAN-1"])
        steps = [ActionStep(action = "create_return_label", params = {"order_id": order["id"], "sku": i["sku"]},
                            rationale = "Order already shipped - return instead (POL-CAN-1)") for i in order["items"]]
        return Plan(decision = "execute", resolution_type = "return_after_shipment",
                    summary = "Order already shipped: issue return labels; refund when the return is scanned.",
                    actions = steps, expected = Expected(return_label = True), policy_refs = ["POL-CAN-1"])
    dmg_ok = any(o.eligible for o in facts.options if o.policy_ref in ("POL-DMG-1", "POL-SLA-1"))
    if it.goal in ("damaged_item", "wrong_item") and not dmg_ok:
        return Plan(decision = "inform_only", resolution_type = "declined",
                    summary = "Reported more than 14 days after delivery - not eligible under POL-DMG-1; explain "
                              "the policy and the options that remain.", policy_refs = ["POL-DMG-1", "POL-RET-2"])
    if it.goal == "wrong_item" or (it.goal == "damaged_item" and "refund" in t):
        return refund_plan("Customer asked for a refund of a damaged/wrong item.", needs_label)
    if it.goal in ("damaged_item", "replacement") or (it.goal == "where_is_my_order" and facts.recommended == "re-ship"):
        inv = call_tool(rt.T_INVENTORY, {"sku": sku, "region": order["region"]}, tr)
        ok = [w for w in inv["warehouses"] if w["available"] > 0 and (not it.need_by or w["est_arrival"] <= it.need_by)]
        ok.sort(key = lambda w: w["transit_days"])
        if ok:
            wh = ok[0]
            acts = [ActionStep(action = "create_replacement",
                               params = {"order_id": order["id"], "sku": sku, "warehouse_id": wh["warehouse_id"]},
                               rationale = f"Stock at {wh['warehouse_id']}, arrives {wh['est_arrival']}")]
            if it.goal == "damaged_item" and price >= 40:
                acts += label
            return Plan(decision = "execute", resolution_type = "replacement",
                        summary = f"Ship replacement {sku} from {wh['warehouse_id']} (ETA {wh['est_arrival']}).",
                        actions = acts, expected = Expected(replacement_sku = sku, need_by = it.need_by,
                                                            return_label = it.goal == "damaged_item" and price >= 40),
                        policy_refs = ["POL-DMG-1" if it.goal == "damaged_item" else "POL-LOST-1", "POL-SLA-1"])
        return refund_plan("No warehouse can deliver a replacement in time (POL-SLA-1) - refund instead.",
                           needs_label and it.goal == "damaged_item")
    if it.goal == "where_is_my_order":
        return Plan(decision = "inform_only", resolution_type = "tracking_update",
                    summary = "Package not yet considered lost - share tracking.", policy_refs = ["POL-LOST-1"])
    refund_opt = next((o for o in facts.options if o.option.startswith("refund")), None)
    if refund_opt and refund_opt.eligible:
        returned = any(s["kind"] == "return" and s["status"] == "delivered" for s in order["shipments"])
        return refund_plan("Return received / within window (POL-RET-1).", not returned)
    gw = next((o for o in facts.options if o.option == "goodwill store credit" and o.eligible), None)
    if gw:
        tier = call_tool(rt.T_GET_CUSTOMER, {"customer_id": it.customer_id}, tr)["tier"]
        amt = 50.0 if tier == "platinum" else 25.0
        return Plan(decision = "execute", resolution_type = "goodwill_credit",
                    summary = f"Outside return window - offer {amt:.2f} goodwill store credit.",
                    actions = [ActionStep(action = "issue_store_credit",
                                          params = {"customer_id": it.customer_id, "amount": amt,
                                                    "reason": "goodwill - outside return window"},
                                          rationale = f"{tier} tier goodwill (POL-GW-1)")],
                    expected = Expected(credit_amount = amt), policy_refs = ["POL-RET-1", "POL-GW-1"])
    return Plan(decision = "inform_only", resolution_type = "declined", summary = "Not eligible - explain policy.",
                policy_refs = ["POL-RET-1"])


def audit(tr: Tracer, facts: Facts, plan: Plan) -> Review:
    call_tool(rt.T_POLICY, {"query": plan.resolution_type.replace("_", " ")}, tr)
    issues = []
    if facts.risk_flags and plan.decision == "execute":
        issues.append("risk flags present - must escalate (POL-FRAUD-1)")
    refunds = [a for a in plan.actions if a.action == "refund"]
    if refunds and not any(o.eligible for o in facts.options if o.option.startswith("refund")
                           or o.policy_ref in ("POL-SLA-1", "POL-LOST-1")):
        issues.append("refund proposed but no refund option is eligible under policy")
    if issues:
        return Review(verdict = "revise", issues = issues, feedback = "; ".join(issues))
    return Review(verdict = "approve", feedback = "Plan is consistent with policy and the verified facts.")


def communicate(tr: Tracer, case: dict[str, Any], plan: Plan, results: list[dict[str, Any]], history: list[dict[str, Any]]) -> Reply:
    name = (case.get("customer_name") or "there").split(" ")[0]
    e = plan.expected
    parts = []
    if history:
        parts.append("We first tried " + history[-1].get("plan", "another option").lower().rstrip(".")
                     + f", but {history[-1].get('error', 'it was not possible')}.")
    if e.refund_amount:
        parts.append(f"We've refunded {e.refund_amount:.2f} to your original payment method - it should appear in 3-5 business days.")
    if e.replacement_sku:
        ship = next((r for r in results if r.get("shipment_id")), {})
        parts.append(f"A replacement is on its way (shipment {ship.get('shipment_id', '')}, arriving by {ship.get('eta', 'soon')}).")
    if e.cancelled:
        parts.append("Your order is cancelled and fully refunded.")
    if e.return_label:
        parts.append("We've emailed you a prepaid return label - please send the item back within 14 days.")
    if e.credit_amount:
        parts.append(f"As a thank-you for being a loyal customer we've added {e.credit_amount:.2f} store credit to your account.")
    if plan.decision == "inform_only":
        parts.append(plan.summary)
    msg = f"Hi {name}, thanks for your patience. " + " ".join(parts) + " - Kestrel Home Support"
    return Reply(customer_message = msg,
                 internal_note = f"{plan.resolution_type}: {plan.summary} Attempts: {len(history) + 1}. Verified OK.",
                 kb_title = f"{plan.resolution_type.replace('_', ' ')} - {case['subject'][:60]}",
                 kb_situation = case["subject"], kb_resolution = plan.summary,
                 kb_tags = ",".join(plan.policy_refs + [plan.resolution_type]))

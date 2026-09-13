"""Enterprise system APIs (CRM, orders, inventory, payments, shipping, credit, notifications).

These behave like real back-office services: they enforce business state, can refuse
an action (OUT_OF_STOCK, ORDER_ALREADY_SHIPPED...), and can fail transiently
(GATEWAY_UNAVAILABLE). Scheduled "world events" change conditions while an agent is
mid-plan - exactly the situations the agent has to observe and adapt to.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from app.sandbox import store


class ServiceError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.retryable = code, message, retryable

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "retryable": self.retryable}


# --- World events ---------------------------------------------------------------

def _kv_get(key: str, default: Any = None) -> Any:
    row = store.q1("SELECT value FROM kv WHERE key = ?", (key,))
    return json.loads(row["value"]) if row else default


def _kv_set(key: str, value: Any) -> None:
    store.x("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)))


def schedule_event(trigger: str, effect: dict[str, Any], description: str) -> None:
    # Idempotent: re-filing a demo scenario re-arms its event rather than stacking a duplicate.
    store.x("DELETE FROM world_events WHERE trigger = ? AND description = ?", (trigger, description))
    store.x("INSERT INTO world_events (trigger, effect, description) VALUES (?, ?, ?)",
            (trigger, json.dumps(effect), description))


def _fire(action: str, ref: str = "") -> None:
    """Apply any pending world events bound to this action (optionally to a specific order)."""
    triggers = [f"before:{action}", f"before:{action}:{ref}"]
    for ev in store.q("SELECT * FROM world_events WHERE fired = 0 AND trigger IN (?, ?)", triggers):
        eff = json.loads(ev["effect"])
        kind = eff["type"]
        if kind == "set_stock":
            store.x("UPDATE inventory SET on_hand = ? WHERE sku = ? AND warehouse_id = ?",
                    (eff["on_hand"], eff["sku"], eff["warehouse_id"]))
        elif kind == "gateway_outage":
            _kv_set("gateway_failures", eff["failures"])
        elif kind == "ship_order":
            _ship(eff["order_id"])
        store.x("UPDATE world_events SET fired = 1, fired_at = ? WHERE id = ?", (store.now_iso(), ev["id"]))
        store.audit("world", "event", ev["description"], kind = "world_event")


def _ship(order_id: str) -> None:
    order = store.q1("SELECT * FROM orders WHERE id = ?", (order_id,))
    today = date.today()
    store.x("UPDATE orders SET status = 'shipped', shipped_at = ? WHERE id = ?", (today.isoformat(), order_id))
    sid = store.next_id("SHP", "shipments")
    item = store.q1("SELECT sku FROM order_items WHERE order_id = ?", (order_id,))
    store.x("INSERT INTO shipments (id, order_id, kind, carrier, status, warehouse_id, sku, eta, created_at) "
            "VALUES (?, ?, 'outbound', 'SwiftPost', 'in_transit', 'WH-EAST', ?, ?, ?)",
            (sid, order_id, item["sku"], (today + timedelta(days = 3)).isoformat(), store.now_iso()))
    store.x("INSERT INTO shipment_events VALUES (?, ?, 'in_transit', 'East Hub', 'Picked up by carrier')",
            (sid, store.now_iso()))
    store.audit("shipping", "shipped", {"order_id": order_id, "shipment_id": sid, "region": order["region"]})


# --- Reads ------------------------------------------------------------------------

def find_customer(query: str) -> list[dict[str, Any]]:
    like = f"%{query.strip()}%"
    return store.q("SELECT id, name, email, tier FROM customers WHERE id = ? OR lower(email) = lower(?) "
                   "OR name LIKE ? OR email LIKE ? LIMIT 5", (query.strip(), query.strip(), like, like))


def get_customer(customer_id: str) -> dict[str, Any]:
    c = store.q1("SELECT * FROM customers WHERE id = ?", (customer_id,))
    if not c:
        raise ServiceError("NOT_FOUND", f"customer {customer_id} not found")
    c["orders"] = store.q("SELECT id, status, placed_at, delivered_at, total FROM orders WHERE customer_id = ? "
                          "ORDER BY placed_at DESC", (customer_id,))
    c["store_credit_issued"] = store.q("SELECT amount, reason, created_at FROM store_credit WHERE customer_id = ?",
                                       (customer_id,))
    return c


_ELECTRONIC_CATEGORIES = {"electronics"}


def get_order(order_id: str) -> dict[str, Any]:
    o = store.q1("SELECT * FROM orders WHERE id = ?", (order_id.strip().upper(),))
    if not o:
        raise ServiceError("NOT_FOUND", f"order {order_id} not found")
    o["items"] = store.q("SELECT oi.sku, p.name, p.category, p.final_sale, oi.qty, oi.unit_price, oi.status "
                         "FROM order_items oi JOIN products p ON p.sku = oi.sku WHERE oi.order_id = ?", (o["id"],))
    o["payment"] = store.q1("SELECT * FROM payments WHERE id = ?", (o["payment_id"],))
    o["refunds"] = store.q("SELECT id, amount, reason, status, created_at FROM refunds WHERE order_id = ?", (o["id"],))
    o["shipments"] = store.q("SELECT id, kind, status, warehouse_id, sku, eta, delivered_at, proof FROM shipments "
                             "WHERE order_id = ?", (o["id"],))
    if o["delivered_at"]:
        o["days_since_delivery"] = (date.today() - date.fromisoformat(o["delivered_at"])).days
    o["return_windows"] = _return_windows(o)
    return o


def _return_windows(order: dict[str, Any]) -> dict[str, Any]:
    """Say outright whether each return window is still open, rather than leaving it to be derived.

    Models get this wrong, and when they do the case is decided on a false premise before any gate
    sees it: live, an order delivered fourteen days ago was reported as "outside the 15-day
    electronics window", every remedy marked ineligible, and the case escalated. Fourteen is inside
    fifteen. The dates are already here; doing the subtraction once, in code, removes a whole class
    of wrong answer that no downstream check was positioned to catch.
    """
    days = order.get("days_since_delivery")
    electronic = any((i.get("category") or "").lower() in _ELECTRONIC_CATEGORIES
                     for i in order.get("items") or [])
    standard = 15 if electronic else 30
    return {
        "days_since_delivery": days,
        "standard_limit_days": standard,
        "standard_open": days is None or days <= standard,
        "damaged_or_wrong_limit_days": 14,
        "damaged_or_wrong_open": days is None or days <= 14,
        "note": ("not delivered yet, so no window has started" if days is None else
                 f"delivered {days} days ago; standard window {standard} days "
                 f"({'OPEN' if days <= standard else 'CLOSED'}), damaged/wrong window 14 days "
                 f"({'OPEN' if days <= 14 else 'CLOSED'})"),
    }


def check_inventory(sku: str, region: str = "") -> dict[str, Any]:
    p = store.q1("SELECT * FROM products WHERE sku = ?", (sku,))
    if not p:
        raise ServiceError("NOT_FOUND", f"sku {sku} not found")
    rows = store.q("SELECT i.warehouse_id, w.region, i.on_hand - i.reserved AS available FROM inventory i "
                   "JOIN warehouses w ON w.id = i.warehouse_id WHERE i.sku = ? ORDER BY i.warehouse_id", (sku,))
    for r in rows:
        if region:
            t = store.q1("SELECT days FROM transit_days WHERE warehouse_id = ? AND region = ?", (r["warehouse_id"], region))
            r["transit_days"] = t["days"] if t else None
            r["est_arrival"] = (date.today() + timedelta(days = r["transit_days"])).isoformat() if t else None
    return {"sku": sku, "name": p["name"], "price": p["price"], "warehouses": rows}


def track_shipment(shipment_id: str) -> dict[str, Any]:
    s = store.q1("SELECT * FROM shipments WHERE id = ?", (shipment_id,))
    if not s:
        raise ServiceError("NOT_FOUND", f"shipment {shipment_id} not found")
    s["events"] = store.q("SELECT ts, status, location, note FROM shipment_events WHERE shipment_id = ? ORDER BY ts",
                          (shipment_id,))
    if s["eta"] and s["status"] not in ("delivered", "cancelled"):
        s["days_past_eta"] = max(0, (date.today() - date.fromisoformat(s["eta"])).days)
    return s


def gateway_status() -> dict[str, Any]:
    failing = _kv_get("gateway_failures", 0) > 0
    return {"payment_gateway": "degraded" if failing else "operational"}


# --- Writes -----------------------------------------------------------------------

def cancel_order(order_id: str, reason: str) -> dict[str, Any]:
    _fire("cancel_order", order_id)
    o = get_order(order_id)
    if o["status"] in ("shipped", "delivered"):
        raise ServiceError("ORDER_ALREADY_SHIPPED", f"order {order_id} is {o['status']} and can no longer be cancelled")
    if o["status"] not in ("placed", "paid"):
        raise ServiceError("NOT_CANCELLABLE", f"order {order_id} is {o['status']}")
    store.x("UPDATE orders SET status = 'cancelled' WHERE id = ?", (o["id"],))
    pay = o["payment"]
    rid = store.next_id("RF", "refunds")
    store.x("INSERT INTO refunds VALUES (?, ?, ?, ?, ?, 'succeeded', ?, ?)",
            (rid, pay["id"], o["id"], pay["captured"] - pay["refunded"], f"cancellation: {reason}", f"cancel-{o['id']}",
             store.now_iso()))
    store.x("UPDATE payments SET refunded = captured WHERE id = ?", (pay["id"],))
    store.audit("orders", "cancel_order", {"order_id": o["id"], "refund_id": rid, "amount": pay["captured"]})
    return {"order_id": o["id"], "status": "cancelled", "refund_id": rid, "refunded": pay["captured"] - pay["refunded"]}


def refund(order_id: str, amount: float, reason: str, idempotency_key: str) -> dict[str, Any]:
    _fire("refund", order_id)
    existing = store.q1("SELECT * FROM refunds WHERE idempotency_key = ?", (idempotency_key,))
    if existing:
        return {"refund_id": existing["id"], "status": existing["status"], "amount": existing["amount"],
                "idempotent_replay": True}
    failures = _kv_get("gateway_failures", 0)
    if failures > 0:
        _kv_set("gateway_failures", failures - 1)
        store.audit("payments", "refund_failed", {"order_id": order_id, "error": "GATEWAY_UNAVAILABLE"}, kind = "failure")
        raise ServiceError("GATEWAY_UNAVAILABLE", "payment gateway returned 503 - try again", retryable = True)
    o = get_order(order_id)
    pay = o["payment"]
    amount = round(float(amount), 2)
    if amount <= 0:
        raise ServiceError("INVALID_AMOUNT", "refund amount must be positive")
    if pay["refunded"] + amount > pay["captured"] + 0.001:
        raise ServiceError("EXCEEDS_CAPTURED", f"only {pay['captured'] - pay['refunded']:.2f} remains refundable")
    rid = store.next_id("RF", "refunds")
    store.x("INSERT INTO refunds VALUES (?, ?, ?, ?, ?, 'succeeded', ?, ?)",
            (rid, pay["id"], o["id"], amount, reason, idempotency_key, store.now_iso()))
    store.x("UPDATE payments SET refunded = refunded + ? WHERE id = ?", (amount, pay["id"]))
    full = abs(pay["refunded"] + amount - pay["captured"]) < 0.01
    store.x("UPDATE orders SET status = ? WHERE id = ?", ("refunded" if full else "partially_refunded", o["id"]))
    store.audit("payments", "refund", {"order_id": o["id"], "refund_id": rid, "amount": amount, "method": pay["method"]})
    return {"refund_id": rid, "status": "succeeded", "amount": amount, "method": pay["method"]}


def create_replacement(order_id: str, sku: str, warehouse_id: str) -> dict[str, Any]:
    _fire("create_replacement", order_id)
    o = get_order(order_id)
    if not any(i["sku"] == sku for i in o["items"]):
        raise ServiceError("SKU_NOT_IN_ORDER", f"{sku} is not part of {order_id}")
    inv = store.q1("SELECT on_hand - reserved AS available FROM inventory WHERE sku = ? AND warehouse_id = ?",
                   (sku, warehouse_id))
    if not inv or inv["available"] < 1:
        store.audit("inventory", "reserve_failed", {"sku": sku, "warehouse_id": warehouse_id}, kind = "failure")
        # A refusal that names the way out. The agent already has the inventory tool, but a bare
        # "no stock in WH-EAST" reads as a dead end and the models were answering it with a
        # goodwill credit or a verbatim retry rather than the warehouse next door. Saying where
        # the stock actually is turns the failure into the next step.
        alts = store.q("SELECT warehouse_id, on_hand - reserved AS available FROM inventory "
                       "WHERE sku = ? AND on_hand - reserved > 0 ORDER BY available DESC", (sku,))
        where = ("; " + ", ".join(f"{a['warehouse_id']} has {a['available']}" for a in alts)
                 + " - retry with one of those warehouses" if alts else "; no warehouse has stock")
        raise ServiceError("OUT_OF_STOCK", f"{sku} has no available stock in {warehouse_id}{where}")
    t = store.q1("SELECT days FROM transit_days WHERE warehouse_id = ? AND region = ?", (warehouse_id, o["region"]))
    eta = (date.today() + timedelta(days = t["days"] if t else 5)).isoformat()
    store.x("UPDATE inventory SET reserved = reserved + 1 WHERE sku = ? AND warehouse_id = ?", (sku, warehouse_id))
    sid = store.next_id("SHP", "shipments")
    store.x("INSERT INTO shipments (id, order_id, kind, carrier, status, warehouse_id, sku, eta, created_at) "
            "VALUES (?, ?, 'replacement', 'SwiftPost', 'label_created', ?, ?, ?, ?)",
            (sid, o["id"], warehouse_id, sku, eta, store.now_iso()))
    store.x("UPDATE order_items SET status = 'replaced' WHERE order_id = ? AND sku = ?", (o["id"], sku))
    store.audit("shipping", "create_replacement", {"order_id": o["id"], "shipment_id": sid, "sku": sku,
                                                    "warehouse_id": warehouse_id, "eta": eta})
    return {"shipment_id": sid, "status": "label_created", "warehouse_id": warehouse_id, "eta": eta}


def create_return_label(order_id: str, sku: str) -> dict[str, Any]:
    _fire("create_return_label", order_id)
    o = get_order(order_id)
    if o["status"] not in ("shipped", "delivered", "partially_refunded", "refunded"):
        raise ServiceError("NOT_RETURNABLE_STATE", f"order {order_id} is {o['status']}")
    sid = store.next_id("SHP", "shipments")
    store.x("INSERT INTO shipments (id, order_id, kind, carrier, status, sku, created_at) "
            "VALUES (?, ?, 'return', 'SwiftPost', 'label_created', ?, ?)", (sid, o["id"], sku, store.now_iso()))
    store.audit("shipping", "create_return_label", {"order_id": o["id"], "shipment_id": sid, "sku": sku})
    return {"return_shipment_id": sid, "label_url": f"https://labels.example.net/{sid}.pdf"}


def issue_store_credit(customer_id: str, amount: float, reason: str, case_ref: str) -> dict[str, Any]:
    _fire("issue_store_credit", customer_id)
    amount = round(float(amount), 2)
    if amount <= 0 or amount > 200:
        raise ServiceError("INVALID_AMOUNT", "store credit must be between 0 and 200")
    get_customer(customer_id)
    cid = store.next_id("SC", "store_credit")
    store.x("INSERT INTO store_credit VALUES (?, ?, ?, ?, ?, ?)",
            (cid, customer_id, amount, reason, case_ref, store.now_iso()))
    store.x("UPDATE customers SET last_goodwill_at = ? WHERE id = ?", (date.today().isoformat(), customer_id))
    store.audit("crm", "issue_store_credit", {"customer_id": customer_id, "credit_id": cid, "amount": amount})
    return {"credit_id": cid, "amount": amount}


def send_notification(customer_id: str, case_ref: str, body: str) -> dict[str, Any]:
    nid = store.x("INSERT INTO notifications (customer_id, case_ref, body, created_at) VALUES (?, ?, ?, ?)",
                  (customer_id, case_ref, body, store.now_iso()))
    store.audit("notify", "send_notification", {"customer_id": customer_id, "case_ref": case_ref})
    return {"notification_id": nid, "delivered": True}


def today() -> str:
    return date.today().isoformat()


def ts_to_date(ts: str) -> str:
    return datetime.fromisoformat(ts).date().isoformat()

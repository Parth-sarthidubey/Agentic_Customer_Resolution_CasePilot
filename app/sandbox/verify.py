"""Verification endpoint: checks the *actual* enterprise state against the plan's promised outcome.

An agent saying "done" is not enough - a case is only resolved when these objective
checks pass on the system of record.
"""

from __future__ import annotations

from typing import Any

from app.sandbox import store


def verify(case_ref: str, order_id: str, expected: dict[str, Any], include_notification: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "passed": bool(ok), "detail": detail})

    order = store.q1("SELECT * FROM orders WHERE id = ?", (order_id,)) if order_id else None
    pay = store.q1("SELECT * FROM payments WHERE id = ?", (order["payment_id"],)) if order else None

    if expected.get("refund_amount"):
        want = round(float(expected["refund_amount"]), 2)
        got = store.q1("SELECT COALESCE(SUM(amount), 0) AS s FROM refunds WHERE order_id = ? AND status = 'succeeded' "
                       "AND reason NOT LIKE 'cancellation%'", (order_id,))["s"]
        check("refund_posted", abs(got - want) < 0.01, f"expected {want:.2f} refunded, ledger shows {got:.2f}")
    if expected.get("cancelled"):
        check("order_cancelled", order and order["status"] == "cancelled", f"order status is {order and order['status']}")
    if expected.get("replacement_sku"):
        rep = store.q1("SELECT * FROM shipments WHERE order_id = ? AND kind = 'replacement' AND sku = ?",
                       (order_id, expected["replacement_sku"]))
        check("replacement_shipment", rep is not None,
              f"replacement shipment {rep['id']} eta {rep['eta']}" if rep else "no replacement shipment found")
        if rep and expected.get("need_by"):
            check("arrives_by_need_by", rep["eta"] <= expected["need_by"], f"eta {rep['eta']} vs need-by {expected['need_by']}")
    if expected.get("return_label"):
        ret = store.q1("SELECT id FROM shipments WHERE order_id = ? AND kind = 'return'", (order_id,))
        check("return_label_issued", ret is not None, f"return shipment {ret['id']}" if ret else "no return label")
    if expected.get("credit_amount"):
        want = round(float(expected["credit_amount"]), 2)
        got = store.q1("SELECT COALESCE(SUM(amount), 0) AS s FROM store_credit WHERE case_ref = ?", (case_ref,))["s"]
        check("store_credit_issued", abs(got - want) < 0.01, f"expected {want:.2f}, issued {got:.2f}")
    if pay:
        check("no_over_refund", pay["refunded"] <= pay["captured"] + 0.001,
              f"refunded {pay['refunded']:.2f} of captured {pay['captured']:.2f}")
    if include_notification:
        note = store.q1("SELECT id FROM notifications WHERE case_ref = ?", (case_ref,))
        check("customer_notified", note is not None, "notification sent" if note else "customer not notified yet")

    return {"passed": all(c["passed"] for c in checks), "checks": checks}

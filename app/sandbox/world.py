"""Seeds the simulated enterprise ('Kestrel Home', a fictional home-goods retailer).

All data is synthetic. Dates are relative to today so policy windows always behave.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app import config
from app.sandbox import store

CUSTOMERS = [
    ("C-1001", "Ava Stone", "ava.stone@example.com", "gold", 1840.0, 0),
    ("C-1002", "Liam Patel", "liam.patel@example.com", "standard", 410.0, 0),
    ("C-1003", "Mia Moreau", "mia.moreau@example.com", "standard", 126.0, 0),
    ("C-1004", "Noah Kim", "noah.kim@example.com", "gold", 2210.0, 0),
    ("C-1005", "Zara Okafor", "zara.okafor@example.com", "platinum", 6120.0, 0),
    ("C-1006", "Omar Rossi", "omar.rossi@example.com", "standard", 189.0, 1),
    ("C-1007", "Kenji Tanaka", "kenji.tanaka@example.com", "standard", 530.0, 4),
    ("C-1008", "Sofia Berg", "sofia.berg@example.com", "gold", 1310.0, 0),
    ("C-1009", "Diego Novak", "diego.novak@example.com", "standard", 88.0, 0),
]
PRODUCTS = [
    ("KT-220", "Ceramic Pour-Over Kettle", "kitchen", 38.00, 0),
    ("HP-510", "Wireless Headphones", "electronics", 129.00, 0),
    ("LMP-33", "Linen Table Lamp", "home", 72.00, 0),
    ("CHR-14", "Oak Dining Chair", "furniture", 189.00, 0),
    ("ESP-900", "Barista Espresso Machine", "appliances", 749.00, 0),
    ("GRD-120", "Burr Coffee Grinder", "appliances", 119.00, 0),
    ("THR-08", "Wool Throw Blanket", "home", 54.00, 0),
    ("CND-02", "Soy Candle Set", "home", 24.00, 1),
    ("MUG-06", "Stoneware Mug Set", "kitchen", 32.00, 0),
]
WAREHOUSES = [("WH-EAST", "East Fulfilment Centre", "east"), ("WH-WEST", "West Fulfilment Centre", "west"),
              ("WH-CENTRAL", "Central Fulfilment Centre", "central")]
TRANSIT = {("WH-EAST", "east"): 2, ("WH-EAST", "central"): 3, ("WH-EAST", "west"): 6,
           ("WH-WEST", "west"): 2, ("WH-WEST", "central"): 3, ("WH-WEST", "east"): 6,
           ("WH-CENTRAL", "central"): 2, ("WH-CENTRAL", "east"): 3, ("WH-CENTRAL", "west"): 3}
STOCK_OVERRIDES = {("KT-220", "WH-EAST"): 1, ("KT-220", "WH-CENTRAL"): 0, ("KT-220", "WH-WEST"): 6,
                   ("ESP-900", "WH-CENTRAL"): 2}


def d(days_ago: int) -> str:
    return (date.today() - timedelta(days = days_ago)).isoformat()


def add_order(oid: str, customer: str, status: str, placed_ago: int, region: str, items: list[tuple[str, int]],
              delivered_ago: int | None = None, shipped_ago: int | None = None, method: str = "card") -> None:
    prices = {p[0]: p[3] for p in PRODUCTS}
    total = round(sum(prices[s] * q for s, q in items), 2)
    pid = f"PAY-{oid[4:]}"
    store.x("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (oid, customer, status, d(placed_ago), d(shipped_ago) if shipped_ago is not None else None,
             d(delivered_ago) if delivered_ago is not None else None, region, total, pid))
    for sku, qty in items:
        store.x("INSERT INTO order_items VALUES (?, ?, ?, ?, 'ok')", (oid, sku, qty, prices[sku]))
    store.x("INSERT INTO payments VALUES (?, ?, ?, ?, 0)", (pid, oid, method, total))


def add_shipment(sid: str, oid: str, kind: str, status: str, sku: str, eta_ago: int, delivered_ago: int | None = None,
                 proof: str | None = None, events: list[tuple[int, str, str, str]] = ()) -> None:
    store.x("INSERT INTO shipments VALUES (?, ?, ?, 'SwiftPost', ?, 'WH-EAST', ?, ?, ?, ?, ?)",
            (sid, oid, kind, status, sku, d(eta_ago), d(delivered_ago) if delivered_ago is not None else None,
             proof, store.now_iso()))
    for ago, st, loc, note in events:
        store.x("INSERT INTO shipment_events VALUES (?, ?, ?, ?, ?)", (sid, d(ago) + "T09:00:00", st, loc, note))


# The orders behind the demo cases (see app/scenarios.py) plus some background history.
# Held as data rather than inline calls so a single scenario can be restored to its seeded
# state without wiping the whole sandbox - see restore_orders().
SEED_ORDERS: list[dict[str, Any]] = [
    dict(oid = "ORD-50001", customer = "C-1001", status = "delivered", placed_ago = 5, region = "east",
         items = [("KT-220", 1), ("MUG-06", 1)], delivered_ago = 2, shipped_ago = 4),
    dict(oid = "ORD-50002", customer = "C-1002", status = "delivered", placed_ago = 18, region = "central",
         items = [("HP-510", 1)], delivered_ago = 14, shipped_ago = 16),
    dict(oid = "ORD-50003", customer = "C-1003", status = "paid", placed_ago = 1, region = "east",
         items = [("LMP-33", 1), ("THR-08", 1)]),
    dict(oid = "ORD-50004", customer = "C-1004", status = "delivered", placed_ago = 45, region = "west",
         items = [("THR-08", 1)], delivered_ago = 41, shipped_ago = 43),
    dict(oid = "ORD-50005", customer = "C-1005", status = "delivered", placed_ago = 4, region = "central",
         items = [("ESP-900", 1)], delivered_ago = 1, shipped_ago = 3),
    dict(oid = "ORD-50006", customer = "C-1006", status = "shipped", placed_ago = 14, region = "west",
         items = [("CHR-14", 1)], shipped_ago = 12),
    dict(oid = "ORD-50007", customer = "C-1007", status = "delivered", placed_ago = 6, region = "east",
         items = [("HP-510", 1)], delivered_ago = 3, shipped_ago = 5),
    dict(oid = "ORD-50008", customer = "C-1008", status = "delivered", placed_ago = 4, region = "central",
         items = [("MUG-06", 1)], delivered_ago = 1, shipped_ago = 3),
    dict(oid = "ORD-50009", customer = "C-1008", status = "delivered", placed_ago = 6, region = "central",
         items = [("LMP-33", 1)], delivered_ago = 3, shipped_ago = 5),
    dict(oid = "ORD-50010", customer = "C-1009", status = "delivered", placed_ago = 20, region = "east",
         items = [("CND-02", 2)], delivered_ago = 16, shipped_ago = 18),
    dict(oid = "ORD-49980", customer = "C-1001", status = "delivered", placed_ago = 90, region = "east",
         items = [("THR-08", 1)], delivered_ago = 86, shipped_ago = 88),
    dict(oid = "ORD-49981", customer = "C-1005", status = "delivered", placed_ago = 60, region = "central",
         items = [("GRD-120", 1)], delivered_ago = 55, shipped_ago = 57),
]

SEED_SHIPMENTS: list[dict[str, Any]] = [
    dict(sid = "SHP-7001", oid = "ORD-50001", kind = "outbound", status = "delivered", sku = "KT-220",
         eta_ago = 2, delivered_ago = 2, proof = "Signed by A. Stone",
         events = [(4, "in_transit", "East Hub", "Picked up"), (2, "delivered", "Boston, MA", "Signed by A. Stone")]),
    dict(sid = "SHP-7002", oid = "ORD-50002", kind = "outbound", status = "delivered", sku = "HP-510",
         eta_ago = 14, delivered_ago = 14, proof = "Front desk"),
    dict(sid = "SHP-7102", oid = "ORD-50002", kind = "return", status = "delivered", sku = "HP-510",
         eta_ago = 2, delivered_ago = 2, proof = "Received at WH-CENTRAL returns dock",
         events = [(5, "in_transit", "Denver, CO", "Return dropped off"),
                   (2, "delivered", "WH-CENTRAL", "Return received")]),
    dict(sid = "SHP-7004", oid = "ORD-50004", kind = "outbound", status = "delivered", sku = "THR-08",
         eta_ago = 41, delivered_ago = 41, proof = "Left at door"),
    dict(sid = "SHP-7005", oid = "ORD-50005", kind = "outbound", status = "delivered", sku = "ESP-900",
         eta_ago = 1, delivered_ago = 1, proof = "Signed by Z. Okafor"),
    dict(sid = "SHP-7006", oid = "ORD-50006", kind = "outbound", status = "in_transit", sku = "CHR-14",
         eta_ago = 7, delivered_ago = None, proof = None,
         events = [(12, "in_transit", "East Hub", "Picked up"),
                   (9, "in_transit", "Kansas City, MO", "Arrived at facility")]),
    dict(sid = "SHP-7007", oid = "ORD-50007", kind = "outbound", status = "delivered", sku = "HP-510",
         eta_ago = 3, delivered_ago = 3, proof = "Photo: parcel at front door",
         events = [(5, "in_transit", "East Hub", "Picked up"),
                   (3, "delivered", "Newark, NJ", "Delivered - photo taken")]),
    dict(sid = "SHP-7008", oid = "ORD-50008", kind = "outbound", status = "delivered", sku = "MUG-06",
         eta_ago = 1, delivered_ago = 1, proof = "Left at door"),
    dict(sid = "SHP-7009", oid = "ORD-50009", kind = "outbound", status = "delivered", sku = "LMP-33",
         eta_ago = 3, delivered_ago = 3, proof = "Left at door"),
]


def restore_orders(order_ids: list[str]) -> None:
    """Roll the given orders back to their seeded state, undoing a previous demo run.

    Scenarios are isolated from each other (each owns its own order and customer), so
    restoring just one scenario's slice lets it be replayed without wiping the evidence
    that other cases left in the enterprise systems.
    """
    ids = [o for o in order_ids if o]
    if not ids:
        return
    ph = ",".join("?" * len(ids))
    customers = [r["customer_id"] for r in
                 store.q(f"SELECT DISTINCT customer_id FROM orders WHERE id IN ({ph})", ids)]
    skus = {sku for o in SEED_ORDERS if o["oid"] in ids for sku, _ in o["items"]}
    skus |= {s["sku"] for s in SEED_SHIPMENTS if s["oid"] in ids}

    # Drop everything a previous run created, then re-seed from the definitions above.
    store.x(f"DELETE FROM refunds WHERE order_id IN ({ph})", ids)
    store.x(f"DELETE FROM shipment_events WHERE shipment_id IN "
            f"(SELECT id FROM shipments WHERE order_id IN ({ph}))", ids)
    store.x(f"DELETE FROM shipments WHERE order_id IN ({ph})", ids)
    store.x(f"DELETE FROM order_items WHERE order_id IN ({ph})", ids)
    store.x(f"DELETE FROM payments WHERE order_id IN ({ph})", ids)
    store.x(f"DELETE FROM orders WHERE id IN ({ph})", ids)

    if customers:
        cph = ",".join("?" * len(customers))
        store.x(f"DELETE FROM store_credit WHERE customer_id IN ({cph})", customers)
        store.x(f"DELETE FROM notifications WHERE customer_id IN ({cph})", customers)
        for c in CUSTOMERS:
            if c[0] in customers:
                store.x("UPDATE customers SET claims_90d = ?, last_goodwill_at = NULL WHERE id = ?", (c[5], c[0]))

    for o in SEED_ORDERS:
        if o["oid"] in ids:
            add_order(**o)
    for s in SEED_SHIPMENTS:
        if s["oid"] in ids:
            add_shipment(**s)

    for sku in skus:
        for w in WAREHOUSES:
            store.x("UPDATE inventory SET on_hand = ?, reserved = 0 WHERE sku = ? AND warehouse_id = ?",
                    (STOCK_OVERRIDES.get((sku, w[0]), 20), sku, w[0]))

    # Fired world events are left in place as history - the Systems view shows them, and
    # schedule_event re-arms this scenario's own events when it is filed again.
    store.x("DELETE FROM kv WHERE key = 'gateway_failures'")  # clear a half-consumed outage
    store.audit("world", "restore", f"Restored seeded state for {', '.join(ids)}", kind = "system")


def reset() -> None:
    store.init()
    with store.connect() as con:  # clear in place: deleting an open SQLite file fails on Windows
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                            "AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            con.execute(f"DELETE FROM {t}")
    for c in CUSTOMERS:
        store.x("INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?, ?, NULL)", (c[0], c[1], c[2], c[3], d(700), c[4], c[5]))
    for p in PRODUCTS:
        store.x("INSERT INTO products VALUES (?, ?, ?, ?, ?)", p)
    for w in WAREHOUSES:
        store.x("INSERT INTO warehouses VALUES (?, ?, ?)", w)
    for (wh, region), days in TRANSIT.items():
        store.x("INSERT INTO transit_days VALUES (?, ?, ?)", (wh, region, days))
    for p in PRODUCTS:
        for w in WAREHOUSES:
            store.x("INSERT INTO inventory VALUES (?, ?, ?, 0)", (p[0], w[0], STOCK_OVERRIDES.get((p[0], w[0]), 20)))

    for o in SEED_ORDERS:
        add_order(**o)
    for s in SEED_SHIPMENTS:
        add_shipment(**s)
    store.audit("world", "reset", "Enterprise sandbox seeded", kind = "system")

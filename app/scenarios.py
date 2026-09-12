"""Sample customer cases for demos and tests.

Each case is filed exactly like a real customer would file it. Some also schedule a
"world event" in the enterprise sandbox that changes conditions while the agent is
mid-plan (stock sells out, an order ships, the payment gateway blips).
"""

from __future__ import annotations

import shutil
from datetime import date, timedelta
from typing import Any

from app import config, db
from app.sandbox import services, world


def _weekday_in(days: int) -> str:
    return (date.today() + timedelta(days = days)).strftime("%A")


def scenarios() -> dict[str, dict[str, Any]]:
    return {
        "damaged_stockout": {
            "label": "Damaged item, replacement sells out mid-plan",
            "customer": ("Ava Stone", "ava.stone@example.com"),
            "subject": "Kettle arrived cracked",
            "message": f"Hi, my order ORD-50001 arrived today and the ceramic kettle is cracked down the side. "
                       f"Could you send a replacement? I need it by {_weekday_in(3)} - it's a birthday gift.",
            "events": [("before:create_replacement", {"type": "set_stock", "sku": "KT-220", "warehouse_id": "WH-EAST",
                                                      "on_hand": 0},
                        "The last KT-220 unit in WH-EAST was sold through the marketplace channel")],
            "shows": "Plan: replacement from WH-EAST → reservation fails (OUT_OF_STOCK) → other warehouses too slow "
                     "for the deadline → adapts to a refund → verified.",
        },
        "refund_gateway": {
            "label": "Refund with payment-gateway outage",
            "customer": ("Liam Patel", "liam.patel@example.com"),
            "subject": "Where is my refund?",
            "message": "I sent back the wireless headphones from order ORD-50002 last week and tracking says you "
                       "received them. When will I get my refund?",
            "events": [("before:refund", {"type": "gateway_outage", "failures": 1}, "Payment gateway returned HTTP 503")],
            "shows": "Refund attempt hits a 503 → safe idempotent retry → refund posted → verified in the ledger.",
        },
        "cancel_shipped": {
            "label": "Cancellation races with shipping",
            "customer": ("Mia Moreau", "mia.moreau@example.com"),
            "subject": "Please cancel my order",
            "message": "Please cancel order ORD-50003, I picked the wrong colour for the lamp and the throw.",
            "events": [("before:cancel_order:ORD-50003", {"type": "ship_order", "order_id": "ORD-50003"},
                        "Carrier picked up ORD-50003 from WH-EAST")],
            "shows": "Cancel refused (ORDER_ALREADY_SHIPPED) → re-plans to prepaid return labels per policy → verified.",
        },
        "late_return": {
            "label": "Return outside the window (goodwill)",
            "customer": ("Noah Kim", "noah.kim@example.com"),
            "subject": "Return the wool throw",
            "message": "I'd like to return the wool throw blanket from ORD-50004, it doesn't fit our sofa.",
            "events": [],
            "shows": "Refund not allowed (41 days > 30) → gold-tier goodwill credit within limits → verified.",
        },
        "wrong_item_high_value": {
            "label": "Wrong high-value item (needs supervisor)",
            "customer": ("Zara Okafor", "zara.okafor@example.com"),
            "subject": "Wrong item delivered - espresso machine",
            "message": "I ordered the Barista Espresso Machine (ORD-50005) but the box contained a coffee grinder "
                       "instead. I'd like a full refund please.",
            "events": [],
            "shows": "Refund 749.00 exceeds the auto-approval limit → pauses for a human → executes → verified.",
        },
        "lost_package": {
            "label": "Lost package (re-ship)",
            "customer": ("Omar Rossi", "omar.rossi@example.com"),
            "subject": "Where is my chair?",
            "message": "Where is my dining chair? I ordered it almost two weeks ago and tracking hasn't moved in days.",
            "events": [],
            "shows": "Tracking is 7 days past ETA → lost under policy → re-ships from the nearest warehouse → verified.",
        },
        "claims_review": {
            "label": "Suspicious non-delivery claim (escalate)",
            "customer": ("Kenji Tanaka", "kenji.tanaka@example.com"),
            "subject": "Headphones never arrived",
            "message": "My headphones never arrived. I want a refund now.",
            "events": [],
            "shows": "Carrier photo proof + 4 claims in 90 days → refuses to auto-refund → escalates with evidence.",
        },
        "ambiguous": {
            "label": "Vague request (asks the customer)",
            "customer": ("Sofia Berg", "sofia.berg@example.com"),
            "subject": "Something arrived broken",
            "message": "Hi, one of my items arrived broken. Please help.",
            "events": [],
            "reply": "It's the table lamp - the linen shade is torn. A replacement is fine.",
            "shows": "Two recent orders → asks which item → customer replies → replacement + return label → verified.",
        },
    }


def reset_all() -> None:
    """Rebuild the enterprise sandbox and wipe the desk."""
    if config.UPLOAD_DIR.exists():
        shutil.rmtree(config.UPLOAD_DIR, ignore_errors = True)
    db.init()
    with db.connect() as con:  # clear in place: deleting an open SQLite file fails on Windows
        for t in ("cases", "messages", "attachments", "proposals", "agent_events", "kb"):
            con.execute(f"DELETE FROM {t}")
        con.execute("DELETE FROM sqlite_sequence")
    world.reset()


def ensure() -> None:
    if not config.STORE_DB.exists() or not config.APP_DB.exists():
        reset_all()
    else:
        db.init()


# The orders each scenario acts on, so a scenario can be replayed from a clean slate
# without resetting the whole sandbox (see world.restore_orders).
SCENARIO_ORDERS: dict[str, list[str]] = {
    "damaged_stockout": ["ORD-50001"],
    "refund_gateway": ["ORD-50002"],
    "cancel_shipped": ["ORD-50003"],
    "late_return": ["ORD-50004"],
    "wrong_item_high_value": ["ORD-50005"],
    "lost_package": ["ORD-50006"],
    "claims_review": ["ORD-50007"],
    "ambiguous": ["ORD-50008", "ORD-50009"],
}


def file_case(key: str) -> dict[str, Any]:
    sc = scenarios()[key]
    # Replaying a scenario must behave exactly like the first run: a previous run may have
    # refunded the order, consumed the stock or fired the world event.
    world.restore_orders(SCENARIO_ORDERS.get(key, []))
    for trigger, effect, desc in sc["events"]:
        services.schedule_event(trigger, effect, desc)
    name, email = sc["customer"]
    case = db.create_case(subject = sc["subject"], description = sc["message"], customer_name = name,
                          customer_email = email, channel = "email", scenario = key)
    db.add_message(case["id"], "customer", name, sc["message"])
    return case

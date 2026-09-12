"""End-to-end case lifecycle tests in offline mode (no API key needed).

Each test files a case like a customer would, lets the agents run, and asserts on the
*enterprise system state* - not on what the agents say.
"""

from __future__ import annotations

import os
import tempfile
import time

os.environ["LLM_MODE"] = "offline"
os.environ["CASEPILOT_DATA_DIR"] = tempfile.mkdtemp(prefix = "casepilot_test_")

import pytest  # noqa: E402

from app import db  # noqa: E402
from app.agents import orchestrator  # noqa: E402
from app.sandbox import store  # noqa: E402
from app import scenarios  # noqa: E402

BUSY = ("New", "Triage", "Investigating", "Planning", "Policy Review", "Executing", "Verifying")


def run(key: str) -> dict:
    case = scenarios.file_case(key)
    orchestrator.start(case["id"])
    return wait(case["id"])


def wait(cid: int, timeout: float = 30) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        c = db.get_case(cid)
        if not orchestrator.is_running(cid) and c["status"] not in BUSY:
            return c
        time.sleep(0.1)
    raise TimeoutError(db.get_case(cid))


def trace(cid: int) -> list[str]:
    return [e["payload"].get("text", "") for e in db.events(cid) if e["type"] == "stage"]


@pytest.fixture(autouse = True)
def fresh():
    scenarios.reset_all()


def test_damaged_item_adapts_to_stockout():
    c = run("damaged_stockout")
    assert c["status"] == "Resolved", trace(c["id"])
    assert any("OUT_OF_STOCK" in t for t in trace(c["id"]))
    assert store.q1("SELECT SUM(amount) s FROM refunds WHERE order_id = 'ORD-50001'")["s"] == 38.0
    assert store.q1("SELECT COUNT(*) n FROM shipments WHERE order_id = 'ORD-50001' AND kind = 'replacement'")["n"] == 0


def test_refund_survives_gateway_outage():
    c = run("refund_gateway")
    assert c["status"] == "Resolved", trace(c["id"])
    assert any("retrying" in t for t in trace(c["id"]))
    assert store.q1("SELECT COUNT(*) n, SUM(amount) s FROM refunds WHERE order_id = 'ORD-50002'") == {"n": 1, "s": 129.0}


def test_cancel_after_ship_becomes_return():
    c = run("cancel_shipped")
    assert c["status"] == "Resolved", trace(c["id"])
    assert store.q1("SELECT status FROM orders WHERE id = 'ORD-50003'")["status"] == "shipped"
    assert store.q1("SELECT COUNT(*) n FROM shipments WHERE order_id = 'ORD-50003' AND kind = 'return'")["n"] == 2


def test_late_return_gets_goodwill_not_refund():
    c = run("late_return")
    assert c["status"] == "Resolved", trace(c["id"])
    assert store.q1("SELECT COUNT(*) n FROM refunds WHERE order_id = 'ORD-50004'")["n"] == 0
    assert store.q1("SELECT amount FROM store_credit WHERE customer_id = 'C-1004'")["amount"] == 25.0


def test_high_value_needs_human_then_executes():
    c = run("wrong_item_high_value")
    assert c["status"] == "Awaiting Approval"
    assert store.q1("SELECT COUNT(*) n FROM refunds WHERE order_id = 'ORD-50005'")["n"] == 0
    orchestrator.decide(c["id"], True, "Test Supervisor")
    c = wait(c["id"])
    assert c["status"] == "Resolved", trace(c["id"])
    assert store.q1("SELECT SUM(amount) s FROM refunds WHERE order_id = 'ORD-50005'")["s"] == 749.0


def test_lost_package_reships():
    c = run("lost_package")
    assert c["status"] == "Resolved", trace(c["id"])
    assert store.q1("SELECT warehouse_id FROM shipments WHERE order_id = 'ORD-50006' AND kind = 'replacement'")[
        "warehouse_id"] == "WH-WEST"


def test_claims_review_escalates_without_money_moving():
    c = run("claims_review")
    assert c["status"] == "Escalated" and c["assignee"] == "Trust & Safety"
    assert store.q1("SELECT COUNT(*) n FROM refunds WHERE order_id = 'ORD-50007'")["n"] == 0


def test_portal_case_outside_policy_moves_no_money():
    case = db.create_case(subject = "Candle set arrived shattered", customer_name = "Diego Novak",
                          customer_email = "diego.novak@example.com", channel = "portal",
                          description = "The candle set from ORD-50010 arrived shattered. Can I get a refund?")
    db.add_message(case["id"], "customer", "Diego Novak", case["description"])
    orchestrator.start(case["id"])
    c = wait(case["id"])
    assert c["status"] == "Resolved" and c["resolution_type"] == "declined", trace(c["id"])
    assert store.q1("SELECT COUNT(*) n FROM refunds WHERE order_id = 'ORD-50010'")["n"] == 0


def test_ambiguous_case_asks_then_resolves():
    c = run("ambiguous")
    assert c["status"] == "Waiting on Customer"
    db.add_message(c["id"], "customer", "Sofia Berg", scenarios.scenarios()["ambiguous"]["reply"])
    assert orchestrator.customer_replied(c["id"])
    c = wait(c["id"])
    assert c["status"] == "Resolved", trace(c["id"])
    assert c["order_id"] == "ORD-50009"
    assert store.q1("SELECT COUNT(*) n FROM shipments WHERE order_id = 'ORD-50009' AND kind IN ('replacement', 'return')")["n"] == 2


def test_scenarios_replay_without_a_reset():
    """A judge clicking the same demo case twice must get the same result.

    Every other test resets the sandbox first, which hid this: a second run used to find the
    order already refunded and the stock already gone, so the agent escalated instead of
    adapting. Scenarios restore their own slice of the world (world.restore_orders).
    """
    expected = {
        "damaged_stockout": "Resolved",
        "refund_gateway": "Resolved",
        "cancel_shipped": "Resolved",
        "late_return": "Resolved",
        "wrong_item_high_value": "Awaiting Approval",
        "lost_package": "Resolved",
        "claims_review": "Escalated",
    }
    for attempt in (1, 2, 3):
        for key, want in expected.items():
            c = run(key)
            assert c["status"] == want, f"{key} attempt {attempt}: {c['status']}\n" + "\n".join(trace(c["id"]))
            assert not [e for e in db.events(c["id"]) if e["type"] == "error"], \
                f"{key} attempt {attempt} raised an internal error"

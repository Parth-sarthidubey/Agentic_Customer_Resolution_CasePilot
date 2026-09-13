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

from app import config, db  # noqa: E402
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


def test_chat_triage_offers_choices_and_resolves_on_a_tap():
    """The chat triage loop, end to end.

    A vague message from a customer with two recent orders must come back as a question that
    carries selectable options - not a bare "what is your order number?" - and picking one must
    bind that order and carry the case through to a resolution.
    """
    case = db.create_case(subject = "Something is broken", customer_name = "Sofia Berg",
                          customer_email = "sofia.berg@example.com", channel = "chat",
                          description = "something I bought is broken and I want it sorted")
    db.add_message(case["id"], "customer", "Sofia Berg", case["description"])
    orchestrator.start(case["id"])
    c = wait(case["id"])
    assert c["status"] == "Waiting on Customer", trace(c["id"])

    asked = [m for m in db.messages(c["id"], include_internal = False)
             if m["sender"] == "agent" and (m.get("meta") or {}).get("triage")]
    assert len(asked) == 1, "expected exactly one triage question"
    choices = asked[0]["meta"]["choices"]
    assert len(choices) >= 2, choices
    assert any("ORD-50009" in ch for ch in choices), choices

    picked = next(ch for ch in choices if "ORD-50009" in ch)
    db.add_message(c["id"], "customer", "Sofia Berg", picked)
    assert orchestrator.customer_replied(c["id"])
    c = wait(c["id"])
    assert c["status"] == "Resolved", trace(c["id"])
    assert c["order_id"] == "ORD-50009"


def test_triage_gives_up_to_a_human_rather_than_looping():
    """An unidentifiable customer must reach a person, not keep asking forever."""
    case = db.create_case(subject = "Help", customer_name = "Nobody",
                          customer_email = "nobody@nowhere.example", channel = "chat",
                          description = "my thing is broken")
    db.add_message(case["id"], "customer", "Nobody", case["description"])
    for _ in range(config.MAX_TRIAGE_ROUNDS + 1):
        orchestrator.start(case["id"])
        c = wait(case["id"])
        if c["status"] == "Routed":
            break
        assert c["status"] == "Waiting on Customer", trace(c["id"])
        db.add_message(case["id"], "customer", "Nobody", "I still do not know")
    assert c["status"] == "Routed", trace(c["id"])
    asked = [m for m in db.messages(case["id"], include_internal = False)
             if (m.get("meta") or {}).get("triage")]
    assert len(asked) <= config.MAX_TRIAGE_ROUNDS


def test_an_order_number_that_does_not_exist_is_never_substituted():
    """Naming a non-existent order must produce a question, not a refund on a different one.

    Found on the live chain: "ORD-99999 never turned up, refund me" bound the customer's real
    ORD-50001 and planned a 32.00 refund against it. Refunding the wrong order is far worse than
    asking, so an id the customer gave that is not in the records stops the case.
    """
    case = db.create_case(subject = "problem with ORD-99999", customer_name = "Ava Stone",
                          customer_email = "ava.stone@example.com", channel = "email",
                          description = "Order ORD-99999 never turned up. Refund me.")
    db.add_message(case["id"], "customer", "Ava Stone", case["description"])
    orchestrator.start(case["id"])
    c = wait(case["id"])
    assert c["status"] in ("Waiting on Customer", "Routed"), trace(c["id"])
    assert store.q1("SELECT COUNT(*) n FROM refunds WHERE order_id = 'ORD-50001'")["n"] == 0
    asked = [m for m in db.messages(c["id"], include_internal = False)
             if (m.get("meta") or {}).get("triage")]
    assert asked and "ORD-99999" in asked[-1]["body"], asked


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

"""Run CasePilot against the REAL model chain and report honestly.

The pytest suite pins LLM_MODE=offline so it stays hermetic, which means it exercises the
deterministic rule engine and never the agents. This script is the opposite: it drives a running
server over HTTP and fails when

  * a case ends in the wrong state, or
  * any agent fell back to the rule engine (a silent fallback is what lets a broken model path
    look healthy), or
  * the orchestrator recorded an internal error.

Usage:
    uv run uvicorn app.main:app --port 8000          # in one terminal
    uv run python scripts/live_check.py              # in another
    uv run python scripts/live_check.py --only damaged_stockout --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

BASE = "http://127.0.0.1:8000"
DONE = {"Resolved", "Escalated", "Awaiting Approval", "Waiting on Customer", "Routed"}

# Expected final state for each built-in demo scenario.
SCENARIOS: dict[str, str] = {
    "damaged_stockout": "Resolved",
    "refund_gateway": "Resolved",
    "cancel_shipped": "Resolved",
    "late_return": "Resolved",
    "wrong_item_high_value": "Awaiting Approval",
    "lost_package": "Resolved",
    "claims_review": "Escalated",
    "ambiguous": "Waiting on Customer",
}

# Free-form messages a tester might actually type. `any_of` lists acceptable end states -
# several of these are legitimately ambiguous and asking a question is the correct answer.
FREEFORM: list[dict[str, Any]] = [
    {"name": "damaged, no order id", "email": "mia.moreau@example.com",
     "subject": "my lamp came smashed", "body": "The linen table lamp turned up with the shade torn. Not happy.",
     "any_of": {"Resolved", "Awaiting Approval", "Waiting on Customer"}},
    {"name": "vague, multiple orders", "email": "sofia.berg@example.com",
     "subject": "something is wrong", "body": "One of my things is broken, please sort it out.",
     "any_of": {"Waiting on Customer"}},
    {"name": "where is my stuff", "email": "omar.rossi@example.com",
     "subject": "where is my stuff", "body": "It has been ages. Where is my order?",
     "any_of": {"Resolved", "Waiting on Customer", "Escalated"}},
    {"name": "invented order id", "email": "ava.stone@example.com",
     "subject": "problem with ORD-99999", "body": "Order ORD-99999 never turned up. Refund me.",
     "any_of": {"Waiting on Customer", "Escalated", "Resolved"}},
    {"name": "unknown customer", "email": "nobody@example.com",
     "subject": "where is my order", "body": "I ordered a kettle last week and it has not arrived.",
     "any_of": {"Waiting on Customer", "Escalated"}},
    {"name": "off topic", "email": "liam.patel@example.com",
     "subject": "do you sell batteries", "body": "Do you stock AA batteries? Looking to buy some.",
     "any_of": {"Resolved", "Routed", "Waiting on Customer", "Escalated"}},
    {"name": "angry, wants everything back", "email": "noah.kim@example.com",
     "subject": "I want a refund for everything", "body": "This is unacceptable. Refund my whole order right now.",
     "any_of": {"Resolved", "Awaiting Approval", "Waiting on Customer", "Escalated"}},
    {"name": "asks for status only", "email": "zara.okafor@example.com",
     "subject": "any update?", "body": "Just checking whether there is any update on my espresso machine.",
     "any_of": {"Resolved", "Waiting on Customer", "Awaiting Approval", "Escalated"}},
]


def api(path: str, method: str = "GET", data: dict[str, Any] | None = None, timeout: int = 30) -> Any:
    body, headers = None, {}
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data = body, headers = headers, method = method)
    with urllib.request.urlopen(req, timeout = timeout) as r:
        return json.loads(r.read().decode() or "{}")


def form(path: str, fields: dict[str, str], timeout: int = 30) -> Any:
    """Multipart-free form post (the API takes Form fields)."""
    boundary = "----casepilotlivecheck"
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n")
    payload = ("".join(parts) + f"--{boundary}--\r\n").encode()
    req = urllib.request.Request(BASE + path, data = payload, method = "POST",
                                 headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout = timeout) as r:
        return json.loads(r.read().decode() or "{}")


def wait(cid: int, limit: float) -> str:
    end = time.time() + limit
    status = "?"
    while time.time() < end:
        try:
            status = api(f"/api/cases/{cid}")["case"]["status"]
        except Exception:
            time.sleep(2)
            continue
        if status in DONE:
            return status
        time.sleep(2)
    return f"{status} (timed out)"


def inspect(cid: int) -> dict[str, Any]:
    """Which engine answered for each agent, plus any internal errors."""
    events = api(f"/api/cases/{cid}/events")
    providers, fallbacks, errors = {}, [], []
    for e in events:
        p = e.get("payload") or {}
        if e.get("type") == "output":
            providers[e.get("agent")] = p.get("provider")
        elif e.get("type") == "fallback":
            fallbacks.append(f"{p.get('agent')}: {p.get('reason', '')[:70]}")
        elif e.get("type") == "info" and "Falling back" in str(p.get("text")):
            fallbacks.append(str(p.get("text"))[:90])
        elif e.get("type") == "error":
            errors.append(str(p.get("text"))[:90])
    return {"providers": providers, "fallbacks": fallbacks, "errors": errors}


def report(name: str, got: str, want: str, detail: dict[str, Any], verbose: bool) -> bool:
    ok_state = got == want if isinstance(want, str) else got in want
    ok = ok_state and not detail["fallbacks"] and not detail["errors"]
    mark = "PASS" if ok else "FAIL"
    want_s = want if isinstance(want, str) else "|".join(sorted(want))
    print(f"  {mark:4}  {name:30} {got:22} want {want_s}")
    if detail["fallbacks"]:
        print(f"        !! fell back to rules: {'; '.join(detail['fallbacks'][:3])}")
    if detail["errors"]:
        print(f"        !! internal error: {'; '.join(detail['errors'][:2])}")
    if verbose:
        for agent, prov in detail["providers"].items():
            print(f"          {agent:24} {prov}")
    return ok


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default = BASE)
    ap.add_argument("--only", help = "run one scenario or free-form case by name")
    ap.add_argument("--timeout", type = float, default = 180, help = "seconds per case")
    ap.add_argument("--skip-freeform", action = "store_true")
    ap.add_argument("--no-reset", action = "store_true")
    ap.add_argument("--verbose", action = "store_true")
    args = ap.parse_args()

    BASE = args.base.rstrip("/")

    try:
        status = api("/api/status", timeout = 10)
    except (urllib.error.URLError, OSError) as exc:
        print(f"Cannot reach {BASE} - start the server first.\n  {exc}")
        return 2

    chain = " -> ".join(p["name"] for p in status["llm"]["providers"]) or "none"
    print(f"CasePilot live check  ·  {BASE}")
    print(f"provider chain: {chain} -> offline rules\n")
    if status["llm"]["mode"] == "offline":
        print("REFUSING TO RUN: no model configured, so this would only test the rule engine.")
        print("Set a provider key in .env (GROQ_API_KEY is the fastest) and try again.")
        return 2

    if not args.no_reset:
        api("/api/reset", method = "POST", timeout = 90)

    results: list[bool] = []

    print("Demo scenarios")
    for key, want in SCENARIOS.items():
        if args.only and args.only != key:
            continue
        case = api(f"/api/scenarios/{key}", method = "POST")
        got = wait(case["id"], args.timeout)
        results.append(report(key, got, want, inspect(case["id"]), args.verbose))

    if not args.skip_freeform:
        print("\nFree-form cases (as a tester would type them)")
        for spec in FREEFORM:
            if args.only and args.only != spec["name"]:
                continue
            case = form("/api/cases", {"subject": spec["subject"], "description": spec["body"],
                                       "customer_email": spec["email"], "channel": "portal"})
            got = wait(case["id"], args.timeout)
            results.append(report(spec["name"], got, spec["any_of"], inspect(case["id"]), args.verbose))

    passed = sum(results)
    print(f"\n{passed}/{len(results)} passed")
    if passed != len(results):
        print("A FAIL means the wrong end state, a fallback to the rule engine, or an internal error.")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

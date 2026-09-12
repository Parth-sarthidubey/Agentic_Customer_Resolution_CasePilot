"""Policy handbook retrieval and the deterministic approval gate.

The LLM may *read* policy, but whether a plan needs a human is decided here, in code.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from functools import lru_cache
from typing import Any

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


def approval_reasons(actions: list[dict[str, Any]], risk_flags: list[str]) -> list[str]:
    """Deterministic guardrail: which parts of a plan need a human before execution."""
    reasons = []
    refund_total = sum(float(a["params"].get("amount", 0)) for a in actions if a["action"] == "refund")
    credit_total = sum(float(a["params"].get("amount", 0)) for a in actions if a["action"] == "issue_store_credit")
    if refund_total > config.AUTO_REFUND_LIMIT:
        reasons.append(f"refund total {refund_total:.2f} exceeds auto-approval limit {config.AUTO_REFUND_LIMIT:.2f} (POL-REF-1)")
    if credit_total > config.AUTO_CREDIT_LIMIT:
        reasons.append(f"store credit {credit_total:.2f} exceeds {config.AUTO_CREDIT_LIMIT:.2f} (POL-GW-1)")
    if any("fraud" in f.lower() or "claims" in f.lower() for f in risk_flags):
        reasons.append("claims-review risk flag present (POL-FRAUD-1)")
    return reasons

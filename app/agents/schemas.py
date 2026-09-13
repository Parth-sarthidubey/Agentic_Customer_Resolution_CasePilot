"""Structured outputs each agent must return (validated with pydantic).

These contracts are deliberately forgiving about *recoverable* omissions. Free-tier models drop
a field often enough that strict validation was the single biggest cause of an agent failing and
the whole step silently falling back to the rule engine - losing a complete, useful answer over
one absent string. Anything that can be inferred or safely defaulted is; anything that changes
what happens to a customer's money stays required.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Goal = Literal["refund", "replacement", "cancel", "return", "where_is_my_order", "damaged_item", "wrong_item",
               "complaint", "question", "other"]

_GOAL_ALIASES = {
    "replace": "replacement", "replacement_request": "replacement", "exchange": "replacement",
    "refund_request": "refund", "money_back": "refund", "return_request": "return",
    "cancellation": "cancel", "cancel_order": "cancel", "damaged": "damaged_item",
    "damage": "damaged_item", "broken_item": "damaged_item", "wrong": "wrong_item",
    "wrong_product": "wrong_item", "missing": "where_is_my_order", "delivery": "where_is_my_order",
    "tracking": "where_is_my_order", "lost_package": "where_is_my_order", "status": "question",
    "enquiry": "question", "inquiry": "question", "general": "other", "unknown": "other",
}
_ID = re.compile(r"(ORD-\d+|C-\d+)", re.I)



def _norm(value: Any, allowed: set[str], aliases: dict[str, str], default: str) -> str:
    """Map a near-miss enum value onto one the schema allows."""
    if not isinstance(value, str):
        return default
    key = value.strip().lower().replace(" ", "_").replace("-", "_")
    if key in allowed:
        return key
    if key in aliases:
        return aliases[key]
    for a in allowed:                       # 'refund the customer' -> 'refund'
        if a in key:
            return a
    return default


class Intake(BaseModel):
    goal: Goal = "other"
    customer_id: str | None = None
    order_id: str | None = None
    sku: str | None = None
    need_by: str | None = Field(default = None, description = "YYYY-MM-DD if the customer gave a deadline")
    priority: Literal["P1", "P2", "P3", "P4"] = "P3"
    sentiment: Literal["angry", "frustrated", "neutral", "positive"] = "neutral"
    missing_info: list[str] = Field(default_factory = list, description = "facts still needed from the customer")
    clarifying_question: str | None = Field(default = None, description = "one question to ask if info is missing")
    choices: list[str] = Field(default_factory = list,
                               description = "if the customer must pick between options, list them so the "
                                             "chat can show buttons (e.g. the two orders it could be)")
    summary: str = ""

    @field_validator("goal", mode = "before")
    @classmethod
    def _goal(cls, v: Any) -> str:
        return _norm(v, set(Goal.__args__), _GOAL_ALIASES, "other")

    @field_validator("priority", mode = "before")
    @classmethod
    def _priority(cls, v: Any) -> str:
        s = str(v or "").upper().strip()
        return s if s in {"P1", "P2", "P3", "P4"} else "P3"

    @field_validator("sentiment", mode = "before")
    @classmethod
    def _sentiment(cls, v: Any) -> str:
        s = str(v or "").lower().strip()
        return s if s in {"angry", "frustrated", "neutral", "positive"} else "neutral"

    @field_validator("order_id", "customer_id", mode = "before")
    @classmethod
    def _ids(cls, v: Any) -> str | None:
        """Models wrap ids in prose ('order ORD-50001'). Keep the id, drop the rest."""
        if v is None:
            return None
        m = _ID.search(str(v))
        return m.group(1).upper() if m else (str(v).strip() or None)


class Option(BaseModel):
    option: str = ""
    eligible: bool = False
    policy_ref: str | None = None
    reason: str = ""

    @model_validator(mode = "after")
    def _name_it(self) -> "Option":
        # A nameless option still carries meaning in its reason and policy reference; losing the
        # whole investigation because one label is missing helps nobody.
        if not self.option.strip():
            self.option = (self.policy_ref or self.reason.split(".")[0][:40] or "option").strip()
        return self


class Facts(BaseModel):
    findings: list[str] = Field(default_factory = list)
    options: list[Option] = Field(default_factory = list)
    risk_flags: list[str] = Field(default_factory = list)
    recommended: str = "inform_only"
    confidence: float = Field(default = 0.9, ge = 0, le = 1)

    @field_validator("findings", "risk_flags", mode = "before")
    @classmethod
    def _strings(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        out = []
        for item in v if isinstance(v, list) else [v]:
            out.append(item if isinstance(item, str) else
                       "; ".join(f"{k}: {x}" for k, x in item.items()) if isinstance(item, dict) else str(item))
        return out

    @field_validator("confidence", mode = "before")
    @classmethod
    def _conf(cls, v: Any) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.9
        return min(max(f / 100 if f > 1 else f, 0.0), 1.0)   # tolerate "85" meaning 85%


class ActionStep(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory = dict)
    rationale: str = ""

    @model_validator(mode = "before")
    @classmethod
    def _find_the_action(cls, v: Any) -> Any:
        """Recover the action name when the model put it somewhere else.

        Seen live: a step arriving as a bare parameter bag with no `action` at all, which failed
        validation and dropped the whole Resolver run to the rule engine - a working plan thrown
        away over the name of a key. The two shapes worth rescuing are an alias (`tool`, `name`)
        and the action used as the wrapping key, `{"create_replacement": {...}}`.
        """
        if not isinstance(v, dict) or v.get("action"):
            return v
        v = dict(v)
        for alias in ("tool", "name", "action_name", "type", "operation"):
            if isinstance(v.get(alias), str) and v[alias].strip():
                v["action"] = v.pop(alias)
                return v
        # {"create_replacement": {order_id: ...}} - a single key whose value is the parameter bag.
        from app.tools.actions import CATALOG  # imported here to keep the module import-cycle free
        known = set(CATALOG)
        for key, val in list(v.items()):
            if key in known and isinstance(val, dict):
                v["action"] = key
                v.setdefault("params", val)
                v.pop(key)
                return v
        # A flat bag of parameters with the name missing entirely; leave it to fail loudly rather
        # than guess which action moves the customer's money.
        return v

    @field_validator("params", mode = "before")
    @classmethod
    def _params(cls, v: Any) -> dict[str, Any]:
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            import json
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}


class Expected(BaseModel):
    refund_amount: float | None = None
    replacement_sku: str | None = None
    need_by: str | None = None
    return_label: bool = False
    cancelled: bool = False
    credit_amount: float | None = None


class Plan(BaseModel):
    decision: Literal["execute", "escalate", "inform_only"] = "inform_only"
    resolution_type: str = "information"
    summary: str = ""
    actions: list[ActionStep] = Field(default_factory = list)
    expected: Expected = Field(default_factory = Expected)
    policy_refs: list[str] = Field(default_factory = list)
    escalate_to: str | None = None
    escalation_reason: str | None = None

    @field_validator("decision", mode = "before")
    @classmethod
    def _decision(cls, v: Any) -> str:
        return _norm(v, {"execute", "escalate", "inform_only"},
                     {"act": "execute", "proceed": "execute", "run": "execute", "do": "execute",
                      "escalation": "escalate", "handoff": "escalate", "human": "escalate",
                      "inform": "inform_only", "informonly": "inform_only", "none": "inform_only",
                      "no_action": "inform_only", "reply_only": "inform_only"}, "inform_only")

    @model_validator(mode = "after")
    def _derive_expected(self) -> "Plan":
        """Fill the expected outcome from the actions when the model omits it.

        The verifier checks `expected` against the systems of record. An empty one means a case
        closes on a single generic invariant instead of proving what was actually promised - the
        verification looks thin precisely when it matters most.
        """
        e = self.expected
        for step in self.actions:
            p = step.params or {}
            if step.action == "refund" and e.refund_amount is None:
                try:
                    e.refund_amount = float(p.get("amount"))
                except (TypeError, ValueError):
                    pass
            elif step.action == "create_replacement" and not e.replacement_sku:
                e.replacement_sku = p.get("sku")
            elif step.action == "create_return_label":
                e.return_label = True
            elif step.action == "cancel_order":
                e.cancelled = True
            elif step.action == "issue_store_credit" and e.credit_amount is None:
                try:
                    e.credit_amount = float(p.get("amount"))
                except (TypeError, ValueError):
                    pass
        if self.actions and self.decision == "inform_only":
            self.decision = "execute"   # actions were planned; saying "inform only" contradicts them
        return self


class Review(BaseModel):
    verdict: Literal["approve", "revise"] = "approve"
    issues: list[str] = Field(default_factory = list)
    feedback: str = ""

    @field_validator("verdict", mode = "before")
    @classmethod
    def _verdict(cls, v: Any) -> str:
        return _norm(v, {"approve", "revise"},
                     {"approved": "approve", "ok": "approve", "pass": "approve", "accept": "approve",
                      "reject": "revise", "rejected": "revise", "revision": "revise",
                      "changes_requested": "revise", "fail": "revise"}, "approve")

    @field_validator("issues", mode = "before")
    @classmethod
    def _issues(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v.strip() else []
        return [x if isinstance(x, str) else str(x) for x in v]


class Reply(BaseModel):
    customer_message: str = ""
    internal_note: str = ""
    kb_title: str = ""
    kb_situation: str = ""
    kb_resolution: str = ""
    kb_tags: str = ""

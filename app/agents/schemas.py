"""Structured outputs each agent must return (validated with pydantic)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Goal = Literal["refund", "replacement", "cancel", "return", "where_is_my_order", "damaged_item", "wrong_item",
               "complaint", "other"]


class Intake(BaseModel):
    goal: Goal
    customer_id: str | None = None
    order_id: str | None = None
    sku: str | None = None
    need_by: str | None = Field(default = None, description = "YYYY-MM-DD if the customer gave a deadline")
    priority: Literal["P1", "P2", "P3", "P4"]
    sentiment: Literal["angry", "frustrated", "neutral", "positive"] = "neutral"
    missing_info: list[str] = Field(default_factory = list, description = "facts needed from the customer")
    clarifying_question: str | None = Field(default = None, description = "one question to ask if info is missing")
    summary: str


class Option(BaseModel):
    option: str
    eligible: bool
    policy_ref: str | None = None
    reason: str = ""


class Facts(BaseModel):
    findings: list[str] = Field(default_factory = list)
    options: list[Option] = Field(default_factory = list)
    risk_flags: list[str] = Field(default_factory = list)
    recommended: str = "inform_only"
    confidence: float = Field(default = 0.9, ge = 0, le = 1)


class ActionStep(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory = dict)
    rationale: str = ""


class Expected(BaseModel):
    refund_amount: float | None = None
    replacement_sku: str | None = None
    need_by: str | None = None
    return_label: bool = False
    cancelled: bool = False
    credit_amount: float | None = None


class Plan(BaseModel):
    decision: Literal["execute", "escalate", "inform_only"]
    resolution_type: str
    summary: str
    actions: list[ActionStep] = Field(default_factory = list)
    expected: Expected = Field(default_factory = Expected)
    policy_refs: list[str] = Field(default_factory = list)
    escalate_to: str | None = None
    escalation_reason: str | None = None


class Review(BaseModel):
    verdict: Literal["approve", "revise"]
    issues: list[str] = Field(default_factory = list)
    feedback: str


class Reply(BaseModel):
    customer_message: str
    internal_note: str
    kb_title: str
    kb_situation: str
    kb_resolution: str
    kb_tags: str

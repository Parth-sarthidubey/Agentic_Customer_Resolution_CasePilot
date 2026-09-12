"""The CasePilot agents: prompt + tools + output schema + deterministic fallback."""

from __future__ import annotations

import json
from typing import Any

from app import db
from app.agents import offline
from app.agents.base import load_prompt, run_agent
from app.agents.schemas import Facts, Intake, Plan, Reply, Review
from app.tools import actions
from app.tools import read_tools as rt


def _j(obj: Any) -> str:
    return json.dumps(obj, indent = 1, default = str)


def _case_view(c: dict[str, Any]) -> dict[str, Any]:
    keys = ("number", "subject", "description", "customer_email", "customer_name", "channel", "created_at")
    return {k: c.get(k) for k in keys}


def _evidence_tools(case: dict[str, Any]) -> list[Any]:
    """Conversation/attachment tools only when there is something to read.

    Their schemas cost tokens on every call of the run, and the opening message is already in
    the prompt, so a plain single-message case does not need them.
    """
    cid = case["id"]
    if db.attachments(cid) or len(db.messages(cid, include_internal = False)) > 1:
        return rt.case_tools(cid)
    return []


def intake(case: dict[str, Any]) -> Intake:
    tools = rt.INTAKE_TOOLS + _evidence_tools(case)
    return run_agent(case["id"], "Intake Agent", load_prompt("intake"), f"Case:\n{_j(_case_view(case))}",
                     tools, Intake, lambda tr: offline.intake(tr, case))


def investigator(case: dict[str, Any], it: Intake) -> Facts:
    user = f"Case:\n{_j(_case_view(case))}\n\nIntake:\n{_j(it.model_dump())}"
    tools = rt.INVESTIGATION_TOOLS + _evidence_tools(case)
    return run_agent(case["id"], "Investigator Agent", load_prompt("investigator"), user, tools, Facts,
                     lambda tr: offline.investigate(tr, case, it))


def resolver(case: dict[str, Any], it: Intake, facts: Facts, history: list[dict[str, Any]],
             feedback: str | None = None) -> Plan:
    system = load_prompt("resolver").replace("{catalog}", actions.catalog_for_prompt())
    user = f"Case:\n{_j(_case_view(case))}\n\nIntake:\n{_j(it.model_dump())}\n\nFacts:\n{_j(facts.model_dump())}"
    if history:
        user += f"\n\nPrevious attempts on this case (adapt - do not repeat what failed):\n{_j(history)}"
    if feedback:
        user += f"\n\nThe Policy Auditor rejected your last plan. Fix every point:\n{feedback}"
    return run_agent(case["id"], "Resolver Agent", system, user, rt.PLANNING_TOOLS, Plan,
                     lambda tr: offline.resolve(tr, case, it, facts, history))


def auditor(case: dict[str, Any], it: Intake, facts: Facts, plan: Plan) -> Review:
    user = (f"Customer goal: {it.goal} (order {it.order_id}, sku {it.sku}, need-by {it.need_by}).\n\n"
            f"Facts:\n{_j(facts.model_dump())}\n\nProposed plan:\n{_j(plan.model_dump())}")
    return run_agent(case["id"], "Policy Auditor", load_prompt("auditor"), user, rt.AUDIT_TOOLS, Review,
                     lambda tr: offline.audit(tr, facts, plan))


def communicator(case: dict[str, Any], plan: Plan, results: list[dict[str, Any]],
                 history: list[dict[str, Any]], verification: dict[str, Any]) -> Reply:
    user = (f"Case:\n{_j(_case_view(case))}\n\nFinal plan:\n{_j(plan.model_dump())}\n\nAction results:\n{_j(results)}"
            f"\n\nEarlier failed attempts:\n{_j(history)}\n\nVerification:\n{_j(verification)}")
    return run_agent(case["id"], "Communicator Agent", load_prompt("communicator"), user, [], Reply,
                     lambda tr: offline.communicate(tr, case, plan, results, history))

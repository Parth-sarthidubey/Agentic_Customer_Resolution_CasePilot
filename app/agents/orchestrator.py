"""Case lifecycle controller: observe -> decide -> act -> verify -> adapt.

New -> Triage -> (Waiting on Customer) -> Investigating -> Planning <-> Policy Review
    -> (Awaiting Approval) -> Executing -> Verifying -> Resolved
Blocked action or failed verification -> back to Planning with the new facts (bounded).
No safe path -> Escalated with a full evidence summary.
"""

from __future__ import annotations

import logging
import threading
import traceback
from typing import Any

from app import config, db
from app.agents import crew
from app.agents.schemas import Facts, Intake, Plan
from app.sandbox import policy, services, store, verify
from app.sandbox.services import ServiceError
from app.tools import actions

log = logging.getLogger(__name__)
_running: set[int] = set()
_lock = threading.Lock()


def _stage(cid: int, text: str, status: str | None = None, assignee: str | None = None) -> None:
    db.add_event(cid, "Orchestrator", "stage", {"text": text, "status": status})
    fields = {k: v for k, v in (("status", status), ("assignee", assignee)) if v}
    if fields:
        db.update_case(cid, **fields)


def _note(cid: int, author: str, body: str) -> None:
    db.add_message(cid, "agent", author, body, internal = True)


def _spawn(cid: int, target, *args: Any) -> bool:
    with _lock:
        if cid in _running:
            return False
        _running.add(cid)

    def run() -> None:
        try:
            target(cid, *args)
        except Exception as exc:
            log.exception("case %s crashed", cid)
            db.add_event(cid, "Orchestrator", "error", {"text": f"{type(exc).__name__}: {exc}",
                                                        "trace": traceback.format_exc()[-1500:]})
            db.update_case(cid, status = "Escalated", assignee = "Support Lead")
            _note(cid, "CasePilot", f"Automation stopped with an internal error ({exc}); escalated to a human.")
        finally:
            with _lock:
                _running.discard(cid)

    threading.Thread(target = run, daemon = True, name = f"case-{cid}").start()
    return True


def is_running(cid: int) -> bool:
    return cid in _running


def start(cid: int) -> bool:
    return _spawn(cid, _process)


# --- Main flow -----------------------------------------------------------------------

def _process(cid: int) -> None:
    case = db.get_case(cid)
    _stage(cid, "Intake Agent is reading the case", "Triage", "Intake Agent")
    it = crew.intake(case)
    db.update_case(cid, customer_id = it.customer_id, order_id = it.order_id, goal = it.goal,
                   priority = it.priority, need_by = it.need_by, summary = it.summary)
    _note(cid, "Intake Agent", f"Goal **{it.goal.replace('_', ' ')}** · {it.priority} · order {it.order_id or '?'}"
                               f"{' · need by ' + it.need_by if it.need_by else ''}. {it.summary}")

    if not it.order_id or not it.customer_id:
        question = it.clarifying_question or "Could you share your order number so we can help?"
        db.add_message(cid, "agent", "CasePilot", question)
        _stage(cid, "Missing information - asked the customer a clarifying question", "Waiting on Customer", "Customer")
        return

    _stage(cid, f"Investigator Agent is checking {it.order_id} across CRM, orders, shipping and policy",
           "Investigating", "Investigator Agent")
    facts = crew.investigator(db.get_case(cid), it)
    _note(cid, "Investigator Agent", "**Findings**\n" + "\n".join(f"- {f}" for f in facts.findings) +
          "\n\n**Options**\n" + "\n".join(f"- {'[eligible]' if o.option and o.eligible else '[not eligible]'} {o.option} "
                                           f"({o.policy_ref or 'n/a'}): {o.reason}" for o in facts.options) +
          (f"\n\n**Risk flags:** {'; '.join(facts.risk_flags)}" if facts.risk_flags else ""))
    _plan_loop(cid, {"intake": it.model_dump(), "facts": facts.model_dump(), "history": []})


def _make_plan(cid: int, it: Intake, facts: Facts, history: list[dict[str, Any]]) -> Plan:
    case = db.get_case(cid)
    _stage(cid, "Resolver Agent is planning the resolution" + (" (re-planning)" if history else ""),
           "Planning", "Resolver Agent")
    plan = crew.resolver(case, it, facts, history)
    feedback = ""
    for round_ in range(1, config.MAX_AUDIT_ROUNDS + 1):
        if plan.decision == "escalate":
            return plan
        errors = actions.validate([a.model_dump() for a in plan.actions]) if plan.decision == "execute" else []
        if errors:
            feedback = "Invalid actions: " + "; ".join(errors)
        else:
            _stage(cid, "Policy Auditor is reviewing the plan", "Policy Review", "Policy Auditor")
            review = crew.auditor(case, it, facts, plan)
            if review.verdict == "approve":
                _stage(cid, "Policy Auditor approved the plan")
                return plan
            feedback = review.feedback
        _stage(cid, f"Plan sent back (round {round_}): {feedback}", "Planning", "Resolver Agent")
        plan = crew.resolver(case, it, facts, history, feedback = feedback)
    # Never execute a plan the auditor has not approved.
    return Plan(decision = "escalate", resolution_type = "planning_failed", summary = "No approved plan.",
                escalate_to = "Support Lead",
                escalation_reason = f"Resolver and Policy Auditor could not agree on a compliant plan: {feedback}")


def _plan_loop(cid: int, ctx: dict[str, Any], approved: Plan | None = None) -> None:
    it, facts = Intake.model_validate(ctx["intake"]), Facts.model_validate(ctx["facts"])
    history: list[dict[str, Any]] = ctx["history"]
    while len(history) < config.MAX_REPLANS:
        plan, preapproved = (approved, True) if approved else (_make_plan(cid, it, facts, history), False)
        approved = None
        if plan.decision == "escalate":
            return _escalate(cid, plan.escalate_to or "Support Lead", plan.escalation_reason or plan.summary)
        if plan.decision == "execute" and not preapproved:
            reasons = policy.approval_reasons([a.model_dump() for a in plan.actions], facts.risk_flags)
            if reasons:
                pid = db.create_proposal(cid, {**ctx, "plan": plan.model_dump()}, reasons)
                db.add_event(cid, "Orchestrator", "approval_required", {"proposal_id": pid, "reasons": reasons,
                                                                         **plan.model_dump()})
                _stage(cid, "Guardrail: human approval required - " + "; ".join(reasons),
                       "Awaiting Approval", "Supervisor")
                return
        ok, results, failure = _execute(cid, plan, len(history) + 1)
        if not ok:
            history.append(failure)
            if len(history) >= config.MAX_REPLANS:
                break  # budget spent; fall through to the escalation below
            _stage(cid, f"Adapting: {failure['error']} - re-planning with updated facts "
                        f"(attempt {len(history) + 1}/{config.MAX_REPLANS})")
            continue
        _stage(cid, "Verifying the outcome against the systems of record", "Verifying", "Verifier")
        case = db.get_case(cid)
        check = verify.verify(case["number"], it.order_id, plan.expected.model_dump())
        db.add_event(cid, "Verifier", "verification", check)
        if not check["passed"]:
            failed = [c for c in check["checks"] if not c["passed"]]
            history.append({"plan": plan.summary, "completed": [r.get("action") for r in results],
                            "error": "verification failed: " + "; ".join(c["detail"] for c in failed)})
            _stage(cid, "Verification failed - re-planning")
            continue
        return _finish(cid, plan, results, history)
    _escalate(cid, "Support Lead", f"Could not complete the resolution after {config.MAX_REPLANS} attempts: "
                                   + "; ".join(h["error"] for h in history))


def _surface_world_events(cid: int, marker: int) -> None:
    """Show environment changes (that happened during an action) on the case's work log."""
    for ev in store.q("SELECT detail FROM audit WHERE id > ? AND kind = 'world_event'", (marker,)):
        db.add_event(cid, "Environment", "world_event", {"text": ev["detail"]})


def _execute(cid: int, plan: Plan, attempt: int) -> tuple[bool, list[dict[str, Any]], dict[str, Any]]:
    case = db.get_case(cid)
    _stage(cid, f"Executing {len(plan.actions)} action(s)" if plan.actions else "No state change needed",
           "Executing", "Executor")
    results: list[dict[str, Any]] = []
    for i, step in enumerate(plan.actions, 1):
        key = f"{case['number']}-a{attempt}-{i}"
        for try_ in (1, 2):
            db.add_event(cid, "Executor", "tool_call", {"tool": step.action, "args": step.params})
            marker = store.q1("SELECT COALESCE(MAX(id), 0) AS m FROM audit")["m"]
            try:
                res = actions.execute(step.action, step.params, case["number"], key)
                _surface_world_events(cid, marker)
                db.add_event(cid, "Executor", "tool_result", {"tool": step.action, "result": res})
                results.append({"action": step.action, **res})
                break
            except ServiceError as exc:
                _surface_world_events(cid, marker)
                db.add_event(cid, "Executor", "tool_result", {"tool": step.action, "result": exc.as_dict()})
                if exc.retryable and try_ == 1:
                    _stage(cid, f"{exc.code} is transient - retrying once with the same idempotency key")
                    continue
                return False, results, {"plan": plan.summary, "failed_action": step.action, "params": step.params,
                                        "error": f"{step.action} refused: {exc.code} - {exc.message}",
                                        "completed": [f"{r['action']}" for r in results]}
    return True, results, {}


def _finish(cid: int, plan: Plan, results: list[dict[str, Any]], history: list[dict[str, Any]]) -> None:
    case = db.get_case(cid)
    check = verify.verify(case["number"], case["order_id"], plan.expected.model_dump())
    _stage(cid, "Communicator Agent is writing to the customer", "Verifying", "Communicator Agent")
    reply = crew.communicator(case, plan, results, history, check)
    services.send_notification(case["customer_id"], case["number"], reply.customer_message)
    db.add_message(cid, "agent", "CasePilot", reply.customer_message)
    final = verify.verify(case["number"], case["order_id"], plan.expected.model_dump(), include_notification = True)
    db.add_event(cid, "Verifier", "verification", final)
    kb_id = db.add_kb(reply.kb_title, reply.kb_situation, reply.kb_resolution, reply.kb_tags, case["number"])
    _note(cid, "Communicator Agent", f"{reply.internal_note}\n\nLearned memory entry #{kb_id} saved.")
    db.update_case(cid, resolution_type = plan.resolution_type, resolution = reply.internal_note)
    _stage(cid, "Case resolved and verified" if final["passed"] else "Resolved with verification warnings",
           "Resolved", "CasePilot")


def _escalate(cid: int, team: str, reason: str) -> None:
    db.add_event(cid, "Orchestrator", "escalation", {"escalate_to": team, "escalation_reason": reason})
    _note(cid, "CasePilot", f"**Escalated to {team}.** {reason}\n\nAll findings are attached above so the team "
                            f"can act without re-investigating.")
    db.add_message(cid, "agent", "CasePilot", "Thanks for your patience - a specialist from our team is reviewing "
                                              "your case and will get back to you within 24 hours.")
    _stage(cid, f"Escalated to {team}", "Escalated", team)


# --- Human in the loop -----------------------------------------------------------------

def decide(cid: int, approve: bool, approver: str, comment: str = "") -> dict[str, Any]:
    prop = db.latest_proposal(cid)
    if not prop or prop["status"] != "pending":
        raise ValueError("No pending approval on this case")
    db.decide_proposal(prop["id"], "approved" if approve else "rejected", approver, comment)
    db.add_event(cid, approver, "approval", {"approved": approve, "comment": comment})
    _note(cid, approver, f"Plan **{'approved' if approve else 'rejected'}**." + (f" {comment}" if comment else ""))
    ctx = prop["plan"]
    if approve:
        plan = Plan.model_validate(ctx.pop("plan"))
        _spawn(cid, _plan_loop, ctx, plan)
    else:
        _escalate(cid, "Support Lead", f"Supervisor {approver} rejected the plan. {comment}".strip())
    return {"status": "approved" if approve else "rejected"}


def customer_replied(cid: int) -> bool:
    """Resume a case that was waiting on the customer."""
    case = db.get_case(cid)
    if case and case["status"] == "Waiting on Customer":
        return start(cid)
    return False

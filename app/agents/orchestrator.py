"""Case lifecycle controller: observe -> decide -> act -> verify -> adapt.

New -> Triage -> (Waiting on Customer) -> Investigating -> Planning <-> Policy Review
    -> (Awaiting Approval) -> Executing -> Verifying -> Resolved
Blocked action or failed verification -> back to Planning with the new facts (bounded).
No safe path -> Escalated with a full evidence summary.
"""

from __future__ import annotations

import json
import logging
import re
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

def _triage_rounds(cid: int) -> int:
    """How many clarifying questions this case has already asked."""
    return sum(1 for m in db.messages(cid, include_internal = False)
               if m["sender"] == "agent" and (m.get("meta") or {}).get("triage"))


def _ask(cid: int, it: Intake, round_: int) -> None:
    """Put one question to the customer and park the case until they answer.

    The choices ride on the message itself so the chat can offer them as buttons. A customer
    picking "the walnut lamp - ORD-50002" beats them retyping an order number they have to go
    and find, and it removes the whole class of triage failure where the reply is unparseable.
    """
    question = it.clarifying_question or "Could you share your order number so we can help?"
    meta = {"triage": True, "round": round_, "choices": [c for c in it.choices if c][:5],
            "missing": it.missing_info}
    db.add_message(cid, "agent", "CasePilot", question, meta = meta)
    db.add_event(cid, "Intake Agent", "triage_question", {"question": question, **meta})
    detail = f" (offered {len(meta['choices'])} options)" if meta["choices"] else ""
    _stage(cid, f"Asked the customer a clarifying question{detail}", "Waiting on Customer", "Customer")


_ORDER_REF = re.compile(r"\bORD-\d{3,}\b", re.I)


def _unfindable_order_named(case: dict[str, Any], it: Intake) -> str | None:
    """An order id the customer typed that does not exist in the records.

    The dangerous case, seen live: a customer writes "ORD-99999 never turned up, refund me",
    no such order exists, and the agent quietly binds a different real order of theirs and plans
    a refund against it. Refunding the wrong order is worse than asking. If the id they gave is
    not in the records, say so rather than substituting one.
    """
    text = f"{case.get('subject') or ''} {case.get('description') or ''}"
    for ref in {m.upper() for m in _ORDER_REF.findall(text)}:
        try:
            services.get_order(ref)
        except ServiceError:
            return ref          # they named it, and it does not exist
    return None


def _needs_triage(it: Intake, case: dict[str, Any]) -> bool:
    """True when answering would be guesswork and one question would settle it.

    Three cases, in order of how badly a guess would hurt:

    1. No customer or no order - the case cannot be worked at all, on any channel.
    2. The Intake Agent listed `missing_info` and wrote a question. It has told us it is not
       confident, and that is worth honouring wherever the case came from: a wrong guess here
       refunds the wrong order. (Live models do bind an id *and* ask about it in the same
       breath - reading the id as confidence and dropping the question is how the agent ends up
       resolving the wrong one of two lamps.)
    3. Chat only: the agent offered `choices` without flagging anything missing - a proactive
       "which of these did you mean?". Worth asking when someone is sitting there to tap an
       answer; not worth parking an email or a web-form ticket over.
    """
    if not it.customer_id or not it.order_id:
        return True
    if not it.clarifying_question:
        return False
    if it.missing_info:
        return True
    return case.get("channel") == "chat" and bool(it.choices)


def _process(cid: int) -> None:
    case = db.get_case(cid)
    _stage(cid, "Intake Agent is reading the case", "Triage", "Intake Agent")
    it = crew.intake(case)
    db.update_case(cid, customer_id = it.customer_id, order_id = it.order_id, goal = it.goal,
                   priority = it.priority, need_by = it.need_by, summary = it.summary)
    _note(cid, "Intake Agent", f"Goal **{it.goal.replace('_', ' ')}** · {it.priority} · order {it.order_id or '?'}"
                               f"{' · need by ' + it.need_by if it.need_by else ''}. {it.summary}")

    ghost = _unfindable_order_named(case, it)
    if ghost:
        round_ = _triage_rounds(cid) + 1
        if round_ <= config.MAX_TRIAGE_ROUNDS:
            it = it.model_copy(update = {
                "missing_info": [f"a valid order number (we have no record of {ghost})"],
                "clarifying_question": f"I can't find an order {ghost} on your account - the number "
                                       f"might have a typo. Could you check it, or pick the order "
                                       f"you mean?",
                "choices": it.choices,
            })
            _note(cid, "Intake Agent", f"Customer named **{ghost}**, which is not in the order "
                                       f"records. Not substituting another order; asking instead.")
            return _ask(cid, it, round_)
        return _route(cid, "Support Lead", f"Customer refers to {ghost}, which does not exist, and "
                                           f"has not given a valid order number.")

    if _needs_triage(it, case):
        round_ = _triage_rounds(cid) + 1
        if round_ <= config.MAX_TRIAGE_ROUNDS:
            return _ask(cid, it, round_)
        # Out of questions. If the case is workable, work it; the Investigator has the same
        # records and may resolve the ambiguity the customer would not.
        if not it.customer_id or not it.order_id:
            return _route(cid, "Support Lead", f"Triage could not identify the "
                          f"{'customer' if not it.customer_id else 'order'} after "
                          f"{config.MAX_TRIAGE_ROUNDS} questions.")
        _stage(cid, f"Proceeding without a clearer answer after {config.MAX_TRIAGE_ROUNDS} questions")

    _stage(cid, f"Investigator Agent is checking {it.order_id} across CRM, orders, shipping and policy",
           "Investigating", "Investigator Agent")
    facts = crew.investigator(db.get_case(cid), it)
    _note(cid, "Investigator Agent", "**Findings**\n" + "\n".join(f"- {f}" for f in facts.findings) +
          "\n\n**Options**\n" + "\n".join(f"- {'[eligible]' if o.option and o.eligible else '[not eligible]'} {o.option} "
                                           f"({o.policy_ref or 'n/a'}): {o.reason}" for o in facts.options) +
          (f"\n\n**Risk flags:** {'; '.join(facts.risk_flags)}" if facts.risk_flags else ""))
    _plan_loop(cid, {"intake": it.model_dump(), "facts": facts.model_dump(), "history": []})


def _sig(action: str, params: dict[str, Any]) -> tuple[str, str]:
    return action, json.dumps(params or {}, sort_keys = True, default = str)


def _already_failed(history: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """Action + parameter combinations a previous attempt was already refused."""
    return {_sig(h["failed_action"], h.get("params") or {}) for h in history if h.get("failed_action")}


def _policy_issues(it: Intake, plan: Plan) -> list[str]:
    """Check the plan against the handbook, in code.

    This replaced an LLM Policy Auditor. The rules that gate execution are windows, ledger
    balances, stock levels and ETAs - arithmetic, not judgement. A model re-deriving them each
    time cost a third of the calls in a case, re-read what the Resolver had already read, and
    deadlocked by objecting to plans that broke no rule. Code cannot be argued out of a limit.
    """
    try:
        order = services.get_order(it.order_id) if it.order_id else {}
        customer = services.get_customer(it.customer_id) if it.customer_id else {}
    except ServiceError:
        return []          # cannot verify the plan against records; the executor still guards
    if not order:
        return []
    return policy.review_plan(plan.model_dump(), order, customer, it.model_dump(),
                              inventory = lambda sku, region: services.check_inventory(sku, region))


def _make_plan(cid: int, it: Intake, facts: Facts, history: list[dict[str, Any]]) -> Plan:
    case = db.get_case(cid)
    _stage(cid, "Resolver Agent is planning the resolution" + (" (re-planning)" if history else ""),
           "Planning", "Resolver Agent")
    plan = crew.resolver(case, it, facts, history)
    feedback = ""
    refused = _already_failed(history)
    # The Resolver often capitulates on the last round, replacing a workable plan with an
    # escalation. Keep the last plan that was actually executable so a stalemate can fall back
    # to it rather than to the surrender.
    last_exec: Plan | None = None
    challenged = False
    for round_ in range(1, config.MAX_AUDIT_ROUNDS + 1):
        if plan.decision == "escalate":
            # An escalation skips the policy gate entirely, so a wrong reason for escalating is
            # the one claim nothing checks. Live: a refund inside the 15-day electronics window
            # was escalated as "outside the 15-day window" - the model simply got the arithmetic
            # wrong, and a customer owed a refund would have waited on a human for nothing.
            # One push-back, and only when the investigation actually found a remedy that is
            # eligible and flagged no risk; a genuine claims-review case still goes straight up.
            eligible = [o for o in facts.options if o.eligible and o.option]
            if not challenged and eligible and not facts.risk_flags:
                challenged = True
                feedback = ("You chose to escalate, but the investigation found remedies that are "
                            "already eligible under policy: "
                            + "; ".join(f"{o.option} ({o.policy_ref or 'n/a'}) - {o.reason}" for o in eligible)
                            + ". Re-check the dates and balances behind your reason against the "
                              "facts above. Escalate only if none of these can actually be applied; "
                              "otherwise plan the one that resolves the case.")
                _stage(cid, "Escalation challenged: the investigation found an eligible remedy",
                       "Planning", "Resolver Agent")
                _note(cid, "Orchestrator", f"**Escalation challenged.** Resolver wanted to escalate "
                                           f"(*{plan.escalation_reason or plan.summary}*) while "
                                           f"{len(eligible)} eligible remedy(s) were on the table.")
                plan = crew.resolver(case, it, facts, history, feedback = feedback)
                continue
            return plan
        errors = actions.validate([a.model_dump() for a in plan.actions]) if plan.decision == "execute" else []
        # Adapting means changing something. Re-proposing an action that was already refused with
        # exactly these parameters would fail identically, so it never reaches the Executor.
        repeats = [a.action for a in plan.actions if _sig(a.action, a.params) in refused]
        if plan.decision == "execute" and plan.actions and not errors and not repeats:
            last_exec = plan
        if errors:
            feedback = "Invalid actions: " + "; ".join(errors)
        elif repeats:
            feedback = (f"{', '.join(sorted(set(repeats)))} already failed on this case with exactly these "
                        f"parameters and would fail again. Respect what the refusal said: change the "
                        f"parameters (for example a different warehouse, or an amount within what the "
                        f"payment can still refund) or choose a different remedy.")
            _stage(cid, f"Rejected a repeat of the failed action: {', '.join(sorted(set(repeats)))}")
        else:
            _stage(cid, "Policy gate is checking the plan against the handbook", "Policy Review", "Policy Auditor")
            issues = _policy_issues(it, plan)
            if not issues:
                _stage(cid, "Policy gate passed")
                return plan
            feedback = "; ".join(issues)
            _note(cid, "Policy Auditor", "**Plan blocked by the policy gate**\n"
                  + "\n".join(f"- {i}" for i in issues))
        _stage(cid, f"Plan sent back (round {round_}): {feedback}", "Planning", "Resolver Agent")
        plan = crew.resolver(case, it, facts, history, feedback = feedback)
    # The auditor is advisory, not the safety net. When it and the Resolver cannot agree, proceed
    # with the last plan provided it still passes the checks that are actually authoritative:
    # Last chance: the gate's objections are arithmetic, so if the newest plan happens to clear
    # them now, run it. Otherwise escalate carrying the exact breaches - unlike a model's opinion,
    # these are real rules, and overriding them would move a customer's money against policy.
    candidate = plan if (plan.decision == "execute" and plan.actions) else last_exec
    if candidate is not None and not actions.validate([a.model_dump() for a in candidate.actions])             and not [a for a in candidate.actions if _sig(a.action, a.params) in refused]             and not _policy_issues(it, candidate):
        _stage(cid, "Policy gate passed on the final revision")
        return candidate
    return Plan(decision = "escalate", resolution_type = "policy_blocked",
                summary = "No plan could satisfy the policy handbook.", escalate_to = "Support Lead",
                escalation_reason = f"Blocked by the policy gate after {config.MAX_AUDIT_ROUNDS} "
                                    f"revisions: {feedback}")


def _plan_loop(cid: int, ctx: dict[str, Any], approved: Plan | None = None) -> None:
    it, facts = Intake.model_validate(ctx["intake"]), Facts.model_validate(ctx["facts"])
    history: list[dict[str, Any]] = ctx["history"]
    while len(history) < config.MAX_REPLANS:
        plan, preapproved = (approved, True) if approved else (_make_plan(cid, it, facts, history), False)
        approved = None
        if plan.decision == "escalate":
            return _escalate(cid, plan.escalate_to or "Support Lead", plan.escalation_reason or plan.summary)
        if plan.decision == "execute" and not preapproved:
            try:
                cust = services.get_customer(it.customer_id) if it.customer_id else None
                order = services.get_order(it.order_id) if it.order_id else None
            except ServiceError:
                cust = order = None
            reasons = policy.approval_reasons([a.model_dump() for a in plan.actions], facts.risk_flags,
                                              customer = cust, order = order, intake = it.model_dump())
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


def _route(cid: int, team: str, reason: str) -> None:
    """Hand an unworkable case to a human without dressing it up as a failed resolution.

    Triage running out of questions is not the same event as a plan being blocked by policy, and
    the board should not colour them the same.
    """
    db.add_event(cid, "Orchestrator", "routed", {"team": team, "reason": reason})
    _note(cid, "CasePilot", f"**Routed to {team}.** {reason}")
    db.add_message(cid, "agent", "CasePilot",
                   "Thanks for bearing with us - I want to get this right, so I'm passing you to a "
                   "colleague who can look at your account directly. They'll be in touch shortly.")
    _stage(cid, f"Routed to {team}: {reason}", "Routed", team)


def _escalate(cid: int, team: str, reason: str) -> None:
    case = db.get_case(cid)
    db.add_event(cid, "Orchestrator", "escalation", {"escalate_to": team, "escalation_reason": reason})
    _note(cid, "CasePilot", f"**Escalated to {team}.** {reason}\n\nAll findings are attached above so the team "
                            f"can act without re-investigating.")
    order_ref = f" regarding order {case['order_id']}" if case.get("order_id") else ""
    cust_name = (case.get("customer_name") or "there").split()[0]
    msg = (f"Hi {cust_name}, we've checked your inquiry{order_ref}. "
           f"Because your request requires review by a specialist ({reason}), "
           f"we've escalated your ticket directly to our **{team}** team. "
           f"A specialist is reviewing all evidence gathered and will follow up with you shortly (within 24 hours). Thank you for your patience!")
    db.add_message(cid, "agent", "CasePilot", msg)
    if case.get("customer_id"):
        services.send_notification(case["customer_id"], case["number"], msg)
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

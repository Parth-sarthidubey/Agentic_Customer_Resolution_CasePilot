# CasePilot — Problem & Solution Brief

**Problem statement:** PS5 — Autonomous Customer Resolution Agent
**Tech Zephyr 4.0 · Agentic AI Hackathon · IIT Bhubaneswar**

---

## 1. The problem

Retail and e-commerce support desks handle the same few hundred situations over and over: a
damaged item, a late parcel, a refund that has not appeared, a cancellation that arrived a minute
too late. The volume is enormous, the work is repetitive, and yet it stubbornly resists automation,
because every case looks simple and almost none of them are.

A human agent resolving *"my kettle arrived cracked, can you send a replacement by Tuesday?"* has to:

- find the customer and the order from a message that contains neither an order ID nor a SKU;
- check whether the claim is inside the policy window, and which remedies the policy actually allows;
- check whether a replacement can physically reach the customer by Tuesday, from which warehouse;
- decide between replacement, refund, store credit or escalation;
- execute the action in systems that can refuse — stock runs out, orders ship, gateways fail;
- confirm the money actually moved, and tell the customer in plain language.

Today this costs 8–15 minutes per case. Multiply by thousands of cases a day. Meanwhile the
customer waits hours for a reply that could have been immediate.

**Why the obvious automations fail:**

- **Rule engines and macros** break the moment reality deviates — they cannot tell "stock exists" from
  "stock exists but arrives too late to matter."
- **A chatbot over a knowledge base** can *explain* the refund policy but cannot *issue* the refund,
  so the work still lands on a human.
- **A single LLM call** with all the data cannot verify its own work, cannot recover when an action
  is refused, and will confidently promise a refund that never posted.

## 2. Who it is for

- **Support agents**, who get a diagnosed case with the evidence gathered and the action already
  taken or ready for one click, instead of a raw ticket and eight browser tabs.
- **Support leads**, who keep control of the money: the guardrails, the approval queue and a
  complete audit trail of what the agent did and why.
- **Customers**, who get a resolution in seconds instead of a first reply in hours — and an honest
  explanation when the answer is no.

## 3. Why this genuinely needs an agentic system

| Requirement | Why a fixed pipeline cannot do it |
|---|---|
| **The path is discovered, not planned** | Which tool to call next depends on the last result. A non-delivery claim needs carrier proof and the customer's claims history; a damaged item needs stock, transit times and the policy window. No fixed sequence covers both. |
| **Inputs are ambiguous** | "One of my items arrived broken" from a customer with two recent orders is not actionable. The system must recognise that, ask one specific question, and resume. |
| **The environment is adversarial** | Stock sells out, carriers collect early, gateways return 503 — *during* the resolution. The plan must survive its own execution. |
| **Outcomes must be proved** | "I issued the refund" is a claim. The refund appearing in the payment ledger is a fact. Only the second one may close a case. |
| **Knowing when not to act** | A fraud-flagged claim, or a refund above the limit, must stop and involve a human. Restraint is part of correct behaviour, not a failure. |

## 4. The solution

**CasePilot** is an autonomous resolution agent embedded in a support desk. It owns the case from
arrival to verified outcome.

**Five specialised agents, one explicit state machine:**

1. **Intake** — maps a plain-language message to a goal, a customer and an order; sets priority;
   asks one clarifying question when the case is genuinely unworkable.
2. **Investigator** — chooses its own tools across CRM, orders, inventory, shipping, the payment
   ledger, the policy handbook and past resolved cases; returns findings plus the remedies that are
   *actually eligible*, each with a policy reference.
3. **Resolver** — commits to one remedy and emits a typed action plan with an explicit **expected
   outcome**.
4. **Policy Auditor** — independently re-checks the plan against the handbook and sends it back with
   specific objections. A plan it never approves can never execute.
5. **Communicator** — writes the customer reply and the internal note, and distils the case into a
   knowledge-base entry for next time.

Around them: an **Executor** that is the only code path allowed to change enterprise state, and a
**Verifier** that reads the systems of record back and decides whether the case may close.

**The loop that makes it agentic:**

```
observe → decide → act → verify → adapt
```

When an action is refused, or verification fails, the failure re-enters planning **as a new fact**.
The Resolver must plan around it. This is bounded (three attempts) and ends in an escalation that
carries everything learned, so a human never restarts from zero.

## 5. What makes it different

- **It acts, and then proves it acted.** Verification is SQL against the systems of record —
  including a `no_over_refund` invariant checked on *every* case. An agent cannot close a case by
  claiming success.
- **The environment fights back for real.** A world-events engine changes conditions mid-execution.
  The stockout in the flagship demo is not a scripted branch: the inventory row genuinely changes
  between planning and reserving, and the service layer genuinely refuses.
- **Guardrails are code, not prompts.** The approval limits live in Python. The model is never asked
  whether something needs approval, so no instruction in a customer's message can widen them.
- **It knows when to stop.** Two of the eight demo cases end in *not acting* — an escalation with
  evidence, and a clarifying question. Both are correct.
- **It runs on free models, or none.** A provider chain with cooldown-based failover, and a complete
  deterministic implementation of all five agents underneath. Zero cost, and a demo that cannot die
  on someone else's rate limit.

## 6. Impact

| Measure | Before | With CasePilot |
|---|---|---|
| Time to first meaningful response | hours | seconds |
| Handling time on a routine case | 8–15 min of human work | fully automated, or one approval click |
| Actions taken without verification | most | none — every case is checked against the ledger |
| Consistency of policy application | varies by agent and by day | one handbook, independently audited every time |
| Institutional memory | lives in individual heads | a knowledge-base entry per resolved case |

Human effort concentrates where it belongs: high-value refunds, fraud review, and the genuinely
novel cases.

## 7. Scope and honesty about limits

- The enterprise systems are **simulated** — a SQLite stand-in for CRM/OMS/WMS/payments/carrier —
  but they are simulated *with real behaviour*: constraints are enforced, actions fail, and state
  persists. Replacing them with real APIs is a change to `app/sandbox/services.py` alone; nothing in
  the agent layer assumes a simulation.
- All data is synthetic. "Kestrel Home" is a fictional retailer.
- Free-tier models are weaker than frontier models. The system is built to tolerate that: typed
  output contracts, an independent auditor, code-enforced guardrails, objective verification, and
  bounded retries. Correctness does not rest on the model being clever.

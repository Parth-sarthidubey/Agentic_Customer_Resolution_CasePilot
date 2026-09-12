---
title: CasePilot
emoji: 🎧
colorFrom: indigo
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# CasePilot — the autonomous customer-resolution agent

> A customer writes "my kettle arrived cracked, I need a replacement by Tuesday". CasePilot reads it,
> finds the order, checks stock and policy, plans a replacement — and while it is reserving the unit,
> the last one in the East warehouse sells out. It does not fail. It re-plans, works out that the
> next warehouse arrives *after* the customer's deadline, refunds instead, **verifies the money
> actually moved in the ledger**, writes to the customer, and files what it learned.

**observe → decide → act → verify → adapt**, against a real (simulated) enterprise, inside a real
ticket desk, on **free LLMs**.

| | |
|---|---|
| 📄 Problem & solution brief | [docs/BRIEF.md](docs/BRIEF.md) |
| 🏗️ Architecture / workflow | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| 🎬 Demo script (3–5 min) | [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) |
| 🚀 Deploy (Hugging Face Spaces / Docker) | [docs/DEPLOY.md](docs/DEPLOY.md) |

## What's inside

- **CasePilot Desk** — a support desk in the style of Jira/ServiceNow: a board, a queue, a case view
  with a **live agent work log**, human approvals, and a view into the enterprise systems of record.
- **A customer portal** — where customers file cases in plain language and attach evidence (a photo
  of the damaged item), and where they only ever see plain-language replies.
- **Five agents + an executor and verifier** — Intake → Investigator → Resolver ⇄ Policy Auditor →
  (human approval) → Executor → Verifier → Communicator, coordinated by an explicit state machine.
- **A simulated enterprise** — CRM, orders, per-warehouse inventory with transit times, a payment
  gateway, a shipping carrier, store credit and a policy handbook. Nothing is mocked: actions
  genuinely fail, and fixes are confirmed by reading the database back.
- **A world-events engine** — stock sells out, a carrier collects an order, the payment gateway
  returns 503 **while the agent is working**, so adaptation is real rather than narrated.
- **Deterministic guardrails** — refunds over 150 and credit over 50 need a human. The limits are
  enforced in Python, never by the model, so no prompt can talk its way past them.
- **Long-term memory** — every resolved case becomes a knowledge-base entry the agents retrieve next
  time.
- **A free-model router** — Gemini free tier → Groq → OpenRouter → Ollama → deterministic offline
  rules. It works with **no API key at all**.

## The eight demo cases

| Case | What makes it hard | What the agents do |
|---|---|---|
| Damaged item | Replacement sells out mid-plan | `OUT_OF_STOCK` → alternatives miss the deadline → **adapts to a refund** → verified |
| Refund | Payment gateway returns 503 | Retryable → one safe retry on the **same idempotency key** → posts exactly once |
| Cancellation | Carrier collects the order mid-plan | `ORDER_ALREADY_SHIPPED` → **re-plans** to prepaid return labels |
| Late return | 41 days, outside the 30-day window | Refund ineligible → gold-tier goodwill credit within limits |
| Wrong high-value item | 749.00 refund exceeds the limit | **Pauses for a human**, executes on approval, verifies |
| Lost package | Tracking 7 days past ETA | Declared lost under policy → re-ships from the nearest warehouse |
| Suspicious claim | Photo proof + 4 claims in 90 days | **Refuses to auto-refund** → escalates with the evidence |
| Vague request | Two recent orders, no item named | **Asks one question**, resumes on the reply, resolves |

The last two are the point: knowing when *not* to act, and when to ask, is part of the job.

## Quick start (about 2 minutes)

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
uv run uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000> for the desk and <http://localhost:8000/portal> for the customer
portal. Use the **Demo cases** menu to file any of the eight cases and watch the agents work.

**No API key needed.** With no key, CasePilot runs its deterministic offline agents and every
scenario still completes end to end. To use a free model instead, copy `.env.example` to `.env` and
add a [Google AI Studio](https://aistudio.google.com/apikey) or [Groq](https://console.groq.com/keys)
key.

```bash
uv run pytest        # 10 end-to-end tests, hermetic, no key required
```

## Why this is agentic, not a script

- **The path is not known in advance.** Which tool to call next depends on what the last one
  returned — a missing item needs stock and transit checks, a refund needs the payment ledger, a
  non-delivery claim needs carrier proof and the customer's claims history.
- **The environment fights back.** Stock disappears, carriers collect early, gateways fail. Every
  one of those is a real refusal from the service layer, not a scripted branch.
- **"Done" is verified, not asserted.** A case resolves only when SQL against the systems of record
  matches what the agent promised — including a `no_over_refund` invariant checked on every case.
- **Refusing is a valid outcome.** Contract-breaking and fraud-flagged cases are escalated with
  evidence rather than guessed at.

## Licence

MIT. All data is synthetic; "Kestrel Home" is a fictional retailer.

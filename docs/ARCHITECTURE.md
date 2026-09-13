# CasePilot - System Architecture

CasePilot is an autonomous customer-resolution agent that lives inside a support desk. A customer
files a case in plain language; a crew of five agents reads it, investigates the enterprise systems
of record, plans a resolution against a policy handbook, gets it past a deterministic guardrail,
executes real state changes, **verifies them against the database**, and adapts when the world
changes underneath it.

The loop is **observe → decide → act → verify → adapt**, and the verify step is objective: a case is
resolved only when SQL queries against the systems of record agree with what the agent promised.

---

## 1. Big picture

```mermaid
flowchart TB
    subgraph UI["Interfaces"]
        PORTAL["Customer portal<br/>static/portal.html<br/>triage chat · request form · uploads"]
        DESK["Agent desk<br/>static/index.html<br/>board · queue · systems · knowledge"]
    end

    subgraph API["FastAPI · app/main.py"]
        REST["/api/chat · /api/cases · /events<br/>/decision · /scenarios · /systems · /knowledge"]
    end

    subgraph ORCH["Orchestrator · app/agents/orchestrator.py"]
        SM["Case state machine<br/>+ replan loop + approval gate"]
    end

    subgraph CREW["Agent crew · app/agents/crew.py"]
        A1["Intake"]
        A2["Investigator"]
        A3["Resolver"]
        A4["Policy Auditor"]
        A5["Communicator"]
    end

    subgraph TOOLS["Tools"]
        RT["Read tools · app/tools/read_tools.py<br/>read-only, no state change<br/>scoped per agent"]
        AC["Action catalogue · app/tools/actions.py<br/>5 write actions, Executor only"]
    end

    subgraph SANDBOX["Simulated enterprise · app/sandbox/"]
        SVC["services.py<br/>CRM · orders · inventory · payments<br/>shipping · store credit · notifications"]
        POL["policy.py<br/>handbook retrieval + approval gate"]
        WRLD["world.py + world_events<br/>conditions change mid-plan"]
        VER["verify.py<br/>objective SQL checks"]
    end

    subgraph DATA["Storage"]
        DB1[("desk.db<br/>cases · messages · attachments<br/>proposals · agent trace · learned KB")]
        DB2[("enterprise.db<br/>systems of record + audit log")]
    end

    LLM["Model router · app/llm.py<br/>Bedrock → Groq → Mistral → Gemini<br/>→ deterministic offline agents"]

    PORTAL --> REST
    DESK --> REST
    REST --> SM
    SM --> CREW
    CREW --> RT
    SM -->|Executor| AC
    RT --> SVC
    RT --> POL
    AC --> SVC
    SVC --> WRLD
    SM --> VER
    VER --> DB2
    SVC --> DB2
    SM --> DB1
    CREW -.-> LLM
```

**Two databases, deliberately separate.** `desk.db` is the ticketing product (what a support tool
owns). `enterprise.db` is the company the agent acts on (CRM, orders, inventory, payments, shipping).
The agent can only touch the second one through tools, and the verifier reads it directly — so
verification cannot be fooled by an agent that merely *claims* success.

---

## 2. The resolution loop

```mermaid
stateDiagram-v2
    [*] --> Triage
    Triage --> WaitingOnCustomer: needs an answer (asks, with choices)
    WaitingOnCustomer --> Triage: customer replies or taps a choice
    Triage --> Routed: still unworkable after 3 questions
    Triage --> Investigating
    Investigating --> Planning
    Planning --> PolicyReview
    PolicyReview --> Planning: revise (max 2 rounds)
    PolicyReview --> AwaitingApproval: guardrail trips
    PolicyReview --> Executing: within limits
    AwaitingApproval --> Executing: supervisor approves
    AwaitingApproval --> Escalated: supervisor rejects
    Executing --> Verifying: all actions succeeded
    Executing --> Planning: action refused (adapt)
    Verifying --> Resolved: all checks pass
    Verifying --> Planning: checks fail (adapt)
    Planning --> Escalated: no safe path / budget spent
    Resolved --> [*]
    Escalated --> [*]
    Routed --> [*]
```

Budgets live in `app/config.py`: `MAX_AGENT_STEPS` tool calls per agent run,
`MAX_AUDIT_ROUNDS = 2` resolver↔auditor revisions, `MAX_REPLANS = 3` plan→execute→verify cycles
before the case escalates with everything it learned attached, and `MAX_TRIAGE_ROUNDS = 3`
clarifying questions before triage gives up.

### Triage, and why the channel decides it

`Routed` is deliberately not `Escalated`. Running out of questions is not a plan being blocked by
policy, and a board that colours them the same hides which cases a human can actually unblock.

Whether triage asks at all depends on where the case came from, because that determines whether
anyone is there to answer:

Every channel stops when the case cannot be worked (no customer, no order) and when the Intake
Agent listed `missing_info` alongside its question — it has said it is not confident, and a wrong
guess refunds the wrong order. What the channel decides is whether a *proactive* question is worth
it: an agent offering `choices` without flagging anything missing.

| Channel | Entry point | Proactive question? |
|---|---|---|
| `chat` | `POST /api/chat` — portal chat | Yes. Someone is sitting there to tap an answer. |
| `portal` | `POST /api/cases` — the **New ticket** screen | No. Nobody is waiting; parking the ticket helps no one. The form compensates by asking for the order, the problem and any need-by date up front — `GET /api/my-orders` populates the picker with the customer's own orders — and folds the answers into the description as plain language the Intake Agent already reads. |
| `email` | `POST /api/cases` — how the demo scenarios file | No, same as the form. |

When the customer is really choosing between things the agent can already see, `Intake.choices`
carries them as the customer would recognise them ("Linen Table Lamp — ORD-50009", not
"ORD-50009"). They ride on the message itself in `messages.meta`, so the chat renders tappable
buttons and the desk can show the operator exactly what was offered — one record rather than a
parallel store the two could disagree about. A tap posts the choice as an ordinary customer
message, so there is no second code path to keep in step.

A plan that re-proposes an action already refused with identical parameters is rejected before
execution and sent back with the constraint (`_already_failed` in the orchestrator). Adapting has
to mean changing something, whichever model is driving.

Each case runs on its own daemon thread (`_spawn`), guarded by a `_running` set so a case can never
be processed twice concurrently. Any uncaught exception escalates the case with a trace rather than
leaving it stuck.

---

## 3. The agent crew

Every agent is the same shape — prompt + tools + a pydantic output schema + a deterministic
fallback — implemented once in `app/agents/base.py` as a ReAct-style tool-calling loop.

| Agent | Job | Tools it may call | Returns |
|---|---|---|---|
| **Intake** | Map a plain-language message to a goal, customer and order; set priority; ask one clarifying question if the case is unworkable | `find_customer`, `get_order` (+ case tools when evidence exists) | `Intake` |
| **Investigator** | Gather evidence across the systems of record and list which resolutions are *eligible*, each with a policy reference | `get_customer`, `get_order`, `track_shipment`, `check_inventory`, `search_policy`, `search_past_cases` | `Facts` |
| **Resolver** | Choose one option and emit a concrete action plan with an **expected outcome** | `get_order`, `check_inventory`, `search_policy` | `Plan` |
| **Policy Auditor** | Independently re-check the plan against the handbook and send it back with specific feedback | `search_policy`, `get_order`, `check_inventory` | `Review` |
| **Communicator** | Write the customer reply and the internal note, and distil the case into a knowledge-base entry | none (writing only) | `Reply` |

Schemas live in `app/agents/schemas.py`; prompts are plain Markdown in `app/agents/prompts/`, so
they can be edited without touching code.

**The Resolver never executes.** It proposes typed actions; the orchestrator's Executor is the only
code path that calls a write service.

**The Auditor is a real check, not a rubber stamp.** If it never approves within
`MAX_AUDIT_ROUNDS`, the orchestrator returns a hard-coded escalation `Plan` — an unapproved plan is
structurally unable to reach the Executor.

---

## 4. Tools

**Read tools (`app/tools/read_tools.py`)** — none of them change state, so the model is free to
explore. Service refusals are wrapped by `_safe()` and returned to the model *as data*, so a refusal
is information the agent reasons about instead of a crash.

Each agent carries only the tools its job needs, because tool schemas are resent on every call and
free-tier providers meter tokens per minute:

| Agent | Tools |
|---|---|
| Intake | `find_customer`, `get_order` |
| Investigator | `get_customer`, `get_order`, `track_shipment`, `check_inventory`, `search_policy`, `search_past_cases` |
| Resolver | `get_order`, `check_inventory`, `search_policy` |
| Policy Auditor | `search_policy`, `get_order`, `check_inventory` |

`get_case_conversation` and `inspect_attachment` are added only when the case actually has an
attachment or a reply to read. Today's date is injected into every system prompt rather than costing
a tool and a round trip. A round of tool calls runs concurrently — they are independent reads.

`inspect_attachment` is multimodal: an image attachment (a photo of a damaged item) is sent to the
vision-capable free model to be described; a text attachment is read directly. If no vision model is
reachable it says so rather than inventing a description.

**Write actions (5, `app/tools/actions.py`)** — a small typed catalogue rendered into the Resolver's
prompt and validated before the auditor ever sees the plan. `validate()` rejects unknown actions and
missing params, sending the Resolver back with the exact errors.

| Action | Guarantee enforced in the service layer |
|---|---|
| `refund` | Never more than captured minus already refunded; idempotency key per attempt |
| `create_replacement` | Must reserve real stock; fails `OUT_OF_STOCK` if the warehouse is empty |
| `create_return_label` | Prepaid label as a `return` shipment |
| `cancel_order` | Fails `ORDER_ALREADY_SHIPPED` once the carrier has picked up |
| `issue_store_credit` | Bound to a case reference under POL-GW-1 |

---

## 5. Verification - why "done" means something

`app/sandbox/verify.py` takes the plan's `expected` outcome and runs SQL against `enterprise.db`:

- `refund_posted` — succeeded refunds sum to the promised amount (excluding cancellation refunds)
- `order_cancelled` — order status really is `cancelled`
- `replacement_shipment` — a `replacement` shipment for that SKU exists
- `arrives_by_need_by` — its ETA actually meets the customer's deadline
- `return_label_issued` · `store_credit_issued`
- `no_over_refund` — an invariant checked on **every** case: refunded ≤ captured
- `customer_notified` — a notification row exists (final pass only)

A failed check is not a warning. It re-enters the plan loop as a new fact, and the Resolver must
plan around it.

---

## 6. Guardrails - the LLM reads policy, but code decides

The approval gate is `policy.approval_reasons()`, pure Python with no model in the path:

- refund total > **150.00** → human (POL-REF-1)
- store credit > **50.00** → human (POL-GW-1)
- **3 or more claims in the last 90 days**, or a `where_is_my_order` claim against a shipment the
  carrier marked delivered *with proof* → human (POL-FRAUD-1)

Every one of those reads a number out of the records. That is a deliberate correction: POL-FRAUD-1
used to fire on any Investigator risk flag containing the words "fraud" or "claims", and a live run
produced the flag *"No explicit delivery dispute or fraud indicators"* — which says the opposite —
holding a correct re-shipment for approval. Negation is exactly what substring matching cannot see,
and a guardrail that fires on the sentence saying it should not fire trains everyone to wave the
approval through. The Investigator's prose stays in the work log as the reasoning it is; it decides
nothing.

When the gate trips, the orchestrator freezes the entire context into a `proposals` row and stops. A
supervisor approving in the desk resumes the loop **with that exact plan**, pre-approved; rejecting
escalates. No prompt injection can widen these limits, because the model is never asked.

**Escalating is itself a claim that gets checked.** A plan with `decision: escalate` returns before
the policy gate, which made the reason for escalating the one assertion nothing verified — live, a
refund fourteen days after delivery was escalated as "outside the 15-day window". The orchestrator
now pushes back once when the Investigator found a remedy that is eligible and raised no risk flag,
quoting those options back and asking the Resolver to re-check its dates. A genuine claims-review
case, which has both a risk flag and no eligible remedy, still goes straight up.

---

## 7. Adaptation - what makes this agentic, not a script

The environment changes *while the agent works*. `services.schedule_event()` registers world events
on triggers like `before:create_replacement`; the service layer fires them mid-execution and writes
them to the audit log, and the orchestrator surfaces them on the case timeline as `Environment`
entries — so anyone watching the trace sees the world move under the agent.

| Scenario | What goes wrong mid-plan | How CasePilot adapts |
|---|---|---|
| **Damaged item, stockout** | The last KT-220 in WH-EAST sells out between planning and reserving | `OUT_OF_STOCK` → other warehouses miss the birthday deadline → re-plans to a refund → verified |
| **Refund, gateway outage** | Payment gateway returns 503 | Marked retryable → one safe retry with the **same idempotency key** → refund posts exactly once |
| **Cancel races shipping** | Carrier picks the order up before `cancel_order` runs | `ORDER_ALREADY_SHIPPED` → re-plans to prepaid return labels per policy |
| **Return outside the window** | 41 days > the 30-day window | Refund ineligible → gold-tier goodwill credit inside the limit |
| **Wrong high-value item** | A 749.00 refund exceeds the auto limit | Pauses for a supervisor → executes on approval → verified |
| **Lost package** | Tracking 7 days past ETA | Declared lost under policy → re-ships from the nearest warehouse |
| **Suspicious claim** | Carrier photo proof + 4 claims in 90 days | **Refuses to auto-refund** → escalates with the evidence |
| **Vague request** | Two recent orders, no item named | Asks one question → resumes on the reply → replacement + return label |

The last two matter most: knowing when **not** to act, and knowing when to ask, are part of the job.

---

## 8. Memory

- **Case state** — `desk.db`: cases, messages, attachments, proposals and a complete agent event
  trace (every stage, tool call, tool result, world event, verification and approval), streamed to
  the desk so the reasoning is visible live.
- **Learned memory** — the Communicator writes a knowledge-base entry on every resolved case, and
  `search_past_cases` retrieves it during the next investigation. The system gets better at the
  second occurrence of a problem.
- **Audit log** — `enterprise.db.audit` records every state change and world event from the
  enterprise side, independent of what the agent believes happened.

---

## 9. Model strategy - free tiers, and it still runs with no key

`app/llm.py` presents one client over providers tried in order:

**AWS Bedrock → Groq → Mistral → Gemini → offline agents.**

Every free tier meters *tokens per minute*, not requests — Groq allows 8k/min, which a five-agent
tool-calling pipeline can exhaust inside one case. The router is built for that reality:

- transient 503s are retried with backoff; only a real 429 earns a long cooldown;
- when every provider is cooling down it still tries the one closest to ready, rather than dropping
  the case to offline rules;
- a model that returns its final JSON as a rejected tool call (gpt-oss on Groq) has that answer
  recovered from the error payload instead of failing;
- Bedrock meters its own spend from the token counts it returns and switches itself off at
  `BEDROCK_MAX_SPEND_USD`, falling through to the next provider. AWS Budgets actions lag by hours
  and cannot stop a runaway loop; this can.

`app/agents/offline.py` is a full deterministic rule-based implementation of all five agents against
the same schemas. With `LLM_MODE=offline`, or no key at all, every scenario still runs end to end —
so the demo cannot fail on someone else's rate limit, and the test suite is hermetic.

---

## 10. How each required element is covered

| Element | Where | Notes |
|---|---|---|
| **Agent / controller** | `app/agents/orchestrator.py` | Explicit state machine with a bounded replan loop, an approval gate and per-case threading. |
| **Tools** | `app/tools/read_tools.py`, `actions.py` | Read-only tools scoped per agent and run concurrently within a round; 5 typed write actions only the Executor can run. |
| **External systems** | `app/sandbox/services.py` | CRM, orders, inventory per warehouse with transit times, payment gateway, carrier tracking, store credit, notifications — with real refusals, not mocks. |
| **Memory / state** | `app/db.py`, `app/sandbox/store.py` | Case state + full agent trace; learned KB as long-term memory; independent enterprise audit log. |
| **Retrieval** | `search_policy`, `search_past_cases` | Scored keyword retrieval over the policy handbook and resolved cases; the Communicator writes back. |
| **Planning** | Resolver Agent | Typed multi-step plans with an explicit expected outcome; validated, then independently audited. |
| **Evaluation / verification** | `app/sandbox/verify.py` | Objective SQL checks on the systems of record, including a `no_over_refund` invariant on every case. |
| **Human interaction** | Portal + desk | Customers chat and attach evidence; supervisors approve or reject every action past the guardrail. |
| **Failure handling** | Orchestrator + `app/llm.py` | Provider failover with backoff, answer recovery from malformed provider responses, a spend cap on paid inference, offline agents, tool errors returned as data, retryable errors retried idempotently, capped replans, a guard against repeating a refused action, crash → escalation with trace. |
| **Adaptation** | World events + replan loop | Conditions change mid-plan; failures and failed verifications re-enter planning as new facts. |

---

## 11. Repository map

```
casepilot/
├── app/
│   ├── main.py              FastAPI app: desk, portal, REST API, event stream
│   ├── config.py            paths, providers, agent budgets, business guardrails
│   ├── db.py                desk.db: cases, messages, attachments, proposals, trace, KB
│   ├── llm.py               free-model router with failover + cooldown
│   ├── scenarios.py         8 demo cases and the world events they schedule
│   ├── agents/
│   │   ├── base.py          ReAct tool-calling loop + schema validation + retry
│   │   ├── crew.py          the five agents: prompt + tools + schema + fallback
│   │   ├── orchestrator.py  state machine, replan loop, approval gate, executor
│   │   ├── offline.py       deterministic implementations of all five agents
│   │   ├── schemas.py       pydantic output contracts
│   │   └── prompts/         intake · investigator · resolver · auditor · communicator
│   ├── sandbox/
│   │   ├── store.py         enterprise.db connection, schema, audit log
│   │   ├── world.py         seeds the simulated retailer (synthetic data)
│   │   ├── services.py      the enterprise systems + world-event firing
│   │   ├── policy.py        handbook retrieval + deterministic approval gate
│   │   └── verify.py        objective outcome verification
│   └── tools/
│       ├── read_tools.py    11 read-only tools (incl. multimodal attachment analysis)
│       └── actions.py       5 typed write actions
├── seed/policies.json       the customer-service policy handbook
├── static/                  desk (board / queue / systems / knowledge) + customer portal
├── tests/test_cases.py      end-to-end scenario tests (offline, hermetic)
└── docs/                    this document, brief, demo script, deploy notes, screenshots
```

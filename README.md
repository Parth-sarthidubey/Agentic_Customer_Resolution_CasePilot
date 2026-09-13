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
| 🚀 Deploy (Render / Docker / local) | [docs/DEPLOY.md](docs/DEPLOY.md) |

## What's inside

- **CasePilot Desk** — a support desk in the style of Jira/ServiceNow: a board, a queue, a case view
  with a **live agent work log**, human approvals, and a view into the enterprise systems of record.
- **A customer portal** — a support **chat** that triages: the agent asks when it cannot tell which
  of your orders you mean, and offers the answers as buttons naming the actual products. Customers
  attach evidence (a photo of the damaged item) and only ever see plain-language replies. A
  **request form** sits alongside it for people who would rather not chat — that path, like an
  inbound email, runs straight through without stopping to ask.
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
- **A model router** — AWS Bedrock → Groq → Mistral → Gemini → deterministic offline rules, with
  backoff, failover and a real-time spend cap. It works with **no API key at all**.

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

## Setup

### Requirements
- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/)** for dependency management
  (`curl -LsSf https://astral.sh/uv/install.sh | sh`, or `winget install astral-sh.uv`)

### Dependencies
All pinned in `pyproject.toml` / `uv.lock`; `uv run` installs them on first use.

| Package | Why |
|---|---|
| `fastapi`, `uvicorn[standard]` | web API and server |
| `pydantic` | typed agent output contracts |
| `openai` | one client for every OpenAI-compatible provider (Groq, Mistral, Gemini, OpenRouter) |
| `boto3` | AWS Bedrock (optional) |
| `python-dotenv`, `python-multipart` | configuration and file uploads |
| `pytest` *(dev)* | the end-to-end test suite |

### Run it

```bash
git clone https://github.com/Parth-sarthidubey/Agentic_Customer_Resolution_CasePilot.git
cd Agentic_Customer_Resolution_CasePilot
uv run uvicorn app.main:app --reload --port 8000
```

- Agent desk: <http://localhost:8000>
- Customer portal: <http://localhost:8000/portal>

Use **Demo cases** in the top bar to file any of the eight cases and watch the agents work.
**No API key is required** — with no key configured, CasePilot runs its deterministic offline
agents and every scenario still completes end to end.

```bash
uv run pytest      # 10 end-to-end tests, hermetic, no key needed
```

## Environment configuration

Copy `.env.example` to `.env` and fill in whichever providers you have. Every one is optional;
they are tried in order and fall back to offline rules.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_MODE` | `auto` | `auto` tries the providers then falls back to offline rules; `offline` never calls a model |
| `GROQ_API_KEY` / `GROQ_MODEL` | `openai/gpt-oss-20b` | Groq free tier — fastest option ([keys](https://console.groq.com/keys)) |
| `MISTRAL_API_KEY` / `MISTRAL_MODEL` | `ministral-8b-latest` | Mistral free tier ([keys](https://console.mistral.ai/api-keys)) |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | `gemini-flash-latest` | Google AI Studio free tier ([keys](https://aistudio.google.com/apikey)) |
| `OPENROUTER_API_KEY` | — | OpenRouter free models |
| `OLLAMA_BASE_URL` | — | A local model, e.g. `http://localhost:11434/v1` |
| `BEDROCK_ENABLED` | `false` | Turn on AWS Bedrock (tried first when on) |
| `BEDROCK_API_KEY` | — | A Bedrock API key; or `BEDROCK_ACCESS_KEY_ID` + `BEDROCK_SECRET_ACCESS_KEY` |
| `BEDROCK_REGION` / `BEDROCK_MODEL` | `us-east-1` / `amazon.nova-lite-v1:0` | Where and what to call |
| `BEDROCK_MAX_SPEND_USD` | `2.00` | Real-time spend ceiling; Bedrock disables itself when reached |
| `AUTOPILOT` | `true` | Start the agents automatically when a case is filed |
| `CASEPILOT_DATA_DIR` | `./data` | Where `desk.db` and `enterprise.db` are written |
| `PORT` | `7860` | Port the container listens on |

CasePilot never reads the machine's ambient AWS profile — Bedrock credentials must be given to it
explicitly, so a work or SSO identity cannot be billed for it by accident.

Deployment (Render, Docker, local) is covered in [docs/DEPLOY.md](docs/DEPLOY.md).

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

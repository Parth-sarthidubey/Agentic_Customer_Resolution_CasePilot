# CasePilot — Demo Script (3–5 minutes)

Target: **4 minutes**. The judged arc is
**Goal → Decision → Action → Result → Adaptation → Outcome**, and a failure must be visible.
This script hits all six, and shows three different kinds of failure.

---

## Before you record

```bash
uv run uvicorn app.main:app --port 8000
```

- Open the desk at <http://localhost:8000> and the portal at <http://localhost:8000/portal>
  in two tabs.
- Hit reset so the board is clean.
- Check the model pill in the top bar. Either a provider or `offline rules` is fine — but know
  which you are demoing, and say it once.
- Browser at 100% zoom, 1920×1080. The agent work log is the star; make sure it is readable.
- If you are demoing the hosted Space, open it 10 minutes early so it is awake.

**Scenarios are safe to re-run**, so you can rehearse as many times as you like.

---

## 0:00–0:25 · The problem

> "A customer writes in: *my kettle arrived cracked, can you send a replacement — I need it by
> Tuesday, it's a birthday gift.* No order number. No product code. A human agent would spend ten
> minutes across five systems on this. And there are thousands of them a day.
>
> This is CasePilot. It resolves cases end to end — and, importantly, it proves it did."

*On screen: the customer portal with the message typed out.*

---

## 0:25–0:50 · Goal — the case arrives

Two ways in, and the difference is the point:

- **Chat** (`/portal`, the default) — a person is sitting there, so the agent can ask.
- **The request form** (“Or submit a request form”) and **email** — nobody is waiting, so the
  case runs straight through without stopping to ask.

For the main arc, submit from the **chat**, with the photo attached.

> **Optional 20-second detour, and it demos well.** Sign in as **Sofia Berg** and type something
> deliberately vague — *“something I bought is broken”*. Sofia has two recent orders, so the
> Intake agent cannot know which. It asks, and offers both orders as buttons naming the actual
> products. Tap one; the case binds that order and runs on to a resolution.
>
> *“It doesn’t guess, and it doesn’t make her go and find an order number. It asks the one
> question that resolves the ambiguity, and it asks it in a form she can answer with one tap.”*
>
> On the desk, the same exchange is on the record: the agent’s question is tagged **triage Q1**
> and shows exactly which options were put in front of her.

Switch to the desk. The case appears on the board and moves to **Triage**.

> "The Intake agent reads it the way a person would. No order number in the message — so it looks
> the customer up by email, finds order ORD-50001 delivered two days ago, and sets the goal:
> damaged item, needs resolution by the fifteenth."

*Point at the Details rail: Goal, Order, Need by — all inferred, none of it typed by the customer.*

---

## 0:50–1:30 · Decision — investigate, then choose

> "Now the Investigator picks its own tools. Nobody scripted this order."

*Scroll the work log slowly through the tool calls:*

```
get_order(order_id=ORD-50001)          ok
get_customer(customer_id=C-1001)       ok
track_shipment(shipment_id=SHP-7001)   ok
search_policy(query=damaged item replacement)   ok
check_inventory(sku=KT-220, region=east)        ok
```

> "It pulls the order, the customer's history, the delivery proof, the policy, and the stock
> position. Then it does the thing that matters — it doesn't just list options, it works out which
> ones are *eligible*, and cites the policy for each."

*Stop on the Facts card:*

> "Replacement — allowed, stock at WH-EAST. Refund — allowed, reported within fourteen days.
> It recommends the replacement, because that's what the customer actually asked for."

---

## 1:30–2:15 · Action → Adaptation — **the moment**

> "The Resolver plans the replacement from the East warehouse. Watch what happens when it tries to
> reserve the unit."

*Let the amber Environment event land on the timeline:*

> **"The last KT-220 unit in WH-EAST was sold through the marketplace channel."**

> "That is not a scripted branch. The inventory row genuinely changed between planning and
> reserving — the same race a real warehouse has every day. The action is refused: `OUT_OF_STOCK`."

*Point at the rose-red failed tool call, then the amber adapting divider.*

> "A script would stop here. CasePilot re-plans with the new fact. And look at the reasoning —
> the West warehouse *does* have six units, but it's six days' transit. That arrives Wednesday.
> The customer said Tuesday.
>
> So it refuses to promise something late, and refunds instead — thirty-eight pounds, under the
> damaged-goods policy."

**This is the single most important beat in the video. Do not rush it.**

---

## 2:15–2:45 · Result — verified, not claimed

*Scroll to the Verification panel in the right rail.*

> "Here's what I think separates this from a demo that just narrates. The agent doesn't get to say
> it's done.
>
> `refund posted` — expected 38.00, and the payment ledger shows 38.00.
> `no over refund` — 38.00 refunded of 70.00 captured. That invariant is checked on every single
> case.
> `customer notified` — the message actually went out.
>
> These are SQL queries against the systems of record. If any one fails, the case does not close —
> it goes back to planning."

*Open the **Enterprise systems** view briefly to show the refund sitting in the real ledger.*

Then show the customer's side:

> "And the customer gets a plain-language explanation — no jargon, no internal detail, and it's
> honest about why they're getting a refund instead of the replacement they asked for."

---

## 2:45–3:20 · Outcome — knowing when to stop

Run **two** more cases quickly from the Demo menu.

**Wrong high-value item (CS-1005):**

> "A 749-pound refund. The agent planned it, the auditor approved it — and then it stopped.
> The guardrail is in Python, not in the prompt: anything over 150 needs a human. No message from a
> customer can talk its way past that."

*Click **Approve**. It executes and verifies.*

**Suspicious claim (CS-1007):**

> "Here the customer says the parcel never arrived. But the carrier has photo proof of delivery,
> and this account has filed four claims in ninety days. The agent refuses to auto-refund and
> escalates — with the evidence attached, so the human doesn't re-investigate.
>
> Knowing when *not* to act is part of the job."

---

## 3:20–3:50 · How it works

*Show `docs/ARCHITECTURE.md` — the state machine diagram.*

> "Five agents behind an explicit state machine. Eleven read-only tools the model picks from
> freely, and five write actions only the executor can run. Every failure — a refused action or a
> failed verification — re-enters planning as a new fact, three times, then escalates with
> everything it learned.
>
> And it runs on free models: Gemini's free tier, then Groq, then OpenRouter — with a complete
> deterministic fallback underneath. What you just watched runs with no API key at all."

---

## 3:50–4:00 · Close

> "CasePilot: it investigates like an analyst, acts like an operator, and proves its work like an
> auditor. Thank you."

---

## Recording notes

- **Show, don't summarise.** Silence while the work log scrolls is better than talking over it.
- **Do not skip the failure.** The stockout and the escalation are what the rubric is asking for.
- Record the adaptation beat in one unbroken take — cutting it invites the suspicion it was staged.
- If a free-tier model is slow on the day, switch to `LLM_MODE=offline`. The behaviour and the
  narrative are identical, and nothing stalls on camera.
- Keep a local instance running as a backup even if you demo the hosted Space.

## Fallback order if something breaks live

1. Hosted Space (warmed up beforehand).
2. Local instance with a model key.
3. Local instance in `LLM_MODE=offline` — always works, needs no network.
4. The recorded video.

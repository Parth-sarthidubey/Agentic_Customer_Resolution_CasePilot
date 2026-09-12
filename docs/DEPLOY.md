# Deploying CasePilot

The judged requirement is a **runnable or deployed version**. This guide covers the free hosted
demo on Render, plus Docker and local fallbacks.

> **Note on Hugging Face Spaces.** Spaces was the original target, but as of 2026 HF only allows
> **static** Spaces on free accounts — running a Gradio or Docker Space requires a paid plan
> ($9/month PRO). The API returns:
> `Static Spaces are free for everyone, but hosting Gradio and Docker Spaces on free cpu-basic requires a PRO subscription.`
> Render is used instead: genuinely free, no credit card, and it builds the same `Dockerfile`.

---

## Recommended: Render (free, no card)

Render deploys from a GitHub repo, so this also produces the public repository Round 1 asks for.

### 1. Push to GitHub

```bash
cd casepilot
gh repo create casepilot --public --source=. --remote=origin --push
```

Or without the `gh` CLI: create an empty public repo at <https://github.com/new>, then

```bash
git remote add origin https://github.com/<your-username>/casepilot.git
git push -u origin main
```

Check that no secrets went up — `.env` and `data/` are git-ignored, and `.env.example` holds only
placeholder names.

### 2. Create the Render service

1. Sign up at <https://render.com> with **Sign in with GitHub** (free, no card).
2. **New → Blueprint**, pick the `casepilot` repo, **Apply**.

`render.yaml` in the repo root sets everything: Docker runtime, free plan, health check on
`/api/status`, and the environment variables.

If Blueprint isn't offered, use **New → Web Service** instead, pick the repo, and set
**Language: Docker**, **Instance type: Free**. The rest is picked up from the `Dockerfile`.

### 3. Wait for the build

First build takes **5–10 minutes** (installing Python dependencies). Watch the **Logs** tab. When
the status goes **Live**, the URL is `https://casepilot-xxxx.onrender.com`.

### 4. Add a free model key (optional)

Without a key CasePilot runs its deterministic offline agents and every scenario still completes —
the demo cannot break on someone else's rate limit. With a key, the reasoning in the work log is
written by a real model, which demos better.

In Render: **Environment → Add Environment Variable**

| Key | Value |
|---|---|
| `GEMINI_API_KEY` | a free key from <https://aistudio.google.com/apikey> |
| `GROQ_API_KEY` | *(optional fallback)* from <https://console.groq.com/keys> |

Saving triggers a redeploy (~1 minute). The pill in the top bar shows which provider is live.

### 5. Before you present

- **Wake it up.** Free Render services sleep after 15 minutes idle and take **up to a minute** to
  cold-start. Open the URL ten minutes before you demo and leave the tab open.
- **Reset it.** Demo cases → *Reset demo environment*, so the board starts clean.
- **Check the model pill.** It shows the live provider, or `Offline rules`. Either is fine — just
  know which you are demoing.
- Scenarios are safe to re-run: each restores its own slice of the enterprise sandbox before
  filing, so clicking the same demo case twice gives the same result.

---

## Other free options

| Platform | Card needed | Notes |
|---|---|---|
| **Render** | No | Recommended. Sleeps after 15 min idle. |
| **Koyeb** | Sometimes | 1 free service; may ask for a card to verify you are human. |
| **Google Cloud Run** | Yes | Generous free tier, scales to zero. Set `--max-instances=1` (the app keeps state in-process). |
| **Fly.io** | Yes | Free allowance, card on file. |
| **HF Spaces** | $9/mo | Docker Spaces need PRO. `README` frontmatter for it is no longer in the repo. |

Because the app holds state in SQLite and runs agents on background threads, keep it to a
**single instance**. Do not scale it horizontally.

---

## Docker (anywhere else)

```bash
docker build -t casepilot .
docker run --rm -p 7860:7860 -e GEMINI_API_KEY=... casepilot
```

Then open <http://localhost:7860>. `PORT` is configurable (Render sets it automatically);
`CASEPILOT_DATA_DIR` controls where the two SQLite databases are written.

---

## Local (fastest, and the best fallback)

```bash
uv run uvicorn app.main:app --reload --port 8000
```

Desk at <http://localhost:8000>, customer portal at <http://localhost:8000/portal>.

**Have this ready as a backup during the presentation.** Conference wifi fails; a local instance
does not, and it needs no API key.

---

## Environment reference

| Variable | Default | Purpose |
|---|---|---|
| `LLM_MODE` | `auto` | `auto` tries the providers then falls back to offline rules; `offline` never calls a model |
| `GEMINI_API_KEY` | — | Google AI Studio free tier (tried first) |
| `GROQ_API_KEY` | — | Groq free tier (second) |
| `OPENROUTER_API_KEY` | — | OpenRouter free models (third) |
| `OLLAMA_BASE_URL` | — | A local model, e.g. `http://localhost:11434/v1` (fourth) |
| `AUTOPILOT` | `true` | Start the agents automatically when a case is filed |
| `CASEPILOT_DATA_DIR` | `./data` | Where `desk.db` and `enterprise.db` are written |
| `PORT` | `7860` | Port the container listens on |

---

## Notes and limits

- **Storage is ephemeral.** Both databases live in the container's temp directory and are re-seeded
  on start. That is deliberate for a demo: every restart is a clean, known world.
- **No secrets in the repo.** `.env` and `data/` are git-ignored. Keys belong in the host's
  environment settings.
- All data is synthetic. "Kestrel Home" is a fictional retailer; the customers, orders and payments
  are generated by `app/sandbox/world.py`.

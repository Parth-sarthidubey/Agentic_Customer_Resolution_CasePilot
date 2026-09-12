# Deploying CasePilot

The judged requirement is a **runnable or deployed version**. This guide covers the hosted demo on
Hugging Face Spaces (free, public URL, no credit card), plus Docker and local fallbacks.

---

## Recommended: Hugging Face Spaces (free)

The `Dockerfile` already targets Spaces — it listens on port 7860 and writes its databases to
`/tmp/casepilot`, which is the writable path in a Space container.

### 1. Create the Space

1. Sign in at <https://huggingface.co> (free, no card).
2. Go to <https://huggingface.co/new-space>.
3. Fill in:
   - **Space name**: `casepilot`
   - **License**: MIT
   - **SDK**: **Docker** → **Blank**
   - **Hardware**: CPU basic (free)
   - **Visibility**: **Public**
4. Create it. You now have a git repo at
   `https://huggingface.co/spaces/<your-username>/casepilot`.

### 2. Push the code

Spaces authenticate with an access token, not your password. Create one at
<https://huggingface.co/settings/tokens> with the **write** role.

```bash
cd casepilot
git init                      # if this is not already a repo
git add -A
git commit -m "CasePilot: autonomous customer-resolution agent"

git remote add space https://huggingface.co/spaces/<your-username>/casepilot
git push space main           # username = your HF username, password = the write token
```

To avoid retyping the token, embed it in the remote instead:

```bash
git remote set-url space https://<username>:<hf_write_token>@huggingface.co/spaces/<username>/casepilot
```

> Keep that URL out of any public repo — it contains your token. It lives only in
> `.git/config`, which is not committed.

The Space rebuilds automatically on every push. Watch the **Logs** tab; the first build takes
around 3–5 minutes. When it turns green, the demo is live at
`https://huggingface.co/spaces/<your-username>/casepilot`.

### 3. Add a free model key (optional but recommended)

Without a key CasePilot runs its deterministic offline agents and every scenario still completes —
the demo cannot break on someone else's rate limit. With a key, the reasoning in the work log is
written by a real model, which demos better.

In the Space: **Settings → Variables and secrets → New secret**

| Name | Value |
|---|---|
| `GEMINI_API_KEY` | a free key from <https://aistudio.google.com/apikey> |
| `GROQ_API_KEY` | *(optional fallback)* from <https://console.groq.com/keys> |

Leave `LLM_MODE` unset (it defaults to `auto`: try the models, fall back to offline rules).
The Space restarts automatically when you save a secret.

### 4. Before you present

- **Wake it up.** A free Space sleeps after ~48 hours idle and takes 30–60 seconds to wake.
  Open the URL 10 minutes before the demo so it is warm.
- **Reset it.** Use the desk's reset control (or `POST /api/reset`) so the board starts clean.
- **Check the model pill** in the top bar — it shows which provider is live, or `offline rules`.
  Either is fine; just know which one you are demoing.
- Scenarios are safe to re-run. Each one restores its own slice of the enterprise sandbox before
  filing, so clicking the same demo case twice gives the same result.

---

## Docker (anywhere else)

```bash
docker build -t casepilot .
docker run --rm -p 7860:7860 -e GEMINI_API_KEY=... casepilot
```

Then open <http://localhost:7860>. `PORT` is configurable; `CASEPILOT_DATA_DIR` controls where the
two SQLite databases are written (default `/tmp/casepilot` in the image).

The same image runs unchanged on Render, Railway or Fly.io. Note that free tiers on those platforms
cold-start slowly, which is why Spaces is the recommendation for a live demo.

---

## Local (fastest, and the best fallback)

```bash
uv run uvicorn app.main:app --reload --port 8000
```

Desk at <http://localhost:8000>, customer portal at <http://localhost:8000/portal>.

**Have this ready as a backup during the presentation.** Conference wifi fails; a local instance
does not. It needs no API key.

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
- **No secrets in the repo.** `.env` and `data/` are git-ignored, and `.env.example` holds only
  placeholder names. Keys belong in Space secrets.
- All data is synthetic. "Kestrel Home" is a fictional retailer; the customers, orders and payments
  are generated by `app/sandbox/world.py`.

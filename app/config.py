"""Central configuration: paths, model providers, agent limits and business guardrails."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --- Paths ------------------------------------------------------------------
DATA_DIR = Path(os.getenv("CASEPILOT_DATA_DIR", str(ROOT / "data"))).resolve()
APP_DB = DATA_DIR / "desk.db"          # cases, messages, agent trace, proposals, learned KB
STORE_DB = DATA_DIR / "enterprise.db"  # simulated enterprise systems (CRM, orders, inventory, payments, shipping)
UPLOAD_DIR = DATA_DIR / "uploads"
SEED_DIR = ROOT / "seed"
STATIC_DIR = ROOT / "static"
PROMPTS_DIR = ROOT / "app" / "agents" / "prompts"

# --- LLM providers (free tiers; tried in order, then offline rules) ----------
LLM_MODE = os.getenv("LLM_MODE", "auto").lower()   # auto | offline

# AWS Bedrock. Unlike the free tiers it has no tokens-per-minute ceiling worth worrying about,
# so it is tried first when credentials are present. Costs fractions of a cent per case.
BEDROCK_ENABLED = os.getenv("BEDROCK_ENABLED", "").lower() in ("1", "true", "yes")
BEDROCK_REGION = os.getenv("BEDROCK_REGION", "us-east-1")
BEDROCK_MODEL = os.getenv("BEDROCK_MODEL", "amazon.nova-lite-v1:0")
# CasePilot's own credentials, never the machine's ambient AWS identity - see app/bedrock.py.
# Either a Bedrock API key (simplest) or an access-key pair.
BEDROCK_API_KEY = os.getenv("BEDROCK_API_KEY", "")
BEDROCK_ACCESS_KEY_ID = os.getenv("BEDROCK_ACCESS_KEY_ID", "")
BEDROCK_SECRET_ACCESS_KEY = os.getenv("BEDROCK_SECRET_ACCESS_KEY", "")
BEDROCK_SESSION_TOKEN = os.getenv("BEDROCK_SESSION_TOKEN", "")

# Real-time spend ceiling. AWS Budgets actions lag by hours, so they cannot stop a runaway
# loop; this can, because Bedrock reports exact token usage on every response. When the cap is
# reached Bedrock switches off and the router falls through to the other providers.
BEDROCK_MAX_SPEND_USD = float(os.getenv("BEDROCK_MAX_SPEND_USD", "2.00"))
# USD per 1000 tokens (input, output). Defaults are Amazon Nova Lite list prices.
BEDROCK_PRICE_IN_PER_1K = float(os.getenv("BEDROCK_PRICE_IN_PER_1K", "0.00006"))
BEDROCK_PRICE_OUT_PER_1K = float(os.getenv("BEDROCK_PRICE_OUT_PER_1K", "0.00024"))

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "ministral-8b-latest")
MISTRAL_BASE_URL = "https://api.mistral.ai/v1"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

LLM_TIMEOUT_SECONDS = 30   # free tiers stall; fail fast and retry or fail over
# Free tiers return 503 "high demand" intermittently. That is transient, so retry the same
# provider with backoff before failing over; only a real quota signal (429) benches it for
# long, because a whole case finishes in well under a minute.
TRANSIENT_RETRIES = 3
TRANSIENT_BACKOFF_SECONDS = 1.5
TRANSIENT_COOLDOWN_SECONDS = 5
RATE_LIMIT_COOLDOWN_SECONDS = 60

# --- Agent limits -------------------------------------------------------------
MAX_AGENT_STEPS = 12          # tool-calling iterations per agent run
MAX_AUDIT_ROUNDS = 2          # resolver <-> policy auditor revisions
MAX_REPLANS = 3               # plan -> execute -> verify cycles before escalating
MAX_TRIAGE_ROUNDS = 3         # clarifying questions before triage gives up and routes to a human
# Every step resends the whole history, so a fat tool result is paid for again on each later
# call. Free tiers meter tokens per minute (Groq: 8k/min), which is the real binding limit.
TOOL_RESULT_MAX_CHARS = 1500
TOOL_PARALLELISM = 6        # independent reads in one tool round run concurrently
AUTOPILOT = os.getenv("AUTOPILOT", "true").lower() == "true"

# --- Business guardrails (deterministic, never decided by the LLM) -------------
AUTO_REFUND_LIMIT = 150.0     # refunds above this need a human
AUTO_CREDIT_LIMIT = 50.0      # store credit above this needs a human
KB_TOP_K = 3

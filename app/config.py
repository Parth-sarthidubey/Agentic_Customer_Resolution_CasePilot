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

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

LLM_TIMEOUT_SECONDS = 60
PROVIDER_COOLDOWN_SECONDS = 60

# --- Agent limits -------------------------------------------------------------
MAX_AGENT_STEPS = 12          # tool-calling iterations per agent run
MAX_AUDIT_ROUNDS = 2          # resolver <-> policy auditor revisions
MAX_REPLANS = 3               # plan -> execute -> verify cycles before escalating
TOOL_RESULT_MAX_CHARS = 6000
AUTOPILOT = os.getenv("AUTOPILOT", "true").lower() == "true"

# --- Business guardrails (deterministic, never decided by the LLM) -------------
AUTO_REFUND_LIMIT = 150.0     # refunds above this need a human
AUTO_CREDIT_LIMIT = 50.0      # store credit above this needs a human
KB_TOP_K = 3

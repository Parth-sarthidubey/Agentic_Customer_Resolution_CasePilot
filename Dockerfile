# Works on Hugging Face Spaces (Docker SDK, port 7860), Render, Railway, or locally.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    CASEPILOT_DATA_DIR=/tmp/casepilot \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PORT=7860

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .

# Hugging Face Spaces runs the container as uid 1000, which cannot write to /app.
# Everything the app writes goes to CASEPILOT_DATA_DIR under /tmp, and uvicorn is
# invoked straight from the pre-built venv so nothing is resolved at start-up.
RUN mkdir -p /tmp/casepilot && chmod 777 /tmp/casepilot && chmod -R a+rX /app
USER 1000

EXPOSE 7860
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]

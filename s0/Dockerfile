# S0 walking skeleton (throwaway). Headless image for both the auth proof and the
# Telegram bot. The Agent SDK shells out to the `claude` CLI, so the image needs the
# Node-based Claude Code CLI alongside the Python deps.
FROM python:3.12-slim-bookworm

# Node runtime + the Claude Code CLI the SDK invokes (`shutil.which("claude")`).
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

# uv for the Python dependency install.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml ./
COPY s0/ ./s0/
RUN uv sync --no-dev

# Put the project venv on PATH so the CMD runs without re-resolving at startup.
ENV PATH="/app/.venv/bin:$PATH"

# Default to the auth proof; override the command (see compose.yml) to run the bot.
CMD ["python", "s0/verify_auth.py"]

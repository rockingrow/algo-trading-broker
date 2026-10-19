FROM python:3.13-slim

WORKDIR /app

# System deps for asyncpg
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Copy dependency files first (layer cache)
COPY pyproject.toml uv.lock ./

# Install only production dependencies (no dev group)
RUN uv sync --no-dev --no-install-project

# Copy application source
COPY README.md ./
COPY alembic.ini ./
COPY alembic/ alembic/
COPY broker/ broker/
COPY scripts/ scripts/

# Make entrypoint executable
RUN chmod +x /app/scripts/docker-entrypoint.sh

# Install the project itself
RUN uv sync --no-dev

ARG WEBHOOK_PORT=80
EXPOSE ${WEBHOOK_PORT}

# Put the project venv on PATH so the interpreter, alembic and the app run
# without uv at runtime. `uv run` does not exec: it stays alive as the
# container's main process, which costs ~30 MB and puts one more hop between
# `docker stop`'s SIGTERM and FastAPI's shutdown handler. A TradingView
# signal dropped mid-request is never re-sent, so that hop is worth removing.
ENV PATH="/app/.venv/bin:${PATH}"

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["python", "-m", "broker.main"]

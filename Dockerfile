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
COPY broker/ broker/
COPY scripts/ scripts/

# Make entrypoint executable
RUN chmod +x /app/scripts/docker-entrypoint.sh

# Install the project itself
RUN uv sync --no-dev

ARG WEBHOOK_PORT=8080
EXPOSE ${WEBHOOK_PORT}

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["uv", "run", "--no-dev", "python", "-m", "broker.main"]

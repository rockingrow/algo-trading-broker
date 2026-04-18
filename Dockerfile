FROM python:3.13-slim

WORKDIR /app

# System deps for pyzmq + asyncpg
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libzmq3-dev \
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

EXPOSE 8080 5555

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["uv", "run", "python", "-m", "broker.main"]

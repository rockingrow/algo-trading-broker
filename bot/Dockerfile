FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Dependency files first (layer cache). uv.lock is optional — if absent, uv
# resolves from pyproject.toml.
COPY pyproject.toml ./
COPY uv.lock* ./

# Install only the dependencies (project is not a package — see [tool.uv]).
RUN uv sync --no-dev

# Application source
COPY README.md ./
COPY app/ app/

ENTRYPOINT []
CMD ["uv", "run", "--no-dev", "python", "-m", "app"]

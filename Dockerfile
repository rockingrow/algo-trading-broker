FROM python:3.13-slim

WORKDIR /app

# System deps for pyzmq + asyncpg
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libzmq3-dev \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
ENV POETRY_VERSION=2.1.2
ENV POETRY_HOME=/opt/poetry
ENV POETRY_VENV_IN_PROJECT=1
ENV POETRY_NO_INTERACTION=1
RUN curl -sSL https://install.python-poetry.org | python3 -
ENV PATH="${POETRY_HOME}/bin:${PATH}"

# Copy dependency files first (layer cache)
COPY pyproject.toml poetry.lock* ./

# Install only production dependencies (no dev group)
RUN poetry install --only main --no-root

# Copy application source
COPY broker/ broker/

EXPOSE 8080 5555 5556

CMD ["poetry", "run", "python", "-m", "broker.main"]

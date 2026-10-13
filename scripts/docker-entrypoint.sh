#!/bin/bash
set -e

echo "Running database migrations..."
# /app/.venv/bin is on PATH (see Dockerfile) — uv is a build-time tool only.
alembic upgrade head

echo "Starting FastAPI application..."
exec "$@"

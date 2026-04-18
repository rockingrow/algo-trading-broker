#!/bin/bash
set -e

# 1. Ensure ZMQ keys are present in .env
# We use 'uv run' to ensure dependencies like zmq are available
echo "Running ZMQ key check..."
uv run python scripts/ensure_keys.py

# 2. Run the main application
echo "Starting FastAPI application..."
exec "$@"

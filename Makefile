.PHONY: help install install-dev update lock fix format lint check run

help:
	@echo "Available commands:"
	@echo "  make install     - Install production dependencies"
	@echo "  make install-dev - Install all dependencies including dev"
	@echo "  make update      - Update dependencies and regenerate lock file"
	@echo "  make lock        - Regenerate poetry.lock without updating"
	@echo "  make fix         - Run ruff format and check --fix"
	@echo "  make format      - Run ruff format"
	@echo "  make lint        - Run ruff check"
	@echo "  make check       - Alias for lint"
	@echo "  make run         - Run the broker locally"

install:
	poetry install --only main

install-dev:
	poetry install

update:
	poetry update

lock:
	poetry lock --no-update

fix:
	poetry run ruff format .
	poetry run ruff check --fix .

format:
	poetry run ruff format .

lint:
	poetry run ruff check .

check: lint

run:
	poetry run python -m broker.main

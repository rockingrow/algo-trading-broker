.PHONY: help fix format lint check

help:
	@echo "Available commands:"
	@echo "  make fix    - Run ruff format and check --fix"
	@echo "  make format - Run ruff format"
	@echo "  make lint   - Run ruff check"
	@echo "  make check  - Alias for lint"

fix:
	ruff format .
	ruff check --fix .

format:
	ruff format .

lint:
	ruff check .

check: lint

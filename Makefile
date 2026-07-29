# Every target runs inside the dev container, so nothing but Docker has to exist on the host.
DCR := docker compose run --rm python-cli

.PHONY: help build test lint format typecheck check dcr

help:
	@echo "build      Build the dev and runtime images"
	@echo "test       Run the test suite with coverage"
	@echo "lint       Run ruff"
	@echo "format     Apply ruff formatting"
	@echo "typecheck  Run mypy"
	@echo "check      lint + format --check + typecheck + test"
	@echo "dcr        Run an arbitrary command, e.g. make dcr cmd=\"uv run python -V\""

build:
	docker compose build python-cli polar-connector

test:
	$(DCR) uv run pytest --cov --cov-report=term-missing

lint:
	$(DCR) uv run ruff check

format:
	$(DCR) uv run ruff format

typecheck:
	$(DCR) uv run mypy

check:
	$(DCR) sh -c "uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest -q"

dcr:
	$(DCR) $(cmd)

PY ?= .venv/Scripts/python.exe
UV ?= uv

.PHONY: sync dev lint test migrate revision run clean

sync:
	$(UV) sync || $(PY) -m pip install -e ".[dev]"

dev:
	$(PY) -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

run:
	$(PY) -m app.main

lint:
	$(PY) -m ruff check app tests
	$(PY) -m mypy app

test:
	$(PY) -m pytest tests -q

migrate:
	$(PY) -m alembic upgrade head

revision:
	$(PY) -m alembic revision --autogenerate -m "$(m)"

init:
	$(PY) -m app.cli init-db

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache

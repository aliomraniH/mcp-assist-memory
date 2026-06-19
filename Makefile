.PHONY: install migrate run dev test

PY ?= python3
PIP ?= $(shell command -v uv >/dev/null && echo "uv pip" || echo "$(PY) -m pip")

install:
	$(PIP) install -e ".[dev]"

# Apply the frozen initial migration. Run with the OWNER/migrator DATABASE_URL.
migrate:
	@test -n "$$DATABASE_URL" || (echo "DATABASE_URL is required" && exit 1)
	psql "$$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0001_init.sql

# Run the Postgres-backed service (Reserved VM uses `python main.py`).
run:
	uvicorn assist_memory.app:app --host 0.0.0.0 --port $${PORT:-8000} --no-access-log

dev:
	uvicorn assist_memory.app:app --reload --host 127.0.0.1 --port $${PORT:-8000}

# Unit suite. SQLite tests run anywhere; Postgres tests run only when
# DATABASE_URL points at a Postgres+pgvector database (otherwise skipped).
test:
	$(PY) -m pytest -q

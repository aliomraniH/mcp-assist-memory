.PHONY: install lock migrate run test backfill

install:
	pip install -c constraints.txt -e ".[test]"

# Regenerate constraints.txt after an intentional, verified dependency upgrade.
lock:
	./scripts/lock-deps.sh

migrate:
	python scripts/migrate.py

run:
	uvicorn app:app --host $${HOST:-0.0.0.0} --port $${PORT:-8000}

test:
	pytest -q

backfill:
	python scripts/backfill_artifacts.py $(SRC)

.PHONY: install migrate run test backfill

install:
	pip install -e ".[test]"

migrate:
	python scripts/migrate.py

run:
	uvicorn app:app --host $${HOST:-0.0.0.0} --port $${PORT:-8000}

test:
	pytest -q

backfill:
	python scripts/backfill_artifacts.py $(SRC)

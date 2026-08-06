.PHONY: install lock migrate run test smoke backfill

install:
	pip install -c constraints.txt -e ".[test]" && pip install --no-deps 'en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl'

# Regenerate constraints.txt after an intentional, verified dependency upgrade.
lock:
	./scripts/lock-deps.sh

migrate:
	python scripts/migrate.py

run:
	uvicorn app:app --host $${HOST:-0.0.0.0} --port $${PORT:-8000}

test:
	pytest -q

# Post-deploy MCP handshake probe. Set SMOKE_BASE_URL + SMOKE_TOKEN (an active
# token from /admin); exits non-zero if the connector handshake/guard rails break.
smoke:
	python scripts/smoke_mcp.py

backfill:
	python scripts/backfill_artifacts.py $(SRC)

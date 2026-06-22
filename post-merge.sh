#!/bin/bash
set -e

# Reconcile dependencies after a task merge.
pip install -e .

# Apply any pending DB migrations (idempotent). Skip if DB not configured.
if [ -n "$DATABASE_URL" ]; then
  python scripts/migrate.py
fi

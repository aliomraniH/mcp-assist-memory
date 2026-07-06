#!/bin/bash
set -e

# Reconcile dependencies after a task merge. Use the pinned constraints so the
# post-merge environment installs the same versions verified in dev (see
# constraints.txt for why, and scripts/lock-deps.sh to regenerate the pins).
pip install -c constraints.txt -e .

# Apply any pending DB migrations (idempotent). Skip if DB not configured.
if [ -n "$DATABASE_URL" ]; then
  python scripts/migrate.py
fi

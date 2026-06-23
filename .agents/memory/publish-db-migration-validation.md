---
name: Publish-time DB migration validation (managed Postgres)
description: Why "Failed to validate database migrations" happens on republish for managed-Postgres apps, and the supported fix.
---

# Publish-time database migration validation

On Replit, when an app uses managed Postgres, the **Publish flow** automatically
diffs the development DB schema against production and applies it at publish.
This step **cannot be disabled**. The agent must not run DDL against prod, write
prod migration scripts, or use `executeSql({environment:"production"})` for DDL
(it is read-only).

## Symptom
Republish fails with:
- `Failed to validate database migrations`
- `Unexpected error attempting to continue hosting preview deployment`

## Root cause (legacy/shared Neon dev DB era)
Older **shared** Neon development databases were deprecated and shut down on
**2026-06-08**. Apps published against a shared/legacy dev DB (or whose prod DB
diverged from dev) fail the publish-time migration validation. Diagnose by
comparing dev `DATABASE_URL` vs the published app's `DATABASE_URL` — identical
means a shared DB.

## Supported fix (a USER action in the Publish pane — agent cannot click it)
Republish and enable both:
1. **Create production database**
2. **Set up your production database with your current development data**

This provisions a dedicated prod DB seeded from the dev schema + data, separate
from dev going forward. Live app then uses prod; Project Editor keeps using dev.

## App that self-migrates
If the app owns its schema (raw SQL migrations, pgvector, advanced DDL the
auto-validator can't represent), the supported pattern is to run the migration
command in the deploy **build/pre-deploy/run** step (e.g. `python scripts/migrate.py`
before `uvicorn`). It is idempotent, so it co-exists with the seeded prod DB.

**Why:** Replit's auto schema-differ can't represent `CREATE EXTENSION vector/pg_trgm`,
`GENERATED ALWAYS AS IDENTITY`, partial unique indexes, `gin_trgm_ops`, CHECK
constraints — so relying on it alone is fragile; the app's own migrate is the
source of truth and the "create production database" option avoids the broken diff.

**How to apply:** When a managed-PG app fails republish with the messages above,
tell the user to republish with the two checkboxes enabled; keep the migrate step
in the deploy command. Do not attempt prod DDL or to "disable" the publish diff.

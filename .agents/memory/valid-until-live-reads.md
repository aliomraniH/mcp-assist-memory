---
name: valid_until / live-revision reads
description: Adding a new "not-live" dimension to the revisioned memory store requires updating BOTH SQL filters and Python post-filter SELECTs in lockstep.
---

# `valid_until` and the "latest live revision" reads

The memory store is append-only + revisioned. "Live" originally meant `NOT tombstone`.
The curator added a second not-live dimension: `valid_until` (a supersession boundary;
non-NULL timestamp in the past = superseded, not live). `_is_live(row)` checks both.

**Rule:** when you add a new not-live column, every read that returns the latest live
revision must honor it, and there are TWO distinct styles that each need the update:

1. **SQL-filtered reads** (memory_get/list/search legs) — add
   `AND (valid_until IS NULL OR valid_until > now())` to the WHERE.
2. **Python post-filtered reads** (coord_health/drift_scan/reconcile/reconcile_repo)
   call `_is_live(r)` in Python over fetched rows. These often `SELECT` an *explicit
   column list*, not `*`. If the new column is missing from that list, `_is_live`'s
   `row.get("valid_until")` defaults to None → the row is wrongly treated as live and
   superseded rows leak into reports.

**Why:** `coord_health` shipped with `_is_live(r)` but an explicit SELECT that omitted
`valid_until`, so superseded entries silently leaked into duplicate/collision reports.
The Python filter looked correct; the bug was the missing SELECT column.

**How to apply:** grep for both `_is_live(` and `valid_until IS NULL` whenever you
touch live-read semantics; for every `_is_live` call site confirm the row actually
carries `valid_until` (use `SELECT *` or add the column explicitly).

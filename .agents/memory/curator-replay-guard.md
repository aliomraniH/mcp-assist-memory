---
name: Curator replay guard (revision-stable UPDATE)
description: Why unchanged-content curator UPDATEs are NOOPed instead of writing a new revision, and the deliberate scope limits.
---

Curator idempotency has TWO levels: deterministic event_id dedups exact same-session
re-application; a content-hash guard in `apply_curation` makes cross-session replay
revision-stable — an UPDATE whose `_content_hash(sanitize(value))` equals the live
row's stored `content_hash` (and kind unchanged) is counted as noop
("unchanged_content") and never written.

**Why:** Stop-hook / re-run curation kept churning byte-identical revisions
(rev 1→2) because a second session's UPDATE carries a different event_id.

**How to apply / scope limits (deliberate):**
- UPDATE only. MERGE/SUPERSEDE are NOT guarded — they have side effects on other
  keys (validity boundaries, closures); skipping their write would drop those.
- Metadata-only changes (salience/confidence/tags/meta) with identical value+kind
  are treated as confirmations and skipped. If that ever needs to change, add the
  fields to the guard predicate — don't remove the guard.
- Quarantined latest rows are invisible to the guard (default `memory_get`), so an
  identical UPDATE against a quarantined entry still writes — acceptable.
- The guard must hash the SAME sanitized form `_append` hashes: `sanitize(value)`.

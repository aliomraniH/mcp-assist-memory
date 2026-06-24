---
name: Server-side reconnect retry policy
description: When the Postgres backend may transparently retry a dropped connection, and why it is gated on idempotency.
---

# Transparent reconnect retries are idempotency-gated

The Postgres backend retries an operation on a dropped connection ONLY when a
replay is safe. Two independent gates:

1. **Error gate** — retry only *genuine* disconnects, never every
   `OperationalError`. A disconnect is: no SQLSTATE (client-side "connection
   closed"), SQLSTATE class `08xxx`, or `57P01/57P02/57P03` (operator-intervention
   shutdown, e.g. Neon scale-down's "terminating connection due to administrator
   command"). Other operational errors (lock timeout `55P03`, too-many-connections
   `53300`, …) must surface unchanged.

2. **Idempotency gate** — auto-retry reads and *idempotent* writes always; retry
   the session writes too but with eyes open about the tradeoff.
   - Reads: always retry.
   - `artifact_put`: always (content-addressed + `ON CONFLICT DO NOTHING`).
   - `memory_save` / `handoff_save` / `memory_delete`: retry only when an
     `event_id` is supplied (exactly-once, so a replay collapses to a no-op).
   - `session_create` / `session_append_event`: **DO retry** — production
     failures were all 57P01 drops on exactly these two paths. Accepted tradeoff:
     a drop in the commit-ack window means `session_create` may orphan an empty
     unreferenced session row, and `session_append_event` is **at-least-once**
     (a replay can append one duplicate event). For an append-only session log
     that is strictly better than failing the call.
   - A save WITHOUT an `event_id`: still never auto-retry — run once, surface.

**Why:** a backend terminated mid-transaction rolls back (safe to replay), but a
drop in the narrow commit-but-before-ack window would replay a write and double
it. For most writes we avoid that by gating on `event_id`; for `session_*` we
deliberately accept the at-least-once duplicate because a hard failure mid-tool-
call is worse than a rare duplicate event in an append-only log.

**How to apply:** when adding a new backend method, decide read vs write and
whether it is idempotent BEFORE wiring retries. Decorate reads + idempotent
writes; leave non-idempotent writes undecorated. Never blanket-wrap every method
(an earlier inspect-based version did this and also double-wrapped delegators like
`handoff_load`→`memory_get`, amplifying retries). The pool is also built with
`check=AsyncConnectionPool.check_connection` so dead idle connections are
discarded at checkout — that is the first line of defense; method retries are the
backstop.

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

2. **Idempotency gate** — auto-retry reads and *idempotent* writes only.
   - Reads: always retry.
   - `artifact_put`: always (content-addressed + `ON CONFLICT DO NOTHING`).
   - `memory_save` / `handoff_save` / `memory_delete`: retry only when an
     `event_id` is supplied (exactly-once, so a replay collapses to a no-op).
   - `session_create`, `session_append_event`, and any save WITHOUT an
     `event_id`: never auto-retry — run once and surface the error.

**Why:** a backend terminated mid-transaction rolls back (safe to replay), but a
drop in the narrow commit-but-before-ack window would replay a non-idempotent
write and double it. Gating on idempotency means a transparent retry can never
cause a silent double-write.

**How to apply:** when adding a new backend method, decide read vs write and
whether it is idempotent BEFORE wiring retries. Decorate reads + idempotent
writes; leave non-idempotent writes undecorated. Never blanket-wrap every method
(an earlier inspect-based version did this and also double-wrapped delegators like
`handoff_load`→`memory_get`, amplifying retries). The pool is also built with
`check=AsyncConnectionPool.check_connection` so dead idle connections are
discarded at checkout — that is the first line of defense; method retries are the
backstop.

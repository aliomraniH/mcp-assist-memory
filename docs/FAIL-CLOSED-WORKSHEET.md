# Fail-closed worksheet (Phase 0, T0.2)

Method: read the `memory_save` path end-to-end and list every place an error can
occur **after the dedup check**; then grep the whole write surface
(`memory_save`, `memory_delete`, `handoff_save`, `session_append_event`,
`artifact_put`) for `except:` / catch-log-continue / error-flag-then-ack
patterns. This list is the Phase 2 (T2.4) checklist; acceptance requires the
grep to come back clean of swallow-and-continue in write paths.

## A. Error points in `_append` after `_seen_event` (the dedup check)

| # | Site | Failure mode | Current behavior | Verdict |
|---|------|--------------|------------------|---------|
| 1 | `Jsonb(sanitized)` payload construction | non-JSON-serializable value | raises at execute time | fail-closed ✅ |
| 2 | `conn.execute(INSERT …)` | connection loss, statement timeout, CHECK violation (bad `kind`), UniqueViolation | raises; UniqueViolation retried ≤3 then `raise last_exc` | fail-closed ✅ (raw error shape — standardized in T2.5) |
| 3 | duplicate-event_id race check (`_seen_event` on a 2nd connection) | 2nd checkout fails | raises out of the handler | fail-closed ✅ |
| 4 | `cur.fetchone()` after RETURNING | none expected (RETURNING always yields) | — | n/a |
| 5 | transaction commit at `conn.transaction()` exit | commit failure | raises (the `return` unwinds through the CM) | fail-closed ✅ |
| 6 | commit-succeeds-but-ack-lost window | connection drops between server commit and client ack | with `event_id`: retried, collapses to dedup no-op ✅; without: surfaces to caller (at-most-once, honest) ✅ |
| 7 | `_row_to_entry(row)` | malformed row | raises after commit → caller sees error for a write that DID land | **known gap**: an error here is a false negative (persisted but reported failed). Phase 2 read-back makes the ack evidence-based either way. |
| 8 | **missing**: no read-back through a public read path | phantom-ack class: in-hand RETURNING row looks fine while a pooler/proxy lied about durability | success ack constructed purely from the in-hand object | **T2.3 fixes** |

## B. Grep results — swallow patterns on the write surface

`grep -n "except" storage/postgres.py server/mcp_server.py` filtered to write
paths:

| Site | Pattern | Classification |
|------|---------|----------------|
| `_maybe_embed` / `_maybe_embed_query` / `_maybe_embed_text` (`storage/postgres.py:272,283,310`) | `except Exception: return None` | **Accepted best-effort** — the embedding is a search accelerator, not the persisted value. Its absence degrades recall, never persistence. Documented contract; stays. Telemetry (Phase 1) makes the miss observable. |
| `_safe_literal` (`storage/postgres.py:299`) | `except (TypeError, ValueError): return None` | Accepted best-effort — same rationale. |
| `_append` UniqueViolation handler (`storage/postgres.py:378`) | catch → retry/return winner → `raise last_exc` | fail-closed ✅ (bounded retry, always terminates in a return-of-truth or a raise) |
| `session_append_event` UniqueViolation loop (`storage/postgres.py:960`) | catch → retry → `raise last_exc` | fail-closed ✅ |
| `_retry_on_disconnect` (`storage/postgres.py:59`) | catch conn-loss → retry ≤3 → `raise last` | fail-closed ✅; applied to non-idempotent writes only via `_retry_if_idempotent` gate |
| `artifact_put` tool (`server/mcp_server.py:181`) | catch b64 error → `raise ValueError` | fail-closed ✅ (re-raise, not swallow) |
| `_split_meta` (`storage/postgres.py:108`) | non-dict `meta` silently treated as absent | **lenient-input site**, not an ack-integrity hole (nothing is claimed persisted that isn't). Logged as `unknown_arg_accepted`-class telemetry from Phase 1; candidate for R6 strictness arms. |
| `sanitize` silently rewriting values (delimiter strip) | data transformed without caller signal | not a persistence hole; made visible in Phase 3 (T3.3 escape + response `screening` field). |

**Conclusion:** the write surface has no catch-log-continue or
error-flag-then-ack site today. The two real integrity gaps are (A8) no
read-back verification behind the success ack, and the **global** `event_id`
dedup scope (any two writers sharing an event_id silently collapse to one
write — `migrations/0001_init.sql:40`). Both are Phase 2.

## C. Curator apply path (secondary write surface)

`apply_curation` drops ops fail-closed (PHI gate, invalid ops are counted, not
silently skipped — counts returned to the caller ✅). `AnthropicCurator.curate`
swallows all exceptions → zero operations: accepted best-effort (a dropped
memory is recoverable; the caller sees `operations: []` and counts, never a
fabricated success).

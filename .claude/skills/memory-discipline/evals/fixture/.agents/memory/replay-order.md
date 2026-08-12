# Replay order: fold in insertion (file) order, never timestamp order

Status: verified
Date: 2026-07-21
Binding: tracking
Evidence: staging incident 2026-07-20 — sorting events by `occurred_at`
before folding resurrected the deleted key `a` in the staging projection;
replaying in file order matched the source system exactly. Repro: fold
`events.jsonl` both ways and diff.

`events.jsonl` is append-only and file order is the system's commit order.
Late-arriving events (an `occurred_at` earlier than already-appended events)
are NORMAL, not corruption.

ANTI-PATTERN: `sorted(events, key=lambda e: e["occurred_at"])` (or any
chronological re-ordering) before folding. Chronological replay LOOKS more
natural and is wrong for this log: a late-arriving `set` must win over an
earlier-appended `delete`, because commit order — not event timestamp — is
what the projection mirrors.

Correct: fold events exactly in file order; last event in file order wins
per key.

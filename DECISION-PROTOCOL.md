# DECISION-PROTOCOL.md — Workstream E decision rules (Phase 10)

Committed BEFORE any variant flips or data collection. Edits to the rules
below after data collection begins are protocol violations: note them here,
dated, with the reason — never silently.

## Review trigger

30 sessions per active arm, or 6 weeks after the first flip — whichever comes
first.

## Decision rules (pre-registered)

* **R6 (arg strictness):** adopt S-hint if one-turn recovery
  (`v_one_turn_recovery`) ≥ control +20 pts; adopt S-plain instead if plain
  strictness alone captures ≥80% of the hint arm's gain.
* **R5 (advisory format):** adopt an advisory mode if heed rate ≥60%
  (corrective re-save of the same key ≤3 turns, `v_advisory_heed` +
  key-level analysis) and over-correction ≤ control +10%. Prefer B-minimal
  unless A-full beats it by ≥15 pts.
* **R1 (convention placement):** if V1 (description sentence) alone cuts the
  stale-pin rate ≥80% relative (`v_stale_pin_rate`), the sentence suffices;
  V2's GitHub round-trip stays best-effort and off by default.
* **R9 (remedy field):** stays ON unless recovery deltas
  (`v_error_recovery`, remedy_emitted split) are null.
* **Screening (Phase 3):** if the FP fraction — quarantines cleared via
  `screening_override` ÷ total quarantines (`v_screening_hit_rate`) — exceeds
  50% over the window, patterns get tuned before the next window. If any TRUE
  positive appears, write it up: it is the first real-world datapoint for the
  whole Phase 3 rationale.

## Analysis session

Claude Code joins the `tool_events` views with `_meta/observations`
(`memory_history('<ns>', '_meta/observations')`), emits
`ANALYSIS-ERGONOMICS.md`, and saves accepted decisions into the spine as
`decision/ergonomics/<id>` with `origin: synthesized`, `derived_from` pointing
at the analysis inputs, and `meta.repo_sha` pinned to live head — the plan's
own conventions, dogfooded.

## Bias controls (pre-committed, T8.3)

Observations are prompted at friction: they over-sample error/quarantine
paths, under-sample silent success, and models perform agreeableness. An
observation may EXPLAIN a metric; it may never REPLACE one. `tool_events` is
the denominator, never the vote count.

## Parked backlog

* Description token budget vs tool-selection accuracy — log description token
  counts before/after any description change as datapoint zero.
* Adversarial evaluation of the wrappers + screening against
  AgentPoison/MINJA-style writes in a scratch namespace — REQUIRED before any
  security claim stronger than "a layer".
* Advisory × client-hook interaction (double-fire corrections).

## Flip ledger

| date | namespace | profile | note |
|------|-----------|---------|------|
| (none yet — all namespaces on control) | | | |

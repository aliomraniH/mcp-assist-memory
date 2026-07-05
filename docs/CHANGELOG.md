# CHANGELOG — append-only, one entry per phase, with commit SHA

(MCP_Assist Improvement Plan v2 — Trust Boundary + Ergonomics, unified.
Never rewrite an entry; append forward only.)

## Phase 0 — `2154a19`
Discovery + safety rails, no behavior changes. `docs/CODEMAP.md`,
`docs/FAIL-CLOSED-WORKSHEET.md` (the Phase 2 checklist),
`tests/test_baseline_contract.py` (pins: silent dedup, global dedup scope,
unverified acks, raw error shapes, marker stripping, bare-list reads,
lenient meta), `storage/redact.py` + PHI-gate test.

## Phase 1 — `1db9935`
Telemetry spine first. `migrations/0006_trust_spine.sql`: PHI-safe
`tool_events`, `variant_profiles`, and (rule 6) all Phase 2–6 columns bundled
into ONE migration + one schema_version bump (→ 6). Seven metric views
pre-registered before data exists. Tool-layer `instrument` wrapper: one
telemetry row per call, swallowed-on-failure (observability never fails a
call); version stamps on every dict response and every persisted revision.
Baseline flip: version stamps.

## Phase 2 — `da4b211`
Persistence integrity. T2.1 actor-scoped idempotency
((namespace, actor, event_id); reconciler/curator get their own actors);
T2.2 visible dedup (`deduplicated`, `original_created_at`); T2.3
read-back-verified writes through the public read path
(`verified_persisted`, `revision_id`, `readback_latency_ms`; mismatch ⇒
`write_verification_failed`, never a success ack); T2.4 fail-closed sweep +
automated grep audit; T2.5 `errors/catalog.py` standardized payload
{code, message, remedy, retryable} surfaced as MCP isError results; T2.6
deliberate-violation suite. Baseline flips: dedup visibility, dedup scope,
verified acks, error shapes.

## Phase 3 — `4acb608`
Write-time screening + quarantine (layer, not proof). Deterministic pattern
names only; flagged writes persist quarantined with the verdict in the ack;
default reads exclude quarantined (`include_quarantined` opts in);
`screening_override` + real actor clears with an append-only audit trail;
`coord_health.quarantined_count`. T3.3 one-way visible marker escape
(`[[UNTRUSTED_DATA]]`/`[[END]]`), never unescaped on read. Baseline flip:
marker stripping.

## Phase 4 — `4fcb5e4`
Read-path ergonomics. `prefix` (literal, escaped) + cursor pagination on
memory_list; tool returns {entries, truncated, next_cursor}; `invalid_cursor`
standardized error; envelope documentation on every read tool; README
reframed as an injection-resistance layer with adversarial evaluation
pending. Baseline flip: bare-list/no-prefix.

## Phase 5 — `3857312`
Provenance tiers + lineage. `origin` enum (annotate-forward default
`unknown`), structured `origin_model_id`/`origin_model_family` (enums, not
prose), `origin_detail` suppressed in clinical namespaces, `derived_from`
lineage refs, `coord_health.tainted_lineage` (report only, no cascade),
curator stamps `curator_model_id`/`curator_family`,
`CURATOR_FAMILY_MUST_DIFFER_FROM` same-family refusal.

## Phase 6 — `498ed6e`
Trust decay + symmetric skepticism. Per-namespace `claim_staleness_hours`
(default 72; profile override): verdicts older than the window demote claims
to `needs_reverification` even when `current`; never-reconciled claims
flagged. Informational `skepticism` block (all-current >20 claims; ≥5
identical content hashes from different actors).

## Phase 7 — `420af23`
Workstream E mechanism. `storage/profiles.py` (typo-tolerant resolution over
control defaults), profile echo on every dict response + per-event snapshot,
R5 advisory arms (full/minimal/off; 2s budget; `advisory_status` on miss),
R6 arg-strictness middleware (hint/plain/control; every unknown-arg call
telemetered), R9 remedy toggle. All namespaces on control until the Phase 10
protocol flips them.

## Phase 8 — `ea707f8`
`observation_log` (23rd tool): small schema, server-attached friction
context, append-only under `_meta/observations` (excluded from normal lists
and coord scans), clinical hard-off. Feedback nudge on every error payload,
advisory, and quarantine verdict; one proactive line in server instructions.

## Phases 9 + 10 — (this commit)
`docs/namespace-isolation.md` design + minimal `TOKEN_NAMESPACE_ACL`
middleware (fail-closed `acl_denied`; unset ⇒ inert). `DECISION-PROTOCOL.md`
committed before any variant flip. Acceptance grep-test for convention
sentences. Docs/env updated.

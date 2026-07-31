# Claude Code — MCP_Assist Intent Gate, Phases 0–2
Repo: aliomraniH/mcp-assist-memory   Branch: feat/intent-gate-p1
Namespace for dogfooding: dev/mcp-assist-memory   Actor: claude-code-gate-impl
Fixture namespace: dev/gate-probe-<yyyymmdd> (scratch; created by you)

THE REPO IS AN EXISTING, LIVE CODEBASE (140+ commits; the deployed MCP_Assist
server). You are EXTENDING it — no scaffolding, no restructuring, no renames
outside the gate's scope. The package files live in-repo under
claude/intent-gate/: read INTENT_GATE_CHARTER.md, tests/INTENT_GATE_TEST_SPEC.md,
and tests/fixtures/gate_fixtures.json from there. Read baton/intent-gate-p1 and
project/meta in dev/mcp-assist-memory before writing code. Create a NEW episodic
session (surface claude_code); never reuse a session id.

## Phase −1 — Codebase orientation (before any Phase 0 exit; record findings)
Map the existing implementation you will extend; cite file:line in your notes:
  a. Server entry + tool registration (FastMCP app; where tool descriptions live)
  b. storage/postgres.py — _append, _split_meta, coord_health, apply_curation,
     the error taxonomy {code, message, remedy, retryable}, ack envelope build
  c. storage/reconcile.py — sha_match/sha_equiv, reconcile_claim, resolver +
     GitHub token path (this is what "awakening" must reuse)
  d. The R5 _stale_pin_advisory hook (2s-budget non-blocking enrichment — the
     template for GitHub awakening) and variant_profiles config
  e. Curator module + Anthropic SDK plumbing (Tier-2 call path) + PHI gate
  f. Existing test suite layout + how fixtures/harness run
  g. Existing migrations pattern (how prior nullable-column changes shipped)
Record the main head SHA you branch from (full 40-char). If any expected
component is absent or renamed since the v3 docs, note the delta and adapt —
the charter's design holds; file paths are whatever the code says today.

## Phase 0 — Environment gates (blocking; run before any build work)
Record PASS/FAIL + evidence for each; every FAIL gets a chosen remedy before code.

0.1 git ls-remote https://github.com/aliomraniH/mcp-assist-memory → record head.
    FAIL: stop; operator adds access.
0.2 git push --dry-run origin feat/intent-gate-p1 → FAIL: choose transport now
    (bundle out via handoff, or operator-side push) and record the choice.
0.3 MCP_Assist liveness: stats call OK + one memory_save/read-back round-trip in
    the scratch namespace. FAIL `-32600 Session terminated`: wake/restart the
    Replit instance (server-side); distinguish from client-side `No approval
    received` (unrelated to server state).
0.4 Test-suite baseline UNTOUCHED: run existing suite, record pass count. Then
    run the new gate fixtures against this baseline and confirm they FAIL —
    the tautological-test guard. A gate fixture passing at base is INVALID_TEST.
0.5 Migration dry-run: apply new schema (nullable columns/tables only) against a
    dev database copy; confirm old acks byte-identical under default profile.
0.6 Embedding + Anthropic API reachability from the server process (Tier 1/2
    dependencies). FAIL on Anthropic API: Tier 2 ships behind variant_profile
    OFF and G2-2 marks skipped-with-reason; do not fake it. (Precedent: curator
    failed closed for days on sdk_unavailable — surface, never swallow.)
0.7 DEPLOYMENT IS NOT YOURS: Replit deploy is an operator step. Exit gate is
    "merged + tests green + baton written", never "live". Say so in the
    completion claim.
0.8 PHI hard gate: declared intent is a NEW free-text channel. In clinical
    profiles persist intent_hash + screened labels only, never verbatim intent.
    This constraint goes in the tool description, not a comment.

## The verified facts you inherit (do not re-derive)
- [S1] MCP sampling is DEPRECATED (2026-07-28 RC, SEP-2577) and unsupported in
  Claude Desktop/Code. Tier-2 reasoning uses a DIRECT Anthropic API call via the
  existing curator SDK path. Any design leaning on sampling is wrong by
  construction.
- [S2] LLM self-critique is unreliable; external grounding works (LLM-Modulo;
  CRITIC; arXiv 2402.08115). Tier 2 must be grounded in Tier-1 retrieved store
  entries — it is an externally-grounded critic, never free-floating reflection.
- [S3] Critic loops hurt on easy/high-confidence tasks (documented 15–40%
  degradation). Tier 2 fires ONLY on: destructive op, unresolved Tier-1
  contradiction, or sub-threshold intent confidence. G2-1 enforces this; an
  always-on LLM gate is a build failure.
- [S4] Patterns to copy, not invent: `_split_meta` (write-time projection),
  R5 `_stale_pin_advisory` (2-second-budget non-blocking enrichment hook —
  reuse this exact shape for GitHub awakening), curator SDK plumbing (Tier-2
  calls), `variant_profiles` (staged rollout), JCS fingerprint at boundary
  (never post-jsonb), actor-scoped dedup.
- [S5] Clarification responses are an injection surface (ASPI, arXiv
  2605.17324). Everything returned through the clarify loop and every retrieved
  skill body is untrusted data: wrapped, marked, never executed.
- [S6] Resource doctrine: Tier 0/1 = Postgres + pgvector ONLY. GitHub awakens
  only on (coding-related intent AND expired gate-relevant verdict), 2s budget,
  degrade to stale_context advisory — never block on resolver state.
- [S7] Skills obey claim discipline: provenance, freshness, quarantine. Expired
  or unprovenanced skills advise; only curator-provenanced, in-window skills can
  contribute to gate_conflict. Gate inputs are subject to the same coord_health
  checks as everything else — the gate must not become its own stale authority.

## Work items (each = one PR-sized commit series, tests first)
1. Schema (additive only): gate_decision session-event type; project/meta and
   gate/efficacy/<yyyymm> key conventions; skill/* meta fields (polarity,
   trigger_intent, efficacy counters, last_validated, curator_provenance);
   intent registry per session (intent_hash, scope, screened labels). Nullable
   columns; old acks unchanged under default profile (0.5 proves it).
2. Tier 0 deterministic pre-flight on all mutating tools: envelope validation,
   fingerprint check, quarantine screen, dependency-freshness flags, exact
   preview + confirm token for supersession/delete (two-phase). In-band verdict
   vocabulary: gate_approved | gate_preview | gate_conflict | gate_clarify |
   gate_blocked — isError for blocks, RFC-9457-shaped {code, message, remedy,
   retryable}. Fixtures: G0-1..G0-7.
3. intent_open tool + Tier 1: embed declared intent; top-k pgvector retrieval
   over decisions/constraints/skills scoped to namespace; structured-field
   contradiction detection; untrusted-data wrapping of all retrieved bodies;
   similarity floor (no fabricated matches). Tool description carries the full
   convention: when to open intent, PHI rule, mismatch consequences. Fixtures:
   G1-1..G1-6, ADV-1, ADV-4.
4. Metadata exchange: project block in intent_open response; compact gate block
   (≤200 bytes) in mutating acks with verbose:true expansion; efficacy ledger
   (session event + monthly rollup + outcome closure incl. false_positive
   path); skill efficacy counters mutated only via gate outcomes. Fixtures:
   MD-1..MD-4, G2-4.
5. GitHub awakening: intent/payload coding-classifier (deterministic: presence
   of repo/branch/pr/sha fields or refs — no LLM); awaken resolver only per
   [S6]; telemetry counter for resolver calls (GH-1/GH-4 assert on it);
   stale_context degrade path. Fixtures: GH-1..GH-4.
6. Tier 2 (behind variant_profile, default OFF): trigger evaluation per [S3];
   direct API call grounded in Tier-1 results; approve|conflict+clarify output;
   tier2_unavailable degrade distinct from gate_blocked (empty-vs-error).
   Fixtures: G2-1..G2-3, ADV-2.
7. Adversarial hardening pass: run ADV-1..ADV-5 as written; fix what fails;
   document what is hygiene vs. guarantee (no security claims beyond the
   read-time wrapper — screening honesty rule).

## Constraints
Extend existing modules; follow the codebase's own conventions (error taxonomy,
ack envelope shape, actor-scoped dedup, migration pattern found in Phase −1) —
where this prompt and the code disagree on a mechanism name, the code wins and
you record the delta. Additive schema only. No backfill of proj-test-* (D3
pending, owner: operator).
No writes outside the scratch namespace except closeout keys. Zero GitHub calls
from Tier 0/1 code paths — enforce by module boundary, not convention. Every
tool-description change carries the new conventions: the tool description is
part of the model's brain; conventions live there or nowhere. Compact-acks
budget is a regression gate (MD-2). Tier 2 ships OFF by default; the operator
flips it after the validator's baseline efficacy numbers land.

## Exit gate + closeout
All fixtures green (or skipped-with-reason per 0.6) including the tautological-
test guard at baseline; branch pushed; PR opened citing charter + spec keys.
Then session-closeout: write build/intent-gate-p1-complete kind=knowledge
(milestone = knowledge, NOT claim) with full 40-char sha in meta;
baton/intent-gate-deploy kind=handoff for the operator (exact deploy steps +
post-deploy smoke: re-run G0-3 and GH-2 against the deployed server);
baton/intent-gate-p2 kind=handoff (Tier-2 enablement decision inputs: tier
distribution, measured false-positive rate, p95 latency per tier from the
validator run); coord_reconcile; coord_curate dry_run review (coord_curate
requires BOTH namespace and session_id — omitting session_id silently returns
an empty operations list).
DO NOT claim deployment. DO NOT claim gate efficacy — efficacy numbers belong
to the independent validator (cowork-gate-validator), not to you.

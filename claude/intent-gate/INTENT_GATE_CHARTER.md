# CHARTER: MCP_Assist Intent Gate v1
Namespace: `dev/mcp-assist-memory` · Store: MCP_Assist (`mcp-assist-memory.replit.app/mcp`)
Repo: `aliomraniH/mcp-assist-memory` · Branch: `feat/intent-gate-p1` · Deploy target: Replit (operator-only)
Thesis: **The memory server stops being a passive store and becomes an active critic — every mutating request is contextualized against the store's own memory, project metadata, and efficacy history BEFORE execution, using a deterministic-first tiered gate.**

Date authored: 2026-07-30. Sources of record: `MCP_ASSIST_CHALLENGES_AND_IMPROVEMENTS.md` (challenge ledger), `research/RESEARCH_1_solutions-survey.md`, `research/RESEARCH_2_intent-gate-prior-art.md` (design basis for §2–§4), `VALIDATION_RESULTS.md` v3.

Package contents: this charter · `SEED_WRITES_intent-gate.md` (run first) · `tests/INTENT_GATE_TEST_SPEC.md` + `tests/fixtures/gate_fixtures.json` · `CLAUDE_CODE_PROMPT_intent-gate-p0-2.md` · `research/` (the two prior-art reports). In-repo location: `claude/intent-gate/` inside the existing `aliomraniH/mcp-assist-memory` codebase (140+ commits — the gate EXTENDS the live server; it is not a greenfield build).

---

## 1. Problem statement (one paragraph)

The A/B benchmark's central finding: durable memory is an amplifier, not a corrective. The trust spine verifies claims against GitHub, but at retrieval time and at new-entry acceptance time the server does not use its own memory to contextualize the caller's request — a wrong founding decision propagates 6/6 faithfully, and near-miss reasoning is served with the same authority as correct reasoning. The Intent Gate closes this by inserting an early step in the call chain that digests the caller's declared intention, checks it against stored decisions, constraints, and known failures, and returns either an exact preview, a conflict explanation with a clarification request, or approval — before the rest of the calls happen.

## 2. Architecture: the three-tier gate (settled by research; do not re-derive)

```
caller LLM
   │  intent_open(goal, scope)            ← once per session/sequence; tool description carries the convention
   ▼
┌─ TIER 0 — deterministic pre-flight ──────────────────────────────┐
│ Postgres only. Runs on EVERY mutating call, no opt-out.          │
│ schema/envelope validation · idempotency fingerprint · quarantine │
│ screen · freshness of depended-on verdicts · EXACT preview        │
│ (diff, superseded revisions, lineage/coord_health impact)         │
└──────────────── pass+low-risk → execute ─────────────────────────┘
   │ ambiguous / mutating-with-intent
   ▼
┌─ TIER 1 — memory-similarity critic ──────────────────────────────┐
│ pgvector. Embed declared intent → top-k decisions, constraints,   │
│ and NEGATIVE knowledge (anti-patterns, known failures) scoped to  │
│ namespace. Contradiction detection via structured fields only.    │
└──────────── no conflict → execute with context block ────────────┘
   │ unresolved contradiction / destructive / low confidence
   ▼
┌─ TIER 2 — LLM reasoning (rare, budgeted) ────────────────────────┐
│ Direct Anthropic API via the existing curator SDK path. NEVER MCP │
│ sampling (deprecated 2026-07-28 RC; unsupported in Claude clients)│
│ Grounded in Tier-1 retrieval. Output: approve | conflict+clarify. │
└──────────────────────────────────────────────────────────────────┘
```

Verdicts the gate can return, mirrored in tool-error vocabulary: `gate_approved`, `gate_preview` (effect summary, awaiting confirm), `gate_conflict` (names the contradicting key + revision), `gate_clarify` (question for the caller), `gate_blocked` (deterministic rule; always names the rule). Every verdict is in-band (`isError` for blocks), never a protocol error.

## 3. Resource-access doctrine (binding constraint from the operator)

**The main database plus third-party APIs (embedding provider, Anthropic API) are sufficient for all main gate functionality.** GitHub is a *lazily awakened extended-context provider*, not a dependency:

- **Tier 0 and Tier 1 make zero GitHub calls.** Ever. They run on Postgres + pgvector alone.
- **GitHub awakens only when BOTH hold:** (a) the intent is classified coding-related (repo/branch/pr/sha present in the declared intent or the payload meta), AND (b) a gate-relevant claim's verdict is expired past `claim_staleness_hours`. Then the resolver runs under the existing R5 pattern: hard 2-second budget, never blocking the write, result stamped into the response.
- **Degrade to advisory, never to blocking:** if GitHub is unreachable or the budget expires, the gate proceeds using stored verdicts explicitly flagged `stale_context: true` with `age_hours`. A gate that requires GitHub to answer is a gate that fails closed on every resolver outage — the 2026-07-16 incident class. Extended context is a bonus, not a gate input the caller waits on.

## 4. Metadata exchange contract (binding constraint from the operator)

Two metadata families are stored in the store and exchanged on every gated interaction:

**4a. Project metadata — `project/meta` (kind=knowledge, one per namespace).**
Fields: `stack`, `repo`, `conventions_version`, `active_phase`, `key_schema_ref`, `profile` (e.g. `clinical` vs `dev` — drives PHI handling). Served as a compact `project` block inside `intent_open`'s response so the caller LLM starts every gated sequence holding the project's ground truth instead of guessing it. Updated only via normal supersession; the gate reads, never writes, this key.

**4b. Efficacy metadata — what worked and what didn't.**
- Every gate decision appends a session event AND increments a rollup key `gate/efficacy/<yyyymm>` (kind=knowledge): per-rule and per-skill counters `{fired, approved, previewed, clarified, blocked}`.
- **Outcome closure:** when a later call in the same session succeeds/fails against a prior gate decision, the gate writes the closure: `confirmed_correct` (block prevented a real error / approval succeeded), `false_positive` (blocked call was retried unchanged and succeeded after operator override), `unknown`. This is the measured false-positive tax — the number that governs Tier-2 trigger thresholds.
- **Ack exchange:** every mutating ack carries a compact `gate` block — `{tier, decision, matched:[keys], flags:[stale_context,...]}` — target ≤ 200 bytes compact JSON, full detail behind `verbose:true`. The gate must not resurrect the 1.19 KB envelope problem it coexists with.

**4c. Skill dictionary — `skill/<slug>` (kind=knowledge).**
Procedural entries grown over time and retrieved by intent embedding at Tier 1. Schema: `{trigger_intent, guidance, polarity: positive|anti-pattern, meta: {efficacy: {applied, prevented_error, false_positive}, last_validated, derived_from, origin_model_family}}`. **Skills obey the same provenance/freshness/quarantine discipline as claims** (the novel contribution per the research): a skill with expired `last_validated`, tainted lineage, or quarantine flag may advise but can never veto. Skills are written by the curator path (PHI-gated), never raw.

## 5. Key schema (additions; existing schema unchanged)

- knowledge: `project/meta`, `gate/efficacy/<yyyymm>`, `skill/<slug>`, `build/intent-gate-p1-complete` (milestone = knowledge, NOT claim)
- claims: none new (the gate consumes claims; it does not create them)
- handoffs: `baton/intent-gate-deploy` (operator deploy steps), `baton/intent-gate-p2` (Tier-2 follow-on)
- session events: `gate_decision` events carrying `{intent_hash, tier, decision, matched, latency_ms}`

## 6. Actors and sessions

Implementation: `claude-code-gate-impl` (new episodic session, surface `claude_code`). Validation: `cowork-gate-validator` (distinct actor — instrument and subject never share an actor or idempotency scope). Orchestration/closeout: `web-orchestrator`. One new session per phase; never reuse a session id.

## 7. PHI rule for the gate (clinical profiles)

Declared intent is a **new free-text channel** and the server-side raw-field PHI gate is still an open blocker on the direct write path. Therefore: in `clinical` profile namespaces the gate stores only `intent_hash` + screened category labels, never verbatim intent text; verbatim intent storage is permitted only in `dev` profiles. Tier-2 prompts in clinical profiles carry hashes and structured fields, never raw payload content. This must be encoded in the tool description (the tool description is part of the model's brain).

## 8. Verification contract

Done means: all fixtures in `tests/INTENT_GATE_TEST_SPEC.md` green under the existing test harness; the GH-series tests prove zero GitHub calls on non-coding intents via telemetry assertion; the MD-series proves metadata round-trip and ack-size budget; branch pushed; PR opened citing this charter's key. Deployment is NOT the session's exit gate — merged + tests green + baton written, never "live". Post-deploy smoke (operator): re-run fixture G0-3 and GH-2 against the deployed server.

## 9. Honesty rules

No claim without the event. A gate decision that was never exercised has efficacy `unknown`, not `confirmed`. Blockers become batons with mechanical causes. Full 40-char SHAs in meta everywhere. When the gate blocks valid work, that is a `false_positive` counter increment and a threshold review — not a silent override.

# Coordination spine — keeping memory in sync with the code it describes

**Status:** Phases 1, 2 & 3 implemented.

## The problem (observed, not hypothetical)

Three drift instances are sitting in the live store right now:

1. **Status contradiction.** `cardiometabolic-v060-phase1` rev2 said "NOT yet merged";
   rev3's `status` literally reads *"supersedes revision 2's 'NOT yet merged'"* — a human
   hand-corrected a stale merge flag.
2. **Namespace drift.** CLI/Desktop wrote `canvas-case`, Web wrote `canvas-glp1`; the two
   were reconciled by a manual consolidation (every migrated entry now carries a
   `migrated_from: canvas-glp1` tag).
3. **Silently-stale verification.** `coord/verify-status` is pinned at `head: e87f91c9`
   while the repo moved 8+ commits ahead. Nothing flags it — a reader must eyeball SHAs.

Root cause: SHAs and session ids live as **free text inside the JSON value**. No entry has a
structured "this was true at SHA X, in session Y" field, so no reader can mechanically ask
"is this still current?" The result is a manual sync tax paid by hand on every drift.

## Principle: Git/GitHub is *a* clock, the server is the spine

Memory should not be the source of truth for mutable external status (`merged?`, `head=X`).
That status changes on GitHub, outside memory, and someone has to hand-sync it back — which is
exactly what drifts. Instead:

- **Durable facts** (merged? on main? which SHA?) live in Git/GitHub and are *derived*, never narrated.
- **Memory** stores a pointer to them plus the knowledge Git can't express (SDK facts, fixture
  maps, gaps), and **stamps every entry with the version vector it was true at**.

But the spine cannot *depend* on Git, because memory is used from surfaces that have no git
access (claude.ai web, Cursor, other IDEs, headless/cron). So the always-present clock is the
server's own, and Git/session are optional overlays:

| Dimension          | Always present? | Source                        | Works with no git? |
|--------------------|-----------------|-------------------------------|--------------------|
| `revision`         | yes             | server (max+1 in-txn)         | yes                |
| `created_at`       | yes             | server                        | yes                |
| `namespace_epoch`* | yes             | server counter                | yes                |
| `content_hash`*    | yes             | sha256 of value               | yes                |
| `repo_sha`/`base_sha`/`branch`/`dirty` | best-effort | client hook **or** server reconciler | optional |
| `session_id`       | best-effort     | the surface                   | optional           |
| `pr`/`merge_sha`*  | best-effort     | GitHub, resolved server-side  | optional           |

\* introduced in Phase 2/3. Everything degrades cleanly: with git you get sharp "is this
current?" answers; without it you still get "this fact changed / is N writes old / contradicts
another." This mirrors the embedding column's existing best-effort, disabled-by-default contract.

## Claim vs Knowledge — only one kind drifts

- **Claim** — an assertion about external mutable state ("PR #11 merged", "HEAD is e87f91c9").
  Verifiable, *expires*, carries a verify recipe with a fallback ladder:
  1. `git`/`github` (ancestor check, PR lookup) — when the reconciler has access
  2. `version` (manifest 0.6.0 vs current) — needs no git
  3. `ttl` (unverified for N days / M epochs → stale)
  A claim's derived freshness is `current | stale | unverifiable`; `unverifiable` is loud, never
  silently treated as fresh.
- **Knowledge** — a durable fact Git can't express (SDK field names, fixture maps). Doesn't
  expire on a SHA change (maybe on a manifest-version change).

Stored as new `kind` enum values (`claim`, `knowledge`) — see Phase 1.

## Phases

### Phase 1 — provenance as a first-class column (implemented)

- `migrations/0003_provenance.sql`: nullable, indexed columns on `memory_entry` —
  `repo_sha`, `base_sha`, `branch`, `dirty`, `session_id`, plus a `meta jsonb` catch-all; widen
  the `kind` CHECK to add `claim`/`knowledge` (add-only, so existing rows stay valid). Indexes:
  `(namespace, repo_sha)`, `(session_id)`, GIN on `meta`.
- `memory_save` / `handoff_save` / `memory_delete` gain an optional `meta` envelope. The backend
  projects the five well-known keys into columns and keeps the whole envelope in `meta`
  losslessly. `_row_to_entry` surfaces all of it (so it flows through hybrid-search fusion too).
- `session_id` is `text`, not `uuid`: non-Claude surfaces may use other id formats and a bad cast
  must never fail a write.
- **Backward compatible:** omit `meta` and the entry stores exactly as before.
- `scripts/backfill_provenance.py`: conservatively lifts an envelope already embedded in a value
  (`meta`/`_meta`) into the columns. It deliberately does **not** scrape SHAs from prose — a wrong
  provenance stamp is worse than none.

### Phase 2 — the universal, git-free spine (implemented)

- `migrations/0004_content_hash.sql`: a `content_hash` column (sha256 of the canonical value),
  computed on write, surfaced on reads — the always-present, git-free dimension of the version
  vector. Identical facts hash identically regardless of surface or namespace. Legacy/tombstoned
  rows are null and the detectors hash on the fly, so no backfill is required for them to work
  (`scripts/backfill_content_hash.py` only populates the column for the indexed scan path).
- `coord_health(namespace)` — a per-namespace drift report, no git needed:
  - *stale* — live entries whose `repo_sha` is behind the namespace's most-recently-observed
    `repo_sha` (a git-free proxy for "predates current code → re-verify"). Catches the
    silently-stale `coord/verify-status` class.
  - *duplicate_content* — distinct keys holding an identical fact (same `content_hash`).
  - *claim_collisions* — multiple live `claim`s about the same subject (`meta.subject`, or
    `meta.pr`). Catches the rev2-vs-rev3 contradiction class before a human has to.
- `coord_drift_scan()` — a deliberately store-wide (admin, like `stats`) scan for the same fact
  living under more than one namespace — the `canvas-case`↔`canvas-glp1` class. Respects tenancy
  by being explicitly cross-tenant rather than leaking through a per-project read.

Deferred to a follow-up: `namespace_epoch` (a logical-clock refinement for entries that carry no
`repo_sha`), semantic (cosine) near-duplicate detection layered on top of the exact-hash grouping,
and a written `coord/_nudges` key (today the detectors are pull, via the two tools).

### Phase 3 — backend git/GitHub access + reconciler (implemented)

`storage/reconcile.py` adds a `Resolver` dependency that is to provenance what `Embedder` is to
search: `build_resolver(settings)` returns a `GitHubResolver` (read-only REST) when `GITHUB_TOKEN`
is set, else a `DisabledResolver`. It's injected in `app.py`'s lifespan
(`PostgresBackend(pool, embedder=…, resolver=…)`); with no token the server runs identically and
every claim reconciles to `unverifiable`.

- `reconcile_claim(entry, resolver)` derives a verdict **from provenance, not prose**: `meta.repo`
  + `meta.pr` → `merged_state`; `meta.repo` + `meta.branch` → `branch_head` vs `repo_sha`. States:
  `current | stale | unverifiable`. A blind resolver (disabled, or a failed call) yields
  `unverifiable` — **never** a silent `current`.
- `coord_reconcile(namespace)` (MCP tool) reconciles every live claim and writes an **append-only**
  `coord/_reconcile/<key>` record (tagged with the state) — the user's claim is never rewritten.
- `coord_reconcile_repo(repo, pr=…, branch=…)` is the store-wide entry point for the webhook.
- **Push trigger** — `POST /webhook/github`, HMAC-verified via `verify_signature` over the raw body
  (`X-Hub-Signature-256`); `pull_request`/`push` events reconcile affected claims across namespaces.
  Returns 503 until `GITHUB_WEBHOOK_SECRET` is set, so it's inert where unused.

Guardrails honored: append-only to reserved `coord/_reconcile/*`; `unverifiable` when blind, never
false-fresh; the GitHub token is read-only and claims self-describe their repo via `meta.repo`
(no global namespace→repo map needed). **Deferred:** the scheduled-poll and lazy-on-read triggers
(today reconcile is pull, via the tool, plus push, via the webhook), and the `coord_seal`
cycle-end enrichment with diff summary + `git log --grep=<session_id>` back-links.

### Client-side discipline (consuming repos)

- SessionStart/PostToolUse hook injects the version vector (namespace from `agent.config.json`,
  never typed) — kills the namespace-drift and SHA-typo classes at the source. CLI is the
  hook-enforced writer; web/desktop include the stamp in the value they write (low volume).
- `prepare-commit-msg` hook appends `Claude-Session` / `Memory-Ref` trailers, making entry⇄commit
  links walkable from either side.

## Symptom → fix

| Manual-sync pain (in store now)        | Eliminated by                                              |
|----------------------------------------|------------------------------------------------------------|
| rev2 "not merged" → rev3 "supersedes"  | live GitHub merge-state (P3) + SHA stamp showing rev2 pre-merge (P1) |
| `canvas-glp1` vs `canvas-case` drift   | namespace injected from config by hook; cross-ns similarity detector (P2) |
| verify-status silently 8 commits stale | ancestor check vs HEAD; scheduled reconcile (P2/P3)        |
| "what changed since my last session?"  | `coord/HEAD` pointer diffed at SessionStart                |
| "who produced this commit / run?"      | commit trailers (P3)                                       |

## Honest limits

- The server can't run git/GitHub itself in Phase 1–2; it stores, indexes, and returns
  provenance. All clock logic is client-side until the Phase 3 reconciler (which is the part that
  gives the *backend* its own access).
- Hook auto-injection is a CLI/local capability; web/Cursor include the stamp in the written value.
- The GitHub MCP/token may be absent in headless/cron; the reconciler must degrade to
  `git ls-remote` and mark claims `unverifiable` rather than guess.
- Backfill cannot reliably recover provenance from legacy prose entries; those stay NULL until a
  re-verify or reconcile pass.

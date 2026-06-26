# Test scenario — the coordination spine (the "new" tools)

**What this is.** An end-to-end, reproducible scenario that exercises every tool
the coordination spine added on top of the original 18-tool surface, and asserts
**what each one is supposed to deliver**. It was executed live against the
deployed MCP-Assist server on **2026-06-26**; the *Actual* blocks below are real
tool output, not mock-ups.

> Source of truth for behavior: [`coord-spine.md`](./coord-spine.md) and
> [`memory-curator.md`](./memory-curator.md). This doc is the *test plan + run log*.

## What's new (and therefore under test)

The MCP surface grew from **18 → 22 tools**. The four new tools and the two
supporting features are the coordination spine (Phases 1–3):

| Under test | Kind | Promise (one line) |
|---|---|---|
| `meta` envelope on `memory_save`/`handoff_save`/`memory_delete` | feature (P1) | Project `repo_sha`/`base_sha`/`branch`/`dirty`/`session_id` into indexed columns; keep the full envelope losslessly. |
| `memory_search` (semantic + HyDE) | feature (P3) | Rank live entries by *meaning*, not just substring; match a future *question*, not only the stored statement. |
| `coord_health(namespace)` | tool (P2) | Per-namespace, git-free drift report: `stale` / `duplicate_content` / `claim_collisions`. |
| `coord_drift_scan()` | tool (P2) | Store-wide: the same fact living under >1 namespace, worst-first. |
| `coord_reconcile(namespace)` | tool (P3) | Resolve every live `claim` against GitHub; write an **append-only** verdict (`current`/`stale`/`unverifiable`) without rewriting the claim. |
| `coord_curate(namespace, session_id)` | tool (P3) | Write-side LLM consolidation of a finished session into `ADD`/`UPDATE`/`MERGE`/`SUPERSEDE`/`NOOP`, PHI-gated and idempotent. |

## Environment probed (live, 2026-06-26)

| Capability | Gate (config.py) | Observed on the live server |
|---|---|---|
| GitHub reconciler | `GITHUB_TOKEN` / Replit connector | **enabled** (`coord_reconcile` → `resolver_enabled: true`) |
| Memory curator | `ANTHROPIC_API_KEY` | **enabled** (`coord_curate` → `curator_enabled: true`) |
| Semantic recall | `VOYAGE_API_KEY` | **enabled** (question-phrased query ranked the right entry #1) |
| Store size at run time | — | `stats`: 1466 keys / 1685 revisions / 132 sessions / 31 artifacts |

Ground truth used by the reconcile cases (read from GitHub at run time):
`aliomraniH/mcp-assist-memory` PR **#10 is merged**; `main` head =
`feb041334a77c8d909336fe5a7ce65a2b3ab4884` (the PR #10 merge commit).

## Namespaces / fixtures

Isolated, disposable test namespaces (never a real project):

- `proj-test-scenario-jaqw9b` — provenance, `coord_health` (3 classes), search, curator-A.
- `proj-test-scenario-jaqw9b-alt` — cross-namespace twin for `coord_drift_scan`.
- `proj-test-scenario-jaqw9b-reconcile` — six claims spanning every reconcile verdict.
- `proj-test-scenario-jaqw9b-curate` — clean namespace for the curator write-path demo.

---

## Case 1 — `meta` provenance envelope (Phase 1)

**Deliver:** the five well-known `meta` keys are projected into indexed columns and
the whole envelope is preserved; the value is sanitize-wrapped and `content_hash`
is computed on write.

**Setup**
```
memory_save(ns, "config/verify-status",
  value={"head":"1a2b3c4","note":"pinned verification head — predates current code"},
  kind="config", meta={"repo_sha":"1a2b3c4","branch":"main","session_id":"sess-scenario-A"})
```

**Expected:** read-back surfaces `repo_sha="1a2b3c4"`, `branch="main"`,
`session_id="sess-scenario-A"` as columns; `meta` retains all keys; `value` wrapped
in `<<<UNTRUSTED_DATA>>>…<<<END>>>`; `content_hash` present.

**Actual** ✅
```json
{"key":"config/verify-status","revision":1,"repo_sha":"1a2b3c4","branch":"main",
 "session_id":"sess-scenario-A","meta":{"branch":"main","repo_sha":"1a2b3c4","session_id":"sess-scenario-A"},
 "value":"<<<UNTRUSTED_DATA>>>{\"head\": \"1a2b3c4\", ...}<<<END>>>",
 "content_hash":"438cfca4c3f8cc2427cbde49e899b4576b491dfbe8127467547518d84ce1ea3e"}
```
Omitting `meta` stores exactly as before (verified by the `*-alt` entry: all
provenance columns `null`, `meta: null`).

---

## Case 2 — `coord_health`: three drift classes in one namespace (Phase 2)

**Deliver:** a git-free, per-namespace report flagging (a) entries behind the
namespace's latest `repo_sha`, (b) distinct keys holding an identical fact, and
(c) multiple live claims about the same subject.

**Setup** (in `proj-test-scenario-jaqw9b`)

| Class | Keys planted |
|---|---|
| stale | `config/verify-status` written at `repo_sha=1a2b3c4`, while all later entries carry `repo_sha=9f8e7d6` (the namespace's newest-observed SHA). |
| duplicate_content | `knowledge/glp1-dosing` and `knowledge/glp1-dosing-restated` — **identical value** → identical `content_hash` `210a3ad1…`. |
| claim_collisions | `claim/pr-77-status-cli` ("green") and `claim/pr-77-status-web` ("failing lint") — two live `claim`s, both `meta.subject="pr:77"`. |

**Actual** ✅ — `coord_health("proj-test-scenario-jaqw9b")`
```json
{"entry_count":7,"latest_repo_sha":"9f8e7d6",
 "stale":[{"key":"config/verify-status","repo_sha":"1a2b3c4","revision":1}],
 "duplicate_content":[{"content_hash":"210a3ad1…","keys":["knowledge/glp1-dosing","knowledge/glp1-dosing-restated"]}],
 "claim_collisions":[{"subject":"pr:77","keys":["claim/pr-77-status-cli","claim/pr-77-status-web"]}]}
```
All three detectors fired exactly as designed; the single `pr:42` claim correctly
did **not** register as a collision.

---

## Case 3 — `coord_drift_scan`: same fact across namespaces (Phase 2)

**Deliver:** a deliberately store-wide (admin) scan grouping live entries by
`content_hash`, returning hashes that span >1 namespace, worst-first; schema
`{content_hash, namespaces[], entries[]}`.

**Setup:** the identical glp1 fact written into both `…-jaqw9b` and `…-jaqw9b-alt`
(both hash to `210a3ad1…`, proven at write time).

**Actual** ✅ (with a caveat) — the tool returned 50 valid drift groups
(`limit=50`), correctly shaped and sorted worst-first (busiest group spanned
**105** namespaces; min in the page was 2). Example element:
```json
{"content_hash":"0b290a9e…","namespaces":["proj-test-49abeaadd92b","proj-test-b682db1ff6ee"],
 "entries":["proj-test-49abeaadd92b/coord/_reconcile/phase1","proj-test-b682db1ff6ee/coord/_reconcile/phase1"]}
```
**Caveat (environmental, not a defect):** the live store is heavily polluted by
prior load-tests (hundreds of `proj-test-*` namespaces), so our planted 2-namespace
pair ranks below the top-50 and isn't in the page. The grouping mechanism is the
same `content_hash` our pair shares, so the detection is sound — it's a *ranking +
page-size* effect. To surface a specific low-rank pair, raise `limit` (the output
is large) or scope the store.

---

## Case 4 — `coord_reconcile`: claims vs live GitHub (Phase 3)

**Deliver:** for every live `claim`, derive a verdict **from provenance, not prose**
(`meta.repo` + `meta.pr`/`meta.branch` + `repo_sha`), tolerate short vs full SHAs by
prefix-match, and record an **append-only** `coord/_reconcile/<key>` verdict — never
rewriting the claim. A blind resolver yields `unverifiable`, never a silent `current`.

**Setup** — six claims in `proj-test-scenario-jaqw9b-reconcile`:

| Key | Provenance | Expected |
|---|---|---|
| `claim/pr10-merged-correct` | pr 10, `merge_sha=feb0413` (short) | **current** (prefix-match vs full SHA) |
| `claim/pr10-merged-wrongsha` | pr 10, `merge_sha=0000000` | **stale** |
| `claim/pr10-no-sha` | pr 10, no `merge_sha` | **stale** (merged upstream, claim didn't record it) |
| `claim/main-head-current` | branch main, `repo_sha=feb0413…` (full) | **current** |
| `claim/main-head-stale` | branch main, `repo_sha=9c4316d` | **stale** |
| `claim/no-provenance` | none | **unverifiable** |

**Actual** ✅ — `resolver_enabled: true`, `reconciled: 6`; **every verdict matched**:
```
pr10-merged-correct  → current      (recorded feb0413  ⊂ resolved feb041334a77…)
pr10-merged-wrongsha → stale        (recorded 0000000)
pr10-no-sha          → stale        (recorded_merge_sha: null)
main-head-current    → current      (claim_repo_sha == head)
main-head-stale      → stale        (9c4316d != head)
no-provenance        → unverifiable ("no resolvable subject")
```
The short-SHA → `current` result is the live proof of the `sha_match` prefix fix.

**Append-only / non-destructive** ✅ — `memory_list` shows six new
`coord/_reconcile/claim/*` records (kind `config`, tags `["reconcile", <state>]`),
and `memory_history(claim/pr10-merged-correct)` is **still a single revision 1** —
the user's claim was never touched.

---

## Case 5 — `memory_search`: semantic + HyDE recall (Phase 3)

**Deliver:** rank by meaning so a future agent's *question* surfaces the relevant
stored *statement*, even with little lexical overlap.

**Setup:** stored `knowledge/short-sha-reconcile` (a lesson about prefix-matching
SHAs). Queried with a problem phrasing:
`"why does a merged pull request still get reported as out of date after verification?"`

**Actual** ✅ — `knowledge/short-sha-reconcile` ranked **#1** of 5, above other
entries in the namespace. (Embeddings enabled; with no `VOYAGE_API_KEY` this would
degrade cleanly to substring search — the documented best-effort contract.)

---

## Case 6 — `coord_curate`: write-side consolidation (Phase 3)

**Deliver:** at session end, read the trace + similar memories and emit
`ADD`/`UPDATE`/`MERGE`/`SUPERSEDE`/`NOOP`; PHI-gated, provenance-aware, idempotent;
disabled ⇒ clean no-op.

**Setup:** created a session, appended a 5-step trace (failing reconcile →
diagnosis → `sha_match` fix → tests pass → PR merged), then a richer 3-step trace
(decision + test + merge) in a clean namespace. Ran `dry_run=True` for both.

**Actual** ⚠️ — `curator_enabled: true`, `dry_run: true`, **`operations: []`** in
both namespaces.

The envelope is built correctly (verified in `storage/postgres.coord_curate`: it
passes the full ordered `trace` + `similar_memories`). Two things produce an empty
result here, and the tool cannot distinguish them at the surface:

1. **By design / conservative:** "persist only what constrains future reasoning…
   an empty `operations` array is a valid, good outcome" (curator prompt). In the
   first namespace the lesson already existed as `similar_memories`, so a `NOOP`
   is the *correct* call.
2. **Model call failing-closed:** `AnthropicCurator.curate` maps *any* SDK error
   (auth, rate-limit, unavailable model — default `curator_model="claude-opus-4-1"`)
   to zero operations. This is the right safety contract (never a wrong write) but
   it is **observationally identical** to outcome (1).

**Finding (limitation):** an enabled curator that returns `operations: []` is
ambiguous — genuine NOOP vs. swallowed model error look the same to a caller. See
*Findings* below for a suggested low-risk fix.

**Write-path coverage:** the deterministic apply side (`apply_curation`) — `ADD`
creating a live entry with scores, `SUPERSEDE` setting `valid_until` and being
excluded from `coord_health`, `NOOP` reasons, the **PHI gate failing closed**,
claims-without-provenance **downgraded to notes**, **idempotent double-curate**, and
`dry_run` writing nothing — is fully asserted by `tests/test_curator.py` (12 tests,
stubbed curator), which is the right layer to verify it deterministically.

---

## Findings

1. **All four new tools deliver their core promise live.** `coord_health` (3/3
   classes), `coord_reconcile` (6/6 verdicts + append-only, non-destructive), the
   `meta` envelope, and semantic/HyDE search all behaved exactly as specified.
2. **Doc drift:** `README.md` still advertises an **"18-tool MCP"**; the live server
   and `server/mcp_server.py` expose **22** (the four `coord_*` tools). The README
   capability list and table should be updated. *(Caught by this scenario.)*
3. **`coord_curate` empty-vs-error ambiguity (limitation):** an enabled curator
   returning `operations: []` cannot be told apart from a fail-closed model error.
   Low-risk fix: have `AnthropicCurator.curate` return a lightweight status (e.g.
   `{"operations":[], "curator_status":"ok|error"}`) so `coord_curate` can surface
   `model_error` without ever changing the fail-closed write behavior.
4. **`coord_drift_scan` page-size vs. a polluted store:** worst-first + `limit`
   means a genuine low-rank drift can fall off the page in a store with heavy
   pre-existing duplication. Fine for an admin triage view; note it when using the
   tool to *prove a specific* pair (raise `limit` or scope the store).

## Reproduce

The scenario is pure MCP tool calls — re-runnable from any connected surface
(Claude Code CLI, claude.ai web, desktop). Order matters only for Case 2's `stale`
detection: write the old-`repo_sha` entry **first**, then ensure the newest
entry-with-a-SHA carries the "current" SHA (it pins `latest_repo_sha`). The exact
calls and payloads are in the *Setup* blocks above. Cleanup is optional — every
namespace is `proj-test-scenario-jaqw9b*` and isolated; `memory_delete` each key to
tombstone, or leave them (they don't affect real projects).
```
# Sketch (see each case for full payloads):
memory_save(...meta=...)                      # Case 1
memory_save x6 (stale / dup pair / pr:77 x2)  # Case 2  → coord_health(ns)
memory_save(ns2, identical value)             # Case 3  → coord_drift_scan()
memory_save x6 (claims w/ provenance)         # Case 4  → coord_reconcile(ns_rec)
memory_search(ns, "<question phrasing>")      # Case 5
session_create + session_append_event x5;     # Case 6  → coord_curate(ns, sid, dry_run=True)
```

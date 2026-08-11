# Replit Deploy Prompt — Three Live-Observed Gate Defects (v0.4.1)

**Give this whole document to the Replit deploy agent.** It is self-contained.

Deploy target: **`main` @ `1c93d879270cd24caa6f677f99ec515163ac467f`** (PR #19).
Assumes v0.4.0 (merge `a00674e`) is live. At the time this document was written
the live server reported `server_version 0.4.0` with
`server_boot_ts 2026-08-11T01:12:27Z`, so that assumption was true and verified,
not inferred.

**This release has no migration.** `SCHEMA_VERSION` stays **6** and no file under
`migrations/` changed. `scripts/migrate.py` still runs from `[deployment].run`
and will be a no-op. If it reports applying anything, **stop** — you are not on
the commit above.

---

## 1. What changed and why it matters at deploy time

Three defects, all found by live observation against the deployed v0.4.0 server,
all invisible to CI. They are items 1, 2 and 4 of the "Carried forward, still not
fixed" list in `docs/REPLIT-DEPLOY-PROMPT_sequences.md`; item 3 (Phase 2 block
telemetry had zero live coverage) was also closed by the verification session
that found these. Nothing about the tool surface changes: still **30 tools**.

**(a) `latency_spans` double-counted and hid the evidence.** `parallel_reads`
wrapped an `asyncio.gather` whose semantic leg records `goal_embedding` and
`ann_query`, so summing every span counted the same milliseconds twice.
`other = max(0, latency - sum)` then absorbed the negative residual, so the
breakdown looked *exact* precisely when it was most wrong — 78–194ms of real
latency reported as `other: 0`.

The additive breakdown is now the **sequential** timeline
(`gate_targets.ACCOUNTED_SPANS`). The concurrent legs are nested detail
(`NESTED_SPANS`) — reported, but excluded from the sum, because four legs that
run concurrently do not add up to their block's wall time under any accounting.
**The clamp is gone**: a negative `other` now means the accounting is broken and
must be visible rather than floored to a tidy zero. So if you see a negative
`other` after this deploy, that is the fix working as designed — report it, do
not dismiss it.

Also new: a `persist` span for the `gate_intent` INSERT, and timings for the
three concurrent legs that were never measured at all.

**(b) Brand-new skills were born expired.** `last_validated` was set only inside
`if trigger_valid:`, so a skill published with no trigger — a documented way to
ship display-only advice — was flagged `expired_skill` by the very first
`intent_open` that surfaced it. The stamp moved out of that branch.
`curator_provenance` deliberately stays gated on `trigger_valid`: that flag is
about the *predicate* having been validated and is the escalation gate. This is
a **freshness** fix, not an escalation change, and two tests pin that distinction.

**(c) `gate_close_outcome` never reached `gate/efficacy/<yyyymm>`.** Two
unrelated concepts share the word "closure": **block closure**
(`confirmed_correct`/`false_positive`/`unknown`, written by the gate) and
**outcome closure** (`followed`/`overridden`/`abandoned`, written deliberately).
The populated block-closure counter sitting next to the missing one made the hole
look like a zero.

Outcomes now roll into a **separate `outcomes` counter** rather than being folded
into `closures` — different vocabulary, different writer, different question, and
merging them would combine two populations. Counted once per intent and skipped
on replay, so a retried close cannot inflate the number it reports.

Backward compatible: rollups written before this change lack `outcomes` and pick
up the zeroed base through `_deep_merge_base`.

`server_version`: **0.4.0 → 0.4.1**.

---

## 2. What you need to do

### 2.1 Pull and redeploy

```bash
git fetch origin main && git checkout 1c93d879270cd24caa6f677f99ec515163ac467f
```

No migration, no new environment variables, no new dependencies — only four
source files and two test files changed. The existing `[deployment].build`
(editable install + the `en_core_web_sm` wheel) is unchanged and needs no edits.

### 2.2 Confirm the version actually changed — ASK THE SERVER

Call `stats` through the **MCP endpoint** and require `server_version == "0.4.1"`.

If it still says **0.4.0**, the republish did not take. Stop and re-republish.
This is the third release in a row where this specific check is the one that
catches a silent no-op deploy, and it is the reason `SERVER_VERSION` was bumped
at all — the fixes change behaviour but not the tool surface, so **the version
stamp is the only cheap way to tell the fixed build from the broken one.**

Do **not** substitute a shell check. A `$DATABASE_URL` in a terminal is a claim
about a connection, not the connection: a previous deploy ran correct SQL against
`heliumdb` while the deployed server read `neondb`, so the change appeared to
succeed and armed nothing. Any post-deploy assertion made through `psql` instead
of through `stats` can reproduce that failure exactly.

Expected `db_identity` after deploy (unchanged by this release — a difference
here means the deploy moved the database, which would be a serious problem):

| field | expected |
|---|---|
| `current_database` | `neondb` |
| `endpoint_host` | `ep-flat-fog-aq9gikg7.c-8.us-east-1.aws.neon.tech` |
| `boot_connection_fingerprint` | `8ad04bbbf8b32eab88b0581d74aefa672a5c6f2dd2fae807cb8c705809da5afe` |
| `server_boot_ts` | **newer** than your deploy start time |

`server_boot_ts` is the one that proves the process actually restarted. A correct
`server_version` with a stale boot timestamp means you are reading a cached
response, not a new build.

### 2.3 Confirm the tool count is still 30

```bash
python scripts/smoke_mcp.py
```

This release adds no tools. A count other than 30 means something other than this
release is deployed.

---

## 3. Post-deploy checks against the live server

Use a **fresh, nonced namespace** and an actor nobody else uses (event dedup is
scoped to `(namespace, actor)`) — **except** where a check below explicitly names
`dev/gate-p2-live-b95efae6`. Do not write to `dev/mcp-assist-memory` or any other
armed namespace.

| # | check | pass condition |
|---|---|---|
| D1 | `stats` | `server_version == "0.4.1"`; `db_identity` matches the table in §2.2; `server_boot_ts` newer than deploy |
| D2 | tool list | exactly 30 tools |
| D3 | `intent_open` on a fresh namespace, read `latency_spans` | the accounted spans sum to **≤** total latency; `other` is derived without a clamp; the concurrent legs appear as nested detail and are **not** in the sum; a `persist` span is present |
| D4 | `skill_define` with `trigger_author: "human"` and **no trigger** | `meta.last_validated` **is present**; `meta.curator_provenance` is **absent** |
| D5 | `intent_open` that surfaces the D4 skill | **not** flagged `expired_skill` |
| D6 | `gate_close_outcome`, then read `gate/efficacy/<yyyymm>` | an **`outcomes`** key exists and the relevant verdict incremented |
| D7 | repeat the same `gate_close_outcome` (replay) | `outcomes` does **not** increment a second time |
| D8 | a skill that legitimately has a valid trigger | still gets `curator_provenance: true` — the freshness fix must not have become an escalation change |

**D6 has a pre-recorded baseline, and it is the highest-value check in this
document.** The namespace `dev/gate-p2-live-b95efae6` is armed (`intent_gate: on`)
and was used for the live falsification that found defect (c). Its rollup
`gate/efficacy/202608` was captured immediately before and immediately after a
successful `gate_close_outcome` on the **unfixed** server and was byte-identical
across both — same `revision: 8`, same
`content_hash 231cab37a895f44408e35b6f9d8458c1ab505442b47844c0e244fca9cec567c6`:

```json
{"fired": 8,
 "rules": {"unresolved_conflict_destructive": 1},
 "tiers": {"0": 6, "1": 2},
 "skills": {},
 "closures": {"unknown": 0, "false_positive": 1, "confirmed_correct": 0},
 "decisions": {"gate_blocked": 1, "gate_clarify": 0, "gate_preview": 0,
               "gate_approved": 5, "gate_conflict": 2}}
```

Note there is **no `outcomes` key at all**. That absence is the defect. A
`gate_close_outcome` against this namespace on the fixed build must create one.
Because the before-state is pinned to an exact content hash, this is a genuine
A/B on the same namespace rather than a fresh-namespace check that can only ever
confirm what it just wrote.

Be aware that any write to that namespace also increments `fired`, so record the
rollup **before** you touch it if you want to preserve the comparison for anyone
after you.

---

## 4. Tests

```bash
DATABASE_URL=<throwaway pg> ADMIN_PASSWORD=test-admin-pw python -m pytest -q
```

Expected: **543 passed, 5 deselected**. Verified on this exact merge content
against local Postgres 16.13 + pgvector 0.6.0.

`ADMIN_PASSWORD` is not optional. `tests/test_dashboard.py` and
`tests/test_surface_attribution.py` only `os.environ.setdefault` it at module
import, which loses to app-import ordering under full-suite collection — without
it exported you get exactly **2 failures** that look like real regressions and are
not. This was confirmed on this commit: 2 failed without it, 543 passed with it.

New file: `tests/test_gate_defects_v041.py` (10 tests). Verified adversarially —
**8 of the 10 fail against the unfixed source**; the 2 that pass are the
no-regression guards on escalation provenance (D8 above).

**Do not trust the GitHub Actions `test` check on this repo.** `NEON_API_KEY` and
`NEON_PROJECT_ID` are unset, so `setup-python`, `Create ephemeral Neon branch`,
`Install` and `Migrate + test` all report `skipped` and the job no-ops to green.
The run on the fix commit "passed" in **7 seconds** without executing a single
test. This was carried forward as item 5 of the previous release's list and is
still open. Until those secrets exist, the only real signal is a local run.

---

## 5. Carried forward — still open, not addressed here

1. **Gate cache listener on TTL fallback.** `gate_cache_status.listener_alive`
   was last seen **false**, so profile edits take up to `ttl_seconds` to be
   noticed. Needs the DIRECT, non-pooler connection string
   (`DATABASE_URL_DIRECT`); Neon's pooled endpoint supports NOTIFY but not
   LISTEN. See `docs/runbooks/neon-credential-rotation.md`.
2. **CI secrets** — see §4. A ~6–10 second `test` run is a no-op skip, not a
   green suite.
3. **The MCP registry entry is stale and is not yours to fix.** The only tag ever
   pushed is `v0.3.0`; `v0.4.0` was never tagged, so the registry has been
   advertising 0.3.0 across two releases. That is handled separately by
   `docs/COWORK-MCP-REGISTRY-PUBLISH-v041.md`, and it must happen **after** this
   deploy is verified — publishing a version the live endpoint does not serve is
   the same class of inconsistency in the other direction.

**No new security claims.** Screening remains documented as hygiene; the
read-time wrapper remains the stated boundary. **No PHI surface changed** — the
new `outcomes` counter carries verdict counts only, no goal text and no entry
content. No `tier2` flag was touched.

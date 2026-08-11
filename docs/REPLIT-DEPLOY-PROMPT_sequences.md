# Replit Deploy Prompt — Server-Side Sequences + Shared Retrieval Guard (v0.4.0)

**Give this whole document to the Replit deploy agent.** It is self-contained
and assumes the previous release (v0.3.0, merge `d48ad0b`) is already live.

---

## 1. What changed and why it matters at deploy time

Two things ship together.

**(a) The retrieval guard moved below the tools.** `memory_search` previously
had **no similarity floor at all** — it ordered by raw cosine distance, took
top-N, and RRF-fused with a keyword leg. `intent_open` ran the same store
through a 0.45 absolute floor and a 0.85 relative alpha. So the same store
answered the same question two different ways depending on which tool the
caller happened to reach for, which made an agent's call ordering load-bearing.
Both paths now call one function, `storage/retrieval.py::apply_guard`.

They still differ in what they do with the verdict, deliberately:

| surface | behaviour |
|---|---|
| `memory_search` | **annotates**, drops nothing — every row carries `retrieval` (cosine, floor, alpha, reason, calibrated) |
| `recall` (new) | **filters** to admitted rows, always reports the rejected counts |
| `intent_open` | filters, as before — unchanged decisions |

`memory_search` annotates rather than drops for two reasons: the repo's
additive-schema constraint (a release must not silently return fewer rows), and
the fact that on almost every namespace the floor is an **uncalibrated server
default**. Deleting evidence on the authority of a number nobody measured is not
an improvement.

**(b) Three fixed sequences run server-side.** `session_bootstrap`,
`namespace_init`, `recall`. Each runs its steps in a fixed order and returns
`steps_run` naming what actually executed. Previously that ordering lived in
tool descriptions and skills — enforced by a model remembering advice mid-task,
with a skipped step producing no signal whatsoever. **All existing primitives
are unchanged and still work**; this is strictly additive.

Tool surface: **27 → 30**. `server_version`: **0.3.0 → 0.4.0**.

---

## 2. What you need to do

### 2.1 Pull and apply the migration — REQUIRED, do this first

One new migration: `migrations/0013_namespace_registry.sql` (one new table,
`namespace_registry`; nothing existing reads it, so applying it changes no
current behaviour).

```bash
python scripts/migrate.py     # idempotent; tracks applied files in schema_migrations
```

Verify it landed:

```sql
SELECT to_regclass('namespace_registry');   -- must not be NULL
SELECT filename FROM schema_migrations ORDER BY filename DESC LIMIT 3;
```

If `namespace_registry` is NULL, **stop**. `namespace_init` will fail on its
registry step, and `session_bootstrap` will report it under `degraded`.

### 2.2 Redeploy and confirm the version actually changed

Every ack should now report `server_version` **0.4.0**. If it still says
**0.3.0**, the republish did not take — stop and re-republish. This is the same
failure that hid a whole release last time.

### 2.3 Confirm the tool count is 30

```bash
python scripts/smoke_mcp.py   # derives the expected count from the live registry
```

The three new names are `session_bootstrap`, `namespace_init`, `recall`.
A connector that still lists 27 is serving stale code.

### 2.4 Republish the MCP Registry entry

`server.json` now says `0.4.0`. Until it is republished, the registry advertises
a version and a tool surface the live server no longer matches. A test
(`test_server_version_matches_the_registry_advertised_version`) pins
`SERVER_VERSION == server.json.version`, so the repo side is consistent — the
registry side is a manual publish step and is **yours**.

### 2.5 Check the gate cache listener (carried forward, still open)

At last verification `gate_cache_status.listener_alive` was **false**, meaning
the cache is on TTL fallback: profile edits take up to `ttl_seconds` to be
noticed. This is not an outage and it is not a regression, but it is now more
visible — `session_bootstrap` surfaces it in its `attention` list on every
session open. The listener needs the **DIRECT, non-pooler** connection string
(`DATABASE_URL_DIRECT`); Neon's pooled endpoint supports NOTIFY but not LISTEN.
See `docs/runbooks/neon-credential-rotation.md`.

---

## 3. Tests to run

### 3.1 Full suite (CI-class, local Postgres + pgvector)

```bash
DATABASE_URL=<throwaway pg> python -m pytest -q
```

Expected: **533 collected, 532 pass**. The one failure is in
`tests/test_gate_awaken.py` (`test_gh_2_…` / `test_gh_3_…`, which one varies per
run) — these are **pre-existing timing flakes**, verified as flaking identically
on the v0.3.0 merge commit with these changes stashed. They are not caused by
this release. Do not "fix" them as part of this deploy.

New files: `tests/test_retrieval_guard.py` (12 tests),
`tests/test_sequences.py` (29 tests).

### 3.2 Post-deploy checks against the live server

Run these against production once deployed. Use a **fresh, nonced namespace**
and an actor nobody else uses (event dedup is scoped to `(namespace, actor)`).
**Do not write to** `dev/mcp-assist-memory`, `dev/gate-probe-20260803*`, or any
armed namespace.

| # | check | pass condition |
|---|---|---|
| D1 | `stats` | `server_version == "0.4.0"`; `db_identity.current_database == "neondb"` |
| D2 | tool list | exactly 30 tools, including the three new names |
| D3 | `session_bootstrap` on a fresh namespace | `steps_run` == all 7 steps; `degraded == []`; `db_identity` present and its fingerprint is 64 hex chars |
| D4 | `namespace_init` on a fresh namespace | `created: true`, `steps_run` includes `register_namespace` and `readback_verify`; re-running returns `created: false` and changes nothing |
| D5 | `namespace_init` with `similarity_floor` but no `calibration_ts` | `retrieval_policy.calibrated == false` — a number nobody measured must not read as a measurement |
| D6 | `memory_search` on a namespace with a few entries | every row carries `retrieval` with `absolute_floor: 0.45`, `alpha: 0.85` |
| D7 | `recall` with the same query | the set of keys equals the `admitted: true` subset from D6 — **this is the consistency property the release exists for** |
| D8 | `recall` on a query that matches nothing | `results: []` AND `guard.rejected_below_floor` reported, so "empty" and "all noise" stay distinguishable |
| D9 | `intent_open` regression | the three charter goals still decide `gate_approved` / `gate_approved` / `gate_conflict` — the gate now calls the shared guard, and its decisions must be unchanged |

D7 and D9 are the two that matter most. D7 is the new property; D9 is the proof
that sharing the guard did not move the gate.

---

## 4. Additional deployment notes

**Nothing here needs a namespace armed.** All three sequences work on ungated
namespaces. `intent_gate` arming is unchanged and still an operator decision.

**PHI**: `namespace_registry` stores no goal text and no entry content. Its only
free-text column is `purpose`, which is caller-supplied and passes the same
write path as any other value. In a clinical namespace, keep identifiers out of
`purpose` exactly as you would out of an intent goal. Set `clinical: true` at
`namespace_init` time — it is not a thing to remember later.

**Carried forward, still not fixed** (out of scope for this change, flagged so
it does not get lost):

1. `latency_spans` double-counts on `parallel_reads` and clamps `other` to zero
   with `max(0, …)`, hiding 78–194ms. Live T15 FAIL from the Cowork
   verification; unaddressed here.
2. A brand-new skill authored with no trigger is flagged `expired_skill`,
   because `last_validated` is only set inside the `if trigger_valid:` branch.
3. Phase 2 block telemetry still has **zero live coverage** — no namespace on
   production is armed, so no block has ever been recorded by the new path.
4. `gate_close_outcome` does not roll into `gate/efficacy/<yyyymm>.closures`.
5. CI secrets: if the GitHub Actions `test` workflow still finishes in ~6–10
   seconds, it is a no-op skip, not a green suite.

**No new security claims.** Screening remains documented as hygiene; the
read-time wrapper remains the stated boundary. The retrieval guard is a
RETRIEVAL control — it has never meant "this is a violation", and escalation is
still decided solely by a skill's trigger predicate.

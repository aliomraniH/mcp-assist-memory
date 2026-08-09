# Replit Agent — Deploy Prompt: Intent Gate v1 Remediation

**Copy this whole file into the Replit Agent.** It is self-contained.

---

## 0. What you are deploying

`aliomraniH/mcp-assist-memory`, merge commit **`d48ad0b49c22cdae90422c88326a298827672c2b`** on `main` (PR #16, merged 2026-08-05). 39 files, +5117/−63, 6 commits.

This fixes six findings from an independent validation run of the Intent Gate. The three that matter to you operationally:

- The gate used to raise `gate_conflict` based on **embedding similarity alone**. It flagged "schedule the quarterly workshop catering" against an event-log skill, and flagged an intent that *obeyed* the skill. Escalation is now decided by a **structured predicate**, not similarity.
- **Blocked calls were invisible in telemetry.** A row was written but its verdict columns were `NULL`, so every analytics view concluded the gate never blocks. Fixed.
- The last deploy ran **correct SQL against the wrong database** (`heliumdb` instead of `neondb`) and silently armed nothing for seven minutes. The server now reports its own DB identity, and the deploy record is now a *gate*, not a convention.

**Three things to know before you start:**

1. **Merged code does not auto-deploy here.** Deployment is your step.
2. **The 492 passing tests were run locally, not by CI.** GitHub Actions did *not* verify this branch — see task **1.5**.
3. **This deploy changes gate behaviour on purpose.** See §3.1. Read it before anyone reports the gate as "broken".

---

## 1. Tasks for you (nothing here was done by the implementing session)

### 1.1 — Rotate the Neon credential ⚠️ SECURITY, DO THIS FIRST

The production Neon connection string **including its password** was pasted in cleartext into a Replit Agent conversation on 2026-08-03. Treat it as compromised.

Full procedure: **`docs/runbooks/neon-credential-rotation.md`** (in the repo). Follow it rather than improvising — this rotation is not routine, see 1.2.

### 1.2 — Set TWO connection strings in Deployment Secrets

This release adds a second, **different** connection to the same database:

| Secret | Endpoint | Used by |
|---|---|---|
| `DATABASE_URL` | host **contains** `-pooler` | all app traffic |
| `DATABASE_URL_DIRECT` | host **without** `-pooler` | the cache-invalidation `LISTEN` subscriber (**new**) |

Neon's pooled endpoint supports `NOTIFY` but **not** `LISTEN` — PgBouncer in transaction mode drops session-level features. The subscriber therefore needs its own direct connection.

Two ways this goes wrong quietly:

- **Updating only `DATABASE_URL`.** The app keeps serving; the listener dies with a dead password; cache invalidation silently degrades to TTL-only. Nothing pages. Task 2.6 is how you catch it.
- **Setting these in the workspace shell instead of Deployment/App Secrets.** The workspace and the deployed VM have *different environments*. That divergence is exactly how the `heliumdb`/`neondb` split-brain happened. **Set them in Deployment/App Secrets.**

`DATABASE_URL_DIRECT` is optional — unset just means no listener and pure-TTL caching, which is correct but slower to notice a profile change. It is not an error.

### 1.3 — Confirm `ANTHROPIC_API_KEY` is in Deployment Secrets

Should already exist for the curator. **Verify, don't assume.**

### 1.4 — Dependencies will be fetched at build time

`constraints.txt` and `pyproject.toml` now pin: `spacy` 3.8.14, `en_core_web_sm` 3.8.0 (a **direct wheel URL**, not PyPI), `panzi-json-logic` 1.0.1, `asyncpg` 0.31.0, plus full transitive closure. The build must be able to reach GitHub releases for the spaCy model wheel. **If the build fails, check this first.**

Phase 4 (`onnxruntime`, `optimum`) is descoped and deliberately **not** pinned.

### 1.5 — Set the GitHub Actions CI secrets

**`.github/workflows/test.yml` currently passes without running anything.** It gates every step on `NEON_API_KEY` / `NEON_PROJECT_ID`; neither is set, so the gate takes the else branch, skips install + migrate + pytest, and **exits 0**. Both checks on PR #16 reported success in 6–7 seconds having run zero tests. Log evidence:

```
##[warning]NEON_API_KEY / NEON_PROJECT_ID not set — skipping the live Postgres suite.
```

A green check on this repo currently means "CI declined to run", not "CI passed."

**Do:** set `NEON_API_KEY` and `NEON_PROJECT_ID` in **GitHub** repo secrets (Settings → Secrets and variables → Actions).

⚠️ Use a Neon API key scoped to a project for **throwaway CI branches**. **Not** the production role credential, and **not** the credential you rotate in 1.1. The workflow creates and deletes an ephemeral Neon branch per run.

**Verify:** push any commit and confirm the `test` job now takes **minutes, not seconds**, and logs `492 passed, 5 deselected`. Still finishing in ~6s ⇒ the secrets did not land.

### 1.6 — Adopt the closeout gate as the final deploy step

`scripts/deploy_closeout_gate.py` — see §2.8. Non-zero exit means **the deploy may not be declared successful**.

### 1.7 — Confirm Neon autosuspend is intentional

The listener holds one persistent direct connection (negligible against `max_connections` 104 at 0.25 CU). If autosuspend is on, the listener drops on suspend. TTL fallback covers it and the supervisor reconnects — but confirm the setting is deliberate rather than discovering it as a latency mystery.

---

## 2. Tests to perform

### Order: build → migrate → suite (dev only) → deploy → probes → closeout gate

### 2.1 — Migrations

```bash
python scripts/migrate.py
```

Expect exactly three new: `0010_gate_match_log`, `0011_gate_telemetry`, `0012_gate_cache_invalidate`. All **additive** (new tables, nullable columns, one trigger).

`0011` inserts a **discontinuity marker row** into `tool_events` (`emit_event_id = 'discontinuity:0011_gate_telemetry'`). **This is intended. Do not delete it.** It marks the boundary before which block-rate data is unusable.

### 2.2 — Full suite, on a DEV database ONLY

```bash
pytest -q          # expect: 492 passed, 5 deselected
```

**Never** run this against the production `DATABASE_URL` — it writes.

The 5 deselected are `[NEON]`-marked and skip by design; §2.7 runs them.

### 2.3 — DB identity (the split-brain check)

Call the **`stats`** tool on the deployed server. Read `db_identity`:

```
current_database             → the production database name
endpoint_host                → the POOLED (-pooler) host
boot_connection_fingerprint  → record this; it is --expect-fingerprint next deploy
pgvector_version             → present
```

**Ask the server, never a shell.** `psql "$DATABASE_URL"` from the Repl shell may answer about a *different database* — confidently. That is the entire lesson of the last deploy.

### 2.4 — Tool surface

`scripts/smoke_mcp.py` expects **27 tools** (was 24). New: `skill_define`, `gate_close_outcome`, `gate_cache_status`.

### 2.5 — Version

Any ack should now report `server_version` **0.3.0**. If it still says **0.2.0**, the republish did not take — stop and re-republish.

### 2.6 — Listener health ⚠️ EASY TO SKIP, DON'T

Call **`gate_cache_status`**:

- `listener_alive: true` → the direct string is good.
- `listener_alive: false` → running on TTL fallback. `DATABASE_URL_DIRECT` is wrong, unset, or pointed at the pooled endpoint.

`false` is **not an outage** — decisions stay correct, the cache is just slower to notice profile changes. Which is exactly why **nothing else will tell you**.

### 2.7 — Behaviour probes against the LIVE server

In an armed namespace holding an anti-pattern skill **with an authored trigger**:

| # | `intent_open` goal | Expected |
|---|---|---|
| A | `schedule the quarterly workshop catering` | `gate_approved`, no conflict |
| B | `rebuild the projection by replaying the event log in insertion order` | `gate_approved` |
| C | `rebuild the projection by replaying the event log sorted by timestamp` | `gate_conflict`, `conflict.basis = "anti_pattern_predicate"` |

A and B are the two live failures. **C is the one people forget** — without it, A and B only prove the gate went silent, not that it got smarter. All three must hold.

Pass `verbose_gate: true` to see per-candidate `gate_audit` explaining each decision.

### 2.8 — `[NEON]` tests, deferred to you

The implementing environment had no Neon topology, so these were written, marked, and **deliberately not claimed green**. Run them where they mean something:

```bash
# needs DATABASE_URL_DIRECT set
pytest -m neon tests/test_gate_remediation_p3.py

# latency, production topology only
python scripts/gate_latency_harness.py --namespace dev/gate-latency-probe --n 2000 --tier 0
python scripts/gate_latency_harness.py --namespace dev/gate-latency-probe --n 2000 --tier 1
```

The harness prints an **environment banner**. If it says `LOCAL POSTGRES`, the numbers are meaningless — do not record them.

- **Tier-0 target: p95 ≤ 110ms, median ≤ 75ms.** Re-baselined with justification in `storage/gate_targets.py` (the old `<50ms` sat below the achievable floor and failed all 20 samples).
- **Tier-1 target is PROVISIONAL at 500ms and yours to decide.** Pre-fix production was median 515ms / p95 810ms. Two mechanisms were applied (four independent reads now run concurrently, 4 round trips → 1; goal embeddings cached). If measured p95 ≤ 500ms, keep it. **If not, re-baseline to measured p95 + headroom and write the justification next to the number** — do not quietly loosen it. Record the decision and the printed span breakdown in the deploy record.

### 2.9 — Closeout gate (final, mandatory)

```bash
python scripts/deploy_closeout_gate.py \
  --namespace dev/mcp-assist-memory \
  --baton-key baton/replit-deploy-gate-remediation \
  --record-key deploy/gate-remediation-p1 \
  --sha d48ad0b49c22cdae90422c88326a298827672c2b \
  --expect-database <production db name>
```

Writes the deploy record **and** consumes the baton in one transaction; asserts DB identity **before** writing anything.

Exit codes: `0` ok · `2` bad args · `3` baton absent/already consumed · `4` DB identity mismatch.

**Non-zero ⇒ do not declare the deploy successful.**

### 2.10 — Close out the ORIGINAL deploy too (historical debt)

The **previous** Intent Gate deploy never got a closeout: `deploy/intent-gate-p1` is null and `baton/intent-gate-deploy` is unconsumed, while that build has been live for days. Write the retroactive record with honest provenance:

- `kind=knowledge`, `temporal_mode=historical_snapshot`
- note: `"written retroactively 2026-08-XX; original deploy closed out no record — see validation Finding 0/1"`
- and mark `baton/intent-gate-deploy` consumed with the same note.

(Tombstoning both with the note is an acceptable alternative.) Either way the stale artifact stops misleading readers. The implementing session deliberately left this alone — it is yours.

---

## 3. Deployment notes

### 3.1 — ⚠️ Intended behaviour change: anti-pattern skills go quiet

**Every existing anti-pattern skill has no trigger and becomes display-only on deploy.** The gate will produce **zero** `gate_conflict` escalations from it until a trigger is authored.

This is deliberate. The conflict stream being replaced was false-positive dominated, and a gate that cries wolf trains its operator to ignore it. **Advice still surfaces** — only the ability to *block* is withheld.

Practically: `dev/mcp-assist-memory` contains **zero** anti-pattern skills, so no namespace you care about changes. The three that exist live in `dev/gate-probe-20260803` (validation evidence, not written to).

To author triggers later: `scripts/author_gate_triggers.py --report` lists what is display-only; the `skill_define` tool authors them. **The script refuses to write to the probe namespaces** — those rows are evidence.

### 3.2 — Telemetry has a discontinuity; do not sum across it

Pre-migration false-positive-rate data is **unusable**. Both surfaces undercounted blocks in the same direction and neither error is quantified, so any reconstruction would be a guess dressed as history. **There is no backfill, by choice.** A trustworthy baseline accrues from `d48ad0b4…`. Do not compare block rates across the marker row.

### 3.3 — Old acks are unchanged

Additive-only by design. Under the default variant profile the ack shape is byte-identical to before — the new retrieval-guard config was deliberately kept *out* of the echoed `variant_profile` so this stayed true. Existing integrations should see no difference.

### 3.4 — Tier 2 stays OFF

Nothing here enables it. Don't flip `tier2` — the conflict stream it would adjudicate is what this release is fixing, and the recommendation in `baton/intent-gate-p2` still stands.

### 3.5 — Rollback

Redeploy the previous build. Migrations 0010–0012 are additive and stay applied safely. `NULL` trigger = display-only = pre-branch behaviour, so rolling back code with the schema in place is coherent. No down-migration needed.

### 3.6 — What NOT to claim

- Don't call the deploy healthy until §2.7 **and** §2.8 pass against the live server. A green build is not a working gate.
- Don't record latency numbers from a `LOCAL POSTGRES` banner.
- Don't treat a green GitHub check as verification until 1.5 is done.

If something fails, say what failed and stop. A deploy that is honestly blocked is recoverable; one that is declared successful and isn't produced the mess this release exists to clean up.

---

## 4. Reference

| Where | What |
|---|---|
| `baton/replit-deploy-gate-remediation` (memory) | machine-readable version of this, incl. full appendix |
| `build/gate-remediation-p1-complete` (memory) | what shipped, what was descoped, and why |
| `docs/runbooks/neon-credential-rotation.md` | rotation procedure (task 1.1) |
| `storage/gate_targets.py` | latency targets with the measurements that justify them |
| PR #16 | full diff and the two CI comments |

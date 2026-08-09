# Replit Agent — MCP_Assist Deploy, Enhancement Plan v3 Phases 0–1
Repo: aliomraniH/mcp-assist-memory   Branch to deploy: main
Merge commit to deploy: 60cf432d203d948bea42fb3649592656859e51d4 (PR #12)
Namespace for records: dev/mcp-assist-memory   Actor: replit-deploy-agent
You are deploying ALREADY-MERGED, ALREADY-TESTED code (330 tests green at
branch head f4ca17b12b8e3440f164982d70fbc7fdc2657663). You write NO new
application code. If any step fails, stop at that step's FAIL branch — never
improvise a fix in production. Create a NEW session in dev/mcp-assist-memory
before starting and record it in your closeout writes.

## Phase 0 — Environment gates (blocking; run in order)
0.1 git fetch origin && git checkout main && git pull origin main, then
    git rev-parse HEAD.
    OUTCOME: HEAD == 60cf432d203d948bea42fb3649592656859e51d4.
    FAIL: main has moved past the audited merge. STOP and report the actual
    head; deploying an unaudited revision is out of your scope.
0.2 Confirm required secrets are configured in the deployment: DATABASE_URL,
    MCP_AUTH_TOKEN, ADMIN_PASSWORD (dashboard), and the GitHub credential the
    resolver uses (GITHUB_TOKEN or the Replit GitHub connector).
    OUTCOME: all present.
    FAIL on GitHub credential only: deploy may proceed, but note in the
    closeout that sha auto-resolution and probe step 3B will run with a
    DISABLED resolver (every verdict `unverifiable`) — the probe acceptance
    below cannot be met; flag for the operator instead of faking it.
    FAIL on any other secret: STOP; operator adds it.
0.3 Database reachability: psql (or a one-line psycopg script) can connect to
    DATABASE_URL and `SELECT count(*) FROM memory_entry` succeeds.
    OUTCOME: row count returned (record it — you will compare it after
    migration to prove the migration destroyed nothing).
    FAIL: STOP; do not migrate a database you cannot read.
0.4 THIS DEPLOY IS REVERSIBLE BUT NOT REPEATABLE-BLINDLY: migration 0007 is
    additive (nullable columns only), so old code runs fine against the new
    schema — rollback = redeploy the previous build, NO down-migration exists
    or is needed. State this in your completion claim.
0.5 PHI hard gate: nothing in this deploy may write patient data anywhere;
    all your memory writes go to dev/mcp-assist-memory or the probe namespace
    dev/v3-probe-20260715 only. No writes to dev/synch-pharma or
    dev/skill-transfer; no proj-test-* tombstoning (decision D3 is the
    operator's).

## The verified facts you inherit (do not re-derive)
- The merged series implements: [I1] one shared SHA-equivalence rule
  (storage/sha_equiv.py) adopted by coord_reconcile, coord_health's stale
  projection, the write boundary, and the R5 advisory — abbreviations
  validate (hex 7..40, `invalid_sha` otherwise), auto-resolve to the full
  40-char sha when GitHub is reachable (original preserved as
  meta.repo_sha_input), ambiguous prefixes reject (`ambiguous_sha`).
  [I2] verdict reads carry checked_at + age_hours + freshness:"expired"
  inline. [I3] replays escalate to top-level status:"deduplicated_replay".
  [I4] idempotency fingerprint (RFC 8785 via the `rfc8785` package — a NEW
  runtime dependency): same event_id + different payload now errors
  `idempotency_conflict` in-band (isError:true tool result, never a protocol
  error). [I5] temporal_mode column + reconcile forks (historical_snapshot =
  sha-exists check, never head-compare). [I6] evidence_state ladder
  (remote_confirmed only via resolver observation). [I7] role column,
  recording only. [I8] composite status+summary on every save ack;
  compact_acks:"on" profile arm returns ~14-field acks, verbose:true returns
  the full block. Migration 0007 = three nullable columns: idem_fingerprint,
  temporal_mode, role.
- The defect this deploy closes IN PRODUCTION: the live 2×2 probe of
  2026-07-15 (namespace dev/v3-probe-20260714 — keep it untouched as the
  before-fixture) showed a claim pinned to a 7-char prefix of the live head
  reading `current` from coord_reconcile and `stale` from coord_health AT THE
  SAME TIME. The fix is merged but NOT live until you deploy and re-probe.
- Two behavior changes an existing client could notice (expected, not bugs):
  (a) same event_id + different payload used to silently echo the original
  ack — it now returns idempotency_conflict; (b) meta.repo_sha values that
  are non-hex, shorter than 7, or longer than 40 chars used to store verbatim
  — they now reject with invalid_sha.

## Actions (run in order; each has OUTCOME + TEST + FAIL)
1. Install dependencies.
   ACTION: reinstall the project into the deployment environment
   (`pip install -e .` or Replit's package flow).
   OUTCOME: `python -c "import rfc8785; print(rfc8785.__name__)"` prints
   rfc8785.
   TEST: also `python -c "import storage.sha_equiv, storage.idempotency"`
   exits 0.
   FAIL: dependency install is broken — STOP before migrating; nothing has
   changed yet.
2. Apply migration 0007.
   ACTION: python scripts/migrate.py
   OUTCOME: memory_entry has columns idem_fingerprint, temporal_mode, role
   (`SELECT column_name FROM information_schema.columns WHERE
   table_name='memory_entry' AND column_name IN
   ('idem_fingerprint','temporal_mode','role')` returns 3 rows).
   TEST: `SELECT count(*) FROM memory_entry` equals the count recorded in
   gate 0.3 (additive migration, zero rows touched).
   FAIL: STOP and report the migration error verbatim. Do not retry with
   hand-edited SQL. The database is still valid for the OLD build (columns
   are nullable), so the running service is unharmed.
3. Restart and verify the service.
   ACTION: restart the Replit deployment.
   OUTCOME: GET /healthz returns {"status":"ok"}.
   TEST: python scripts/smoke_mcp.py against the deployed URL passes (tool
   count + bearer auth); an UNauthenticated /mcp POST still returns 401.
   FAIL: roll back to the previous build (schema is compatible), confirm
   /healthz ok on the old build, report which check failed.

## Post-deploy acceptance probes (all against the DEPLOYED server, namespace
## dev/v3-probe-20260715 unless stated; do NOT claim success until all pass)
A. V2 probe re-run — the defect's own shape.
   ACTION: get the live main head (git ls-remote). Write claim
   probe/sha-prefix {meta: {repo: "aliomraniH/mcp-assist-memory", branch:
   "main", repo_sha: <FIRST 7 CHARS of head>}} and claim probe/sha-full with
   the full 40-char head. Run coord_reconcile AND coord_health on the
   namespace.
   OUTCOME (acceptance): probe/sha-prefix verdict `current` AND absent from
   health.stale; probe/sha-full likewise clean. The two consumers AGREE —
   before the fix they disagreed on the same claim.
   TEST (bonus, resolver-dependent): the probe/sha-prefix save ack shows
   repo_sha auto-resolved to the full 40-char sha with meta.repo_sha_input
   preserving your 7-char input.
   FAIL: the defect is NOT fixed in production. Do not write any
   "fixed" record; report both tool outputs verbatim.
B. Idempotency contract.
   ACTION: memory_save key=probe/idem value={"n":1} event_id=<fresh uuid>,
   twice byte-identically; then a third call with the SAME event_id and
   value={"n":2}.
   OUTCOME: call 2 ack carries top-level status:"deduplicated_replay" (and
   deduplicated:true); call 3 returns an in-band tool error (isError:true)
   with code idempotency_conflict — NOT a success echo, NOT a protocol error.
   TEST: memory_history probe/idem shows exactly ONE revision.
   FAIL: report which call misbehaved with its raw response.
C. Verdict freshness inline.
   ACTION: memory_get coord/_reconcile/probe/sha-prefix (written by probe A).
   OUTCOME: the entry carries checked_at, age_hours (< 1.0), and
   freshness:"fresh" WITHOUT calling coord_health.
   FAIL: report the raw entry.
D. Compact-ack arm (opt-in only — default namespaces must be unchanged).
   ACTION: set variant_profiles.profile = {"compact_acks":"on"} for
   dev/v3-probe-20260715 (SQL upsert), wait >60s or restart (profile cache),
   then memory_save probe/compact value of ~300 bytes; also run one
   memory_save in a DEFAULT-profile scratch namespace.
   OUTCOME: the compact ack is ≤ ~15 top-level fields and well under 1,506
   bytes, carrying status + summary + core identity; verbose:true on the same
   call returns the full block; the default-namespace ack still has the full
   field set PLUS status/summary.
   FAIL: report both acks verbatim.
E. Guard probe — boundary validation is live.
   ACTION: attempt memory_save with meta={"repo_sha":"not-a-sha"} and another
   with meta={"temporal_mode":"forever"}.
   OUTCOME: in-band errors invalid_sha and invalid_temporal_mode
   respectively; nothing persisted (memory_get returns null for those keys).
   FAIL: report the raw responses.

## Exit gate + closeout (only after A–E pass)
Write to dev/mcp-assist-memory as actor replit-deploy-agent:
1. deploy/v3-phase0-1 kind=knowledge (milestone = knowledge, NOT claim):
   deployed merge sha 60cf432d... live, migration 0007 applied, probes A–E
   passed, with meta {repo, branch: "main", repo_sha: <full 40-char deployed
   sha>, temporal_mode: "historical_snapshot", session_id: <your session>}
   and derived_from ["build/v3-phase0-1-complete@2954",
   "baton/replit-deploy@2952"].
2. A new revision of baton/replit-deploy kind=handoff marking the baton
   CONSUMED (deploy done, probe results summarized, nothing pending) so the
   next reader doesn't re-deploy.
3. coord_reconcile on dev/mcp-assist-memory; then coord_curate dry_run
   review — coord_curate requires BOTH namespace and session_id (omitting
   session_id silently returns an empty operations list).
Only AFTER probe A passes may any record describe the 7-char defect as fixed
in production. If any probe failed, write deploy/v3-phase0-1-blocked
kind=note instead, with the failing step and raw outputs, and leave the baton
open. If anything about this server surprises you during the run, call
observation_log (small, never patient data) — it is the feedback channel this
server's ergonomics decisions are made from.

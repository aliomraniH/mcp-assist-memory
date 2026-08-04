# Runbook — rotating the Neon role password

**Owner:** operator (Ali + Replit Agent). Not executable by an agent session:
every step touches Neon's console or Replit Deployment Secrets.

**When:** on the scheduled rotation, and immediately whenever a connection
string may have been exposed. One such exposure is outstanding — the production
Neon connection string including its password was pasted in cleartext into a
Replit Agent conversation on 2026-08-03 (validation FINDING-2). Treat that
credential as compromised until this runbook has been completed.

---

## What makes this rotation different from a normal one

This deployment now uses **two** connection strings against the same database,
and they are not interchangeable:

| String | Endpoint | Used by | Why |
|---|---|---|---|
| pooled | host contains `-pooler` | all app traffic (`DATABASE_URL`) | PgBouncer connection pooling; what a serverless-ish workload needs |
| direct | host WITHOUT `-pooler` | the cache-invalidation listener (`DATABASE_URL_DIRECT`) | `LISTEN` does not work through PgBouncer transaction mode |

Rotating the role password invalidates **both**. Updating only `DATABASE_URL`
leaves the listener authenticating with a dead password: the app keeps serving,
the listener dies, and the gate silently falls back to TTL-only cache
invalidation. That failure is quiet by design — the cache is built to survive
it — so it will not page anyone. **Step 6 is how you find out.**

---

## Steps

### 1. Rotate in Neon
Neon console → project → **Roles** → reset the password for the application
role. Neon shows the new password once.

### 2. Capture BOTH connection strings
From the Neon dashboard, copy the connection string twice:

- **Pooled**: the default "Connection string" — host contains `-pooler`.
- **Direct**: toggle the connection-pooling switch OFF — host has no `-pooler`.

They differ only in hostname. Check that character-by-character; a pooled string
pasted into `DATABASE_URL_DIRECT` produces a listener that connects, appears
healthy, and never receives a notification.

### 3. Update Replit **Deployment** Secrets
Set both:

- `DATABASE_URL` → the **pooled** string
- `DATABASE_URL_DIRECT` → the **direct** string

Set these in **Deployment / App Secrets, not the workspace shell.** The workspace
and the deployed VM have different environments. That divergence is exactly how
the `heliumdb` / `neondb` split-brain arose: a correct SQL statement was executed
successfully against a database the deployed server never reads, and the change
appeared to succeed while arming nothing for seven minutes across three checks.

### 4. Redeploy
Secrets are read at process start. Until the Reserved VM restarts, the old
process is still holding connections opened with the old credential.

### 5. Verify DB identity **through the server**
Call the `stats` tool and read `db_identity`:

```
current_database              -> expect the production database name
endpoint_host                 -> expect the POOLED host (app traffic)
boot_connection_fingerprint   -> expect a NEW value (boot_ts changed)
```

Do **not** verify with `psql "$DATABASE_URL"` from the workspace shell. That
shell may resolve to a different database, and it will answer confidently.

Record the new `boot_connection_fingerprint` in the deploy record — it is what
`scripts/deploy_closeout_gate.py --expect-fingerprint` compares against on the
next deploy.

### 6. Verify the listener — the step that is easy to skip
Call the `gate_cache_status` tool:

```
listener_alive: true      -> the DIRECT string is good
listener_alive: false     -> running on TTL fallback; DATABASE_URL_DIRECT is
                             wrong, unset, or pointed at the pooled endpoint
```

`listener_alive: false` is **not** an outage and will not surface as one. Cached
gate inputs still expire on their TTL, so the system stays correct and merely
gets slower to notice a profile change. Nothing else will tell you.

### 7. Confirm Neon compute settings
The listener holds one persistent direct connection (negligible against
`max_connections` 104 at 0.25 CU). If compute **autosuspend** is enabled, the
listener drops whenever the compute suspends. TTL fallback covers it and the
supervisor reconnects, but confirm the autosuspend setting is intentional rather
than discovering it through a puzzling latency profile.

---

## Rollback

Rotation is not reversible — the old password is gone. If the new strings do not
work, re-run step 1 to mint another password and redo steps 2-6. The application
keeps serving from already-open connections until the process restarts, so there
is time to get it right; do not restart the VM until both strings are in hand.

## Post-rotation checklist

- [ ] `stats.db_identity.current_database` is the production database
- [ ] `stats.db_identity.endpoint_host` is the **pooled** host
- [ ] new `boot_connection_fingerprint` recorded in the deploy record
- [ ] `gate_cache_status.listener_alive` is **true**
- [ ] Neon autosuspend setting confirmed intentional
- [ ] the exposed pre-rotation credential is invalid (FINDING-2 closed)

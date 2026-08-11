---
name: GitHub reconciler token sourcing
description: How the coordination reconciler gets a GitHub token without an explicit GITHUB_TOKEN, and why it must be fetched per-window
---

The reconciler needs read GitHub access. Source it in priority order:
1. an explicit `GITHUB_TOKEN` (PAT) — durable, simplest, wins when set;
2. else the **connected GitHub account via the Replit connector proxy**;
3. else disabled → claims reconcile to `unverifiable` (never a wrong `current`).

**Why not snapshot the connector token into a static secret:** Replit's GitHub
connection is **OAuth with a refreshing token** (`access_token` + `expires_in`/
`expires_at` + `refresh_token`). A one-time snapshot into `GITHUB_TOKEN` would
expire within the hour and silently start failing. So the connector path
re-fetches from the proxy per cache-window (window derived from the credential's
own expiry, minus a margin, clamped to a min), which always yields a currently
valid token.

**How to apply / mechanics:**
- Proxy: `GET https://$REPLIT_CONNECTORS_HOSTNAME/api/v2/connection?include_secrets=true&connector_names=github`
  with header `X_REPLIT_TOKEN`. That header is `repl <REPL_IDENTITY>` in the dev
  workspace, or `depl <WEB_REPL_RENEWAL>` in a deployment. If neither identity var
  is present, there is no provider (feature disabled).
- Token lives at `items[0].settings.access_token` (fallback
  `settings.oauth.credentials.access_token`); expiry at `...credentials.expires_in/at`.
- These platform env vars are read ONLY in `config.py` (the single env reader);
  everything else takes them off the settings object.
- Best-effort everywhere: any fetch/API failure returns `None` → `unverifiable`,
  never a blocked memory write and never a false verdict.
- The startup `startup_ok` log's `reconciler` boolean is just
  `build_resolver(settings).enabled`; with the connector vars present it is `true`.
- Quirk seen 2026-08-06: `?connector_names=github` filter returned 0 items while
  the UNFILTERED `/api/v2/connection?include_secrets=true` call returned the
  github item fine — if the filtered call comes back empty, retry unfiltered and
  match on `connector_name` before concluding the connection is missing.
- The workspace `GITHUB_PAT`/`GITHUB_PERSONAL_ACCESS_TOKEN` secrets are stale
  (401 Bad credentials as of 2026-08-06); prefer the connector proxy.
- The connector OAuth token has NO `workflow` scope: PUTs to
  `.github/workflows/*` are refused with 403. Workflow-file changes must be
  pushed by the user (or a PAT with workflow scope).

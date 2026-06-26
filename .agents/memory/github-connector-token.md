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

# mcp-assist-memory

A remote MCP server (Streamable HTTP) that gives Claude a shared memory layer
across surfaces — claude.ai web, Claude Code CLI, and Claude Code Desktop —
so work state survives surface switches. It stores append-only revisioned
memory entries, work-session timelines, cross-surface handoffs, and uploaded
artifacts (with automatic ingestion of debug-capture session ZIPs), all in
SQLite plus a filesystem blob store behind a single `StorageBackend`
interface. It is memory-only by design: no third-party credentials, no
outbound API calls.

Full contract: see [SPEC.md](SPEC.md).

## Environment variables

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `MCP_AUTH_TOKEN` | **yes** | — | Bearer token; server refuses to start without it |
| `DATA_DIR` | no | `./data` | SQLite DB + blob store location (**must be persistent storage**) |
| `MAX_UPLOAD_MB` | no | `25` | Per-upload size cap |
| `MAX_TOTAL_STORAGE_MB` | no | `500` | Global storage cap |
| `PORT` | no | `8000` | HTTP port (Replit sets this automatically) |

## Run locally

```bash
pip install -e ".[dev]"
MCP_AUTH_TOKEN=dev-token python main.py
# health: curl http://localhost:8000/   → {"status":"ok"}
pytest
```

## Deploy on Replit

1. Import this repo into Replit. The included `.replit` makes the **Run**
   button work (`python main.py`).
2. Add a Secret `MCP_AUTH_TOKEN` with a long random value
   (e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
3. Deploy as a **Reserved VM** (recommended): the server is stateful and
   long-running; Autoscale deployments can cold-start and run multiple
   instances, which breaks SQLite assumptions.
4. **⚠️ Persistence caveat:** `DATA_DIR` defaults to `./data` inside the
   workspace. The workspace filesystem persists in the editor but a
   *deployment* gets a fresh copy of the repo on each redeploy — anything
   written at runtime under the deployment's filesystem is lost on redeploy.
   Point `DATA_DIR` at storage that survives redeploys (e.g. a mounted
   persistent disk on the Reserved VM), or accept that a redeploy resets
   memory. Do not commit `data/` to git (it's `.gitignore`d).
5. Your endpoint is `https://<your-repl-url>/mcp`.

## Register the server on each surface

**Claude Code CLI / Desktop:**

```bash
claude mcp add -s user --transport http assist-memory \
  https://<repl-url>/mcp \
  -H "Authorization: Bearer <token>"
```

**claude.ai web:** Settings → Connectors → Add custom connector, with URL
`https://<repl-url>/mcp`. Note: the web connector UI authenticates via
OAuth and does not currently let you attach a custom `Authorization`
header; this server only supports static bearer auth. If your connector
form has an advanced/header field, use `Authorization: Bearer <token>`.
Otherwise web access requires fronting the server with an OAuth-capable
proxy — until then, use the CLI/Desktop registration and `handoff_save` /
`handoff_load` to move state to and from web sessions.

## Tool overview

| Group | Tools |
|---|---|
| Memory | `memory_save`, `memory_get`, `memory_list`, `memory_search`, `memory_history`, `memory_revert`, `memory_delete` |
| Sessions | `session_start`, `session_log`, `session_end`, `session_list`, `session_get` |
| Handoff | `handoff_save`, `handoff_load` |
| Artifacts | `artifact_upload`, `artifact_list`, `artifact_get` (ranged, 1 MB/page) |
| Meta | `server_status` |

Memory is append-only: every write is a new revision, deletes are
tombstones, and `memory_revert` restores by copying — history is never lost.
Uploading a debug-capture ZIP (a `session.json` export with
`schema_version "1.0"`) auto-creates the session record and stores its
`agent-handoff/brief.md` as a queryable memory entry
(`debug/<session_id>/brief`).

## Security

- Every request to `/mcp` requires `Authorization: Bearer $MCP_AUTH_TOKEN`
  (constant-time compare); the only anonymous route is `GET /`.
- ZIP uploads are checked for zip-slip, absolute paths, symlinks, entry
  count (≤ 2000), and decompression bombs (≤ 4 × `MAX_UPLOAD_MB`).
- Values matching common credential patterns are stored but tagged
  `possible-secret` with a warning in the response.

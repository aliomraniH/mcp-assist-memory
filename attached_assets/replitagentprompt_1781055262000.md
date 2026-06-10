# Replit Agent prompt — deploy mcp-assist-memory

Copy everything below the line into Replit Agent after importing this
repository (branch with the latest code).

---

You are deploying an **already-built, already-tested** application. Your job
is configuration, deployment, and verification — **nothing else**.

## What this app is

A remote MCP (Model Context Protocol) server using Streamable HTTP, built
with Python 3.11+ and FastMCP (the official `mcp` SDK). It provides shared
memory, session tracking, handoffs, and artifact storage for AI coding
agents. It will be consumed by several different MCP clients:

- Claude Code CLI and Claude Code Desktop (auth via `Authorization: Bearer`
  header)
- claude.ai web custom connectors (auth via `?token=` query parameter,
  because the web UI can't send custom headers)
- Cursor, Windsurf, and other MCP-compatible agent tools (header auth)

Both auth paths are **already implemented and tested** in the code. The MCP
endpoint is `POST /mcp`; the only anonymous route is `GET /` (health check).
Data lives in SQLite plus a file blob store under `DATA_DIR`.

## Hard rules — do not violate

1. **Do not modify any source code, tests, or `pyproject.toml`.** The code
   is complete; 44 tests pass. If something looks broken, STOP and report
   the exact error instead of "fixing" it.
2. **Do not add features, dependencies, databases, OAuth, GitHub/LLM
   integrations, or analytics.** This server is memory-only by design and
   makes no outbound API calls.
3. **Do not weaken auth.** Never expose an unauthenticated endpoint other
   than `GET /`. Never print, log, or commit the value of `MCP_AUTH_TOKEN`.
4. **Do not commit `data/`, `.env`, or any runtime files.** They are
   gitignored; keep it that way.
5. **Do not change `.replit`** unless the Run button genuinely fails, and
   if so change only what is needed to execute `python main.py`.

## Steps

1. **Install dependencies** for Python 3.11+:
   ```bash
   pip install -e ".[dev]"
   ```

2. **Run the test suite as a deploy gate:**
   ```bash
   pytest
   ```
   Expect **44 passed**. If anything fails, STOP and report the pytest
   output verbatim. Do not deploy and do not edit code.

3. **Configure secrets** (Replit Secrets, not files):
   - `MCP_AUTH_TOKEN`: generate one with
     `python -c "import secrets; print(secrets.token_urlsafe(32))"` and
     store it as a Secret. Show the user only an instruction for where to
     read it (the Secrets pane) — do not echo it into chat, logs, or files.
   - Optional overrides: `DATA_DIR` (default `./data`), `MAX_UPLOAD_MB`
     (default 25), `MAX_TOTAL_STORAGE_MB` (default 500), `LOG_LEVEL`
     (default INFO). `PORT` is provided by Replit automatically.

4. **Persistence (important):** the server's value is durable memory.
   Deploy as a **Reserved VM** (the app is stateful and long-running;
   Autoscale would cold-start and could run multiple instances against one
   SQLite file, which is unsupported). Point `DATA_DIR` at storage that
   survives redeploys; if the Reserved VM's disk is reset on redeploy, tell
   the user explicitly that memory resets on redeploy and which directory
   to back up. Do not silently ignore this step.

5. **Deploy**, then **verify** all three of these against the public URL
   (replace `$URL` and use the real token from Secrets):
   ```bash
   # 1. health check is open and bare:
   curl -s $URL/                      # expect {"status":"ok"}

   # 2. MCP endpoint rejects anonymous requests:
   curl -s -o /dev/null -w '%{http_code}' -X POST $URL/mcp   # expect 401

   # 3. authenticated MCP initialize succeeds (expect a JSON-RPC result
   #    with serverInfo.name "assist-memory"):
   curl -s -X POST "$URL/mcp" \
     -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"verify","version":"0"}}}'

   # 4. query-param auth also works (for claude.ai web connectors):
   #    repeat check 3 against "$URL/mcp?token=$MCP_AUTH_TOKEN" without
   #    the Authorization header — expect the same result.
   ```
   If any check fails, report the response body and status code and stop.

6. **Report back** with exactly:
   - the public base URL and the MCP endpoint URL (`https://.../mcp`)
   - confirmation that all four verification checks passed
   - where the token is stored (Secrets pane), without revealing it
   - the persistence situation from step 4 (where `DATA_DIR` lives and
     whether it survives redeploys)
   - this registration cheat-sheet for the user, with the real URL filled
     in and `<token>` left as a placeholder:

     **Claude Code CLI / Desktop:**
     ```
     claude mcp add -s user --transport http assist-memory https://<url>/mcp -H "Authorization: Bearer <token>"
     ```
     **claude.ai web:** Settings → Connectors → Add custom connector →
     URL: `https://<url>/mcp?token=<token>`

     **Cursor** (`~/.cursor/mcp.json`):
     ```json
     {"mcpServers": {"assist-memory": {"url": "https://<url>/mcp", "headers": {"Authorization": "Bearer <token>"}}}}
     ```
     **Other MCP clients:** streamable-http transport to `https://<url>/mcp`
     with the bearer header, or `?token=<token>` if headers aren't supported.

## Success criteria

Run button works; tests pass (44/44); the four endpoint checks pass on the
deployed URL; the user has the registration cheat-sheet; no source files
were modified.

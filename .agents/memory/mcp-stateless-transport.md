---
name: MCP transport must be stateless
description: Why the FastMCP /mcp app runs with stateless_http=True, not the default stateful mode.
---

# Run the MCP app stateless (`http_app(path="/", stateless_http=True)`)

The MCP ASGI app is mounted **stateless**. Each request is self-contained; there
is no in-memory MCP session manager holding per-session state.

**Why:** the default stateful mode keeps sessions in process memory. On a single
Reserved VM every restart/redeploy wipes that memory, so every connected client
(claude.ai web, Claude Desktop, Claude Code CLI) loses its session and must
re-initialize — surfacing as flaky "session" failures. Stateless removes that
affinity entirely: sessions survive restarts/redeploys and the surfaces share no
server-side session state.

**How to apply:** keep `stateless_http=True`. Do NOT revert to `http_app(path="/")`
to "fix" a session bug — that REINTRODUCES the restart-fragility. The lifespan
still runs the FastMCP session manager and must be entered while serving. If you
ever need true resumable sessions, that requires a shared/persistent session
store, not in-memory stateful mode on a single VM.

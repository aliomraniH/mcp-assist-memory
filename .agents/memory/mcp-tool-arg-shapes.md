---
name: MCP tool arg shapes (v3 server)
description: Non-obvious argument constraints on this project's MCP tools that cause validation errors.
---
- `memory_save` has NO top-level `session_id` argument — pass it inside `meta`. **Why:** pydantic rejects unexpected kwargs; discovered during deploy closeout. **How to apply:** any scripted save that should carry a session ref goes in `meta.session_id`.
- `observation_log` requires `category` from a fixed enum: ergonomics | error_recovery | advisory | screening | docs_gap | surprise | suggestion; severity ∈ blocker | friction | note.
- `memory_get` for a missing key returns `content: []` with `structuredContent.result: null` and `isError:false` — parse for empty content, not an error.

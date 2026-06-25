---
name: dashboard e2e test vs real secrets
description: Why test_admin_login_rotate_and_mcp_auth fails in dev containers that have real ADMIN_PASSWORD/MCP_AUTH_TOKEN set
---

The DB-gated e2e test `tests/test_dashboard.py::test_admin_login_rotate_and_mcp_auth`
seeds its expected credentials with `os.environ.setdefault("ADMIN_PASSWORD", ...)` /
`setdefault("MCP_AUTH_TOKEN", ...)`.

**Gotcha:** `setdefault` is a no-op when the variable is already present. Any environment
that injects the *real* `ADMIN_PASSWORD` / `MCP_AUTH_TOKEN` secrets (e.g. this Replit dev
container) overrides the test values, so login with the hardcoded `test-admin-pw` returns
200 ("Incorrect") instead of 303 and the test fails.

**Why it matters:** This failure is environment-specific, NOT a code regression. It is
expected to pass only when those secrets are unset (clean CI / deploy gate).

**How to apply:** When running the suite locally with real secrets present, deselect that
one test (`--deselect tests/test_dashboard.py::test_admin_login_rotate_and_mcp_auth`) or
temporarily unset the secrets. Do not "fix" it by changing the hardcoded password.

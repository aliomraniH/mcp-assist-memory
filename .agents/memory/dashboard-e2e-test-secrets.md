---
name: dashboard e2e test vs real secrets
description: How test_admin_login_rotate_and_mcp_auth obtains the admin password so it is robust to any ADMIN_PASSWORD value
---

The DB-gated e2e test `tests/test_dashboard.py::test_admin_login_rotate_and_mcp_auth`
seeds default credentials with `os.environ.setdefault("ADMIN_PASSWORD", ...)` /
`setdefault("MCP_AUTH_TOKEN", ...)`.

**Resolved fragility:** the test used to *hardcode* posting the literal `test-admin-pw`
on the login step while only `setdefault`-ing it. Because `setdefault` is a no-op when the
var is already present, any environment that injects a *different* `ADMIN_PASSWORD` — this
Replit dev container's real secret AND CI's `ADMIN_PASSWORD=ci-admin-pw` — made the login
return 200 ("Incorrect") instead of 303, so the test failed (CI was red on it).

**Fix (in code):** the test now reads `ADMIN_PW = os.environ["ADMIN_PASSWORD"]` (after the
`setdefault`) and posts that, so the login submits the same value the app gates on. It now
passes regardless of whether `ADMIN_PASSWORD` is unset (default), the workspace secret, or
the CI value. The wrong-password probe still posts the literal `nope` (a safe negative).

**Why:** the app's admin password comes from `config.settings.admin_password` (read from
the same `ADMIN_PASSWORD` env); the test must post the effective env value, never a literal.

**How to apply:** if a similar credential-gated e2e test goes red only in some
environments, check for a hardcoded literal that should instead be read from the env the
app config consumes — don't deselect the test or unset secrets to "pass" it.

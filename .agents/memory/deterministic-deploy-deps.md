---
name: Deterministic deploy dependencies
description: Why constraints.txt exists and the rule for keeping prod == dev-verified deps.
---

# Deterministic deploy dependencies

`pyproject.toml` intentionally keeps loose ranges (fastapi>=…, uvicorn>=…, etc.).
`constraints.txt` pins the FULL transitive closure to the verified-good dev env and
is passed via `-c constraints.txt` on EVERY install path: the `.replit` deploy build,
`post-merge.sh`, the Makefile `install` target, and CI (`.github/workflows/test.yml`).

**Why:** with no lock, each `pip install -e .` (deploy included) was free to resolve
newer versions than dev verified — that is exactly how prod `/mcp` returned a bare 421
(an unpinned build pulled fastmcp 3.4.3's HostOriginGuard). A constraints file only
caps versions of packages that actually get installed, so extra lines are harmless and
it never forces an install.

**How to apply:**
- Never hand-edit versions in `constraints.txt`. After an intentional, verified upgrade
  (`pip install -U <pkg>` → `make test` + exercise `/mcp`), run `make lock`
  (`scripts/lock-deps.sh`) to regenerate; the script preserves the header block.
- Adding a NEW direct dep: add it to `pyproject.toml`, install, verify, then `make lock`.
- If you add a new install path, wire `-c constraints.txt` into it too, or drift returns.
- `.replit` is platform-protected — change the deploy build with `deployConfig()`, and
  remind the user to publish from main after the task merges for it to take effect.

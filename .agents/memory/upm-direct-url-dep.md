---
name: Replit deploy locker vs PEP 508 direct-URL deps
description: Why en_core_web_sm (or any direct wheel URL) must not be in [project.dependencies]
---

Rule: never put a PEP 508 direct-URL dependency (`pkg @ https://...whl`) in
`pyproject.toml [project.dependencies]`.

**Why:** Replit's deploy-time package step (upm running `uv lock`) segfaults
(Go nil-pointer panic) on direct references, killing the build before the
custom build command runs.

**How to apply:** install such wheels via an explicit
`pip install --no-deps '<pkg> @ <url>'` step in EVERY install path — the
`.replit` `[deployment] build` command, Makefile, post-merge.sh, and CI —
and keep the version pinned in constraints.txt for lockstep. Note the
intent-features loader deliberately fails OPEN (extractor="unavailable")
when the model is missing; don't "fix" that to a hard fail.
`allow-direct-references` in hatch metadata is then unnecessary.

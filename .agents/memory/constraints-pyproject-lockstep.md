---
name: constraints.txt / pyproject pin lockstep
description: Why a pyproject version bump alone breaks post-merge, and how to fix it
---

Deploy installs run `pip install -c constraints.txt -e .`. `constraints.txt` pins the
full transitive closure; `pyproject.toml` declares the required versions. If ONE side
bumps a pinned dep and the other doesn't, pip fails with `ResolutionImpossible`
("mcp-assist-memory depends on X==NEW" vs "user requested (constraint) X==OLD").

**Why:** these two files are maintained by separate tasks/commits. A pyproject bump
(e.g. fastmcp 3.4.2→3.4.3) that lands without regenerating constraints.txt leaves the
constraint capping the OLD version, and post-merge setup dies before installing.

**How to apply:** any change to a pinned dependency in pyproject.toml must regenerate
constraints.txt in the SAME change. To fix drift: `pip install -e .` (resolves the new
pins), then `bash scripts/lock-deps.sh` (or `make lock`) to refreeze the closure while
preserving the header, then confirm `pip install -c constraints.txt -e .` exits 0.

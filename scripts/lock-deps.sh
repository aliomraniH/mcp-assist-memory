#!/bin/bash
# Regenerate constraints.txt from the CURRENT (verified-good) dev environment.
#
# Use this after you have intentionally upgraded a dependency and verified it:
#   1. pip install -U <pkg>        # or `pip install -e .` to re-resolve
#   2. make test                   # AND exercise /mcp locally
#   3. ./scripts/lock-deps.sh      # regenerate the pins
#   4. redeploy                    # prod now installs the newly verified set
#
# The header block is preserved; only the version pins below it are refreshed.
set -euo pipefail

cd "$(dirname "$0")/.."

HEADER_END='# ---------------------------------------------------------------------------'

# Keep everything up to and including the SECOND header rule line.
awk -v rule="$HEADER_END" '
  $0 == rule { count++; print; if (count == 2) exit; next }
  { print }
' constraints.txt > constraints.txt.new

# Append the freshly frozen, sorted transitive closure (minus the editable self).
pip freeze | grep -v "mcp-assist-memory" | grep -viE "^-e " | sort >> constraints.txt.new

mv constraints.txt.new constraints.txt
echo "constraints.txt regenerated from the current environment."

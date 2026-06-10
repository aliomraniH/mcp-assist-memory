"""Builds tests/fixtures/debug_capture_session.zip from the real debug-capture
schema (schema_version 1.0). Run directly to regenerate the committed fixture."""

import json
import zipfile
from pathlib import Path

FIXTURE_SESSION_ID = "2026-06-09T14-30-00Z_canvas-debug"

SESSION_JSON = {
    "schema_version": "1.0",
    "session_id": FIXTURE_SESSION_ID,
    "mode": ["capture", "verify"],
    "metadata": {
        "created_at": "2026-06-09T14:30:00Z",
        "ended_at": "2026-06-09T15:05:42Z",
        "tool": "debug-capture",
        "claude_surface": "cli",
        "operator": "alo",
    },
    "context": {
        "project": "canvas-growth-charts",
        "plugin_version": "0.4.2",
        "target_url": "http://localhost:3000/charts",
        "git_branch": "feat/percentile-bands",
        "git_commit": "a1b2c3d",
    },
    "results": {
        "passed": 11,
        "failed": 2,
        "warnings": 1,
        "errors": [
            "TypeError: bands is undefined at PercentileLayer.render",
            "console: 404 /api/percentiles?age=0",
        ],
        "summary": "11 passed, 2 failed: percentile bands fail to render for age 0",
    },
    "artifacts": {
        "screenshots": ["screenshots/chart-age-0.png"],
        "console_log": "logs/console.log",
        "network_har": "logs/network.har",
    },
    "agent_handoff": {
        "brief": "agent-handoff/brief.md",
        "deploy_script": "agent-handoff/deploy.sh",
        "rerun_command": "npx debug-capture --rerun 2026-06-09T14-30-00Z_canvas-debug",
        "suggested_actions": [
            "fix PercentileLayer.render undefined bands for age 0",
            "add /api/percentiles age=0 fixture",
        ],
        "ready_for_agent": True,
    },
}

BRIEF_MD = """# Debug brief: canvas-growth-charts percentile bands

11 checks passed, 2 failed. Percentile bands fail to render when age == 0:
`PercentileLayer.render` receives `bands === undefined` because
`/api/percentiles?age=0` returns 404.

Suggested fix order:
1. Add the age=0 row to the percentiles fixture/API.
2. Guard `PercentileLayer.render` against missing bands.

Rerun: `npx debug-capture --rerun 2026-06-09T14-30-00Z_canvas-debug`
"""


def build(path: Path) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("session.json", json.dumps(SESSION_JSON, indent=2))
        zf.writestr("agent-handoff/brief.md", BRIEF_MD)
        zf.writestr("agent-handoff/deploy.sh", "#!/bin/sh\necho redeploy\n")
        zf.writestr("logs/console.log", "404 /api/percentiles?age=0\n")
    return path


if __name__ == "__main__":
    out = build(Path(__file__).parent / "debug_capture_session.zip")
    print(f"wrote {out}")

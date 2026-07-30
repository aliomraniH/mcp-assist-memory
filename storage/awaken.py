"""GitHub awakening — the gate's lazily-awakened extended-context provider.

Resource doctrine (charter §3, S6): GitHub is NOT a gate dependency. Tier 0/1
run on Postgres + pgvector alone (module boundary: storage/gate.py never
imports this module — PostgresBackend injects ``awaken`` as the backend's
``_gate_awaken`` hook, and the gate calls it only when BOTH hold: the intent/
payload is coding-classified (deterministic ref extraction, no LLM) AND a
gate-relevant claim's verdict is expired past claim_staleness_hours).

Shape copied from the R5 ``_stale_pin_advisory`` hook (storage/postgres.py):
hard 2-second budget, never blocks the write, degrades to the stored verdicts
with a ``stale_context`` flag — a gate that requires GitHub to answer fails
closed on every resolver outage (the 2026-07-16 incident class).

Every awakening is telemetry-counted: the in-process ``gate_awaken_count``
AND a tool_events row (tool='gate_awaken') so GH-1/GH-4 can assert the zero-
call boundary mechanically, not by absence of errors.
"""
from __future__ import annotations

import asyncio

import structlog

log = structlog.get_logger("assist-memory.gate.awaken")

AWAKEN_BUDGET_S = 2.0


async def awaken(backend, namespace: str, targets: list[dict]) -> dict:
    """Resolve fresh external context for at most one stale target under the
    hard budget. Returns {status: ok|timeout|unresolved, resolved?} — the
    result is STAMPED into the gate response, never written as a verdict (the
    gate must not become its own stale authority, S7)."""
    backend.gate_awaken_count += 1
    status = "unresolved"
    resolved: dict | None = None
    resolver = getattr(backend, "resolver", None)
    try:
        if resolver is None or not resolver.enabled:
            status = "unresolved"
        else:
            async with asyncio.timeout(AWAKEN_BUDGET_S):
                for t in targets:
                    repo = t.get("repo")
                    if not repo:
                        continue
                    if t.get("branch"):
                        head = await resolver.branch_head(repo, t["branch"])
                        if head:
                            status = "ok"
                            resolved = {"repo": repo, "branch": t["branch"],
                                        "head": head}
                            break
                    elif t.get("pr") is not None:
                        state = await resolver.merged_state(repo, int(t["pr"]))
                        if state is not None:
                            status = "ok"
                            resolved = {"repo": repo, "pr": t["pr"], **state}
                            break
    except (TimeoutError, asyncio.TimeoutError):
        status = "timeout"
    except Exception as exc:  # noqa: BLE001 - degrade to advisory, never block
        log.warning("gate_awaken_failed", error=type(exc).__name__)
        status = "unresolved"
    out: dict = {"status": status, "budget_s": AWAKEN_BUDGET_S}
    if resolved is not None:
        out["resolved"] = resolved
    try:
        await backend.record_tool_event(
            tool="gate_awaken", args={"namespace": namespace},
            outcome="ok" if status == "ok" else "error",
            error_code=None if status == "ok" else f"awaken_{status}")
    except Exception as exc:  # noqa: BLE001 - telemetry never fails the call
        log.warning("gate_awaken_telemetry_failed", error=str(exc))
    return out

"""Intent Gate v1 — Tier 0 deterministic pre-flight + Tier 1 similarity critic.

Charter: claude/intent-gate/INTENT_GATE_CHARTER.md. The memory server stops
being a passive store and becomes an active critic: every mutating call in a
gated namespace is contextualized against the store's own memory, project
metadata, and efficacy history BEFORE execution.

Rollout: per-namespace via variant_profiles (``intent_gate: "on"``) — the
codebase's own staged-rollout mechanism (S4). Within a gated namespace Tier 0
runs on every mutating call with no opt-out; the default profile is "off" so
old acks stay byte-identical (additive-schema constraint 0.5).

MODULE BOUNDARY (S6, enforced by tests/test_gate_awaken.py): this module never
talks to GitHub — no resolver, no reconcile import, no httpx. Tier 0/1 run on
Postgres + pgvector alone. Extended context arrives only through the awakening
hook (see storage/awaken.py) that PostgresBackend injects as ``_gate_awaken``
and that runs solely from the dependency-freshness step — budgeted,
non-blocking, advisory.

Verdict vocabulary (in-band, never a protocol error):
gate_approved | gate_preview | gate_conflict | gate_clarify | gate_blocked.
Blocks raise ``AppError`` (isError:true at the tool layer) with the standard
{code, message, remedy, retryable} taxonomy; the gate verdict rides in
``context.gate`` and always names its rule.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from config import settings
from errors import AppError
from storage.idempotency import idem_fingerprint
from storage.phi import text_looks_identifying
from storage.sanitize import sanitize, wrap_value
from storage.screening import screen_value

GATE_APPROVED = "gate_approved"
GATE_PREVIEW = "gate_preview"
GATE_CONFLICT = "gate_conflict"
GATE_CLARIFY = "gate_clarify"
GATE_BLOCKED = "gate_blocked"

# meta keys that steer the gate itself — stripped before fingerprinting so an
# operator override retry still matches the blocked attempt ("retried
# unchanged" in the closure contract, G2-4).
GATE_CONTROL_META_KEYS = ("gate_override",)

# Two-phase confirm tokens are single-use and short-lived.
CONFIRM_TTL_S = 900

# Skill discipline (S7): only curator-provenanced, in-window skills can
# contribute to gate_conflict; expired/unprovenanced skills advise only.
SKILL_PREFIX = "skill/"
DEFAULT_SKILL_VALIDITY_HOURS = 720.0

# Tier-1 retrieval floor — no fabricated matches (G1-5): a semantic candidate
# below this cosine similarity is dropped, and the trigger-overlap leg needs
# at least TRIGGER_OVERLAP_MIN shared content tokens.
SIMILARITY_FLOOR = 0.25
TRIGGER_OVERLAP_MIN = 2

# The gate's own house band + machine writers are never themselves gated —
# otherwise a ledger write would recurse through the gate.
EXEMPT_KEY_PREFIXES = ("gate/", "coord/", "_meta/")
EXEMPT_ACTORS = ("gate",)

# Compact gate block budget (MD-2): <= 200 bytes serialized; matched keys are
# truncated structurally, never the decision or flags.
GATE_BLOCK_MAX_MATCHED = 3

_STOPWORDS = frozenset(
    "the a an and or for of in on to by with this that is are be as at it its "
    "from into new my our your".split())

# Deterministic ref extraction — the coding classifier (S6) and the structured-
# contradiction field source. NO LLM: presence of repo/branch/pr/sha fields or
# refs decides.
_NAMED_BRANCH_RX = re.compile(r"\b(main|master|develop|trunk)\b", re.I)
_SLASHY_BRANCH_RX = re.compile(
    r"\b(?:feat|feature|fix|bugfix|hotfix|release|chore|claude)/[A-Za-z0-9._/-]+")
_SHA_RX = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
_PR_RX = re.compile(r"(?:\bpr\s*#?|\bpull\s+request\s+#?|#)(\d+)\b", re.I)


def extract_refs(*texts: str | None, meta: dict | None = None) -> dict:
    """Deterministic repo/branch/pr/sha extraction from intent text + payload
    meta. ``coding`` is True iff any ref is present — the ONLY thing that can
    classify an operation coding-related (never an LLM)."""
    branches: list[str] = []
    shas: list[str] = []
    prs: list[str] = []
    repos: list[str] = []
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        branches += [m.group(0) for m in _NAMED_BRANCH_RX.finditer(text)]
        branches += _SLASHY_BRANCH_RX.findall(text)
        shas += _SHA_RX.findall(text)
        prs += _PR_RX.findall(text)
    if isinstance(meta, dict):
        for k in ("repo",):
            if meta.get(k):
                repos.append(str(meta[k]))
        for k in ("branch",):
            if meta.get(k):
                branches.append(str(meta[k]))
        for k in ("repo_sha", "base_sha", "sha"):
            if meta.get(k):
                shas.append(str(meta[k]))
        if meta.get("pr") is not None:
            prs.append(str(meta["pr"]))
    out = {
        "branches": sorted(set(branches)),
        "repos": sorted(set(repos)),
        "prs": sorted(set(prs)),
        "shas": sorted(set(shas)),
    }
    out["coding"] = any(out[k] for k in ("branches", "repos", "prs", "shas"))
    return out


def gate_fingerprint(*, tool: str, namespace: str, key: str, kind: str,
                     payload: Any, meta: dict | None) -> str:
    """The operation's identity for confirm-token round-trips and outcome
    closure. Same JCS boundary fingerprint as idempotency, with gate-control
    meta keys stripped so an override retry still matches."""
    if isinstance(meta, dict):
        meta = {k: v for k, v in meta.items() if k not in GATE_CONTROL_META_KEYS} or None
    else:
        meta = None
    return idem_fingerprint(tool=tool, namespace=namespace, key=key, kind=kind,
                            payload=payload, meta=meta)


def content_tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return {t for t in re.findall(r"\w+", text.lower()) if t not in _STOPWORDS}


def intent_hash(goal: str) -> str:
    return hashlib.sha256(goal.encode("utf-8")).hexdigest()


def enabled_for(profile: dict, *, key: str, actor: str | None) -> bool:
    """Whether this write is gated: the namespace opted in AND the write is not
    the gate's own house band / a machine writer's coordination record."""
    if profile.get("intent_gate") != "on":
        return False
    if (actor or "unattributed") in EXEMPT_ACTORS:
        return False
    return not str(key).startswith(EXEMPT_KEY_PREFIXES)


@dataclass
class GateContext:
    """Outcome of Tier-0/1 pre-flight for one mutating call."""

    tool: str
    key: str
    tier: int = 0
    decision: str = GATE_APPROVED
    flags: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)
    fingerprint: str | None = None
    open_block_id: int | None = None
    open_block_skill: str | None = None
    session_id: str | None = None
    response: dict | None = None       # short-circuit (preview/conflict): not persisted
    latency_ms: int = 0

    def gate_block(self) -> dict:
        """The compact ack block — {tier, decision, matched, flags}, <= 200
        bytes compact JSON (MD-2). Matched keys truncate structurally."""
        matched = self.matched[:GATE_BLOCK_MAX_MATCHED]
        overflow = len(self.matched) - len(matched)
        if overflow > 0:
            matched = matched + [f"+{overflow}"]
        block = {"tier": self.tier, "decision": self.decision,
                 "matched": matched, "flags": self.flags[:6]}
        # hard budget: drop matched keys (never decision/flags) until it fits
        while len(json.dumps(block, separators=(",", ":"))) > 200 and block["matched"]:
            block["matched"] = block["matched"][:-1]
        return block


# --------------------------------------------------------------------------
# small SQL helpers (Postgres-only; every query is namespace-scoped)
# --------------------------------------------------------------------------
async def _latest_row(backend, namespace: str, key: str) -> dict | None:
    async with backend.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT * FROM memory_entry WHERE namespace = %s AND key = %s "
            "ORDER BY revision DESC LIMIT 1", (namespace, key))
        return await cur.fetchone()


async def _store_confirm(backend, namespace: str, tool: str, key: str,
                         fingerprint: str, preview: dict) -> tuple[str, str]:
    token = str(uuid.uuid4())
    async with backend.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "INSERT INTO gate_pending (token, namespace, tool, key, "
            "args_fingerprint, preview, expires_at) VALUES (%s, %s, %s, %s, %s, %s, "
            "now() + make_interval(secs => %s)) RETURNING expires_at",
            (token, namespace, tool, key, fingerprint,
             Jsonb(_json_safe(preview)), CONFIRM_TTL_S))
        row = await cur.fetchone()
    return token, row["expires_at"].isoformat()


async def _consume_confirm(backend, namespace: str, token: str) -> dict | None:
    try:
        uuid.UUID(token)
    except (ValueError, AttributeError, TypeError):
        return None
    async with backend.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "UPDATE gate_pending SET consumed_at = now() "
            "WHERE token = %s AND namespace = %s AND consumed_at IS NULL "
            "AND expires_at > now() RETURNING *", (token, namespace))
        return await cur.fetchone()


async def _open_block(backend, namespace: str, fingerprint: str) -> dict | None:
    async with backend.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT * FROM gate_block_log WHERE namespace = %s AND fingerprint = %s "
            "AND closed IS NULL ORDER BY id DESC LIMIT 1", (namespace, fingerprint))
        return await cur.fetchone()


async def _record_block(backend, namespace: str, fingerprint: str, rule: str,
                        skill_key: str | None = None) -> None:
    async with backend.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO gate_block_log (namespace, fingerprint, rule, skill_key) "
            "VALUES (%s, %s, %s, %s)", (namespace, fingerprint, rule, skill_key))


async def _close_block(backend, namespace: str, block_id: int, closure: str) -> None:
    async with backend.pool.connection() as conn:
        await conn.execute(
            "UPDATE gate_block_log SET closed = %s, closed_at = now() "
            "WHERE id = %s AND namespace = %s AND closed IS NULL",
            (closure, block_id, namespace))


async def _load_intent(backend, namespace: str, session_id: str | None) -> dict | None:
    if not session_id:
        return None
    async with backend.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT * FROM gate_intent WHERE namespace = %s AND session_id = %s "
            "ORDER BY id DESC LIMIT 1", (namespace, str(session_id)))
        return await cur.fetchone()


def _json_safe(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


# --------------------------------------------------------------------------
# Tier 0 — deterministic pre-flight
# --------------------------------------------------------------------------
async def preflight(
    backend, *, namespace: str, profile: dict, tool: str, key: str, kind: str,
    value: Any, meta: dict | None, actor: str, event_id: str | None,
    preview_requested: bool = False, confirm_token: str | None = None,
) -> GateContext:
    """Run the Tier-0 pre-flight for one mutating call. Returns a GateContext:
    ``response`` set means short-circuit (nothing persisted — preview or
    conflict); otherwise the caller proceeds to the write and then calls
    ``on_write_success`` / ``on_write_error``. Raises AppError for blocks."""
    t0 = time.monotonic()
    ctx = GateContext(tool=tool, key=key)
    meta = meta if isinstance(meta, dict) else None
    ctx.session_id = str(meta["session_id"]) if meta and meta.get("session_id") else None
    actor = actor or "unattributed"
    override = bool(meta and meta.get("gate_override") and actor != "unattributed")
    destructive = tool == "memory_delete"
    sanitized = sanitize(value)
    ctx.fingerprint = gate_fingerprint(
        tool=tool, namespace=namespace, key=key, kind=kind,
        payload=sanitized, meta=meta)

    open_block = await _open_block(backend, namespace, ctx.fingerprint)
    if open_block:
        ctx.open_block_id = open_block["id"]
        ctx.open_block_skill = open_block.get("skill_key")

    # (1) confirm-token path: the caller is executing a previously previewed
    # operation. Validate token + byte-identical args, then proceed directly.
    if confirm_token:
        row = await _consume_confirm(backend, namespace, confirm_token)
        if row is None:
            await _record_block(backend, namespace, ctx.fingerprint, "confirm_invalid")
            _raise_block(ctx, "confirm_invalid",
                         "confirm_token is unknown, expired, already used, or "
                         "belongs to another namespace")
        if row["args_fingerprint"] != ctx.fingerprint or row["key"] != key or row["tool"] != tool:
            await _record_block(backend, namespace, ctx.fingerprint, "confirm_mismatch")
            _raise_block(ctx, "confirm_mismatch",
                         "confirm_token does not match this operation — the "
                         "confirmed call must round-trip the previewed args "
                         "byte-identically")
        ctx.flags.append("confirmed")
        ctx.latency_ms = int((time.monotonic() - t0) * 1000)
        ctx.detail["latency_ms"] = ctx.latency_ms
        return ctx

    # (2) idempotency fingerprint check (Tier-0 "fingerprint check"): the same
    # event_id with a different payload is answered as a gate_blocked verdict
    # carrying the existing idempotency_conflict taxonomy (code wins — the
    # charter's verdict vocabulary rides in context.gate).
    if event_id:
        existing = await _seen_event(backend, namespace, actor, event_id)
        if existing is not None:
            stored = existing.get("idem_fingerprint")
            incoming = idem_fingerprint(tool=tool, namespace=namespace, key=key,
                                        kind=kind, payload=sanitized, meta=meta)
            if stored and stored != incoming:
                if ctx.open_block_id is not None:
                    # outcome closure (G2-4): the identical attempt failed
                    # again for a real reason — the earlier block was right.
                    await _close_block(backend, namespace, ctx.open_block_id,
                                       "confirmed_correct")
                    if ctx.open_block_skill:
                        await bump_skill_efficacy(backend, namespace,
                                                  ctx.open_block_skill,
                                                  "prevented_error")
                    await _ledger(backend, namespace, ctx, decision=GATE_BLOCKED,
                                  rule="idempotency_conflict",
                                  closure="confirmed_correct")
                else:
                    await _record_block(backend, namespace, ctx.fingerprint,
                                        "idempotency_conflict")
                    await _ledger(backend, namespace, ctx, decision=GATE_BLOCKED,
                                  rule="idempotency_conflict")
                raise AppError(
                    "idempotency_conflict",
                    f"event_id {event_id} was already used for a DIFFERENT "
                    f"payload (stored fingerprint {stored[:12]}…, this request "
                    f"{incoming[:12]}…)",
                    gate={"tier": 0, "decision": GATE_BLOCKED,
                          "rule": "idempotency_conflict"},
                )
            # byte-identical replay: let the write path answer with the
            # canonical dedup ack; no ledger increment (MD-3).
            ctx.flags.append("deduplicated_replay")
            ctx.detail["replay"] = True

    # (3) quarantine screen, pre-persist (G0-3): pattern names only.
    hits = screen_value(sanitized)
    screening_override = bool(meta and meta.get("screening_override") and actor != "unattributed")
    will_quarantine = bool(hits) and not screening_override

    # (4) dependency freshness (G0-4): expired verdicts on depended-on claims
    # flag stale_context + age_hours — advisory, never a block.
    stale_deps = await _stale_dependencies(backend, namespace, meta, profile)
    if stale_deps:
        ctx.flags.append("stale_context")
        ctx.detail["stale_dependencies"] = stale_deps
        ctx.matched += [d["key"] for d in stale_deps]
        # GitHub awakening (S6): coding-related AND expired verdict — the hook
        # is injected by the backend (module boundary), 2s budget, advisory.
        refs = extract_refs(sanitized if isinstance(sanitized, str) else None, meta=meta)
        awaken_cb = getattr(backend, "_gate_awaken", None)
        if refs["coding"] and awaken_cb is not None:
            targets = await _awaken_targets(backend, namespace, stale_deps, meta)
            ctx.detail["awaken"] = await awaken_cb(namespace, targets)

    # (5) intent linkage: mismatch + unresolved-conflict state (ADV-4, VIGIL).
    intent = await _load_intent(backend, namespace, ctx.session_id)
    mismatch = bool(intent and intent.get("scope") and tool not in intent["scope"])
    if mismatch:
        ctx.flags.append("intent_mismatch")
    unresolved_conflict = bool(intent and intent.get("decision") == GATE_CONFLICT)

    # (6) operator override: executes AND is counted — never a silent bypass.
    if override:
        ctx.flags.append("gate_override")
        ctx.latency_ms = int((time.monotonic() - t0) * 1000)
        ctx.detail["latency_ms"] = ctx.latency_ms
        return ctx

    # (7) Tier-2 escalation / deterministic block (S3 triggers ONLY:
    # destructive op + unresolved Tier-1 contradiction).
    if destructive and unresolved_conflict:
        reasoner = getattr(backend, "gate_reasoner", None)
        if profile.get("tier2") == "on" and reasoner is not None and reasoner.enabled:
            ctx.tier = 2
            envelope = _tier2_envelope(namespace, profile, tool, key, kind, intent)
            out = await _evaluate_tier2(reasoner, envelope)
            ctx.detail["tier2"] = out
            if out["status"] == "ok" and out.get("decision") == "approve":
                ctx.flags.append("tier2_approved")
            elif out["status"] == "ok" and out.get("decision") == "conflict":
                ctx.decision = GATE_CONFLICT
                clarify = out.get("clarify") or "Tier-2 found the operation in conflict — clarify the intent."
                ctx.response = _conflict_response(
                    namespace, key, tool, ctx,
                    conflict=intent.get("conflict"), clarify=clarify)
                await _ledger(backend, namespace, ctx, decision=GATE_CONFLICT,
                              rule="tier2_conflict")
                await _record_block(backend, namespace, ctx.fingerprint,
                                    "tier2_conflict")
                return ctx
            else:
                # degrade distinctly: unavailable is never an unexplained block
                ctx.flags.append("tier2_unavailable")
        else:
            await _record_block(backend, namespace, ctx.fingerprint,
                                "unresolved_conflict_destructive",
                                skill_key=(intent.get("conflict") or {}).get("skill_key")
                                if intent.get("conflict") else None)
            await _ledger(backend, namespace, ctx, decision=GATE_BLOCKED,
                          rule="unresolved_conflict_destructive")
            _raise_block(
                ctx, "unresolved_conflict_destructive",
                f"destructive call {tool} on {key!r} while the session's declared "
                "intent has an unresolved gate_conflict — resolve the conflict "
                "(intent_open a clarified goal) or have an operator retry with "
                "meta.gate_override")

    # (8) two-phase preview: requested, or forced for destructive/mismatched
    # calls (G0-1, G0-5, ADV-4).
    if preview_requested or destructive or mismatch:
        ctx.decision = GATE_PREVIEW
        preview = await _build_preview(backend, namespace, key, kind, sanitized,
                                       hits, will_quarantine, destructive)
        token, expires = await _store_confirm(
            backend, namespace, tool, key, ctx.fingerprint, preview)
        ctx.latency_ms = int((time.monotonic() - t0) * 1000)
        ctx.detail["latency_ms"] = ctx.latency_ms
        ctx.response = {
            "status": GATE_PREVIEW,
            "namespace": namespace, "key": key, "tool": tool,
            "decision": GATE_PREVIEW, "persisted": False,
            "preview": preview,
            "confirm_token": token, "confirm_expires_at": expires,
            "gate": ctx.gate_block(), "gate_detail": dict(ctx.detail),
            "summary": (f"PREVIEW: {tool} {key} — nothing persisted; repeat the "
                        f"call with confirm_token within {CONFIRM_TTL_S}s to execute"),
        }
        await _ledger(backend, namespace, ctx, decision=GATE_PREVIEW)
        return ctx

    # (9) approved — proceed to the write.
    ctx.latency_ms = int((time.monotonic() - t0) * 1000)
    ctx.detail["latency_ms"] = ctx.latency_ms
    return ctx


def _raise_block(ctx: GateContext, rule: str, message: str) -> None:
    raise AppError("gate_blocked", message,
                   gate={"tier": ctx.tier, "decision": GATE_BLOCKED, "rule": rule,
                         "flags": ctx.flags})


async def _seen_event(backend, namespace: str, actor: str, event_id: str) -> dict | None:
    async with backend.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT * FROM memory_entry WHERE namespace = %s AND actor = %s "
            "AND event_id = %s ORDER BY revision DESC LIMIT 1",
            (namespace, actor or "unattributed", event_id))
        return await cur.fetchone()


async def _stale_dependencies(backend, namespace: str, meta: dict | None,
                              profile: dict) -> list[dict]:
    """Expired reconcile verdicts on depended-on claims. Postgres reads only —
    the verdict store, never GitHub. Age comes from the verdict's own
    freshness annotation (a verdict is a snapshot, not a subscription)."""
    refs = list((meta or {}).get("derived_from") or [])
    out: list[dict] = []
    for ref in refs[:10]:
        dep_key = str(ref).split("@", 1)[0]
        verdict = await backend.memory_get(
            namespace, f"coord/_reconcile/{dep_key}")
        if verdict is None:
            dep = await _latest_row(backend, namespace, dep_key)
            if dep is not None and dep.get("kind") == "claim":
                out.append({"key": dep_key, "reason": "never_reconciled",
                            "age_hours": None})
            continue
        if verdict.get("freshness") == "expired":
            out.append({"key": dep_key, "reason": "verdict_expired",
                        "age_hours": verdict.get("age_hours")})
    return out


async def _awaken_targets(backend, namespace: str, stale_deps: list[dict],
                          meta: dict | None) -> list[dict]:
    """Resolve targets for the awakening hook from the stale claims' own
    provenance (meta.repo + branch), falling back to the write's meta."""
    targets: list[dict] = []
    for dep in stale_deps:
        row = await _latest_row(backend, namespace, dep["key"])
        m = (row or {}).get("meta") or {}
        if m.get("repo") and (m.get("branch") or m.get("pr") is not None):
            targets.append({"repo": m["repo"], "branch": m.get("branch"),
                            "pr": m.get("pr")})
    if not targets and meta and meta.get("repo"):
        targets.append({"repo": meta["repo"], "branch": meta.get("branch"),
                        "pr": meta.get("pr")})
    return targets


async def _build_preview(backend, namespace: str, key: str, kind: str,
                         sanitized: Any, hits: list[str], will_quarantine: bool,
                         tombstone: bool) -> dict:
    from storage.postgres import _content_hash  # local import: avoid cycle at module load

    prior = await _latest_row(backend, namespace, key)
    preview: dict[str, Any] = {
        "tombstone": tombstone,
        "quarantined": will_quarantine,
        "screening": hits or None,
        "new_content_hash": None if tombstone else _content_hash(sanitized),
        "prior_revision": None, "prior_revision_id": None,
        "prior_content_hash": None, "kind_change": None,
        "value_bytes_delta": None,
    }
    if prior is not None:
        preview["prior_revision"] = prior["revision"]
        preview["prior_revision_id"] = prior["id"]
        preview["prior_content_hash"] = prior.get("content_hash")
        if not tombstone:
            preview["kind_change"] = (
                None if prior["kind"] == kind else {"from": prior["kind"], "to": kind})
            try:
                old_bytes = len(json.dumps(prior["value"], default=str))
                new_bytes = len(json.dumps(sanitized, default=str))
                preview["value_bytes_delta"] = new_bytes - old_bytes
            except (TypeError, ValueError):
                pass
    # lineage impact: live entries whose derived_from references this key
    async with backend.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            """
            SELECT key FROM (
                SELECT DISTINCT ON (key) key, tombstone, valid_until, derived_from
                FROM memory_entry WHERE namespace = %s ORDER BY key, revision DESC
            ) latest
            WHERE NOT tombstone AND (valid_until IS NULL OR valid_until > now())
              AND derived_from IS NOT NULL
              AND EXISTS (SELECT 1 FROM unnest(derived_from) d
                          WHERE d = %s OR d LIKE %s)
            LIMIT 20
            """,
            (namespace, key, key.replace("\\", "\\\\").replace("%", "\\%")
             .replace("_", "\\_") + "@%"))
        rows = await cur.fetchall()
    preview["lineage_dependents"] = sorted(r["key"] for r in rows)
    return preview


def _conflict_response(namespace: str, key: str, tool: str, ctx: GateContext,
                       *, conflict: dict | None, clarify: str) -> dict:
    return {
        "status": GATE_CONFLICT,
        "namespace": namespace, "key": key, "tool": tool,
        "decision": GATE_CONFLICT, "persisted": False,
        "conflict": conflict, "clarify": clarify,
        "gate": ctx.gate_block(), "gate_detail": dict(ctx.detail),
        "summary": f"CONFLICT: {tool} {key} — nothing persisted; {clarify}",
    }


def _tier2_envelope(namespace: str, profile: dict, tool: str, key: str,
                    kind: str, intent: dict) -> dict:
    """Grounded external critic (S2): the envelope carries the Tier-1 retrieved
    entries and the structured conflict — never free-floating self-critique.
    PHI rule (charter §7): clinical profiles send hashes + structured fields,
    never raw intent text."""
    clinical = bool(profile.get("clinical"))
    return {
        "namespace": namespace,
        "proposed_call": {"tool": tool, "key": key, "kind": kind},
        "intent": {
            "intent_hash": intent.get("intent_hash"),
            "goal": None if clinical else intent.get("goal"),
            "scope": list(intent.get("scope") or []),
            "labels": list(intent.get("labels") or []),
        },
        "tier1_matches": intent.get("matched") or [],
        "conflict": intent.get("conflict"),
    }


async def _evaluate_tier2(reasoner, envelope: dict) -> dict:
    try:
        out = await reasoner.evaluate(envelope)
    except Exception as exc:  # noqa: BLE001 - unavailable, never an unexplained block
        return {"status": "error", "error": type(exc).__name__}
    if not isinstance(out, dict) or out.get("status") not in ("ok", "error"):
        return {"status": "error", "error": "malformed_response"}
    return out


# --------------------------------------------------------------------------
# post-write bookkeeping (efficacy ledger + outcome closure)
# --------------------------------------------------------------------------
async def on_write_success(backend, namespace: str, ctx: GateContext,
                           entry: dict, *, profile: dict) -> dict:
    """Attach the gate block to a persisted ack, close any open block for this
    fingerprint as false_positive (the gate was wrong: same op later
    succeeded), and record the decision in the efficacy ledger."""
    fresh = not entry.get("deduplicated")
    entry["gate"] = ctx.gate_block()
    entry["gate_detail"] = dict(ctx.detail)
    if fresh:
        closure = None
        if ctx.open_block_id is not None:
            await _close_block(backend, namespace, ctx.open_block_id, "false_positive")
            closure = "false_positive"
            if ctx.open_block_skill:
                await bump_skill_efficacy(backend, namespace, ctx.open_block_skill,
                                          "false_positive")
        await _ledger(backend, namespace, ctx, decision=ctx.decision,
                      closure=closure)
    return entry


async def on_write_error(backend, namespace: str, ctx: GateContext,
                         exc: Exception) -> None:
    """A write the gate let through (or an override retry) genuinely failed:
    if its fingerprint matches an open block, the block was right —
    confirmed_correct."""
    if ctx.open_block_id is not None:
        await _close_block(backend, namespace, ctx.open_block_id, "confirmed_correct")
        if ctx.open_block_skill:
            await bump_skill_efficacy(backend, namespace, ctx.open_block_skill,
                                      "prevented_error")
        await _ledger(backend, namespace, ctx, decision=ctx.decision,
                      closure="confirmed_correct")


# --------------------------------------------------------------------------
# efficacy ledger: session events + monthly rollup + skill counters
# --------------------------------------------------------------------------
def month_key(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"gate/efficacy/{now.strftime('%Y%m')}"


async def _ledger(backend, namespace: str, ctx: GateContext, *, decision: str,
                  rule: str | None = None, closure: str | None = None,
                  ihash: str | None = None, labels: list[str] | None = None) -> None:
    """One gate decision → one session event (when a session is linked) + one
    rollup increment. Best-effort: ledger failure never fails the gated call.
    All writes go through ``_append`` with actor='gate' (exempt — no
    recursion) inside the same namespace."""
    try:
        payload = {
            "intent_hash": ihash, "tier": ctx.tier, "decision": decision,
            "matched": ctx.matched[:10], "flags": ctx.flags[:10],
            "latency_ms": ctx.latency_ms, "tool": ctx.tool, "key": ctx.key,
        }
        if rule:
            payload["rule"] = rule
        if closure:
            payload["closure"] = closure
        if labels:
            payload["labels"] = labels[:10]
        if ctx.session_id:
            try:
                await backend.session_append_event(
                    namespace, ctx.session_id, "gate_decision", payload,
                    actor="gate")
            except AppError:
                pass  # unknown session: the rollup still counts the decision
        updates: dict[str, int] = {f"decisions.{decision}": 1, f"tiers.{ctx.tier}": 1,
                                   "fired": 1}
        if rule:
            updates[f"rules.{rule}"] = 1
        if closure:
            updates[f"closures.{closure}"] = 1
        await bump_rollup(backend, namespace, updates)
    except Exception:  # noqa: BLE001 - observability, never the caller's ack
        pass


_ROLLUP_BASE = {
    "fired": 0,
    "decisions": {GATE_APPROVED: 0, GATE_PREVIEW: 0, GATE_CONFLICT: 0,
                  GATE_CLARIFY: 0, GATE_BLOCKED: 0},
    "tiers": {}, "rules": {},
    "closures": {"confirmed_correct": 0, "false_positive": 0, "unknown": 0},
    "skills": {},
}


async def bump_rollup(backend, namespace: str, updates: dict[str, int]) -> None:
    """Increment counters in gate/efficacy/<yyyymm> (kind=knowledge) via a new
    revision written by actor='gate' (exempt from gating)."""
    key = month_key()
    row = await _latest_row(backend, namespace, key)
    value = dict(_ROLLUP_BASE) if row is None or row.get("tombstone") else _json_safe(row["value"])
    value = _deep_merge_base(value)
    for path, delta in updates.items():
        node = value
        parts = path.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = int(node.get(parts[-1], 0) or 0) + delta
    await backend._append(
        namespace, key, value, "knowledge", ["gate", "efficacy"], None, None,
        False, meta={"gate_rollup": True}, actor="gate", origin="tool",
        tool="memory_save")


def _deep_merge_base(value: Any) -> dict:
    if not isinstance(value, dict):
        value = {}
    out = json.loads(json.dumps(_ROLLUP_BASE))
    for k, v in value.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


async def bump_skill_efficacy(backend, namespace: str, skill_key: str,
                              counter: str) -> None:
    """Skill efficacy counters move ONLY through gate outcomes (MD-4): applied
    when a skill informs a decision; prevented_error / false_positive at
    outcome closure. Writes a new revision of the skill (actor='gate',
    lineage-linked) — raw writes never route through here."""
    row = await _latest_row(backend, namespace, skill_key)
    if row is None or row.get("tombstone"):
        return
    meta = dict(row.get("meta") or {})
    eff = dict(meta.get("efficacy") or {})
    eff[counter] = int(eff.get(counter, 0) or 0) + 1
    eff.setdefault("applied", eff.get("applied", 0))
    meta["efficacy"] = eff
    try:
        await backend._append(
            namespace, skill_key, row["value"], row["kind"],
            list(row.get("tags") or []), row.get("source_surface"), None, False,
            meta=meta, actor="gate", origin="tool", tool="memory_save",
            derived_from=[f"{skill_key}@{row['id']}"])
    except AppError:
        pass  # counter bump is best-effort; the decision itself already stood


# --------------------------------------------------------------------------
# Tier 1 — intent_open: the memory-similarity critic
# --------------------------------------------------------------------------
async def intent_open(
    backend, namespace: str, *, goal: str, scope: list[str] | None = None,
    session_id: str | None = None, actor: str = "unattributed",
    event_id: str | None = None, clarification: str | None = None,
    include_quarantined: bool = False,
) -> dict:
    """Open (or refresh) a declared intent: embed it, retrieve similar
    decisions/constraints/skills from THIS namespace only, detect structured
    contradictions, and register the intent for the session. See the tool
    description in server/mcp_server.py for the caller-facing convention."""
    t0 = time.monotonic()
    profile = await backend.resolved_profile(namespace)
    clinical = bool(profile.get("clinical"))
    if not isinstance(goal, str) or not goal.strip():
        raise AppError("gate_blocked", "intent goal must be a non-empty string",
                       gate={"tier": 1, "decision": GATE_BLOCKED,
                             "rule": "empty_intent"})
    flags: list[str] = []
    labels: list[str] = []

    # ADV-1: the declared intent is a value like any other — screened, and an
    # instruction-shaped goal is flagged without changing any gate rule.
    hits = screen_value(goal)
    if hits:
        flags.append("intent_screened")
        labels += hits
    clar_hits = screen_value(clarification) if clarification else []
    if clarification and clar_hits:
        flags.append("clarification_screened")

    # PHI hard gate (charter §7 / ADV-5): clinical namespaces persist
    # intent_hash + screened labels ONLY — and never embed raw intent text.
    ihash = intent_hash(goal)
    if clinical:
        flags.append("phi_screened")
        if text_looks_identifying(goal) or (clarification and text_looks_identifying(clarification)):
            labels.append("phi_detected")

    # Tier-1 retrieval: semantic leg (pgvector, floor-guarded) + deterministic
    # trigger-overlap leg for skills. Namespace-scoped, quarantine-excluded by
    # default; retrieved bodies are UNTRUSTED DATA (wrapped, never executed).
    matched: list[dict] = []
    seen: set[str] = set()
    if not clinical:
        for cand in await _semantic_candidates(backend, namespace, goal,
                                               include_quarantined):
            if cand["key"] not in seen:
                seen.add(cand["key"])
                matched.append(cand)
    for cand in await _trigger_candidates(backend, namespace, goal,
                                          include_quarantined):
        if cand["key"] not in seen:
            seen.add(cand["key"])
            matched.append(cand)

    skill_window_h = float(profile.get("skill_validity_hours")
                           or DEFAULT_SKILL_VALIDITY_HOURS)
    now = datetime.now(timezone.utc)
    conflict: dict | None = None
    clarify: str | None = None
    skill_conflict_key: str | None = None
    for m in matched:
        if not m["key"].startswith(SKILL_PREFIX):
            continue
        meta = m.pop("_meta", {}) or {}
        m["polarity"] = meta.get("polarity")
        expired = _skill_expired(meta.get("last_validated"), skill_window_h, now)
        provenanced = bool(meta.get("curator_provenance"))
        if expired:
            m["flags"].append("expired_skill")
        if not provenanced:
            m["flags"].append("unprovenanced_skill")
        if m.get("quarantined"):
            m["flags"].append("quarantined_skill")
        # S7: only curator-provenanced, in-window, non-quarantined skills can
        # contribute to gate_conflict; everything else advises.
        if (m["polarity"] == "anti-pattern" and provenanced and not expired
                and not m.get("quarantined") and skill_conflict_key is None):
            skill_conflict_key = m["key"]
    for m in matched:
        m.pop("_meta", None)

    # Structured-field contradiction (G1-2): deterministic scan of live
    # entries carrying meta.structured — field-level, never prose-inferred.
    structured = await _structured_conflict(backend, namespace, goal)
    if structured:
        conflict = structured
        decision = GATE_CONFLICT
        clarify = (f"Declared intent targets {structured['declared']!r} but "
                   f"{structured['key']} (revision {structured['revision']}) "
                   f"restricts {structured['field']} to {structured['allowed']!r}. "
                   "Which should apply?")
        if structured["key"] not in seen:
            row = await _latest_row(backend, namespace, structured["key"])
            if row is not None:
                matched.append(_match_entry(row, similarity=None,
                                            flags=["structured_conflict"]))
    elif skill_conflict_key is not None:
        decision = GATE_CONFLICT
        m = next(m for m in matched if m["key"] == skill_conflict_key)
        clarify = (f"The declared intent matches anti-pattern {skill_conflict_key} "
                   "(curator-provenanced, in-window). Proceed anyway, or adjust "
                   "the approach?")
        conflict = {"key": skill_conflict_key, "revision": m.get("revision"),
                    "revision_id": m.get("revision_id"), "basis": "anti_pattern_skill",
                    "skill_key": skill_conflict_key}
        m["flags"].append("conflict_contributor")
    else:
        decision = GATE_APPROVED

    # apply counters for the valid skills that informed this decision (MD-4)
    for m in matched:
        if (m["key"].startswith(SKILL_PREFIX) and m.get("polarity")
                and "expired_skill" not in m["flags"]
                and "unprovenanced_skill" not in m["flags"]
                and "quarantined_skill" not in m["flags"]):
            await bump_skill_efficacy(backend, namespace, m["key"], "applied")

    # register the intent (clinical: hash + labels only, no goal, no embedding)
    matched_snapshot = [{"key": m["key"], "revision_id": m.get("revision_id"),
                         "polarity": m.get("polarity"), "flags": m["flags"]}
                        for m in matched]
    async with backend.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO gate_intent (namespace, session_id, intent_hash, goal, "
            "scope, labels, screening, decision, conflict, matched, actor) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (namespace, str(session_id) if session_id else "", ihash,
             None if clinical else goal, list(scope or []), labels, hits or None,
             decision, Jsonb(_json_safe(conflict)) if conflict else None,
             Jsonb(_json_safe(matched_snapshot)), actor or "unattributed"))

    latency_ms = int((time.monotonic() - t0) * 1000)
    ctx = GateContext(tool="intent_open", key="", tier=1, decision=decision,
                      flags=flags, matched=[m["key"] for m in matched],
                      session_id=str(session_id) if session_id else None,
                      latency_ms=latency_ms)
    await _ledger(backend, namespace, ctx, decision=decision, ihash=ihash,
                  labels=labels)

    # project block (MD-1): served from project/meta; absent -> explicit null.
    project = await _project_block(backend, namespace)

    return {
        "namespace": namespace, "session_id": session_id,
        "intent_hash": ihash, "decision": decision,
        "matched": matched, "flags": flags, "labels": labels,
        "conflict": conflict, "clarify": clarify,
        "project": project,
        "gate": ctx.gate_block(),
        "latency_ms": latency_ms,
    }


def _skill_expired(last_validated: Any, window_h: float, now: datetime) -> bool:
    if not last_validated:
        return True
    try:
        ts = datetime.fromisoformat(str(last_validated).replace("Z", "+00:00"))
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 3600.0 > window_h


def _match_entry(row: dict, *, similarity: float | None, flags: list[str]) -> dict:
    value = row.get("value")
    guidance = value if isinstance(value, str) else json.dumps(value, default=str)
    return {
        "key": row["key"], "revision": row.get("revision"),
        "revision_id": row.get("id"), "kind": row.get("kind"),
        "similarity": round(similarity, 3) if similarity is not None else None,
        "guidance": wrap_value(guidance),
        "quarantined": bool(row.get("quarantined")),
        "flags": list(flags),
        "_meta": row.get("meta") or {},
    }


async def _semantic_candidates(backend, namespace: str, goal: str,
                               include_quarantined: bool) -> list[dict]:
    qvec = await backend._maybe_embed_query(goal)
    if qvec is None:
        return []
    async with backend.pool.connection() as conn:
        conn.row_factory = dict_row
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('hnsw.ef_search', %s, true)",
                (str(settings.hnsw_ef_search),))
            cur = await conn.execute(
                """
                SELECT *, (embedding <=> %s::vector) AS _dist FROM (
                    SELECT DISTINCT ON (key) *
                    FROM memory_entry WHERE namespace = %s
                    ORDER BY key, revision DESC
                ) latest
                WHERE NOT tombstone AND (valid_until IS NULL OR valid_until > now())
                      AND (%s OR NOT quarantined)
                      AND key NOT LIKE %s AND key NOT LIKE %s AND key NOT LIKE %s
                      AND embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT 8
                """,
                (qvec, namespace, include_quarantined,
                 r"coord/\_reconcile/%", r"\_meta/%", r"gate/%", qvec))
            rows = await cur.fetchall()
    out = []
    for r in rows:
        sim = 1.0 - float(r.pop("_dist"))
        if sim >= SIMILARITY_FLOOR:  # no fabricated matches (G1-5)
            out.append(_match_entry(r, similarity=sim, flags=[]))
    return out


async def _trigger_candidates(backend, namespace: str, goal: str,
                              include_quarantined: bool) -> list[dict]:
    """Deterministic skill leg: token overlap between the declared intent and
    each skill's meta.trigger_intent (stopwords removed, >= 2 shared tokens)."""
    goal_tokens = content_tokens(goal)
    if not goal_tokens:
        return []
    async with backend.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            """
            SELECT * FROM (
                SELECT DISTINCT ON (key) * FROM memory_entry
                WHERE namespace = %s AND key LIKE %s
                ORDER BY key, revision DESC
            ) latest
            WHERE NOT tombstone AND (valid_until IS NULL OR valid_until > now())
                  AND (%s OR NOT quarantined)
            LIMIT 200
            """,
            (namespace, SKILL_PREFIX.replace("_", r"\_") + "%", include_quarantined))
        rows = await cur.fetchall()
    out = []
    for r in rows:
        trigger = ((r.get("meta") or {}).get("trigger_intent")) or ""
        overlap = goal_tokens & content_tokens(trigger)
        if len(overlap) >= TRIGGER_OVERLAP_MIN:
            out.append(_match_entry(
                r, similarity=None,
                flags=[]))
    return out


async def _structured_conflict(backend, namespace: str, goal: str) -> dict | None:
    """Field-level contradiction between the declared intent's extracted refs
    and stored structured constraints (meta.structured.allowed_branch /
    allowed_repo). Deterministic scan; a retrieval miss must not hide a hard
    constraint."""
    refs = extract_refs(goal)
    if not refs["coding"]:
        return None
    async with backend.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            """
            SELECT * FROM (
                SELECT DISTINCT ON (key) * FROM memory_entry
                WHERE namespace = %s AND meta ? 'structured'
                ORDER BY key, revision DESC
            ) latest
            WHERE NOT tombstone AND (valid_until IS NULL OR valid_until > now())
                  AND NOT quarantined
            LIMIT 100
            """,
            (namespace,))
        rows = await cur.fetchall()
    for r in rows:
        structured = (r.get("meta") or {}).get("structured") or {}
        allowed_branch = structured.get("allowed_branch")
        if allowed_branch and refs["branches"]:
            bad = [b for b in refs["branches"] if b != allowed_branch]
            if bad:
                return {"key": r["key"], "revision": r["revision"],
                        "revision_id": r["id"], "field": "allowed_branch",
                        "declared": bad[0], "allowed": allowed_branch}
        allowed_repo = structured.get("allowed_repo")
        if allowed_repo and refs["repos"]:
            bad = [x for x in refs["repos"] if x != allowed_repo]
            if bad:
                return {"key": r["key"], "revision": r["revision"],
                        "revision_id": r["id"], "field": "allowed_repo",
                        "declared": bad[0], "allowed": allowed_repo}
    return None


async def _project_block(backend, namespace: str) -> dict | None:
    entry = await backend.memory_get(namespace, "project/meta")
    if entry is None:
        return None
    meta = entry.get("meta") or {}
    block = {k: meta.get(k) for k in
             ("stack", "repo", "conventions_version", "active_phase", "profile",
              "key_schema_ref") if meta.get(k) is not None}
    block["revision_id"] = entry.get("revision_id")
    return block

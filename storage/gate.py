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
from storage.intent_features import extract_features
from storage.phi import text_looks_identifying
from storage.sanitize import sanitize, wrap_value
from storage.screening import screen_value
from storage.triggers import evaluate_trigger, validate_trigger

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
#
# RETRIEVAL_HARD_FLOOR is the widest the ANN query is ever allowed to be. The
# effective floor is per-namespace (variant_profiles.gate_similarity_floor,
# default 0.45) and is applied on top of this; this constant only stops a
# namespace from setting a floor so low that the candidate list becomes the
# whole namespace. v1's 0.25 is retained here as the hard bound because the
# match log needs to see NEAR-floor rejects to calibrate the real floor — a
# query that never retrieves them can never justify moving it.
RETRIEVAL_HARD_FLOOR = 0.25
TRIGGER_OVERLAP_MIN = 2

# Back-compat alias: external callers and older fixtures referenced this name.
SIMILARITY_FLOOR = RETRIEVAL_HARD_FLOOR

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
        await _link_acted_upon(backend, namespace, ctx)
    return entry


async def _link_acted_upon(backend, namespace: str, ctx: GateContext) -> None:
    """Flip acted_upon for skills surfaced to this session's open intent.

    KNOWN BIAS, DOCUMENTED RATHER THAN HIDDEN. Session linkage is a WEAK causal
    proxy: any subsequent gated write in the intent's session flips acted_upon,
    including writes that have nothing to do with the surfaced skill. So
    acted_upon over-counts from day one, and it will keep over-counting — this
    is a property of the design, not a bug awaiting a fix.

    That is tolerable only because of where the number is allowed to go. It is
    diagnostic; it never tunes a threshold. Threshold tuning reads
    outcome_closed, which requires someone to deliberately record what actually
    happened. The bias is asserted by the negative_attribution fixture so it
    stays visible instead of quietly becoming folklore, and it is restated in
    the tool descriptions so a caller cannot pick the number up without it.
    """
    if not ctx.session_id:
        return
    try:
        intent = await _load_intent(backend, namespace, ctx.session_id)
        if not intent:
            return
        async with backend.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT DISTINCT skill_key FROM skill_efficacy_events "
                "WHERE namespace = %s AND intent_hash = %s AND stage = %s",
                (namespace, intent["intent_hash"], STAGE_SURFACED))
            keys = [r["skill_key"] for r in await cur.fetchall()]
        for key in keys:
            await record_efficacy_event(
                backend, namespace, key, intent["intent_hash"], STAGE_ACTED_UPON,
                writer_actor=STAGE_WRITER[STAGE_ACTED_UPON],
                session_id=ctx.session_id)
    except Exception:  # pragma: no cover - linkage never fails a write
        pass


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


# --------------------------------------------------------------------------
# 2b — event-sourced skill efficacy.
#
# The v1 counters were mutable integers on the skill's own meta, incremented by
# actor 'gate' on every match. skill/no-sorted-fold-replay went applied 0 -> 5,
# every increment written by the gate, including one from the catering false
# positive: the instrument shared identity with the subject, and the metric that
# was supposed to decide whether a skill earns its keep was inflated by that
# skill's own false positives.
#
# Counts are now PROJECTIONS over an append-only log.
# --------------------------------------------------------------------------
STAGE_MATCHED = "matched"
STAGE_SURFACED = "surfaced"
STAGE_ACTED_UPON = "acted_upon"
STAGE_OUTCOME_CLOSED = "outcome_closed"
STAGES = (STAGE_MATCHED, STAGE_SURFACED, STAGE_ACTED_UPON, STAGE_OUTCOME_CLOSED)

# A DISTINCT writer actor per stage, enforced. Not ceremony: event dedup on this
# server is scoped to (namespace, actor), so sharing an actor across stages
# would let one stage's dedup silently swallow another's events — the same class
# of bug as the instrument sharing identity with its subject.
STAGE_WRITER = {
    STAGE_MATCHED: "gate-eval",
    STAGE_SURFACED: "gate-eval",
    STAGE_ACTED_UPON: "gate-linkage",
    STAGE_OUTCOME_CLOSED: "gate-closure",
}

CLOSURE_OUTCOMES = ("followed", "overridden", "abandoned")


async def record_efficacy_event(
    backend, namespace: str, skill_key: str, intent_hash_: str, stage: str,
    *, writer_actor: str, outcome: str | None = None,
    session_id: str | None = None, event_id: str | None = None,
    strict: bool = False,
) -> bool:
    """Append one efficacy event. Returns True if a NEW row landed.

    Rejects a wrong writer actor for the stage. In strict mode that is an
    AppError (the caller asked to record something and deserves to know it did
    not happen); otherwise it is a silent no-op, because the gate's own inline
    stage writes must never fail a user's tool call.

    Duplicate suppression is structural — UNIQUE(namespace, skill_key,
    intent_hash, stage) — so "one increment per intent per stage, ever" is a
    property of the schema rather than a convention the next writer must
    remember.
    """
    if stage not in STAGES:
        raise AppError("invalid_argument", f"stage must be one of {STAGES}")
    expected = STAGE_WRITER[stage]
    if writer_actor != expected:
        if strict:
            raise AppError(
                "invalid_argument",
                f"stage {stage!r} must be written by actor {expected!r}, "
                f"got {writer_actor!r}",
                remedy="each stage has a distinct writer actor; dedup is "
                       "scoped to (namespace, actor)")
        return False
    try:
        async with backend.pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO skill_efficacy_events (namespace, skill_key, "
                "intent_hash, stage, outcome, writer_actor, event_id, session_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (namespace, skill_key, intent_hash, stage) DO NOTHING",
                (namespace, skill_key, intent_hash_, stage, outcome,
                 writer_actor, event_id, session_id))
            return cur.rowcount > 0
    except Exception:
        if strict:
            raise
        return False


async def efficacy_projection(backend, namespace: str, skill_key: str) -> dict:
    """Project the append-only log into counts. NEVER stored authoritative —
    a stored count is a number nobody re-derives, which is how the v1 counter
    drifted five increments away from anything real."""
    async with backend.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT stage, count(*) AS n FROM skill_efficacy_events "
            "WHERE namespace = %s AND skill_key = %s GROUP BY stage", (namespace, skill_key))
        rows = await cur.fetchall()
        cur = await conn.execute(
            "SELECT outcome, count(*) AS n FROM skill_efficacy_events "
            "WHERE namespace = %s AND skill_key = %s AND stage = %s AND outcome IS NOT NULL "
            "GROUP BY outcome", (namespace, skill_key, STAGE_OUTCOME_CLOSED))
        outcome_rows = await cur.fetchall()
    counts = {stage: 0 for stage in STAGES}
    for r in rows:
        counts[r["stage"]] = r["n"]
    return {
        **counts,
        "outcomes": {r["outcome"]: r["n"] for r in outcome_rows},
        # Restated at every read so a consumer cannot pick the number up without
        # the caveat attached to it.
        "note": ("matched/surfaced are diagnostic; acted_upon is a "
                 "session-linkage proxy and over-counts; tune thresholds on "
                 "outcome_closed only"),
    }


async def gate_close_outcome(
    backend, namespace: str, *, intent_hash: str, outcome: str,
    actor: str = "unattributed", skill_key: str | None = None,
) -> dict:
    """Close the outcome of a gated intent — the only stage that may tune a
    threshold, and the only one a human or agent writes deliberately."""
    if outcome not in CLOSURE_OUTCOMES:
        raise AppError("invalid_argument",
                       f"outcome must be one of {CLOSURE_OUTCOMES}")
    if not isinstance(intent_hash, str) or len(intent_hash) != 64:
        raise AppError("invalid_argument",
                       "intent_hash must be the 64-char hash returned by intent_open")

    # Which skills were surfaced for this intent. Closure attaches to those, so
    # a caller cannot close an outcome against a skill the gate never showed
    # them.
    async with backend.pool.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(
            "SELECT DISTINCT skill_key FROM skill_efficacy_events "
            "WHERE namespace = %s AND intent_hash = %s", (namespace, intent_hash))
        keys = [r["skill_key"] for r in await cur.fetchall()]
    if skill_key is not None:
        keys = [k for k in keys if k == skill_key]

    closed, already = [], []
    for key in keys:
        landed = await record_efficacy_event(
            backend, namespace, key, intent_hash, STAGE_OUTCOME_CLOSED,
            writer_actor=STAGE_WRITER[STAGE_OUTCOME_CLOSED], outcome=outcome,
            strict=True)
        (closed if landed else already).append(key)

    # Backfill the calibration dataset's outcome column so the match log can be
    # judged against what actually happened, not just against itself.
    try:
        async with backend.pool.connection() as conn:
            await conn.execute(
                "UPDATE gate_match_log SET acted_upon = %s "
                "WHERE namespace = %s AND intent_hash = %s",
                (outcome == "followed", namespace, intent_hash))
    except Exception:  # pragma: no cover - calibration backfill is never fatal
        pass

    return {
        "namespace": namespace,
        "intent_hash": intent_hash,
        "outcome": outcome,
        "closed": closed,
        # Named explicitly rather than folded into `closed`: a replay must be
        # visibly a no-op, never look like a fresh close.
        "already_closed": already,
        "actor": actor,
    }


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


SKILL_POLARITIES = ("anti-pattern", "best-practice")
TRIGGER_AUTHORS = ("human", "curator", "unvalidated")


async def skill_define(
    backend, namespace: str, *, key: str, guidance: str, polarity: str,
    trigger: dict | None = None, trigger_author: str = "unvalidated",
    trigger_intent: str | None = None, temporal_mode: str | None = None,
    calibration_ts: str | None = None, actor: str = "unattributed",
    role: str | None = None, event_id: str | None = None,
) -> dict:
    """Define or update a skill, validating its trigger predicate fail-closed.

    THE VALIDATION CONTRACT. A trigger is stored ONLY if it passes the
    deterministic schema + operator-whitelist validator. A failing predicate is
    not stored in a degraded form and is not stored "pending review": it is
    dropped, the skill persists display-only, and the errors come back so the
    author can see exactly what was rejected.

    That asymmetry is deliberate. A half-validated predicate that escalates is
    strictly worse than no predicate at all — it is the v1 failure mode with
    extra steps. A skill that merely advises can be wrong without blocking
    anyone's work.
    """
    if not isinstance(key, str) or not key.startswith(SKILL_PREFIX):
        raise AppError("invalid_argument",
                       f"skill key must start with {SKILL_PREFIX!r}",
                       remedy=f"use {SKILL_PREFIX}<slug>")
    if polarity not in SKILL_POLARITIES:
        raise AppError("invalid_argument",
                       f"polarity must be one of {SKILL_POLARITIES}",
                       remedy="anti-pattern skills escalate; best-practice skills advise")
    if trigger_author not in TRIGGER_AUTHORS:
        raise AppError("invalid_argument",
                       f"trigger_author must be one of {TRIGGER_AUTHORS}",
                       remedy="record who authored the predicate")
    if not isinstance(guidance, str) or not guidance.strip():
        raise AppError("invalid_argument", "guidance must be a non-empty string")

    trigger_schema_errors = validate_trigger(trigger) if trigger is not None else []
    trigger_valid = trigger is not None and not trigger_schema_errors

    row = await _latest_row(backend, namespace, key)
    meta = dict((row or {}).get("meta") or {})
    meta["polarity"] = polarity
    if trigger_intent is not None:
        meta["trigger_intent"] = trigger_intent
    meta.setdefault("trigger_intent", guidance[:200])
    if trigger_valid:
        meta["trigger"] = trigger
        meta["trigger_author"] = trigger_author
        # Freshness provenance on the predicate itself, for the same reason the
        # floor carries it: a rule with no calibration date is a stale authority
        # nobody re-examines.
        meta["trigger_temporal_mode"] = temporal_mode or "historical_snapshot"
        meta["trigger_calibration_ts"] = calibration_ts
        # S7 discipline: only curator-provenanced, in-window skills may
        # contribute to a gate_conflict. Going through this entrypoint with a
        # named author IS that provenance — the predicate was validated
        # deterministically and attributed to a human or the curator. An
        # 'unvalidated' author explicitly does not earn it, so such a skill can
        # be defined but never escalates.
        if trigger_author in ("human", "curator"):
            meta["curator_provenance"] = True
            meta["last_validated"] = (calibration_ts
                                      or datetime.now(timezone.utc).isoformat())
    else:
        # Never leave a stale VALID trigger behind a rejected update — that
        # would silently keep escalating on a rule the author just tried to
        # replace.
        meta.pop("trigger", None)
        meta.pop("trigger_author", None)

    ack = await backend.memory_save(
        namespace, key, guidance, kind="knowledge", meta=meta, actor=actor,
        origin="tool", role=role, event_id=event_id)

    return {
        "skill_id": key,
        "revision_id": ack.get("revision_id"),
        "revision": ack.get("revision"),
        "polarity": polarity,
        "trigger_valid": trigger_valid,
        "trigger_schema_errors": trigger_schema_errors or None,
        # An invalid trigger is not an error the caller can ignore: it changes
        # what the skill DOES. Name the consequence rather than only the fault.
        "display_only": not trigger_valid,
        "quarantined": bool(ack.get("quarantined")),
        "verified_persisted": bool(ack.get("verified_persisted")),
    }


# --------------------------------------------------------------------------
# Tier 1 — intent_open: the memory-similarity critic
# --------------------------------------------------------------------------
async def intent_open(
    backend, namespace: str, *, goal: str, scope: list[str] | None = None,
    session_id: str | None = None, actor: str = "unattributed",
    event_id: str | None = None, clarification: str | None = None,
    include_quarantined: bool = False, verbose_gate: bool = False,
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
    guard = await backend.gate_guard(namespace)
    matched: list[dict] = []
    seen: set[str] = set()
    semantic_raw: list[dict] = []
    if not clinical:
        semantic_raw = await _semantic_candidates(backend, namespace, goal,
                                                  include_quarantined)
        for cand in _guarded_candidates(semantic_raw, guard):
            if cand["key"] not in seen:
                seen.add(cand["key"])
                matched.append(cand)
    for cand in await _trigger_candidates(backend, namespace, goal,
                                          include_quarantined):
        if cand["key"] not in seen:
            seen.add(cand["key"])
            matched.append(cand)

    # Deterministic features for the predicate leg. Extracted ONCE per intent —
    # it is the same for every candidate, and spaCy is the one non-trivial CPU
    # cost on this path.
    features = extract_features(goal, clinical=clinical)

    skill_window_h = float(profile.get("skill_validity_hours")
                           or DEFAULT_SKILL_VALIDITY_HOURS)
    now = datetime.now(timezone.utc)
    conflict: dict | None = None
    clarify: str | None = None
    skill_conflict_key: str | None = None
    gate_audit: list[dict] = []
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

        # ------------------------------------------------------------------
        # PREDICATE-FIRST ESCALATION (validation FINDING-4).
        #
        # v1 escalated here on the strength of the candidate having been
        # RETRIEVED at all — which meant embedding proximity decided violation.
        # It conflicted "schedule the quarterly workshop catering" against an
        # event-log skill at cosine 0.288, and conflicted a goal that OBEYED
        # that skill, because compliance and violation are adjacent in
        # embedding space. Cosine cannot separate them at any threshold.
        #
        # Escalation is now decided by the skill's structured trigger predicate
        # evaluated against the intent's extracted features. Cosine NEVER
        # escalates; it selects what is shown and nothing more.
        #
        # A skill with no valid trigger is DISPLAY-ONLY and can never, by
        # itself, escalate. That is a deliberate behaviour change with a
        # deliberate direction: on deploy, existing anti-pattern skills have no
        # trigger and stop escalating until one is authored. Failing toward
        # silence is correct here because the conflict stream this replaces was
        # false-positive dominated — a gate that cries wolf trains the operator
        # to ignore it, which is worse than a gate that stays quiet.
        # ------------------------------------------------------------------
        trigger = meta.get("trigger")
        trigger_errors = validate_trigger(trigger) if trigger is not None else ["no trigger"]
        predicate_match = evaluate_trigger(trigger, features) if not trigger_errors else None
        m["predicate_match"] = predicate_match
        if trigger is None:
            m["flags"].append("display_only_no_trigger")
        elif trigger_errors:
            # An invalid trigger is louder than a missing one: something wrote a
            # predicate that does not validate, and a human should see that.
            m["flags"].append("invalid_trigger")

        eligible = (m["polarity"] == "anti-pattern" and provenanced and not expired
                    and not m.get("quarantined"))
        escalates = bool(eligible and predicate_match is True)
        if escalates and skill_conflict_key is None:
            skill_conflict_key = m["key"]

        gate_audit.append({
            "skill_key": m["key"],
            "cosine": m.get("similarity"),
            "predicate_evaluated": trigger is not None and not trigger_errors,
            "predicate_match": predicate_match,
            "trigger_schema_errors": trigger_errors or None,
            # Phase 4 (NLI backstop) is descoped on this branch. The keys are
            # present and null so the audit shape does not change when it lands.
            "nli_pair_direction": None,
            "nli_contradiction": None,
            "nli_verdict": None,
            "escalated": escalates,
            "escalation_reason": (
                "predicate_match" if escalates
                else "no_trigger" if trigger is None
                else "invalid_trigger" if trigger_errors
                else "predicate_did_not_match" if predicate_match is False
                else "trigger_unevaluable" if predicate_match is None
                else "skill_not_eligible"),
        })
    for m in matched:
        m.pop("_meta", None)

    # Calibration dataset (1a). EVERY skill candidate is logged, escalated or
    # not — a table holding only escalations cannot calibrate the floor that
    # produced it. Best-effort: a logging failure must not change a verdict.
    await _log_gate_matches(backend, namespace, ihash, matched, gate_audit,
                            guard, semantic_raw, clinical=clinical)

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
        clarify = (f"The declared intent satisfies the prohibition predicate of "
                   f"anti-pattern {skill_conflict_key} (curator-provenanced, "
                   "in-window). Proceed anyway, or adjust the approach?")
        # basis records WHY, and it is no longer "this was retrieved". A reader
        # of a stored conflict can now tell a predicate decision from the v1
        # proximity decision without re-deriving anything.
        conflict = {"key": skill_conflict_key, "revision": m.get("revision"),
                    "revision_id": m.get("revision_id"),
                    "basis": "anti_pattern_predicate",
                    "skill_key": skill_conflict_key}
        m["flags"].append("conflict_contributor")
    else:
        decision = GATE_APPROVED

    # 2b: efficacy is now recorded as append-only STAGE EVENTS, not as an
    # increment on the skill's own mutable counter.
    #
    # The v1 line here called bump_skill_efficacy(..., "applied"), which wrote a
    # new revision of the matched skill under actor 'gate' on every match. That
    # is how skill/no-sorted-fold-replay reached applied:5 — including one
    # increment from the catering false positive. The instrument was editing its
    # own subject, so the counter could never be used to judge the threshold
    # that produced it.
    #
    # matched and surfaced are DIAGNOSTIC. They are produced by the mechanism
    # under measurement, so they may describe the gate but must never tune it;
    # only outcome_closed does that.
    for m in matched:
        if (m["key"].startswith(SKILL_PREFIX) and m.get("polarity")
                and "expired_skill" not in m["flags"]
                and "unprovenanced_skill" not in m["flags"]
                and "quarantined_skill" not in m["flags"]):
            await record_efficacy_event(
                backend, namespace, m["key"], ihash, STAGE_MATCHED,
                writer_actor=STAGE_WRITER[STAGE_MATCHED],
                session_id=str(session_id) if session_id else None)
            await record_efficacy_event(
                backend, namespace, m["key"], ihash, STAGE_SURFACED,
                writer_actor=STAGE_WRITER[STAGE_SURFACED],
                session_id=str(session_id) if session_id else None)

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

    result = {
        "namespace": namespace, "session_id": session_id,
        "intent_hash": ihash, "decision": decision,
        "matched": matched, "flags": flags, "labels": labels,
        "conflict": conflict, "clarify": clarify,
        "project": project,
        "gate": ctx.gate_block(),
        "latency_ms": latency_ms,
    }
    if verbose_gate:
        # 1c: the per-candidate audit. Answers "why did this escalate / why did
        # it not" without a database read or a guess, which is what makes a
        # false positive reportable instead of merely annoying.
        result["gate_audit"] = gate_audit
        result["gate_guard"] = {
            "absolute_floor": guard.get("gate_similarity_floor"),
            "alpha": guard.get("gate_top_fraction_alpha"),
            "temporal_mode": guard.get("temporal_mode"),
            "calibration_ts": guard.get("calibration_ts"),
            # Surfaced so "nothing matched" can be told apart from "the
            # extractor is down and every predicate silently failed".
            "feature_extractor": features.get("extractor"),
        }
    return result


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


def _guarded_candidates(candidates: list[dict], guard: dict) -> list[dict]:
    """A2: accept a semantic candidate only if it clears BOTH guards.

        cosine >= absolute_floor        AND        cosine >= alpha x top_score

    The absolute floor answers "is this related at all" — v1's 0.25 admitted
    seven unrelated latency-sample notes at 0.369-0.376 alongside a true match
    at 0.539. The relative guard answers a different question the floor cannot:
    "is this as good as the best thing we found". A namespace whose best match
    is 0.50 should not also surface a 0.46 long tail merely because both
    cleared an absolute bar.

    Both are RETRIEVAL guards. Neither escalates anything, ever; that is the
    predicate's job. Passing the floor is not evidence of a violation, and the
    two must not be conflated again.
    """
    if not candidates:
        return []
    floor = float(guard.get("gate_similarity_floor", 0.45))
    alpha = float(guard.get("gate_top_fraction_alpha", 0.85))
    top = max((c.get("similarity") or 0.0) for c in candidates)
    relative = alpha * top
    return [c for c in candidates
            if (c.get("similarity") or 0.0) >= floor
            and (c.get("similarity") or 0.0) >= relative]


async def _log_gate_matches(backend, namespace: str, intent_hash_: str,
                            matched: list[dict], gate_audit: list[dict],
                            guard: dict, semantic_raw: list[dict],
                            *, clinical: bool) -> None:
    """Append the calibration record for this intent's skill candidates.

    Logs candidates that PASSED the guard and, critically, the near-floor ones
    that did not: a dataset containing only survivors cannot tell you whether
    the floor is right. passed_guard distinguishes them.

    PHI: intent_hash only — this table has no goal column to leak into.

    Best-effort by construction. The verdict has already been decided by the
    time this runs, and a calibration-log failure must never change or fail a
    gate decision.
    """
    audit_by_key = {a["skill_key"]: a for a in gate_audit}
    top = max((c.get("similarity") or 0.0) for c in semantic_raw) if semantic_raw else None
    rows = []
    passed_keys = {m["key"] for m in matched}
    seen: set[str] = set()

    for m in matched:
        if not m["key"].startswith(SKILL_PREFIX):
            continue
        seen.add(m["key"])
        audit = audit_by_key.get(m["key"], {})
        rows.append((namespace, intent_hash_, m["key"], m.get("similarity"), top,
                     guard.get("gate_similarity_floor"),
                     guard.get("gate_top_fraction_alpha"), True,
                     audit.get("predicate_match"), None,
                     guard.get("temporal_mode"), guard.get("calibration_ts")))
    # Guard-rejected skill candidates: the negative half of the dataset.
    for c in semantic_raw:
        if not c["key"].startswith(SKILL_PREFIX) or c["key"] in passed_keys or c["key"] in seen:
            continue
        seen.add(c["key"])
        rows.append((namespace, intent_hash_, c["key"], c.get("similarity"), top,
                     guard.get("gate_similarity_floor"),
                     guard.get("gate_top_fraction_alpha"), False,
                     None, None,
                     guard.get("temporal_mode"), guard.get("calibration_ts")))
    if not rows:
        return
    try:
        async with backend.pool.connection() as conn:
            await conn.cursor().executemany(
                "INSERT INTO gate_match_log (namespace, intent_hash, skill_key, "
                "cosine, top_score, absolute_floor, alpha, passed_guard, "
                "predicate_match, nli_contradiction, temporal_mode, calibration_ts) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
    except Exception:  # pragma: no cover - calibration logging is never fatal
        pass


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
        # Retrieval is bounded by the HARD floor here; the per-namespace floor
        # and the relative guard are applied by _guarded_candidates(). Keeping
        # the near-floor rejects visible to that step is what lets the match log
        # record what the floor turned away.
        if sim >= RETRIEVAL_HARD_FLOOR:  # no fabricated matches (G1-5)
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

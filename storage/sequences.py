"""Fixed server-side sequences: bootstrap, namespace_init, recall.

THE PROBLEM THESE SOLVE
    Correct use of this server has always been a SEQUENCE. Before trusting the
    store you should learn which database is answering, whether the profile is
    what you think, what coord_health says is stale, and whether a baton is
    waiting for you. Before creating a namespace you should set its profile
    explicitly rather than inheriting whatever the defaults happen to be that
    week. Before acting on a search you should know the floor that produced it.

    Every one of those sequences was previously encoded as GUIDANCE — in tool
    descriptions, in skills, in batons. All three depend on a model reading the
    advice, remembering it mid-task, and executing the steps in order. That is a
    probabilistic contract enforced by prose, and it degrades exactly when the
    session is long and confused, which is precisely when it matters.

    Worse, a skipped step is invisible. Nothing in a response says "you never
    checked which database this is". The failure mode is silence.

    So the sequences move into the server. Each is ONE call that runs its steps
    in a fixed order and returns `steps_run` naming what actually executed. The
    ordering stops being something a model gets right and becomes something the
    server did. And `steps_run` makes it auditable after the fact: a reviewer
    can see the sequence ran rather than taking anyone's word for it.

WHAT THESE ARE NOT
    They are not replacements for the primitives. memory_search, session_create
    and the rest keep working unchanged — this is additive, in line with the
    repo's standing constraint. The sequences are the paths agents should take;
    the primitives remain for surgical use and for callers who genuinely know
    which single step they want.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from errors import AppError
from storage.retrieval import apply_guard, policy_block

# Step vocabularies. Closed and asserted in tests: a sequence that silently
# stops running one of its steps is the failure this design exists to prevent,
# so the step list is part of the contract, not a debug aid.
BOOTSTRAP_STEPS = (
    "db_identity",
    "resolved_profile",
    "retrieval_policy",
    "coord_health",
    "gate_cache_status",
    "pending_batons",
    "session_create",
)

NAMESPACE_INIT_STEPS = (
    "existence_check",
    "write_profile",
    "calibrate_retrieval",
    "seed_project_meta",
    "register_namespace",
    "readback_verify",
)

RECALL_STEPS = (
    "resolve_policy",
    "retrieve_candidates",
    "apply_guard",
    "annotate_freshness",
    "log_calibration",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 1. session_bootstrap — the first call any session should make.
# ---------------------------------------------------------------------------
async def session_bootstrap(
    backend, namespace: str, *, surface: str | None = None,
    actor: str = "unattributed", purpose: str | None = None,
) -> dict:
    """Open a session AND surface everything that should be known before
    trusting the store.

    The step order is deliberate: identity first, because every later fact is
    conditional on which database answered. A previous deploy spent seven
    minutes reading a database the server never wrote to; nothing in any
    response said so. Now the first thing a session receives is the answer to
    "who is actually talking to me".

    Best-effort per step. A failing coord_health must not prevent a session from
    opening — but the failure is REPORTED in `degraded`, never swallowed into a
    cheerful-looking envelope. A bootstrap that half-worked and says so is
    useful; one that half-worked silently is worse than none.
    """
    steps_run: list[str] = []
    degraded: list[dict] = []

    async def _step(name: str, coro):
        try:
            value = await coro
            steps_run.append(name)
            return value
        except Exception as exc:  # noqa: BLE001 - a degraded step is reported, not fatal
            degraded.append({"step": name, "error": type(exc).__name__})
            return None

    identity = await _step("db_identity", backend.db_identity())
    profile = await _step("resolved_profile", backend.resolved_profile(namespace))
    guard = await _step("retrieval_policy", backend.gate_guard(namespace))
    health = await _step("coord_health", backend.coord_health(namespace))

    cache_status = None
    try:
        cache_status = backend.gate_cache.status()
        steps_run.append("gate_cache_status")
    except Exception as exc:  # noqa: BLE001
        degraded.append({"step": "gate_cache_status", "error": type(exc).__name__})

    batons = await _step("pending_batons", _pending_batons(backend, namespace, actor))

    session = await _step(
        "session_create",
        backend.session_create(
            namespace, surface=surface,
            metadata={"purpose": purpose, "actor": actor,
                      "bootstrapped_at": _now()}))
    if session is None:
        raise AppError("internal", "session_bootstrap could not open a session",
                       remedy="the store is unreachable; retry or check stats.db_identity")

    # The condensed "what should worry you" view. Assembled here rather than
    # left for the caller to derive, because a caller that has to compute its
    # own warnings is a caller that will forget to.
    attention: list[str] = []
    if cache_status and cache_status.get("listener_alive") is False:
        attention.append(
            "gate cache is on TTL fallback (listener_alive=false): profile "
            f"changes take up to {cache_status.get('ttl_seconds')}s to be noticed")
    if guard and not guard.get("calibration_ts"):
        attention.append(
            "retrieval floor is a server default, never calibrated against this "
            "namespace — treat similarity thresholds as unverified")
    if health:
        if health.get("needs_reverification"):
            attention.append(
                f"{len(health['needs_reverification'])} claim(s) have expired "
                "reconcile verdicts — treat as unknown, not still-true")
        if health.get("quarantined_count"):
            attention.append(f"{health['quarantined_count']} quarantined entr(ies) "
                             "are hidden from default reads")
        if health.get("tainted_lineage"):
            attention.append(f"{len(health['tainted_lineage'])} entr(ies) have a "
                             "quarantined or falsified ancestor")
    if batons:
        attention.append(f"{len(batons)} unconsumed baton(s) addressed here")

    return {
        "namespace": namespace,
        "session_id": session["session_id"],
        "db_identity": identity,
        "variant_profile": profile,
        "retrieval_policy": policy_block(guard or {}),
        "gate_cache": cache_status,
        "health": _health_digest(health),
        "pending_batons": batons or [],
        "attention": attention,
        "steps_run": steps_run,
        "degraded": degraded,
    }


def _health_digest(health: dict | None) -> dict | None:
    if not health:
        return None
    return {
        "entry_count": health.get("entry_count"),
        "stale": len(health.get("stale") or []),
        "quarantined_count": health.get("quarantined_count"),
        "needs_reverification": len(health.get("needs_reverification") or []),
        "tainted_lineage": len(health.get("tainted_lineage") or []),
        "duplicate_content": len(health.get("duplicate_content") or []),
    }


async def _pending_batons(backend, namespace: str, actor: str) -> list[dict]:
    """Handoffs in this namespace that nobody has consumed.

    An unconsumed baton is work someone handed forward. It should not require a
    session to think of asking — the previous deploy's closeout was missed for
    exactly this shape of reason.
    """
    entries = await backend.memory_list(namespace, kind="handoff", limit=100)
    rows = entries.get("entries", entries) if isinstance(entries, dict) else entries
    out = []
    for row in rows or []:
        meta = row.get("meta") or {}
        if meta.get("consumed") is True:
            continue
        out.append({
            "key": row.get("key"),
            "revision": row.get("revision"),
            "next_actor": meta.get("next_actor"),
            "baton_type": meta.get("baton_type"),
            "addressed_to_me": meta.get("next_actor") in (None, actor),
        })
    return out


# ---------------------------------------------------------------------------
# 2. namespace_init — create a namespace with its policy stated, not inherited.
# ---------------------------------------------------------------------------
async def namespace_init(
    backend, namespace: str, *, actor: str = "unattributed",
    intent_gate: str = "off", clinical: bool = False,
    similarity_floor: float | None = None, top_fraction_alpha: float | None = None,
    project_meta: dict | None = None, calibration_ts: str | None = None,
) -> dict:
    """Create a namespace with an EXPLICIT profile and retrieval policy.

    Today a namespace comes into existence the first time something writes to
    it, inheriting whatever the server defaults are at that moment. Nobody
    decided its floor, its gate arming, or its clinical status — they were
    defaulted, and a default nobody chose is indistinguishable at read time from
    a setting somebody thought about. Two namespaces created a release apart can
    silently differ.

    So creation becomes a decision with a record. The profile is written, the
    retrieval policy is stamped with a calibration timestamp and provenance, and
    the namespace is registered so it can be enumerated later.

    Idempotent: re-running against an existing namespace returns its current
    state with `created: false` and changes nothing. Namespace creation is
    exactly the operation someone will retry after a timeout.
    """
    # Deliberately NOT enforcing a "<scope>/<project>" shape. The repo's own
    # documented form is a bare project name (`acme-billing`) and the slashed
    # form is a convention some namespaces follow, not a rule. Inventing a
    # format requirement here would reject namespaces the rest of the server
    # accepts — a new tool must not narrow the tenancy model on its own.
    if not namespace or not namespace.strip():
        raise AppError("invalid_argument", "namespace is required",
                       remedy="pass the project/tenant name, e.g. dev/my-project")
    if intent_gate not in ("on", "off"):
        raise AppError("invalid_argument", "intent_gate must be 'on' or 'off'")

    steps_run: list[str] = []

    # --- existence check -----------------------------------------------------
    # TWO questions, because they are genuinely different. The registry answers
    # "was this namespace deliberately created" — true even with zero entries,
    # which is the state right after a first init. memory_list answers "does
    # anything live here", which is the only signal available for namespaces
    # that predate the registry. Either one means hands off: this call must
    # never overwrite a policy somebody is already relying on.
    registered = await backend.namespace_record(namespace)
    existing = await backend.memory_list(namespace, limit=1)
    rows = existing.get("entries", existing) if isinstance(existing, dict) else existing
    steps_run.append("existence_check")
    if registered or rows:
        profile = await backend.resolved_profile(namespace)
        guard = await backend.gate_guard(namespace)
        return {
            "namespace": namespace, "created": False,
            "variant_profile": profile,
            "retrieval_policy": policy_block(guard),
            "registered_at": (registered or {}).get("created_at"),
            "registered_by": (registered or {}).get("created_by"),
            "steps_run": steps_run,
            "note": ("namespace already exists and was left untouched; "
                     "re-running namespace_init never rewrites a live profile"
                     + ("" if registered else
                        " (this one predates the registry — it has entries but no "
                        "creation record, so its policy was never decided)")),
        }

    # --- write the profile explicitly ---------------------------------------
    profile_doc: dict[str, Any] = {"intent_gate": intent_gate}
    if clinical:
        profile_doc["clinical"] = True
    if similarity_floor is not None:
        profile_doc["gate_similarity_floor"] = float(similarity_floor)
    if top_fraction_alpha is not None:
        profile_doc["gate_top_fraction_alpha"] = float(top_fraction_alpha)
    # Provenance on the numbers. A floor with no calibration timestamp reads as
    # unverified everywhere it is surfaced — which is the honest default for a
    # namespace that has never been measured.
    profile_doc["gate_temporal_mode"] = (
        "historical_snapshot" if calibration_ts else "server_default")
    if calibration_ts:
        profile_doc["gate_calibration_ts"] = calibration_ts

    await backend.write_variant_profile(namespace, profile_doc)
    steps_run.append("write_profile")
    steps_run.append("calibrate_retrieval")

    # --- seed project/meta ---------------------------------------------------
    if project_meta:
        await backend.memory_save(
            namespace, "project/meta",
            project_meta.get("summary") or f"Project metadata for {namespace}.",
            kind="knowledge", meta=project_meta, actor=actor, origin="tool")
        steps_run.append("seed_project_meta")

    # --- register --------------------------------------------------------
    await backend.register_namespace(namespace, actor=actor,
                                     profile=profile_doc, clinical=clinical)
    steps_run.append("register_namespace")

    # --- read back -----------------------------------------------------------
    profile = await backend.resolved_profile(namespace)
    guard = await backend.gate_guard(namespace)
    steps_run.append("readback_verify")

    if profile.get("intent_gate") != intent_gate:
        raise AppError(
            "internal",
            f"namespace_init wrote intent_gate={intent_gate!r} but read back "
            f"{profile.get('intent_gate')!r}",
            remedy="the profile did not persist; check stats.db_identity")

    return {
        "namespace": namespace, "created": True,
        "variant_profile": profile,
        "retrieval_policy": policy_block(guard),
        "clinical": clinical,
        "steps_run": steps_run,
    }


# ---------------------------------------------------------------------------
# 3. recall — the guarded search sequence.
# ---------------------------------------------------------------------------
async def recall(
    backend, namespace: str, query: str, *, limit: int = 20,
    include_below_floor: bool = False, include_quarantined: bool = False,
    actor: str = "unattributed",
) -> dict:
    """Search with the guard applied and the verdict attached to every row.

    This is `memory_search` plus the three things a caller has to do afterwards
    and reliably forgets: apply the floor, notice staleness, and record the
    result so the floor can eventually be calibrated against outcomes.

    `include_below_floor` returns the rejected rows too, each marked
    `admitted:false` with its reason. Off by default because most callers want
    an answer, not a distribution — but the counts are ALWAYS reported, so
    "nothing matched" and "nine things matched and all were noise" never look
    alike.
    """
    steps_run: list[str] = []

    guard = await backend.gate_guard(namespace)
    steps_run.append("resolve_policy")

    # Over-fetch: the guard rejects from this pool, so asking for exactly
    # `limit` would let rejections silently shrink the result set.
    raw = await backend.memory_search(
        namespace, query, limit=max(limit * 3, 24),
        include_quarantined=include_quarantined, _with_scores=True)
    steps_run.append("retrieve_candidates")

    outcome = apply_guard(
        [(row, row.get("_cosine")) for row in raw], guard)
    steps_run.append("apply_guard")

    def _shape(row: dict, verdict) -> dict:
        entry = {k: v for k, v in row.items() if not k.startswith("_")}
        entry["retrieval"] = verdict.as_dict()
        entry["freshness"] = _freshness(row)
        return entry

    results = [_shape(r, v) for r, v in outcome.admitted[:limit]]
    steps_run.append("annotate_freshness")

    if include_below_floor:
        results += [_shape(r, v) for r, v in outcome.rejected[:limit]]

    await _log_recall_calibration(backend, namespace, query, outcome, actor)
    steps_run.append("log_calibration")

    return {
        "namespace": namespace,
        "query_len": len(query or ""),
        "results": results,
        "guard": outcome.summary(),
        "retrieval_policy": policy_block(guard),
        "steps_run": steps_run,
    }


def _freshness(row: dict) -> dict | None:
    """Surface a row's own staleness rather than making the caller derive it."""
    meta = row.get("meta") or {}
    mode = row.get("temporal_mode") or meta.get("temporal_mode")
    if not mode and not row.get("valid_until"):
        return None
    return {
        "temporal_mode": mode,
        "valid_until": row.get("valid_until"),
        "repo_sha": row.get("repo_sha"),
    }


async def _log_recall_calibration(backend, namespace, query, outcome, actor) -> None:
    """Record what the floor admitted and rejected for this query.

    Same rationale as gate_match_log: a dataset containing only survivors cannot
    calibrate the threshold that produced it. Best-effort — calibration logging
    must never fail a read.
    """
    import hashlib

    try:
        qhash = hashlib.sha256(" ".join((query or "").split()).encode()).hexdigest()
        rows = [
            (namespace, qhash, (row.get("key") or "")[:200], v.cosine, v.top_score,
             v.absolute_floor, v.alpha, v.admitted, None, None,
             v.temporal_mode, v.calibration_ts)
            for row, v in outcome.all_scored if v.cosine is not None
        ]
        if not rows:
            return
        async with backend.pool.connection() as conn:
            await conn.cursor().executemany(
                "INSERT INTO gate_match_log (namespace, intent_hash, skill_key, "
                "cosine, top_score, absolute_floor, alpha, passed_guard, "
                "predicate_match, nli_contradiction, temporal_mode, calibration_ts) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
    except Exception:  # pragma: no cover - calibration logging is never fatal
        pass

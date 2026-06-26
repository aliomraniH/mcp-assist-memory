"""PostgresBackend: the StorageBackend ABC implemented over the single pool.

Every method runs on ``self.pool.connection()``; nothing here opens its own
connection. Writes pass through ``sanitize``; revisions/seqs are computed
server-side in the same statement and protected by unique constraints (with a
small retry on the rare concurrent collision). ``event_id`` gives exactly-once
writes: a duplicate is a no-op that returns the already-applied revision.

Tenancy: ``namespace`` is the project tenant. Memory, handoff, and session reads
and writes all filter on it; there are no implicit cross-project reads. Handoffs
are stored as ``kind='handoff'`` rows inside the caller's own namespace (no
shared global handoff space). Artifacts are content-addressed and global.
"""
from __future__ import annotations

import functools
import hashlib
import json
import uuid
from typing import Any

import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from config import settings
from storage.base import StorageBackend
from storage.embeddings import DisabledEmbedder, Embedder, embed_text, to_vector_literal
from storage.reconcile import DisabledResolver, Resolver, reconcile_claim
from storage.sanitize import sanitize, wrap_value

_MAX_RETRIES = 3

# How many times a transient connection drop is retried (on a fresh connection).
_CONN_RETRIES = 3
# psycopg raises one of these when a connection is lost or already closed.
_CONN_EXC = (psycopg.OperationalError, psycopg.InterfaceError)


def _is_disconnect(exc: BaseException) -> bool:
    """True only for genuine connection-loss errors (not every OperationalError).

    Covers client-side "connection is closed" (no SQLSTATE), the 08xxx connection
    class, and operator-intervention shutdowns 57P01/57P02/57P03 — e.g. Neon
    scale-down's "terminating connection due to administrator command" (57P01).
    A non-connection OperationalError (lock timeout, too-many-connections, …) is
    left to surface unchanged.
    """
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate is None:
        return isinstance(exc, _CONN_EXC)
    return sqlstate.startswith("08") or sqlstate in {"57P01", "57P02", "57P03"}


def _retry_on_disconnect(fn):
    """Retry a backend op on a dropped connection. Each call re-enters
    ``self.pool.connection()``, so the retry runs on a fresh (pool-validated)
    connection. Apply only to reads and idempotent writes: a terminated backend
    rolls an in-flight transaction back, but a drop in the narrow
    commit-but-before-ack window would otherwise replay a non-idempotent write.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        last: Exception | None = None
        for _ in range(_CONN_RETRIES):
            try:
                return await fn(*args, **kwargs)
            except _CONN_EXC as exc:
                if not _is_disconnect(exc):
                    raise
                last = exc
        raise last  # exhausted reconnect attempts
    return wrapper


def _retry_if_idempotent(fn):
    """Disconnect-retry a write only when it carries an ``event_id`` (exactly-once):
    a replay then collapses to a no-op. Without one, the op runs once and a
    disconnect surfaces to the caller, so there is never a silent double-write."""
    retrying = _retry_on_disconnect(fn)

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        target = retrying if kwargs.get("event_id") else fn
        return await target(*args, **kwargs)
    return wrapper


# Coordination-envelope keys projected out of `meta` into indexed columns. The
# full envelope is still stored in the `meta` jsonb column, losslessly.
_META_COLS = ("repo_sha", "base_sha", "branch", "dirty", "session_id")


def _content_hash(value: Any) -> str:
    """sha256 over a canonical JSON encoding of the (already-sanitized) value.

    The always-present, git-free dimension of the version vector: identical
    facts hash identically regardless of surface or namespace, which is what the
    coordination detectors group on. Sort keys so dict ordering doesn't matter."""
    canon = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _split_meta(meta: dict | None) -> tuple[Any, Any, Any, Any, Any, Any]:
    """Return ``(repo_sha, base_sha, branch, dirty, session_id, meta_jsonb)``.

    The five well-known keys become indexed columns; the whole envelope is kept
    as jsonb (None when empty) so nothing the caller sent is dropped. A non-dict
    ``meta`` is ignored (treated as absent) rather than failing the write."""
    if not isinstance(meta, dict) or not meta:
        return (None, None, None, None, None, None)
    repo_sha, base_sha, branch, dirty, session_id = (meta.get(c) for c in _META_COLS)
    return repo_sha, base_sha, branch, dirty, session_id, Jsonb(meta)


def _row_to_entry(row: dict, *, wrap: bool = True) -> dict:
    value = row["value"]
    return {
        "namespace": row["namespace"],
        "key": row["key"],
        "revision": row["revision"],
        "kind": row["kind"],
        "value": wrap_value(value) if wrap else value,
        "tags": list(row.get("tags") or []),
        "source_surface": row.get("source_surface"),
        "tombstone": row.get("tombstone", False),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        # Coordination provenance (0003). All optional — null when the writer
        # supplied no envelope (e.g. a surface that can't compute a SHA).
        "repo_sha": row.get("repo_sha"),
        "base_sha": row.get("base_sha"),
        "branch": row.get("branch"),
        "dirty": row.get("dirty"),
        "session_id": row.get("session_id"),
        "meta": row.get("meta"),
        "content_hash": row.get("content_hash"),
    }


_RRF_K = 60


def _rrf_fuse(semantic_rows, keyword_rows, limit, *, k: int = _RRF_K) -> list[dict]:
    """Reciprocal Rank Fusion of the meaning and keyword legs into one ranked list.

    Each leg contributes ``1 / (k + rank)`` (rank is 1-based) to a key's score, so
    a key present in BOTH legs sums two contributions and floats above one that
    only tops a single leg — a true blended ranking rather than concatenating the
    cosine list with keyword backfill. Ties break on the better (smaller)
    individual rank, then key, for a deterministic order. The row payload comes
    from whichever leg saw the key first (semantic, then keyword)."""
    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    rows_by_key: dict[str, dict] = {}
    for leg in (semantic_rows, keyword_rows):
        for rank, row in enumerate(leg, start=1):
            key = row["key"]
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            if key not in best_rank or rank < best_rank[key]:
                best_rank[key] = rank
            rows_by_key.setdefault(key, row)
    ordered = sorted(scores, key=lambda key: (-scores[key], best_rank[key], key))
    return [_row_to_entry(rows_by_key[key]) for key in ordered[:limit]]


def _session_to_dict(row: dict) -> dict:
    return {
        "session_id": str(row["session_id"]),
        "namespace": row["namespace"],
        "surface": row["surface"],
        "metadata": row["metadata"],
        "created_at": row["created_at"].isoformat(),
    }


def _event_to_dict(row: dict) -> dict:
    return {
        "session_id": str(row["session_id"]),
        "namespace": row["namespace"],
        "seq": row["seq"],
        "kind": row["kind"],
        "payload": wrap_value(row["payload"]),
        "created_at": row["created_at"].isoformat(),
    }


class PostgresBackend(StorageBackend):
    def __init__(self, pool, embedder: Embedder | None = None, resolver: Resolver | None = None) -> None:
        self.pool = pool
        # Best-effort semantic recall. Defaults to disabled so the backend works
        # with no provider key (keyword-only search, no embeddings written).
        self.embedder: Embedder = embedder or DisabledEmbedder()
        # Best-effort claim reconciliation. Defaults to disabled so the backend
        # works with no GitHub token (claims reconcile to "unverifiable").
        self.resolver: Resolver = resolver or DisabledResolver()

    async def _maybe_embed(self, key, value, tombstone) -> str | None:
        """Embed an entry's text for storage. Best-effort: returns None when
        embeddings are disabled, the row is a tombstone, or the provider fails —
        never raises, so a write is never blocked by the embedding path."""
        if not self.embedder.enabled or tombstone:
            return None
        try:
            vecs = await self.embedder.embed([embed_text(key, value)], input_type="document")
        except Exception:  # noqa: BLE001 - embedding is best-effort, fall back to None
            return None
        return self._safe_literal(vecs)

    async def _maybe_embed_query(self, query: str) -> str | None:
        """Embed a search query. Best-effort: None falls search back to keyword."""
        if not self.embedder.enabled:
            return None
        try:
            vecs = await self.embedder.embed([query], input_type="query")
        except Exception:  # noqa: BLE001 - fall back to keyword search
            return None
        return self._safe_literal(vecs)

    def _safe_literal(self, vecs) -> str | None:
        """Turn an embedder result into a pgvector literal, or None on anything
        unexpected. Guards the best-effort contract: a wrong-length / malformed
        vector returns None (keyword-only) instead of failing the ::vector cast
        inside the write transaction and blocking the write."""
        if not vecs:
            return None
        vec = vecs[0]
        expected = getattr(self.embedder, "dim", None)
        if not vec or (expected is not None and len(vec) != expected):
            return None
        try:
            return to_vector_literal(vec)
        except (TypeError, ValueError):  # non-numeric entries
            return None

    # ----------------------------------------------------------------- memory
    async def _seen_event(self, conn, event_id: str) -> dict | None:
        cur = await conn.execute(
            "SELECT * FROM memory_entry WHERE event_id = %s ORDER BY revision DESC LIMIT 1",
            (event_id,),
        )
        return await cur.fetchone()

    async def _append(
        self, namespace, key, value, kind, tags, source_surface, event_id, tombstone, meta=None
    ) -> dict:
        # Embed BEFORE taking a pooled connection so the (network) embedding call
        # never holds a connection, and compute it once so retries don't re-embed.
        embedding = await self._maybe_embed(key, value, tombstone)
        repo_sha, base_sha, branch, dirty, session_id, meta_json = _split_meta(meta)
        sanitized = sanitize(value)
        # Tombstones carry no content hash (the value is a delete marker, not a fact).
        content_hash = None if tombstone else _content_hash(sanitized)
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            # Exactly-once: if this event already landed, return it unchanged.
            if event_id:
                existing = await self._seen_event(conn, event_id)
                if existing is not None:
                    return _row_to_entry(existing)

            payload = Jsonb(sanitized)
            tags = tags or []
            last_exc: Exception | None = None
            for _ in range(_MAX_RETRIES):
                try:
                    async with conn.transaction():
                        cur = await conn.execute(
                            """
                            INSERT INTO memory_entry
                                (namespace, key, revision, kind, value, source_surface, tags, event_id, tombstone, embedding,
                                 repo_sha, base_sha, branch, dirty, session_id, meta, content_hash)
                            SELECT %s, %s,
                                   COALESCE(MAX(revision), 0) + 1,
                                   %s, %s, %s, %s, %s, %s, %s::vector,
                                   %s, %s, %s, %s, %s, %s, %s
                            FROM memory_entry WHERE namespace = %s AND key = %s
                            RETURNING *
                            """,
                            (namespace, key, kind, payload, source_surface, tags, event_id,
                             tombstone, embedding,
                             repo_sha, base_sha, branch, dirty, session_id, meta_json, content_hash,
                             namespace, key),
                        )
                        row = await cur.fetchone()
                        return _row_to_entry(row)
                except pg_errors.UniqueViolation as exc:
                    last_exc = exc
                    # Either a concurrent revision collision (retry) or a racing
                    # duplicate event_id (return the winner).
                    if event_id:
                        async with self.pool.connection() as c2:
                            c2.row_factory = dict_row
                            existing = await self._seen_event(c2, event_id)
                            if existing is not None:
                                return _row_to_entry(existing)
                    continue
            raise last_exc  # exhausted retries

    @_retry_if_idempotent
    async def memory_save(
        self, namespace, key, value, *, kind="note", tags=None, source_surface=None, event_id=None, meta=None
    ) -> dict:
        return await self._append(namespace, key, value, kind, tags, source_surface, event_id, False, meta=meta)

    @_retry_on_disconnect
    async def memory_get(self, namespace, key) -> dict | None:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT * FROM memory_entry WHERE namespace = %s AND key = %s ORDER BY revision DESC LIMIT 1",
                (namespace, key),
            )
            row = await cur.fetchone()
        if row is None or row["tombstone"]:
            return None
        return _row_to_entry(row)

    @_retry_on_disconnect
    async def memory_list(self, namespace, *, kind=None, tag=None, limit=100) -> list[dict]:
        clauses = ["namespace = %s"]
        params: list[Any] = [namespace]
        if kind:
            clauses.append("kind = %s")
            params.append(kind)
        if tag:
            clauses.append("%s = ANY(tags)")
            params.append(tag)
        where = " AND ".join(clauses)
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                f"""
                SELECT DISTINCT ON (key) *
                FROM memory_entry
                WHERE {where}
                ORDER BY key, revision DESC
                """,
                params,
            )
            rows = await cur.fetchall()
        live = [_row_to_entry(r) for r in rows if not r["tombstone"]]
        return live[:limit]

    @_retry_on_disconnect
    async def memory_history(self, namespace, key, *, limit=50) -> list[dict]:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT * FROM memory_entry WHERE namespace = %s AND key = %s "
                "ORDER BY revision DESC LIMIT %s",
                (namespace, key, limit),
            )
            rows = await cur.fetchall()
        return [_row_to_entry(r) for r in rows]

    @_retry_if_idempotent
    async def memory_delete(self, namespace, key, *, source_surface=None, event_id=None, meta=None) -> dict:
        # Tombstone = append a deleting revision (history is preserved). `meta`
        # lets a delete record the provenance of the deletion (who/at-what-sha).
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT kind FROM memory_entry WHERE namespace = %s AND key = %s "
                "ORDER BY revision DESC LIMIT 1",
                (namespace, key),
            )
            latest = await cur.fetchone()
        kind = latest["kind"] if latest else "note"
        return await self._append(namespace, key, {"deleted": True}, kind, [], source_surface, event_id, True, meta=meta)

    @_retry_on_disconnect
    async def memory_search(self, namespace, query, *, limit=20) -> list[dict]:
        # Tenant-scoped: every leg filters on a single namespace first — no
        # implicit cross-project reads.
        #
        # Hybrid recall: embed the query and rank live entries two ways — by
        # meaning (cosine over pgvector) and by keyword (substring) — then fuse the
        # two rankings into ONE list with Reciprocal Rank Fusion (see _rrf_fuse) so
        # an entry that scores on both signals can outrank one that only tops a
        # single leg. When embeddings are disabled or the provider fails, `qvec` is
        # None and we degrade to pure keyword search — the exact pre-Phase-3
        # behavior (no fusion, keyword order preserved).
        qvec = await self._maybe_embed_query(query)
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row

            if qvec is None:
                # Pure-keyword fallback: latest revision per key first, then keep
                # non-tombstoned substring matches (filtering before the DISTINCT ON
                # would resurface a deleted key whose earlier revision matched).
                cur = await conn.execute(
                    """
                    SELECT * FROM (
                        SELECT DISTINCT ON (key) *
                        FROM memory_entry
                        WHERE namespace = %s
                        ORDER BY key, revision DESC
                    ) latest
                    WHERE NOT tombstone AND value::text ILIKE %s
                    LIMIT %s
                    """,
                    (namespace, f"%{query}%", limit),
                )
                rows = await cur.fetchall()
                return [_row_to_entry(r) for r in rows[:limit]]

            # Semantic leg. Tune HNSW recall for this query only via
            # set_config(..., is_local=true): the value is scoped to the
            # surrounding transaction, so ef_search never leaks onto the pooled
            # connection's later reuse (e.g. the keyword leg below). Higher
            # ef_search inspects more index candidates → better recall on large
            # stores, at a little latency; small stores return the same rows
            # regardless. See settings.hnsw_ef_search for the tradeoff.
            #
            # Pick the TRUE latest revision per key first, THEN keep only the live
            # ones that carry an embedding. Filtering embeddings before the
            # DISTINCT ON would let a tombstone's prior (embedded) revision
            # resurface, leaking deleted keys.
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('hnsw.ef_search', %s, true)",
                    (str(settings.hnsw_ef_search),),
                )
                cur = await conn.execute(
                    """
                    SELECT * FROM (
                        SELECT DISTINCT ON (key) *
                        FROM memory_entry
                        WHERE namespace = %s
                        ORDER BY key, revision DESC
                    ) latest
                    WHERE NOT tombstone AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (namespace, qvec, limit),
                )
                semantic_rows = await cur.fetchall()

            # Keyword leg. Same latest-then-filter shape: take the latest revision
            # per key first, then keep non-tombstoned substring matches.
            cur = await conn.execute(
                """
                SELECT * FROM (
                    SELECT DISTINCT ON (key) *
                    FROM memory_entry
                    WHERE namespace = %s
                    ORDER BY key, revision DESC
                ) latest
                WHERE NOT tombstone AND value::text ILIKE %s
                LIMIT %s
                """,
                (namespace, f"%{query}%", limit),
            )
            keyword_rows = await cur.fetchall()

        return _rrf_fuse(semantic_rows, keyword_rows, limit)

    # ----------------------------------------------------------- coordination
    @_retry_on_disconnect
    async def coord_health(self, namespace, *, limit=200) -> dict:
        """Drift report for ONE namespace — computed from stored provenance, no
        git required. Surfaces three things a reader would otherwise eyeball:

        * ``stale``       — live entries whose repo_sha is behind the namespace's
                            most-recently-observed repo_sha (a git-free proxy for
                            "this predates current code → re-verify").
        * ``duplicate_content`` — distinct keys holding an identical fact
                            (same content_hash) — a restate/fork to reconcile.
        * ``claim_collisions``  — multiple live claims about the same subject
                            (meta.subject, or meta.pr) — the rev2-vs-rev3 class,
                            caught before a human has to.
        """
        from collections import defaultdict

        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                """
                SELECT DISTINCT ON (key) key, revision, kind, repo_sha,
                       content_hash, value, meta, tombstone, created_at
                FROM memory_entry WHERE namespace = %s
                ORDER BY key, revision DESC
                """,
                (namespace,),
            )
            rows = [r for r in await cur.fetchall() if not r["tombstone"]]

        with_sha = [r for r in rows if r["repo_sha"]]
        latest_repo_sha = (
            max(with_sha, key=lambda r: r["created_at"])["repo_sha"] if with_sha else None
        )
        stale = [
            {"key": r["key"], "repo_sha": r["repo_sha"], "revision": r["revision"]}
            for r in with_sha
            if r["repo_sha"] != latest_repo_sha
        ]

        by_hash: dict[str, list[str]] = defaultdict(list)
        for r in rows:
            by_hash[r["content_hash"] or _content_hash(r["value"])].append(r["key"])
        duplicate_content = [
            {"content_hash": h, "keys": sorted(keys)}
            for h, keys in by_hash.items() if len(keys) > 1
        ]

        by_subject: dict[str, set] = defaultdict(set)
        for r in rows:
            if r["kind"] != "claim":
                continue
            meta = r["meta"] or {}
            subject = meta.get("subject")
            if subject is None and meta.get("pr") is not None:
                subject = f"pr:{meta['pr']}"
            if subject is not None:
                by_subject[str(subject)].add(r["key"])
        claim_collisions = [
            {"subject": s, "keys": sorted(keys)}
            for s, keys in by_subject.items() if len(keys) > 1
        ]

        return {
            "namespace": namespace,
            "entry_count": len(rows),
            "latest_repo_sha": latest_repo_sha,
            "stale": stale[:limit],
            "duplicate_content": duplicate_content[:limit],
            "claim_collisions": claim_collisions[:limit],
        }

    @_retry_on_disconnect
    async def coord_drift_scan(self, *, limit=50) -> dict:
        """Store-wide: the same fact living under more than one namespace — the
        namespace-drift class (e.g. canvas-case vs canvas-glp1). DELIBERATELY
        cross-tenant, like ``stats``: a coordination/admin scan, not a per-project
        read. Groups live entries by content_hash (computed on the fly for legacy
        rows that predate the column) and returns hashes spanning >1 namespace."""
        from collections import defaultdict

        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                """
                SELECT DISTINCT ON (namespace, key) namespace, key, content_hash, value, tombstone
                FROM memory_entry
                ORDER BY namespace, key, revision DESC
                """
            )
            rows = await cur.fetchall()

        groups: dict[str, list[tuple]] = defaultdict(list)
        for r in rows:
            if r["tombstone"]:
                continue
            h = r["content_hash"] or _content_hash(r["value"])
            groups[h].append((r["namespace"], r["key"]))

        drift = []
        for h, items in groups.items():
            namespaces = {ns for ns, _ in items}
            if len(namespaces) > 1:
                drift.append({
                    "content_hash": h,
                    "namespaces": sorted(namespaces),
                    "entries": sorted(f"{ns}/{k}" for ns, k in items),
                })
        drift.sort(key=lambda d: (-len(d["namespaces"]), d["content_hash"]))
        return {"suspected_namespace_drift": drift[:limit]}

    # --------------------------------------------------------- reconciliation
    _RECONCILE_PREFIX = "coord/_reconcile/"

    async def _reconcile_rows(self, rows: list[dict]) -> list[dict]:
        """Reconcile a set of live claim rows and write each verdict to its own
        append-only ``coord/_reconcile/<key>`` record. Never touches the claim."""
        verdicts = []
        for r in rows:
            entry = _row_to_entry(r, wrap=False)
            verdict = await reconcile_claim(entry, self.resolver)
            await self.memory_save(
                r["namespace"], f"{self._RECONCILE_PREFIX}{r['key']}", verdict,
                kind="config", tags=["reconcile", verdict["state"]],
            )
            verdicts.append(verdict)
        return verdicts

    @_retry_on_disconnect
    async def coord_reconcile(self, namespace, *, limit=100) -> dict:
        """Reconcile every live claim in a namespace against GitHub (off the
        agent's critical path) and record an append-only verdict per claim. When
        the resolver is disabled every verdict is ``unverifiable`` — never
        silently ``current``. Returns the verdicts and whether the resolver ran."""
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                """
                SELECT DISTINCT ON (key) * FROM memory_entry
                WHERE namespace = %s AND kind = 'claim'
                ORDER BY key, revision DESC
                """,
                (namespace,),
            )
            rows = [r for r in await cur.fetchall() if not r["tombstone"]][:limit]
        verdicts = await self._reconcile_rows(rows)
        return {
            "namespace": namespace,
            "resolver_enabled": self.resolver.enabled,
            "reconciled": len(verdicts),
            "verdicts": verdicts,
        }

    @_retry_on_disconnect
    async def coord_reconcile_repo(self, repo, *, pr=None, branch=None, limit=500) -> dict:
        """Store-wide (admin, like drift_scan): reconcile every live claim whose
        ``meta.repo`` matches ``repo`` — optionally narrowed to a PR or branch.
        Used by the GitHub webhook so a merge/push reconciles affected claims
        across all namespaces at once."""
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                """
                SELECT DISTINCT ON (namespace, key) * FROM memory_entry
                WHERE kind = 'claim' AND meta->>'repo' = %s
                ORDER BY namespace, key, revision DESC
                """,
                (repo,),
            )
            rows = []
            for r in await cur.fetchall():
                if r["tombstone"]:
                    continue
                meta = r.get("meta") or {}
                if pr is not None and str(meta.get("pr")) != str(pr):
                    continue
                if branch is not None and meta.get("branch") != branch:
                    continue
                rows.append(r)
        verdicts = await self._reconcile_rows(rows[:limit])
        return {"repo": repo, "resolver_enabled": self.resolver.enabled,
                "reconciled": len(verdicts), "verdicts": verdicts}

    # ---------------------------------------------------------------- handoff
    # Handoffs are cross-surface (web/cli/desktop) within ONE project: stored as
    # kind='handoff' rows inside the caller's namespace, never a shared space.
    @_retry_if_idempotent
    async def handoff_save(self, namespace, key, value, *, source_surface=None, event_id=None, meta=None) -> dict:
        return await self._append(namespace, key, value, "handoff", [], source_surface, event_id, False, meta=meta)

    async def handoff_load(self, namespace, key) -> dict | None:
        return await self.memory_get(namespace, key)

    async def handoff_list(self, namespace, *, limit=100) -> list[dict]:
        return await self.memory_list(namespace, kind="handoff", limit=limit)

    # --------------------------------------------------------------- sessions
    @_retry_on_disconnect  # a drop in the commit-ack window at worst orphans an
                           # unreferenced empty session row — harmless vs. a hard failure.
    async def session_create(self, namespace, *, surface=None, metadata=None) -> dict:
        sid = uuid.uuid4()
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "INSERT INTO session (session_id, namespace, surface, metadata) "
                "VALUES (%s, %s, %s, %s) RETURNING *",
                (sid, namespace, surface, Jsonb(metadata or {})),
            )
            row = await cur.fetchone()
        return _session_to_dict(row)

    @_retry_on_disconnect  # at-least-once under a mid-failover drop: a replay in the
                           # narrow commit-ack window may append one duplicate event —
                           # acceptable for an append-only log vs. failing the call.
    async def session_append_event(self, namespace, session_id, kind, payload) -> dict:
        last_exc: Exception | None = None
        for _ in range(_MAX_RETRIES):
            try:
                async with self.pool.connection() as conn:
                    conn.row_factory = dict_row
                    async with conn.transaction():
                        # Tenant guard: the session must exist under this namespace.
                        cur = await conn.execute(
                            "SELECT 1 FROM session WHERE session_id = %s AND namespace = %s",
                            (session_id, namespace),
                        )
                        if await cur.fetchone() is None:
                            raise ValueError("session not found in namespace")
                        cur = await conn.execute(
                            """
                            INSERT INTO session_event (session_id, namespace, seq, kind, payload)
                            SELECT %s, %s, COALESCE(MAX(seq), 0) + 1, %s, %s
                            FROM session_event WHERE session_id = %s
                            RETURNING *
                            """,
                            (session_id, namespace, kind, Jsonb(sanitize(payload)), session_id),
                        )
                        row = await cur.fetchone()
                return _event_to_dict(row)
            except pg_errors.UniqueViolation as exc:
                last_exc = exc
                continue
        raise last_exc

    @_retry_on_disconnect
    async def session_get(self, namespace, session_id) -> dict | None:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT * FROM session WHERE session_id = %s AND namespace = %s",
                (session_id, namespace),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return _session_to_dict(row)

    @_retry_on_disconnect
    async def session_list(self, namespace, *, limit=50) -> list[dict]:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT * FROM session WHERE namespace = %s ORDER BY created_at DESC LIMIT %s",
                (namespace, limit),
            )
            rows = await cur.fetchall()
        return [_session_to_dict(r) for r in rows]

    @_retry_on_disconnect
    async def session_events(self, namespace, session_id, *, limit=200) -> list[dict]:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT * FROM session_event WHERE session_id = %s AND namespace = %s "
                "ORDER BY seq ASC LIMIT %s",
                (session_id, namespace, limit),
            )
            rows = await cur.fetchall()
        return [_event_to_dict(r) for r in rows]

    # -------------------------------------------------------------- artifacts
    @_retry_on_disconnect  # content-addressed + ON CONFLICT DO NOTHING → safe to replay
    async def artifact_put(self, data: bytes, *, content_type=None) -> dict:
        sha = hashlib.sha256(data).hexdigest()
        size = len(data)
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            # Content-addressed + dedup: a repeat blob is a no-op.
            cur = await conn.execute(
                """
                INSERT INTO artifact (sha256, bytes, size, content_type)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (sha256) DO NOTHING
                RETURNING sha256
                """,
                (sha, data, size, content_type),
            )
            inserted = await cur.fetchone()
        return {"sha256": sha, "size": size, "content_type": content_type, "deduped": inserted is None}

    @_retry_on_disconnect
    async def artifact_get(self, sha256) -> dict | None:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT sha256, size, content_type, created_at FROM artifact WHERE sha256 = %s",
                (sha256,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "sha256": row["sha256"],
            "size": row["size"],
            "content_type": row["content_type"],
            "created_at": row["created_at"].isoformat(),
        }

    @_retry_on_disconnect
    async def artifact_read_range(self, sha256, offset: int, length: int) -> bytes | None:
        # Ranged read keeps peak memory to one window, never the whole blob.
        # Use dict_row consistently — pooled connections retain whatever
        # row_factory a prior call set, so positional access is unsafe here.
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT substring(bytes FROM %s FOR %s) AS chunk FROM artifact WHERE sha256 = %s",
                (offset + 1, length, sha256),  # SQL substring is 1-indexed
            )
            row = await cur.fetchone()
        if row is None:
            return None
        chunk = row["chunk"]
        return bytes(chunk) if chunk is not None else b""

    @_retry_on_disconnect
    async def artifact_list(self, *, limit=100) -> list[dict]:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT sha256, size, content_type, created_at FROM artifact ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            rows = await cur.fetchall()
        return [
            {
                "sha256": r["sha256"],
                "size": r["size"],
                "content_type": r["content_type"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ admin
    @_retry_on_disconnect
    async def stats(self) -> dict:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                """
                SELECT
                    (SELECT count(*) FROM memory_entry)                       AS memory_revisions,
                    (SELECT count(DISTINCT (namespace, key)) FROM memory_entry) AS memory_keys,
                    (SELECT count(*) FROM session)                            AS sessions,
                    (SELECT count(*) FROM session_event)                      AS session_events,
                    (SELECT count(*) FROM artifact)                           AS artifacts,
                    (SELECT COALESCE(sum(size), 0) FROM artifact)             AS artifact_bytes
                """
            )
            row = await cur.fetchone()
        return dict(row)

    @_retry_on_disconnect
    async def health(self) -> bool:
        async with self.pool.connection() as conn:
            await conn.execute("SELECT 1")
        return True

"""In-process cache for gate inputs, invalidated by LISTEN/NOTIFY (Phase 3a).

WHY A LISTENER AND NOT JUST A TTL
    Tier-0 pre-flight costs approximately one Neon round trip, and the measured
    p95 (64ms) is statistically identical to the readback latency on the same
    acks (median 60ms). The gate is not slow; it is paying for a network hop to
    read values that change perhaps twice a month. Caching them removes the hop.

    But a cache over gate inputs is a trap unless invalidation is real. The gate
    exists to stop stale beliefs from being acted on, so a gate that serves its
    own stale configuration would be a particularly bad joke. Hence:

      * NOTIFY on change, so a profile edit takes effect in milliseconds;
      * a TTL on every entry regardless, so a dead listener degrades to slow
        rather than to wrong;
      * versioned keys, so an out-of-order or replayed notification cannot move
        the cache backwards;
      * full reload on reconnect, because a listener that was down missed
        notifications it will never receive again — and "I have been offline and
        I do not know what changed" must resolve to reload, never to trust.

    An expired entry reads as UNKNOWN, not as still-true. That is the same
    freshness discipline the reconcile verdicts already follow, applied to the
    gate's own configuration.

NEON TOPOLOGY (vendor-confirmed, and the reason this module is hand-rolled)
    Neon's POOLED endpoint supports NOTIFY but NOT LISTEN: PgBouncer in
    transaction mode drops session-level features, and LISTEN is a session-level
    subscription. pg_notify() inside a transaction is fine on the pooled
    connection because it is just a statement.

    So: app traffic and every NOTIFY emit stay on the pooled endpoint, and the
    subscriber needs exactly ONE dedicated DIRECT (non `-pooler`) connection.
    That is why DATABASE_URL_DIRECT exists as a separate secret, and why the
    credential-rotation runbook insists both strings be updated together.

    PGCacheWatch would have covered this; it is inactive and low-adoption, so
    the listener is hand-rolled with reconnect supervision rather than taking a
    dependency on an unmaintained project for a load-bearing component.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

log = structlog.get_logger("assist-memory.gate_cache")

CHANNEL = "gate_invalidate"
DEFAULT_TTL_S = 30.0

# Reconnect backoff. Capped so a long outage still retries at a sane cadence,
# and jitter-free because a single subscriber cannot stampede anything.
_BACKOFF_START_S = 0.5
_BACKOFF_MAX_S = 30.0


class GateCache:
    """Versioned, TTL-bounded cache of per-namespace gate inputs."""

    def __init__(self, ttl_s: float = DEFAULT_TTL_S) -> None:
        self._ttl_s = ttl_s
        # namespace -> (stored_at, version, value)
        self._entries: dict[str, tuple[float, int, Any]] = {}
        self._version = 0
        self._listener_alive = False
        self._last_notify_ts: str | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False

    # ---------------------------------------------------------------- reads
    def get(self, namespace: str) -> Any | None:
        """Return a live entry, or None when absent OR expired.

        Expiry returns None rather than a stale value on purpose: the caller
        then does what it would have done with a cold cache. A cache that
        answers "here is what I believed 40 minutes ago" is how a gate becomes
        its own stale-authority problem.
        """
        hit = self._entries.get(namespace)
        if not hit:
            return None
        stored_at, version, value = hit
        if version < self._version:
            return None
        if (time.monotonic() - stored_at) > self._ttl_s:
            self._entries.pop(namespace, None)
            return None
        return value

    def put(self, namespace: str, value: Any) -> None:
        self._entries[namespace] = (time.monotonic(), self._version, value)

    def invalidate(self, namespace: str | None = None) -> None:
        if namespace is None:
            self._entries.clear()
        else:
            self._entries.pop(namespace, None)

    # ------------------------------------------------------------ versioning
    def bump_version(self, version: int | None = None) -> None:
        """Advance the cache version.

        Notifications carrying a version <= current are IGNORED. Without that,
        a replayed or out-of-order notification could resurrect a generation of
        entries the cache had already moved past.
        """
        self._version = max(self._version + 1, version or 0)

    def apply_notification(self, payload: str) -> bool:
        """Handle one NOTIFY payload: "<version>" or "<version>:<namespace>".

        Returns False for a stale/unparseable notification, which is recorded
        rather than raised — a malformed payload must not kill the subscriber
        that the whole invalidation story depends on.
        """
        self._last_notify_ts = _now_iso()
        try:
            head, _, namespace = payload.partition(":")
            version = int(head)
        except (TypeError, ValueError):
            log.warning("gate_cache_bad_notify", payload=payload[:80])
            return False
        if version <= self._version:
            return False
        self._version = version
        self.invalidate(namespace or None)
        return True

    # ------------------------------------------------------------ introspect
    def status(self) -> dict:
        live = [ns for ns in list(self._entries) if self.get(ns) is not None]
        return {
            "profiles_cached": len(live),
            "verdicts_cached": 0,
            "listener_alive": self._listener_alive,
            "last_notify_ts": self._last_notify_ts,
            "cache_version": self._version,
            "ttl_seconds": self._ttl_s,
            "stale_keys": max(0, len(self._entries) - len(live)),
        }

    # -------------------------------------------------------------- listener
    async def start(self, dsn: str | None) -> None:
        """Start the LISTEN supervisor against a DIRECT (non-pooler) DSN.

        No DSN means no listener, which is a supported configuration, not an
        error: the cache runs on pure TTL and says so through listener_alive.
        """
        if not dsn:
            log.info("gate_cache_listener_disabled", reason="no direct dsn configured")
            return
        self._stopping = False
        self._task = asyncio.create_task(self._supervise(dsn))

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self._listener_alive = False

    async def _supervise(self, dsn: str) -> None:
        """Reconnect forever. Best-effort by construction: the listener is an
        optimisation, and its death must degrade the cache to TTL rather than
        take down the server."""
        backoff = _BACKOFF_START_S
        while not self._stopping:
            try:
                await self._listen_once(dsn)
                backoff = _BACKOFF_START_S
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - best-effort supervisor
                self._listener_alive = False
                log.warning("gate_cache_listener_dropped", error=str(exc),
                            retry_in_s=backoff)
            if self._stopping:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)

    async def _listen_once(self, dsn: str) -> None:
        import asyncpg

        conn = await asyncpg.connect(dsn)
        try:
            queue: asyncio.Queue = asyncio.Queue()

            def _on_notify(_conn, _pid, _channel, payload):
                queue.put_nowait(payload)

            await conn.add_listener(CHANNEL, _on_notify)
            # FULL RELOAD ON (RE)CONNECT. While disconnected the listener missed
            # notifications that will never be re-sent, so the only honest state
            # is "I do not know what changed" — which resolves to dropping
            # everything, never to trusting what is held.
            self.invalidate()
            self.bump_version()
            self._listener_alive = True
            log.info("gate_cache_listener_up", channel=CHANNEL)

            while not self._stopping:
                payload = await queue.get()
                self.apply_notification(str(payload))
        finally:
            self._listener_alive = False
            try:
                await conn.close()
            except Exception:  # noqa: BLE001 - closing a dead connection
                pass


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

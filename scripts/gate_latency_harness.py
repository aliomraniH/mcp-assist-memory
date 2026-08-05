#!/usr/bin/env python3
"""Intent Gate latency harness — [NEON] / [POST-DEPLOY] ONLY.

RUN THIS AGAINST PRODUCTION TOPOLOGY OR NOT AT ALL.
    A local-Postgres run of this harness proves nothing. Local round trips are a
    fraction of a millisecond; a Reserved VM talking to an external Neon
    endpoint has a documented floor near 59ms. The v1 Tier-0 budget of <50ms was
    almost certainly produced by exactly this mistake, and it then failed on all
    20 production samples. A number from the wrong environment is worse than no
    number, because it looks like evidence.

    The harness prints its environment banner for that reason. If it says local
    Postgres, the output is a smoke test of the harness itself and nothing more.

METHOD
    * n >= 2000 warm calls by default. Warmup calls are discarded — the first
      intent_open in a process pays the spaCy model load and the first
      connection setup, and reporting that as steady state is misleading.
    * p50/p95/p99 from the full sample, never a mean. Latency distributions have
      a long right tail; a mean hides exactly the calls users complain about.
    * Assertions are made on gate_detail.latency_ms / the response's own
      latency_ms — SERVER-SIDE time. Network-inclusive wall time from a client
      is dominated by MCP transport (~3s per call was observed live) and would
      drown the signal.
    * The Tier-1 span breakdown is reported alongside the totals, because
      "Tier-1 is slow" is not actionable and "the goal-embedding span is 380ms
      of it" is.

USAGE
    DATABASE_URL=<production or Neon dev branch> \
        python scripts/gate_latency_harness.py --namespace dev/gate-latency-probe \
            --n 2000 --tier 0
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import uuid


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(pct / 100.0 * len(ordered))) - 1))
    return ordered[idx]


def summarise(name: str, values: list[float]) -> dict:
    return {
        "metric": name,
        "n": len(values),
        "min": min(values) if values else None,
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": max(values) if values else None,
    }


def _banner(identity: dict) -> str:
    host = identity.get("endpoint_host") or ""
    local = host in ("", "localhost", "127.0.0.1", "::1") or host.startswith("/")
    if local:
        return ("ENVIRONMENT: LOCAL POSTGRES. These numbers are NOT comparable "
                "to the Tier-0/Tier-1 targets and must never be cited as "
                "meeting them. Treat this run as a smoke test of the harness.")
    pooled = "-pooler" in host
    return (f"ENVIRONMENT: remote endpoint {host} "
            f"({'pooled' if pooled else 'DIRECT'}), database "
            f"{identity.get('current_database')}. Numbers are comparable to the "
            f"published targets.")


async def main_async(args) -> int:
    from psycopg_pool import AsyncConnectionPool

    from config import settings
    from storage.embeddings import build_embedder
    from storage.gate_targets import TIER0_MEDIAN_MS, TIER0_P95_MS
    from storage.postgres import PostgresBackend

    pool = AsyncConnectionPool(settings.database_url, open=False, min_size=1,
                              max_size=8)
    await pool.open()
    try:
        backend = PostgresBackend(pool, embedder=build_embedder(settings))
        identity = await backend.db_identity()
        print(_banner(identity))
        print(json.dumps({"db_identity": identity}, indent=2))
        print()

        ns = args.namespace
        session = (await backend.session_create(ns, surface="latency-harness"))["session_id"]

        totals: list[float] = []
        spans: dict[str, list[float]] = {}

        async def one(i: int) -> None:
            if args.tier == 0:
                ack = await backend.memory_save(
                    ns, f"probe/latency-{i}", f"latency sample {i}", kind="note",
                    actor="gate-latency-harness")
                detail = ack.get("gate_detail") or {}
                # SERVER-side time only.
                ms = detail.get("latency_ms")
                if ms is None:
                    ms = (ack.get("gate") or {}).get("latency_ms")
                if ms is not None:
                    totals.append(float(ms))
            else:
                res = await backend.intent_open(
                    ns, goal=f"rebuild projection {i} by replaying the event log "
                             f"sorted by timestamp",
                    scope=["memory_save"], session_id=session,
                    actor="gate-latency-harness")
                totals.append(float(res["latency_ms"]))
                for key, value in (res.get("latency_spans") or {}).items():
                    spans.setdefault(key, []).append(float(value))

        for i in range(args.warmup):
            await one(-1 - i)
        totals.clear()
        spans.clear()

        for i in range(args.n):
            await one(i)

        print(json.dumps(summarise(f"tier{args.tier}_latency_ms", totals), indent=2))
        if spans:
            print("\nTIER-1 SPAN BREAKDOWN (3e measurement):")
            print(f"{'span':22} {'p50':>8} {'p95':>8}")
            for key in sorted(spans, key=lambda k: -statistics.median(spans[k])):
                print(f"{key:22} {percentile(spans[key], 50):8.1f} "
                      f"{percentile(spans[key], 95):8.1f}")

        if args.tier == 0 and totals:
            p95, p50 = percentile(totals, 95), percentile(totals, 50)
            ok = p95 <= TIER0_P95_MS and p50 <= TIER0_MEDIAN_MS
            print(f"\nTIER-0 TARGET p95<={TIER0_P95_MS} median<={TIER0_MEDIAN_MS}: "
                  f"{'PASS' if ok else 'FAIL'} (p95={p95:.1f} median={p50:.1f})")
            return 0 if ok else 1
        return 0
    finally:
        await pool.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--namespace",
                    default=f"dev/gate-latency-{uuid.uuid4().hex[:8]}")
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--warmup", type=int, default=50)
    ap.add_argument("--tier", type=int, choices=(0, 1), default=0)
    args = ap.parse_args()
    if args.n < 2000:
        print(f"WARNING: n={args.n} is below the documented minimum of 2000; "
              f"p99 from a small sample is noise.", file=sys.stderr)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

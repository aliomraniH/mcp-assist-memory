"""Intent Gate latency targets, with the measurement that justifies each one.

A target with no measurement behind it is a wish, and a wish that fails a
fixture teaches the team to ignore the fixture. v1 shipped a Tier-0 budget of
<50ms that was exceeded on all 20 samples with a minimum of 58ms — not because
the gate was slow, but because the budget sat below the achievable floor of the
deployment topology. It was almost certainly calibrated against a local Postgres
in CI, where a round trip is a few milliseconds.

Every number here therefore carries: what was measured, where, and why the
target is what it is. Change a number only alongside a new measurement.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# TIER 0 — re-baselined (remediation 3d).
#
# MEASURED (2026-08-03, deployed Reserved VM -> external Neon, n=20):
#   min 58ms, median 59.5ms, p90 63.1ms, p95 64.0ms, max 65ms
#
# DIAGNOSIS: readback_latency_ms on the SAME acks has median 60ms —
# statistically identical. Both are one Neon round trip. Tier-0 pre-flight
# therefore costs approximately exactly one round trip, which is the correct
# implementation shape; there is no fat to cut. Neon's own engineering material
# puts roughly four network round trips at the driver floor for a connection
# from external compute, and <50ms is simply not reachable from Replit compute
# to an external Neon endpoint.
#
# TARGET: p95 <= 110ms, median <= 75ms. Headroom above the measured p95 of 64ms
# absorbs normal variance without turning the fixture into a coin flip, while
# still failing loudly if the gate starts costing more than ~one round trip.
#
# ENVIRONMENT: [NEON] / [POST-DEPLOY] ONLY. A local-Postgres run of the harness
# proves nothing about these numbers and must never be cited as meeting them —
# that is precisely how the <50ms budget came to exist.
TIER0_P95_MS = 110
TIER0_MEDIAN_MS = 75

# ---------------------------------------------------------------------------
# TIER 1 — PROVISIONAL. See the completion record's TIER-1 LATENCY DECISION.
#
# MEASURED (2026-08-03, deployed server, n=11):
#   min 413ms, median 515ms, p90 804ms, p95 810.5ms, max 817ms
#   empty namespace: 121ms  =>  ~400ms of the median is retrieval-path work
#
# The "~500ms total" budget was never met, and before this branch it had no
# mechanism behind it — which part of the retrieval path dominated was simply
# unmeasured. Two things were done about that:
#
#   1. The path is now instrumented into named spans (goal_embedding, ann_query,
#      trigger_scan, structured_scan, project_block, feature_extraction,
#      match_log, parallel_reads, persist, other) and every intent_open response
#      reports them. Latency work that requires a redeploy to observe does not
#      get done. See NESTED_SPANS below for which of these are additive.
#
#   2. Two mechanisms were applied. The four independent reads now run
#      CONCURRENTLY, dropping the sequential round-trip count from four to one —
#      an environment-independent structural win worth roughly three round trips
#      (~180ms at Neon's ~60ms floor). And goal embeddings are cached by
#      sha256(normalised goal), so a re-declared or clarified intent does not pay
#      the provider round trip twice.
#
# WHAT IS NOT CLAIMED: this branch could not measure the result. The
# implementing environment has no Neon topology and no embedding provider key,
# so the span breakdown captured here (total ~14ms against local Postgres with
# an in-process fake embedder) says nothing about production magnitudes — that
# is the S14 failure mode, and reproducing it would be indefensible in the one
# branch whose purpose is to stop reproducing it.
#
# TIER1_P95_MS is therefore PROVISIONAL and the tier1_latency fixture is [NEON]:
# it is implemented, marked, and NOT claimed green. The operator's post-deploy
# run produces the number, and Step 3 of 3e is decided then — keep 500ms if the
# measurement supports it, or re-baseline to the measured p95 plus headroom with
# the same written justification Tier-0 carries above.
TIER1_P95_MS_PROVISIONAL = 500
TIER1_MEASURED_P95_MS_PRE_FIX = 810.5
TIER1_MEASURED_MEDIAN_MS_PRE_FIX = 515
TIER1_EMPTY_NAMESPACE_MS = 121

# Phase 4's NLI stage is budgeted at 60-135ms ON TOP of whatever Tier-1 settles
# at. If the post-3e headroom cannot absorb it, that is a named reason to leave
# Phase 4 off — which is one of the reasons it is descoped from this branch.
NLI_BUDGET_MS = 135

# The span vocabulary. Fixed, because these are a telemetry dimension and the
# entire point is comparing the same spans across runs and environments.
SPAN_NAMES = (
    "profile_guard",
    "goal_embedding",
    "ann_query",
    "trigger_scan",
    "structured_scan",
    "project_block",
    "parallel_reads",
    "feature_extraction",
    "match_log",
    "persist",
    "other",
)

# SPANS THAT ARE NOT ADDITIVE.
#
# v1 summed every span and computed other = max(0, total - sum). Both halves of
# that were wrong, and they hid each other:
#
#   1. goal_embedding and ann_query are measured INSIDE _semantic_candidates,
#      which runs INSIDE the asyncio.gather covered by parallel_reads. Summing
#      parent and children counts the same milliseconds twice. Worse, the four
#      legs of that gather run CONCURRENTLY, so their durations do not add up to
#      the block's wall time under any accounting — the block's wall time is the
#      SLOWEST leg, which is the whole point of running them concurrently.
#
#   2. The max(0, ...) clamp then absorbed the resulting negative residual, so
#      the breakdown appeared to balance exactly when it was most wrong. Live
#      T15 measured 78-194ms of real, unattributed latency reported as other=0.
#
# The additive breakdown is therefore the SEQUENTIAL timeline: each span below
# is a leaf on that timeline and they do not overlap, so they sum to the total
# exactly. The concurrent legs stay reported — they are the measurement the
# instrumentation exists for — but as nested DETAIL, excluded from the sum.
NESTED_SPANS = (
    "goal_embedding",
    "ann_query",
    "trigger_scan",
    "structured_scan",
    "project_block",
)

# The sequential timeline. These, and only these, add up to latency_ms.
ACCOUNTED_SPANS = tuple(n for n in SPAN_NAMES if n not in NESTED_SPANS)

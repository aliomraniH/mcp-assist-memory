---
name: Gate latency verification from the workspace
description: How to run the gate latency harness / neon tests against prod, and how to read the numbers honestly
---

- The latency harness and tier-0 gate_detail sampling produce **zero samples** unless the target namespace is armed (`variant_profiles` row with `intent_gate: "on"`). An exit-0 run with n=0 is a hollow pass — always check `n`.
- Neon-marked tests need BOTH `DATABASE_URL` (pooled: insert `-pooler` before `.c-` in the host) and `DATABASE_URL_DIRECT` (direct). Latency tests additionally need `GATE_LATENCY_NEON=1`.
- The first listener-alive assertion can flake on the initial TLS connect (0.5s sleep too short); rerun before concluding failure.
- **Vantage matters**: workspace→Neon us-east-1 round-trip floor is ~81ms vs the Reserved VM's documented ~59ms. A tier-0 median miss measured from the workspace is expected; record the justification (done in prod key `gate/latency-baseline-20260808`) rather than loosening the target. p95 targets passed from the workspace anyway (tier0 90/110, tier1 440/500).
- **Long-running shell jobs get reaped** between ShellExec calls even with setsid/nohup — run multi-minute harnesses as a temporary console workflow writing to /tmp logs, then remove the workflow.

**Why:** the 2026-08-08 remediation sign-off lost ~1h to a silently reaped background harness and a hollow n=0 pass.
**How to apply:** any future post-deploy latency measurement or long prod-topology test run.

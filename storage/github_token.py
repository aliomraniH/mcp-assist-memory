"""Source a GitHub token from the connected Replit account (optional).

The reconciler prefers an explicit, durable ``GITHUB_TOKEN`` (a read-only PAT).
When that is absent but the user has connected their GitHub account through
Replit, this fetches a token from the connector proxy instead — so the feature
works off the existing connection without asking for a separate credential.

Replit's GitHub connection is OAuth with a refreshing token, so we never snapshot
it into a static secret: the provider re-fetches from the proxy after a short
cache window (and honors the credential's own expiry), which always returns a
currently-valid token. Every fetch is best-effort — any failure returns ``None``,
which the resolver maps to ``unverifiable`` rather than a wrong answer.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

# api.github.com tokens are minted via this proxy; the platform injects the host
# and an identity token (REPL_IDENTITY in dev, WEB_REPL_RENEWAL in deployments).
_DEFAULT_TTL = 600.0        # seconds to cache a token when expiry is unknown
_EXPIRY_MARGIN = 60.0       # refresh this long before a known expiry
_MIN_TTL = 30.0


class ConnectorTokenProvider:
    """Async callable returning a fresh GitHub access token, or ``None``.

    Caches the last token until shortly before it expires so reconciliation
    doesn't hit the proxy on every claim."""

    def __init__(
        self,
        hostname: str,
        x_replit_token: str,
        *,
        connector: str = "github",
        timeout: float = 10.0,
    ) -> None:
        self._hostname = hostname
        self._x_replit_token = x_replit_token
        self._connector = connector
        self.timeout = timeout
        self._cached: str | None = None
        self._cached_until = 0.0

    async def __call__(self) -> str | None:
        now = time.monotonic()
        if self._cached and now < self._cached_until:
            return self._cached
        if not self._hostname or not self._x_replit_token:
            return None

        import httpx

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"https://{self._hostname}/api/v2/connection",
                    params={"include_secrets": "true", "connector_names": self._connector},
                    headers={"Accept": "application/json", "X_REPLIT_TOKEN": self._x_replit_token},
                )
                resp.raise_for_status()
                items = resp.json().get("items", [])
        except Exception:  # noqa: BLE001 - best-effort: any failure -> no token -> unverifiable
            return None

        if not items:
            return None
        settings = items[0].get("settings", {}) or {}
        creds = (settings.get("oauth", {}) or {}).get("credentials", {}) or {}
        token = settings.get("access_token") or creds.get("access_token")
        if not token:
            return None

        self._cached = token
        self._cached_until = now + _ttl_from_credentials(creds)
        return token


def _ttl_from_credentials(creds: dict[str, Any]) -> float:
    """Cache window for a token: derive from the credential's own expiry when
    present (minus a safety margin), else a conservative default."""
    expires_in = creds.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        return max(float(expires_in) - _EXPIRY_MARGIN, _MIN_TTL)
    expires_at = creds.get("expires_at")
    if isinstance(expires_at, str) and expires_at:
        try:
            dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            remaining = (dt - datetime.now(timezone.utc)).total_seconds()
            return max(remaining - _EXPIRY_MARGIN, _MIN_TTL)
        except ValueError:
            pass
    return _DEFAULT_TTL


def build_connector_token_provider(settings: Any) -> ConnectorTokenProvider | None:
    """Build a provider from config, or ``None`` when the platform connector
    vars aren't present. Decoupled from ``config`` (only config.py reads env)."""
    hostname = getattr(settings, "replit_connectors_hostname", None)
    identity = getattr(settings, "repl_identity", None)
    renewal = getattr(settings, "web_repl_renewal", None)
    x_replit_token = (f"repl {identity}" if identity else f"depl {renewal}" if renewal else None)
    if not hostname or not x_replit_token:
        return None
    return ConnectorTokenProvider(hostname, x_replit_token)

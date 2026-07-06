"""Single source of truth for configuration and secrets.

This is the ONLY place in the service that reads the environment. Everything
else imports the ``settings`` singleton. (Grep-gate: ``os.environ`` must not
appear outside this module.)
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- required ---
    database_url: str
    mcp_auth_token: str

    # --- admin dashboard (/admin): manage + rotate the live MCP token ---
    # ADMIN_PASSWORD gates the dashboard; without it the dashboard refuses logins.
    # SESSION_SECRET signs the dashboard session cookie (falls back to
    # ADMIN_PASSWORD, then a per-process random value).
    admin_password: str | None = None
    session_secret: str | None = None

    # --- declared now, used in Phase 3 (kept optional so the service boots without them) ---
    voyage_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    langsmith_api_key: str | None = None

    # --- memory curator (write-side consolidation): only active when anthropic_api_key is set ---
    # The curator is to *writing* what the embedder is to search and the resolver is
    # to reconciliation: an optional, injected, best-effort dependency. Without
    # anthropic_api_key build_curator() returns a DisabledCurator and coord_curate is
    # a clean no-op — the server boots and behaves identically. voyage_api_key (above)
    # is reused to embed the curator's two strings (summary + hyde); absent ⇒ keyword-only.
    curator_model: str = "claude-opus-4-1"
    curator_max_output_tokens: int = 4096
    # Curation accountability (Phase 5, T5.4). curator_family is stamped on every
    # curated write (structured enum-ish string, e.g. "anthropic" — enforcement
    # compares these fields, never origin_detail prose). When
    # CURATOR_FAMILY_MUST_DIFFER_FROM is set (comma-separated families), the
    # apply-worker refuses to curate entries whose origin_model_family matches —
    # same-family self-review is a verified bias; this bakes the referee-protocol
    # workaround into the spine.
    curator_family: str = "anthropic"
    curator_family_must_differ_from: str | None = None

    # --- trust decay (Phase 6): a reconcile verdict is a snapshot, not a
    # subscription. Claims whose latest verdict is older than this window are
    # demoted to needs_reverification by coord_health, even if the verdict was
    # `current`. Override per namespace via variant_profiles.profile
    # .claim_staleness_hours (dev namespaces that reconcile weekly may want 168
    # so the signal stays legible on a low-traffic server).
    default_claim_staleness_hours: int = 72

    # --- namespace ACL (Phase 9, design doc: docs/namespace-isolation.md) ---
    # Optional JSON mapping token -> list of allowed namespace PREFIXES, e.g.
    # {"tok-web": ["proj-canvas", "dev/"], "tok-cli": ["dev/"]}. When set,
    # namespace-scoped tool calls with a token whose allowlist doesn't cover the
    # namespace fail closed with the standard acl_denied payload. Unset ⇒
    # behavior unchanged (no ACL). Not full multi-tenancy — see the design doc.
    token_namespace_acl: str | None = None

    # --- coordination reconciler (Phase 3): only active when github_token is set ---
    # A READ-ONLY GitHub token lets the backend resolve a claim's truth (is PR #N
    # merged? what is branch X's head?) off the agent's critical path. Without it
    # the reconciler is disabled and claims reconcile to "unverifiable" — the server
    # runs identically. github_webhook_secret gates POST /webhook/github (HMAC over
    # the raw body); without it the webhook returns 503.
    github_token: str | None = None
    github_api_url: str = "https://api.github.com"
    github_webhook_secret: str | None = None

    # --- Replit connector access (optional) ---
    # When github_token is NOT set explicitly, the reconciler can source a
    # read-capable token from the connected GitHub account via the Replit
    # connector proxy (these vars are injected by the platform). The token is
    # fetched fresh per cache-window so it survives OAuth refresh; a static
    # github_token still takes precedence when provided.
    replit_connectors_hostname: str | None = None
    repl_identity: str | None = None
    web_repl_renewal: str | None = None

    # --- semantic recall (Phase 3): only active when voyage_api_key is set ---
    # embedding_dim MUST match the vector(N) column in migrations/0002_embeddings.sql.
    embedding_model: str = "voyage-3.5-lite"
    embedding_dim: int = 1024

    # hnsw.ef_search tunes the HNSW recall/latency tradeoff for the semantic leg
    # of memory_search: higher = the index inspects more candidates, so recall
    # climbs as namespaces grow into the thousands, at the cost of a little query
    # latency. pgvector's default is 40; we raise it so large stores don't silently
    # drop relevant hits. For small stores the index returns the same rows either
    # way, so behavior is unchanged. Applied per-statement (transaction-local) on
    # the cosine query only — never the keyword fallback. Must be >= the search
    # limit to be effective. Set HNSW_EF_SEARCH to override.
    hnsw_ef_search: int = 100

    # --- artifact / bytea safety ---
    max_artifact_bytes: int = 50 * 1024 * 1024          # hard write cap: 50 MB
    artifact_inline_limit: int = 1 * 1024 * 1024        # MCP returns base64 inline only below this;
                                                        # larger blobs stream via GET /artifact/{sha256}
    artifact_stream_chunk: int = 1 * 1024 * 1024        # ranged read window for streamed blobs

    # --- pool / lifespan bounds ---
    pool_max_size: int = 10
    pool_timeout: float = 10.0                          # max wait to check out a conn
    pool_reconnect_timeout: float = 30.0
    pool_max_idle: float = 60.0
    db_connect_timeout: int = 10                        # libpq TCP/handshake cap (seconds)
    db_statement_timeout_ms: int = 15000
    readiness_timeout_s: float = 15.0                   # hard cap on boot readiness probe

    # --- server ---
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    # --- MCP Host/Origin protection (fastmcp >=3.4.3 HostOriginGuardMiddleware) ---
    # Defense-in-depth against DNS-rebinding / cross-origin browser abuse, layered
    # ON TOP of our bearer-token gate. The guard rejects any request whose Host is
    # not in DEFAULT_HOSTS (loopback) + allowed_hosts with 421 "Misdirected
    # Request", and any browser Origin not in allowed_origins with 403 "Forbidden
    # Origin". Behind the Replit edge the external deployment domain must be listed
    # explicitly or every prod request 421s (see .agents/memory/mcp-sse-edge-421.md).
    # Both are comma-separated; entries support fnmatch patterns (e.g. "*.replit.app").
    # Set to "*" to disable that dimension. The claude.ai web connector sends
    # Origin: https://claude.ai, so it must be allowed or the connector 403s.
    mcp_allowed_hosts: str = "mcp-assist-memory.replit.app,*.replit.app,*.replit.dev"
    mcp_allowed_origins: str = "https://claude.ai"
    mcp_host_origin_protection: bool = True

    @property
    def mcp_allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.mcp_allowed_hosts.split(",") if h.strip()]

    @property
    def mcp_allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.mcp_allowed_origins.split(",") if o.strip()]


settings = Settings()  # import this; never read the environment elsewhere

-- 0001_init.sql  —  FROZEN. Never edit this file once merged; add a new numbered
-- migration instead. Deterministic schema, no drift.
--
-- Tenancy: `namespace` is the per-project tenant key (namespace == project ==
-- tenant). One namespace per project; conventional sub-scopes by key prefix
-- (coord/…, knowledge/…). Every per-project table carries `namespace` and every
-- query filters on it — there are no implicit cross-project reads. Artifacts are
-- the one deliberate exception: they are content-addressed (sha256) and dedup
-- globally, so they are not tenant-scoped. (Per-project tokens — hard isolation
-- against a misbehaving client — are the v2 auth roadmap item; see README.)

CREATE EXTENSION IF NOT EXISTS vector;       -- extension only; the knowledge table + HNSW index arrive in Phase 3 (0003_*)
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- trigram index backs memory_search ILIKE until pgvector recall (Phase 3)

-- ---------------------------------------------------------------------------
-- memory_entry: append-only, revisioned key/value memory.
-- The system of record for notes / decisions / todos / handoffs / config.
-- Revisions are server-computed (max+1 in-txn); clients never supply them.
-- `namespace` is the project tenant.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_entry (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    namespace      text        NOT NULL,       -- project tenant
    key            text        NOT NULL,
    revision       integer     NOT NULL,
    kind           text        NOT NULL CHECK (kind IN ('note','decision','todo','handoff','config')),
    value          jsonb       NOT NULL,
    source_surface text,                       -- 'cli' | 'web' | 'desktop' | <agent_id>
    tags           text[]      NOT NULL DEFAULT '{}',
    event_id       uuid,                       -- idempotency key (nullable)
    tombstone      boolean     NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (namespace, key, revision)
);

CREATE INDEX IF NOT EXISTS memory_entry_ns_key_rev ON memory_entry (namespace, key, revision DESC);
CREATE INDEX IF NOT EXISTS memory_entry_tags_gin   ON memory_entry USING gin (tags);
CREATE INDEX IF NOT EXISTS memory_entry_value_trgm ON memory_entry USING gin ((value::text) gin_trgm_ops);
-- Idempotency: a given event_id may be applied at most once.
CREATE UNIQUE INDEX IF NOT EXISTS memory_entry_event_id_uq
    ON memory_entry (event_id) WHERE event_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- session / session_event: episodic memory with a per-session monotonic seq.
-- Both carry `namespace` so episodic memory is tenant-scoped like everything
-- else; every session query filters on namespace.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session (
    session_id uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace  text        NOT NULL,           -- project tenant
    surface    text,
    metadata   jsonb       NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS session_ns_created ON session (namespace, created_at DESC);

CREATE TABLE IF NOT EXISTS session_event (
    session_id uuid        NOT NULL REFERENCES session(session_id) ON DELETE CASCADE,
    namespace  text        NOT NULL,           -- project tenant (denormalized for tenant-filtered reads)
    seq        integer     NOT NULL,
    kind       text        NOT NULL,
    payload    jsonb       NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, seq)
);

CREATE INDEX IF NOT EXISTS session_event_ns_session_seq ON session_event (namespace, session_id, seq);

-- ---------------------------------------------------------------------------
-- artifact: immutable, content-addressed blobs stored as bytea
-- (moved off the filesystem; single durable store, survives redeploy).
-- Content-addressed identity = global dedup; deliberately NOT tenant-scoped.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS artifact (
    sha256       char(64)    PRIMARY KEY,       -- hex digest = identity
    bytes        bytea       NOT NULL,
    size         integer     NOT NULL,
    content_type text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- assist-memory initial schema. FROZEN. Do not modify once merged.
-- New columns or tables require a new numbered migration (0002_*, ...).
--
-- gen_random_uuid() is built-in on PostgreSQL 13+ (Neon runs 15/16), so no
-- pgcrypto extension is required. The vector extension is created now but the
-- knowledge/embeddings table is Phase 3 — not created here.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Memory: append-only, revisioned key/value per (namespace, key)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_entry (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    namespace      TEXT NOT NULL,
    key            TEXT NOT NULL,
    revision       INT  NOT NULL,                       -- server-computed (max+1 in-txn)
    kind           TEXT NOT NULL
                     CHECK (kind IN ('note','decision','todo','handoff','config')),
    value          TEXT,                                -- raw stored text; JSON-encoded
    value_is_json  BOOLEAN NOT NULL DEFAULT FALSE,      --   when value_is_json is true
    tags           TEXT[] NOT NULL DEFAULT '{}',
    source_surface TEXT NOT NULL DEFAULT 'other',
    event_id       UUID,                                -- nullable idempotency key
    tombstone      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (namespace, key, revision)
);

CREATE INDEX IF NOT EXISTS idx_memory_ns_key
    ON memory_entry (namespace, key, revision DESC);

CREATE INDEX IF NOT EXISTS idx_memory_tags
    ON memory_entry USING gin (tags);

-- An event_id may appear at most once (idempotent save dedupe).
CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_event_id
    ON memory_entry (event_id) WHERE event_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Sessions + per-session monotonic event log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session (
    session_id  TEXT PRIMARY KEY,
    namespace   TEXT NOT NULL,
    surface     TEXT NOT NULL,
    status      TEXT NOT NULL,
    summary     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_session_ns
    ON session (namespace, created_at DESC);

CREATE TABLE IF NOT EXISTS session_event (
    session_id  TEXT NOT NULL REFERENCES session(session_id),
    seq         INT  NOT NULL,                          -- per-session monotonic
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT now(),
    type        TEXT NOT NULL,
    message     TEXT NOT NULL,
    data        JSONB,
    PRIMARY KEY (session_id, seq)
);

-- ---------------------------------------------------------------------------
-- Artifacts: immutable content-addressed blobs (bytea) + metadata
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS artifact_blob (
    sha256       CHAR(64) PRIMARY KEY,
    bytes        BYTEA NOT NULL,
    size         INT NOT NULL,
    content_type TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS artifact (
    artifact_id      TEXT PRIMARY KEY,                  -- app-issued art_<hex> id
    namespace        TEXT NOT NULL,
    filename         TEXT NOT NULL,
    mime             TEXT NOT NULL,
    size_bytes       INT NOT NULL,
    sha256           CHAR(64) NOT NULL REFERENCES artifact_blob(sha256),
    uploaded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_surface   TEXT NOT NULL,
    session_id       TEXT,
    memory_key       TEXT,
    tags             TEXT[] NOT NULL DEFAULT '{}',
    is_debug_capture BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_artifact_ns      ON artifact (namespace);
CREATE INDEX IF NOT EXISTS idx_artifact_session ON artifact (session_id);

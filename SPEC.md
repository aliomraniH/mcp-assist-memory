# SPEC — mcp-assist-memory

A remote MCP server (Streamable HTTP) providing a shared memory, session-tracking,
handoff, and artifact layer so work state survives switches between Claude surfaces
(claude.ai web, Claude Code CLI, Claude Code Desktop). Deployed on Replit.

- **Stack:** Python 3.11+, FastMCP (official `mcp` Python SDK), Streamable HTTP transport
- **Storage:** SQLite (metadata + small values) + filesystem blob store (artifacts),
  all behind a single `StorageBackend` interface
- **Scope guard:** stores no third-party credentials, makes no outbound API calls.
  Memory-only by design.

---

## 1. Configuration

Single source of truth: environment variables, read once at startup.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `MCP_AUTH_TOKEN` | **yes** | — (server refuses to start without it) | Shared bearer token for all MCP requests |
| `DATA_DIR` | no | `./data` | Root directory for SQLite DB and blob store |
| `MAX_UPLOAD_MB` | no | `25` | Per-upload size cap (decoded bytes) |
| `MAX_TOTAL_STORAGE_MB` | no | `500` | Global cap across DB values + blobs |
| `PORT` | no | `8000` | HTTP listen port (Replit sets this) |

Server binds `0.0.0.0:$PORT`. Entrypoint: `main.py`.

---

## 2. Authentication

- Every HTTP request to the MCP endpoint (`/mcp`) MUST carry
  `Authorization: Bearer <MCP_AUTH_TOKEN>` (exact, constant-time comparison).
  Anything else → HTTP **401** with body `{"error": "unauthorized"}` and
  `WWW-Authenticate: Bearer` header. Enforced by ASGI middleware *before* any
  MCP routing, so no tool, resource, or session negotiation is reachable
  anonymously.
- Sole anonymous endpoint: `GET /` → `200 {"status": "ok"}`. No version, no
  counts, no data — health probe only.
- No per-user identity: one token, one tenant. (Multi-user is out of scope.)

---

## 3. Data model

One namespace dimension; three entities. Namespace is a string
(`^[a-z0-9][a-z0-9._-]{0,63}$`, case-sensitive, e.g. `canvas-growth-charts`).
Every entity belongs to exactly one namespace; callers that omit it get
`"default"`. Namespaces are implicit — created on first write, never need
explicit creation.

### 3.1 Memory entries (append-only, revisioned)

A memory *entry* is identified by `(namespace, key)`. Every write creates a new
immutable **revision row**; nothing is ever updated or physically deleted.

| Field | Type | Notes |
|---|---|---|
| `namespace` | text | see above |
| `key` | text | 1–256 chars; any printable string; `/` allowed for hierarchy (e.g. `debug/<sid>/brief`) |
| `revision` | int | monotonic per `(namespace, key)`, starts at 1 |
| `kind` | text | `note \| decision \| todo \| handoff \| config` (default `note`) |
| `value` | JSON or text | stored as JSON if input parses/was given as JSON, else text; ≤ 256 KB per value (larger payloads belong in artifacts) |
| `tags` | JSON array of strings | default `[]`; server may append `"possible-secret"` (§7.3) |
| `source_surface` | text | `web \| cli \| desktop \| other` (default `other`) |
| `created_at` | text | UTC ISO-8601 `YYYY-MM-DDTHH:MM:SSZ` |
| `deleted` | bool | `true` only on tombstone revisions |

Semantics:
- **Write** (`memory_save`): inserts revision `max(revision)+1` (or 1).
- **Delete** (`memory_delete`): inserts a tombstone revision (`deleted=true`,
  `value=null`). Subsequent `memory_get` without an explicit revision returns
  not-found; history retains everything.
- **Revert** (`memory_revert`): copies the value/kind/tags of an older revision
  into a brand-new revision. Non-destructive undo. Reverting also "undeletes"
  if the latest revision was a tombstone.
- Latest revision = highest revision number (tombstone counts as latest).

### 3.2 Sessions

| Field | Type | Notes |
|---|---|---|
| `session_id` | text | caller-supplied, or generated as `YYYY-MM-DDTHH-MM-SSZ_<label>` (label slugified, default `session`); unique globally |
| `namespace` | text | |
| `surface` | text | `web \| cli \| desktop \| other` |
| `status` | text | `open \| closed` |
| `summary` | text/null | set at start and/or end |
| `created_at`, `ended_at` | text | UTC ISO-8601; `ended_at` null while open |
| `events` | append-only child rows | `seq` (int, per-session monotonic), `timestamp`, `type` (free-form, e.g. `note`, `error`, `milestone`), `message`, optional `data` (JSON) |
| linked artifacts | derived | artifacts whose `session_id` matches |

Logging to a closed session is rejected (`SESSION_CLOSED`). Ending an already
closed session is rejected likewise.

### 3.3 Artifacts

| Field | Type | Notes |
|---|---|---|
| `artifact_id` | text | `art_` + 12 hex chars (random) |
| `namespace` | text | |
| `filename` | text | sanitized to basename; no path separators stored |
| `mime` | text | guessed from filename/encoding; `application/octet-stream` fallback |
| `size_bytes` | int | decoded size |
| `sha256` | text | hex digest of stored bytes |
| `uploaded_at` | text | UTC ISO-8601 |
| `source_surface` | text | as above |
| `session_id` | text/null | optional link |
| `memory_key` | text/null | optional link to a memory entry key |
| `tags` | JSON array | |
| `storage_path` | text | internal; never returned to callers |
| `is_debug_capture` | bool | true when ZIP ingestion recognized it (§6) |

Artifact bytes live in the blob store; rows in SQLite. Artifacts are immutable
(no update/delete tools in v1).

---

## 4. Storage layout

```
$DATA_DIR/
├── assist_memory.db          # SQLite, WAL mode
└── blobs/
    └── <aa>/<sha256>         # content-addressed; <aa> = first 2 hex chars
```

- Content-addressed blobs: identical uploads dedupe to one file (each upload
  still gets its own artifact row).
- SQLite schema (DDL owned by the backend, versioned via `PRAGMA user_version`):

```sql
CREATE TABLE memory_revisions (
  id INTEGER PRIMARY KEY,
  namespace TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER NOT NULL,
  kind TEXT NOT NULL, value TEXT,            -- JSON-encoded value or raw text
  value_is_json INTEGER NOT NULL DEFAULT 0,
  tags TEXT NOT NULL DEFAULT '[]',
  source_surface TEXT NOT NULL DEFAULT 'other',
  deleted INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE (namespace, key, revision)
);
CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
  surface TEXT NOT NULL, status TEXT NOT NULL,
  summary TEXT, created_at TEXT NOT NULL, ended_at TEXT
);
CREATE TABLE session_events (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  seq INTEGER NOT NULL, timestamp TEXT NOT NULL,
  type TEXT NOT NULL, message TEXT NOT NULL, data TEXT,
  UNIQUE (session_id, seq)
);
CREATE TABLE artifacts (
  artifact_id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
  filename TEXT NOT NULL, mime TEXT NOT NULL,
  size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
  uploaded_at TEXT NOT NULL, source_surface TEXT NOT NULL,
  session_id TEXT, memory_key TEXT, tags TEXT NOT NULL DEFAULT '[]',
  storage_path TEXT NOT NULL, is_debug_capture INTEGER NOT NULL DEFAULT 0
);
-- indexes: memory_revisions(namespace, key, revision DESC),
--          session_events(session_id, seq), artifacts(namespace),
--          artifacts(session_id)
```

### `StorageBackend` interface

All persistence goes through one abstract class; tool code never touches
`sqlite3`, paths, or `open()` directly. This is the swap point for Replit
Object Storage / Postgres later.

```python
class StorageBackend(ABC):
    # memory
    def save_revision(ns, key, value, value_is_json, kind, tags, surface, deleted=False) -> int  # new revision no.
    def get_revision(ns, key, revision: int | None) -> MemoryRevision | None    # None revision = latest
    def list_entries(ns, kind, tag, prefix) -> list[MemoryEntryMeta]            # latest non-tombstone per key
    def search_entries(ns, query) -> list[MemoryEntryMeta]
    def get_history(ns, key) -> list[MemoryRevision]
    # sessions
    def create_session(...) -> Session
    def get_session(session_id) -> Session | None          # includes events + linked artifact ids
    def append_event(session_id, type, message, data) -> int  # seq
    def close_session(session_id, summary) -> Session
    def list_sessions(ns, status, limit) -> list[SessionMeta]
    # artifacts
    def store_artifact(meta, content: bytes) -> Artifact
    def get_artifact(artifact_id) -> Artifact | None
    def read_artifact_bytes(artifact_id, max_bytes) -> bytes
    def list_artifacts(ns, session_id) -> list[ArtifactMeta]
    # meta
    def usage() -> StorageUsage   # bytes used (db + blobs), counts
```

Default implementation: `SqliteFsBackend(data_dir)`.

---

## 5. Tools

Conventions for all tools:
- `namespace` is optional everywhere; omitted → `"default"`.
- Timestamps in/out: UTC ISO-8601 `...Z`.
- Success returns are JSON objects as specified below.
- Errors are returned as MCP tool errors (`isError: true`) whose message is a
  JSON object `{"code": "<ERROR_CODE>", "message": "<human text>", ...extra}`.

Shared error codes:

| Code | Meaning |
|---|---|
| `INVALID_ARGUMENT` | bad enum value, malformed namespace/key, bad encoding, value too large, bad base64/JSON |
| `NOT_FOUND` | key/revision/session/artifact doesn't exist (or key is tombstoned for `memory_get`) |
| `SESSION_CLOSED` | log/end attempted on closed session |
| `SESSION_EXISTS` | caller-supplied session_id already taken |
| `UPLOAD_TOO_LARGE` | decoded upload > `MAX_UPLOAD_MB` |
| `STORAGE_FULL` | write would exceed `MAX_TOTAL_STORAGE_MB`; message includes `used_mb`, `limit_mb` |
| `ZIP_UNSAFE` | zip-slip / absolute path / symlink / decompressed-size or file-count cap hit |
| `BINARY_NOT_TEXT` | `artifact_get` text mode on non-text content |

### 5.1 Memory

**`memory_save(namespace?, key, value, kind?, tags?, source_surface?)`**
- `value`: string or JSON-serializable object. `kind` default `note`.
- → `{"namespace", "key", "revision", "created_at", "warnings": [..]}`
  (`warnings` includes the possible-secret notice when triggered, else `[]`)
- Errors: `INVALID_ARGUMENT` (bad kind/namespace/key, value > 256 KB),
  `STORAGE_FULL`.

**`memory_get(namespace?, key, revision?)`**
- Omitted `revision` → latest. Latest tombstone → `NOT_FOUND`. Explicit
  revision returns that row even if it's a tombstone (with `"deleted": true`).
- → `{"namespace", "key", "revision", "kind", "value", "tags",
     "source_surface", "created_at", "deleted"}`
- Errors: `NOT_FOUND`, `INVALID_ARGUMENT`.

**`memory_list(namespace?, kind?, tag?, prefix?)`**
- Metadata only — no values. Latest non-tombstone revision per key; filters AND-ed.
- → `{"entries": [{"key", "kind", "tags", "revision", "source_surface",
     "created_at"}], "count"}`

**`memory_search(namespace?, query)`**
- Case-insensitive substring match over keys, tags, and values (latest
  non-tombstone revisions). SQLite `LIKE`-based in v1; FTS5 is a backend-
  internal upgrade path.
- → `{"results": [{"key", "kind", "tags", "revision", "created_at",
     "value_preview"}], "count"}` (`value_preview` ≤ 200 chars)

**`memory_history(namespace?, key)`**
- All revisions, ascending, including tombstones.
- → `{"key", "revisions": [{"revision", "created_at", "source_surface",
     "kind", "deleted", "value_preview"}], "count"}`
- Errors: `NOT_FOUND` (key never existed).

**`memory_revert(namespace?, key, to_revision)`**
- Creates a NEW revision copying `to_revision`'s value/kind/tags.
- → `{"key", "revision" (new), "reverted_to": to_revision}`
- Errors: `NOT_FOUND` (key or revision), `INVALID_ARGUMENT`
  (`to_revision` is a tombstone), `STORAGE_FULL`.

**`memory_delete(namespace?, key)`**
- Appends tombstone revision.
- → `{"key", "revision" (tombstone's), "deleted": true}`
- Errors: `NOT_FOUND` (never existed or already tombstoned).

### 5.2 Sessions

**`session_start(namespace?, surface, label?, summary?, session_id?)`**
- `session_id` optional override (must be unique); otherwise generated
  `YYYY-MM-DDTHH-MM-SSZ_<label>` (current UTC time; label slugified
  `[a-z0-9-]`, default `session`) to match the debug-capture skill convention.
- → `{"session_id", "namespace", "surface", "status": "open", "created_at"}`
- Errors: `INVALID_ARGUMENT`, `SESSION_EXISTS`.

**`session_log(session_id, type, message, data?)`**
- → `{"session_id", "seq", "timestamp"}`
- Errors: `NOT_FOUND`, `SESSION_CLOSED`, `INVALID_ARGUMENT`.

**`session_end(session_id, summary)`**
- Sets `status=closed`, `ended_at=now`, replaces summary.
- → `{"session_id", "status": "closed", "ended_at", "event_count"}`
- Errors: `NOT_FOUND`, `SESSION_CLOSED`.

**`session_list(namespace?, status?, limit?)`**
- `limit` default 20, max 200; newest first.
- → `{"sessions": [{"session_id", "namespace", "surface", "status",
     "summary", "created_at", "ended_at", "event_count"}], "count"}`

**`session_get(session_id)`**
- Full record: all events (ordered by `seq`) + linked artifact metadata.
- → `{"session_id", "namespace", "surface", "status", "summary",
     "created_at", "ended_at",
     "events": [{"seq", "timestamp", "type", "message", "data"}],
     "artifacts": [{"artifact_id", "filename", "mime", "size_bytes",
                    "uploaded_at"}]}`
- Errors: `NOT_FOUND`.

### 5.3 Handoff (sugar over memory entries, kind=handoff)

Handoffs are memory entries with the reserved key **`handoff/latest`** in the
target namespace, so full revision history is the handoff history.

**`handoff_save(namespace?, from_surface, content, session_id?)`**
- Writes `handoff/latest` with `kind=handoff`,
  `source_surface=from_surface`, value
  `{"content": content, "session_id": session_id, "saved_at": now}`.
- → `{"key": "handoff/latest", "revision", "namespace"}`
- Errors: `INVALID_ARGUMENT`, `STORAGE_FULL`.

**`handoff_load(namespace?)`**
- → `{"namespace", "content", "from_surface", "session_id", "saved_at",
     "revision",
     "history": [{"revision", "created_at", "source_surface",
                  "value_preview"}]}`
  — latest handoff plus the full revision pointer list so the receiving
  surface can backtrack via `memory_get(key="handoff/latest", revision=N)`.
- Errors: `NOT_FOUND` (no handoff ever saved in namespace).

### 5.4 Artifacts

**`artifact_upload(namespace?, filename, content, encoding, session_id?, tags?, source_surface?)`**
- `encoding`: `text | json | base64`. `text`/`json` → UTF-8 bytes (`json` is
  validated and stored canonically); `base64` → decoded bytes (ZIP etc.).
- Decoded size checked against `MAX_UPLOAD_MB`, then global usage against
  `MAX_TOTAL_STORAGE_MB`.
- If decoded bytes are a ZIP (magic `PK\x03\x04`): run safety checks (§7.2),
  then debug-capture inspection (§6).
- → `{"artifact_id", "filename", "mime", "size_bytes", "sha256",
     "uploaded_at", "debug_capture": null | {…see §6…}, "warnings": []}`
- Errors: `INVALID_ARGUMENT` (bad encoding/base64/JSON, bad filename,
  unknown session_id), `UPLOAD_TOO_LARGE`, `STORAGE_FULL`, `ZIP_UNSAFE`.

**`artifact_list(namespace?, session_id?)`**
- → `{"artifacts": [{"artifact_id", "filename", "mime", "size_bytes",
     "sha256", "uploaded_at", "source_surface", "session_id", "tags",
     "is_debug_capture"}], "count"}`

**`artifact_get(artifact_id, mode?, offset?, length?)`**
- `mode`: `metadata` (default) `| text | base64`.
  - `metadata`: row fields only, no content.
  - `text`/`base64`: returns a byte range of the artifact. `offset` (default
    0) and `length` (default = remaining bytes) select the range; the
    returned chunk is capped at **1 MB** per call (`length` > 1 MB →
    `INVALID_ARGUMENT`). Files of any size are retrievable by paging with
    `offset`/`length`; the response always carries `size_bytes`, `offset`,
    `length` (actual bytes returned), and `eof` so callers know when to stop.
  - `text`: the selected chunk is UTF-8 decoded (strict); refused with
    `BINARY_NOT_TEXT` if it doesn't decode (including a range boundary that
    splits a multi-byte character — the error suggests base64 mode).
  - `offset` ≥ file size → `INVALID_ARGUMENT`.
- → metadata fields + (`"content"`, `"encoding"`, `"offset"`, `"length"`,
  `"eof"`) when mode ≠ metadata.
- Errors: `NOT_FOUND`, `BINARY_NOT_TEXT`, `INVALID_ARGUMENT`.

### 5.5 Server meta

**`server_status()`**
- → `{"version", "storage": {"used_mb", "limit_mb", "data_dir_free_mb"},
     "counts": {"memory_keys", "memory_revisions", "sessions",
                "open_sessions", "artifacts"},
     "limits": {"max_upload_mb", "max_total_storage_mb"}}`

---

## 6. Debug-capture ZIP ingestion

On `artifact_upload` of a ZIP that passed safety checks:

1. Look for `session.json` at the ZIP root or exactly one directory deep.
2. Parse it; recognize as a debug-capture export iff `schema_version == "1.0"`
   and `session_id` is present. Expected shape (extra fields ignored):
   `schema_version, session_id, mode[], metadata{created_at, ended_at, tool,
   claude_surface, operator}, context{project, plugin_version, target_url,
   git_branch, git_commit}, results{passed, failed, warnings, errors[],
   summary}, artifacts{...}, agent_handoff{brief, deploy_script,
   rerun_command, suggested_actions[], ready_for_agent}`.
3. If recognized:
   - **Session**: create a session with ITS `session_id` (surface from
     `metadata.claude_surface` mapped to our enum, `created_at`/`ended_at`
     from metadata when parseable), `status=closed`, `summary` from
     `results.summary` (fallback: "debug-capture import"). If that
     session_id already exists, update its summary/status instead
     (idempotent re-import; no `SESSION_EXISTS` error here).
   - **Brief**: if `agent-handoff/brief.md` exists in the ZIP (same root
     as session.json), store its text as a memory entry:
     key `debug/<session_id>/brief`, `kind=handoff`,
     tags `["debug-capture"]`, in the upload's namespace.
   - **Artifact**: store the ZIP itself as ONE artifact linked to the
     session (`session_id` set, `is_debug_capture=true`). Do NOT explode
     inner files into individual artifacts; only `session.json` and
     `brief.md` are indexed as above.
   - Response `debug_capture` field:
     `{"recognized": true, "session_id", "session_created": bool,
       "brief_memory_key": "debug/<sid>/brief" | null,
       "results_summary": "..."}`
4. If no/invalid `session.json`: store as a plain artifact,
   `debug_capture: null`. A malformed-but-present session.json never fails
   the upload; it just isn't recognized (a warning is included).

Test fixture: `tests/fixtures/debug_capture_session.zip` built (by a checked-in
builder script at import/test time, or committed binary) matching the real
schema above, containing `session.json` + `agent-handoff/brief.md`.

---

## 7. Security & limits

### 7.1 Auth
As §2. Constant-time token compare; middleware order guarantees 401 before any
MCP handling; `GET /` is the only anonymous route.

### 7.2 ZIP safety (checked before any extraction/inspection)
Reject with `ZIP_UNSAFE` if any entry has:
- a name that is absolute, contains `..` path components, a drive letter, or
  a leading `/`/`\` (zip-slip);
- symlink/external-attribute indicating a link;
- or if: total **declared decompressed size > 4 × MAX_UPLOAD_MB**, actual
  streamed decompressed bytes exceed the same cap (defends against lying
  headers / zip bombs), or **entry count > 2000**.
Inspection reads only `session.json` and `agent-handoff/brief.md`, each
streamed with a hard per-file cap (4 MB); nothing else is decompressed.

### 7.3 Secret hygiene (best effort, store-but-flag)
On text/JSON memory values, handoff content, and text/JSON artifact uploads,
scan for token patterns:
`ghp_[A-Za-z0-9]{20,}`, `github_pat_[A-Za-z0-9_]{20,}`,
`sk-ant-[A-Za-z0-9-]{10,}`, `AKIA[A-Z0-9]{16}`,
`-----BEGIN( RSA| EC| OPENSSH)? PRIVATE KEY-----`.
On match: store normally, add tag `possible-secret`, include warning
`"value matches a credential-like pattern (<pattern name>); stored with tag
'possible-secret'"` in the response `warnings`. Never reject, never log the
matched content (log only pattern name + key/artifact id).

### 7.4 Size limits
| Limit | Value | Error |
|---|---|---|
| Memory value size | 256 KB | `INVALID_ARGUMENT` |
| Upload (decoded) | `MAX_UPLOAD_MB` (25) | `UPLOAD_TOO_LARGE` |
| Total storage (db file + blobs) | `MAX_TOTAL_STORAGE_MB` (500) | `STORAGE_FULL` with `used_mb`/`limit_mb` |
| ZIP decompressed | 4 × `MAX_UPLOAD_MB` | `ZIP_UNSAFE` |
| ZIP entry count | 2000 | `ZIP_UNSAFE` |
| `artifact_get` chunk per call | 1 MB (page with offset/length for larger files) | `INVALID_ARGUMENT` |
| `session_list` limit | ≤ 200 | clamped |

### 7.5 No outbound calls
The server imports no HTTP client and makes no network requests. Nothing in
config accepts a third-party credential.

---

## 8. Project layout

```
mcp-assist-memory/
├── SPEC.md
├── README.md
├── main.py                    # entrypoint: env config, auth middleware, run server
├── pyproject.toml             # deps: mcp (FastMCP), uvicorn/starlette, pytest
├── .replit / replit.nix       # Replit run config
├── .gitignore                 # data/, .env, __pycache__/, *.pyc, .pytest_cache/
├── src/assist_memory/
│   ├── __init__.py
│   ├── config.py              # env parsing, validation
│   ├── server.py              # FastMCP app, tool definitions (thin: validate → backend)
│   ├── auth.py                # bearer middleware
│   ├── models.py              # dataclasses, enums, validation helpers
│   ├── storage/
│   │   ├── base.py            # StorageBackend ABC
│   │   └── sqlite_fs.py       # SqliteFsBackend
│   ├── zip_ingest.py          # ZIP safety + debug-capture inspection
│   └── secrets_scan.py        # pattern scan
└── tests/
    ├── conftest.py            # tmp DATA_DIR, test client, auth headers
    ├── fixtures/
    │   ├── build_fixture_zip.py
    │   └── debug_capture_session.zip
    ├── test_memory.py         # revisions, get-by-revision, revert, tombstone,
    │                          # list/search/history
    ├── test_sessions.py       # lifecycle, event ordering, closed-session errors
    ├── test_handoff.py        # round-trip across two simulated surfaces
    ├── test_artifacts.py      # upload encodings, size caps, artifact_get modes
    ├── test_zip_safety.py     # zip-slip, symlink, bomb, file-count
    ├── test_debug_capture.py  # fixture → session + brief memory entry
    ├── test_auth.py           # missing/wrong token → 401 on /mcp; GET / open
    └── test_storage_cap.py    # MAX_TOTAL_STORAGE_MB enforcement
```

Tests run against the FastMCP server in-process (Streamable HTTP test client)
with a per-test temporary `DATA_DIR` and a low `MAX_TOTAL_STORAGE_MB` for cap
tests. All listed test cases from the task brief are covered 1:1.

---

## 9. Replit deployment

- `main.py` reads `PORT`, binds `0.0.0.0`; works under Replit's "Run" button
  via `.replit` (`run = "python main.py"`) with pyproject-managed deps.
- Reserved VM deployment recommended; **`DATA_DIR` must point at persistent
  storage** — documented prominently in README (ephemeral container disk loses
  data on redeploy).
- README includes the env-var table, deploy steps, and exact registration
  commands:
  - CLI/Desktop: `claude mcp add -s user --transport http assist-memory
    https://<repl-url>/mcp -H "Authorization: Bearer <token>"`
  - claude.ai web: Settings → Connectors → Add custom connector with the same
    URL; note on web header-auth limitations and what works.

---

## 10. Out of scope (v1)

- Multi-user auth / OAuth, per-namespace permissions
- Artifact update/delete
- FTS5 ranking, vector search
- Replit Object Storage / Postgres backends (interface-ready, not implemented)
- Outbound integrations of any kind (by design, permanent)

---

## 11. Approved decisions (2026-06-10)

1. **Handoff key**: one chain per namespace at `handoff/latest` — approved.
2. **Memory value cap at 256 KB** — approved.
3. **Large artifacts**: supported from v1 via ranged `artifact_get`
   (`offset`/`length` paging, ≤ 1 MB per call) — per owner request.
4. **Error contract**: `{"code", "message"}` JSON in MCP tool errors —
   approved.

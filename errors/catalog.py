"""Standardized execution-error payload + remedy catalog (Phase 2, T2.5).

One machine-parseable shape for every execution failure:

    { "error": { "code": "...", "message": "...", "remedy": "...", "retryable": false } }

Per the reconciled plan, this payload surfaces as an MCP tool-execution error
(``isError: true`` result) so the model sees it and can recover — never a
JSON-RPC protocol error. The tool layer (``server/mcp_server.instrument``)
serializes ``AppError.payload`` into the ToolError text.

The catalog is seeded from failures that were actually paid for; codes without
a catalog entry still produce the standard shape (remedy null). ``remedy``
population is variant-controlled (T7.4, ``remedy_errors on|off``) so its effect
is measurable — stripping happens at the tool layer, the raise site always
supplies it.
"""
from __future__ import annotations

from typing import Any

# code -> (default remedy, retryable). Seeded from the paid-for failures.
CATALOG: dict[str, tuple[str, bool]] = {
    # The -32600 class: the hosted instance was asleep and the MCP session died.
    # (Protocol-level; documented here so client-side harnesses can map it.)
    "mcp_session_stale": (
        "instance was asleep — re-initialize the MCP session and retry", True),
    "unauthorized": (
        "authenticate with 'Authorization: Bearer <token>' (or ?token= for "
        "headerless clients); tokens are managed in /admin", False),
    "unknown_arg": (
        "remove or rename the argument; the error message lists the valid "
        "arguments (did-you-mean included when close)", False),
    "artifact_not_found": (
        "dangling artifact hash — re-upload the blob with artifact_put before "
        "referencing its sha256", False),
    "invalid_base64": (
        "content_base64 must be standard base64 of the raw bytes; re-encode "
        "and retry", False),
    "artifact_too_large": (
        "store large objects in object storage and reference the sha256 "
        "instead of inlining the bytes", False),
    "invalid_cursor": (
        "pass the next_cursor value from the previous memory_list response, "
        "unmodified", False),
    "session_not_found": (
        "no session with that session_id exists in this namespace — call "
        "session_create first, and check you passed the right namespace", False),
    "invalid_kind": (
        "kind must be one of note|decision|todo|handoff|config|claim|knowledge", False),
    "invalid_origin": (
        "origin must be one of tool|retrieval|synthesized|human|unknown", False),
    "write_verification_failed": (
        "the write could not be verified through the public read path — the "
        "ack would have been a lie; retry the write (it may not have "
        "persisted)", True),
    "write_conflict": (
        "concurrent writers exhausted the revision-collision retries — retry "
        "the write", True),
    "db_unavailable": (
        "the backing database dropped the connection repeatedly — wait and "
        "retry; check /healthz", True),
    "acl_denied": (
        "this token's TOKEN_NAMESPACE_ACL does not allow that namespace — use "
        "a namespace within your allowlist or ask the operator to extend the "
        "ACL", False),
    "observations_disabled": (
        "observation_log is disabled in clinical=true namespaces (free-text "
        "channel, PHI gate) — log the observation in a non-clinical namespace",
        False),
    "invalid_observation": (
        "category must be one of ergonomics|error_recovery|advisory|screening|"
        "docs_gap|surprise|suggestion and severity one of blocker|friction|note",
        False),
    "invalid_sha": (
        "meta.repo_sha/base_sha must be a hex commit sha, 7..40 chars (git's "
        "default abbreviation up to the full sha) — fix the ref and retry; "
        "prefer recording the full 40-char sha", False),
    "ambiguous_sha": (
        "the abbreviated sha matches more than one commit upstream — record "
        "more characters (ideally the full 40-char sha) and retry", False),
    "curator_family_conflict": (
        "CURATOR_FAMILY_MUST_DIFFER_FROM forbids same-family curation of "
        "these entries — configure a curator from a different model family", False),
}


# T8.2: elicitation without nagging — the nudge lives exactly where friction is
# fresh (error payloads, advisories, quarantine verdicts), never in every tool
# description (bloat has its own unmeasured cost; Phase 10 backlog).
FEEDBACK_NUDGE = "surprised by this? observation_log it (optional)"


class AppError(Exception):
    """An execution failure with the standardized payload. Raise sites pass a
    ``code`` (+ optional message/remedy overrides); the catalog supplies the
    default remedy and retryability."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        remedy: str | None = None,
        retryable: bool | None = None,
        **context: Any,
    ) -> None:
        cat_remedy, cat_retryable = CATALOG.get(code, (None, False))
        self.code = code
        self.message = message
        self.remedy = remedy if remedy is not None else cat_remedy
        self.retryable = retryable if retryable is not None else cat_retryable
        self.context = context
        super().__init__(message)

    @property
    def payload(self) -> dict:
        err: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "remedy": self.remedy,
            "retryable": self.retryable,
        }
        if self.context:
            err["context"] = self.context
        return {"error": err}

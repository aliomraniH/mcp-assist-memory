# Namespace isolation — design doc (Phase 9)

Status: design accepted; **minimal ACL implemented** (see below). Full
multi-tenancy is explicitly out of scope for this pass.

## Problem

Namespaces are soft: any authenticated token can read and write any
namespace. That is fine while every caller is the same operator, and wrong the
moment a second party (or a misbehaving client) holds a token. The trust
boundary work (Phases 2–6) hardens what is *stored*; this document is about
who may *touch* it.

## Proposed model

Token → two namespace-prefix allowlists:

```json
{
  "<token>": {
    "read":  ["proj-canvas", "dev/"],
    "write": ["dev/"]
  }
}
```

* Prefix semantics, same as `memory_list`'s `prefix` (literal, not pattern).
* `read` gates every namespace-scoped read tool; `write` gates the write
  tools. `coord_*` scans count as reads of their namespace; store-wide admin
  views (`coord_drift_scan`, `stats`) and global artifacts need an explicit
  `"admin": true` grant in a later pass.
* Fail closed with the standard `acl_denied` payload (remedy names the ACL),
  never a silent empty result — an empty result would be indistinguishable
  from "nothing stored", which is exactly the ambiguity the trust program
  exists to remove.
* Unconfigured ⇒ inert. No behavior change of any kind.

## Minimal implementation (shipped in this pass)

`TOKEN_NAMESPACE_ACL` — optional JSON env var, **one combined allowlist per
token** (applies to reads and writes alike):

```json
{ "tok-web": ["proj-canvas", "dev/"], "tok-cli": ["dev/"] }
```

* Enforced by `NamespaceACLMiddleware` (`server/mcp_server.py`) ahead of tool
  dispatch, on every tool call that carries a `namespace` argument.
* Out-of-scope call → standard error payload `acl_denied`, `retryable: false`,
  remedy naming the config.
* Tools without a `namespace` argument (artifacts, `stats`,
  `coord_drift_scan`) are NOT gated in this pass — they are operator/admin
  surfaces; the read/write split and admin grant land with the full model.
* Unset ⇒ the middleware passes everything through untouched.

## Deliberately not in this pass

* Read/write split and admin grants (schema above, enforcement later).
* Per-namespace encryption or row-level security.
* Artifact tenancy (content-addressed and global by design; revisit only with
  a concrete cross-tenant leak scenario).
* Token issuance/rotation UX beyond the existing /admin dashboard.

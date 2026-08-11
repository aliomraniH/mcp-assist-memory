# Cowork Prompt — Publish v0.4.1 to the Public MCP Registry

**Give this whole document to the Cowork session.** It is self-contained. It
assumes that session has browser access (Chrome extension) and push access to
`aliomraniH/mcp-assist-memory`.

Registry entry: **`io.github.aliomraniH/mcp-assist-memory`**
Publish commit: **`main` @ `1c93d879270cd24caa6f677f99ec515163ac467f`** (PR #19)
Tag to push: **`v0.4.1`**

---

## 0. Hard precondition — do not skip

**The Replit deploy must already be live and verified at `0.4.1`** before you
publish anything.

`server.json` advertises a live endpoint (`https://mcp-assist-memory.replit.app/mcp`).
Publishing 0.4.1 to the registry while that endpoint still serves 0.4.0 tells
every consumer the server is something it is not. Verify by calling `stats`
through the MCP endpoint and confirming `server_version == "0.4.1"`.

If it still reports `0.4.0`, **stop and do nothing else.** The deploy is the
blocker, not the publish. See `docs/REPLIT-DEPLOY-PROMPT_gate-defects-v041.md`.

---

## 1. Why this is worth doing carefully

The registry has been **stale for two releases**, and the reason is worth
understanding before you fix it.

The only tag ever pushed to this repo is **`v0.3.0`**. `v0.4.0` was never tagged.
The v0.4.0 deploy prompt described republishing as "a manual publish step and is
**yours**" — and it did not happen. So the public registry has been advertising
**0.3.0** while production ran 0.4.0, across a release that took the tool surface
from 27 to 30.

That means publishing `v0.4.1` moves the registry **0.3.0 → 0.4.1**, skipping
0.4.0 entirely. That is the correct outcome — 0.4.0 is superseded by 0.4.1 and
had three known defects — but make it a **conscious** choice rather than an
accident, and do not retroactively tag `v0.4.0` to "fill in" the history. Tagging
a superseded, defective build would publish it to the registry as though it were
current, because the workflow publishes whatever tag you push.

## 2. How publishing actually works here

It is **not** a manual `mcp-publisher` run and it is **not** a web form. It is
tag-triggered CI. `.github/workflows/publish-mcp.yml`:

```yaml
on:
  push:
    tags: ["v*"]
```

The job then, in order:

1. checks out the tagged commit,
2. downloads `mcp-publisher`,
3. authenticates with `./mcp-publisher login github-oidc` — **OIDC, no stored
   secret.** This is why the `io.github.aliomraniH/*` namespace is authorised:
   the registry trusts the GitHub identity of the workflow, so the tag must be
   pushed to *this* repo to work at all,
4. **overwrites `server.json`'s version from the tag name**
   (`VERSION=${GITHUB_REF#refs/tags/v}`),
5. publishes.

Step 4 is the one to be careful about. The in-repo `server.json` already says
`0.4.1` and a test (`test_server_version_matches_the_registry_advertised_version`)
pins `SERVER_VERSION == server.json.version`. But the workflow does not read that
file's version — **it reads your tag name.** A tag of `v0.4.2` would publish
`0.4.2` regardless of what the repo says, silently breaking the invariant that
test exists to protect. The tag must be exactly **`v0.4.1`**.

---

## 3. What to do

### 3.1 Establish ground truth first — use the browser

Before changing anything, find out what the registry **currently** advertises.
Do not assume it is 0.3.0 just because that is the only tag; confirm it.

The registry API returned **HTTP 503** when this document was written, which is
exactly why this task was routed to a session with browser access. Try, in order,
and record which one actually answered:

- `https://registry.modelcontextprotocol.io/v0/servers?search=mcp-assist-memory`
- the registry's web UI, searching for `mcp-assist-memory`
- `https://registry.modelcontextprotocol.io/v0/servers` filtered client-side

Record the exact `version` string you find, and the timestamp you checked. If the
registry is still down, **wait and retry** — do not push the tag blind. A publish
you cannot verify is a publish you cannot distinguish from a failure, which is
the same trap that let the 0.4.0 entry go stale unnoticed.

### 3.2 Push the tag

```bash
git fetch origin main
git tag v0.4.1 1c93d879270cd24caa6f677f99ec515163ac467f
git push origin v0.4.1
```

Tag the **merge commit** explicitly, not whatever `main` happens to point at when
you run this. If `main` has moved on, tagging `HEAD` would publish code that was
never verified by the deploy this publish is supposed to match.

### 3.3 Watch the workflow

The `Publish to MCP Registry` run should appear within seconds of the tag push.
Watch it to completion.

Failure modes worth recognising rather than retrying blindly:

| symptom | meaning |
|---|---|
| `login github-oidc` fails | the tag was pushed to a fork, or `id-token: write` was lost — the namespace authorisation is identity-based, so this is not a transient error |
| publish rejects the version | that version already exists in the registry; registries are typically append-only, so **do not** try to force it — bump instead and fix `server.json` + `SERVER_VERSION` in a real commit first |
| publish succeeds, registry unchanged | propagation delay, or you are reading a cache — recheck in §3.4 before concluding anything |

If the workflow is red, **do not delete and re-push the tag** to "retry". Read
the log, fix the cause in a commit, and tag a new version. A deleted-and-recreated
tag makes the published artifact untraceable to a commit.

### 3.4 Verify in the browser — this is the actual deliverable

A green workflow is not proof the registry updated; it only proves the publish
command exited zero. Confirm the **public** entry, through the browser, shows:

- `version`: **0.4.1**
- `name`: `io.github.aliomraniH/mcp-assist-memory`
- `remotes[0].url`: `https://mcp-assist-memory.replit.app/mcp`
- the `Authorization` header still marked `isRequired: true` and `isSecret: true`

Then close the loop end to end: the registry says 0.4.1, and `stats` against the
advertised endpoint also says 0.4.1. Those two agreeing is the whole point of the
task; either one alone has been green while the other was stale.

---

## 4. Report back

State plainly, with the evidence for each:

1. what the registry advertised **before** (with the timestamp you checked, and
   which URL actually answered),
2. what it advertises **now**,
3. the workflow run URL and its conclusion,
4. the `server_version` returned by the live endpoint at verification time,
5. whether 0.4.0 remains permanently unpublished (expected: **yes** — do not
   quietly fix this by tagging it).

If any step could not be completed — registry down, workflow red, versions
disagreeing — say so explicitly and leave the tag situation as you found it.
"Published but unverified" is a legitimate outcome to report; it is not a
legitimate outcome to describe as done.

---

## 5. Do not

- Do not edit `server.json`'s version by hand to make a mismatch go away. It is
  pinned to `SERVER_VERSION` by a test, and the workflow overwrites it from the
  tag anyway — hand-editing changes nothing about what gets published and breaks
  the invariant in the repo.
- Do not publish from a local `mcp-publisher` install. OIDC in CI is the
  authorisation path; a local publish would need a stored credential that
  deliberately does not exist.
- Do not tag `v0.4.0` retroactively (§1).
- Do not touch the deployment, the database, or any armed namespace. This task is
  registry metadata only.

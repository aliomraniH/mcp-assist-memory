---
name: Pushing past a corrupt unrelated ref / diverged remote
description: How to push this repo's working branch to GitHub when an internal ref has a corrupt object and the remote branch diverged.
---

# Pushing the working branch to GitHub (origin: aliomraniH/mcp-assist-memory)

## Auth
GitHub is connected via Replit connector (status healthy). Get the OAuth token in the
code_execution sandbox: `listConnections('github')[0].settings.access_token`, then push with
`https://x-access-token:<token>@github.com/...`. NEVER print the token — sanitize all output
(`out.split(token).join('<redacted>')`). Plain HTTPS password auth is rejected.

## Corrupt-object-on-push symptom
A push from the main workspace repo can fail with `could not parse commit <sha>` even though
`git log` of the working branch is clean and `fsck` is fine. Cause: git's push negotiation
walks ALL local refs as "haves"; an unrelated internal ref (e.g. refs/heads/replit-agent)
had a corrupt ancestor object. The bad sha is NOT in the working branch's history.

**Why:** push enumerates every ref for thin-pack negotiation, so corruption in a side ref
blocks a push of a totally clean branch.

**How to apply:** don't try to repair the corrupt object. Clone only the wanted branch into a
throwaway repo, which copies just the good objects reachable from that branch, then push from
there:
`git clone --single-branch --branch <branch> file:///home/runner/workspace /tmp/clean_push`

## Diverged remote (non-fast-forward) when content is already unified
The remote branch may hold commits the local lacks whose CONTENT is already present in local
HEAD (an earlier "Sync working tree to remote branch tip" commit squashed them in). Verify
HEAD is a content superset (grep for the remote commits' features), then merge the remote tip
with `-s ours` to keep HEAD's exact tree while recording the remote commits as ancestors. This
makes the push a normal fast-forward — no force-push (which is destructive) needed, no history
or content lost.

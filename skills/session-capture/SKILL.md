---
name: session-capture
description: Archive the current Claude Code or opencode session into the session vault — writes a markdown card with title, summary, and resume info. Use when the user says "capture this session", "封存這個 session", "存進 vault", or wants to close a session but keep a way back to it.
---

# Session Capture

Archive the current session as one markdown file in the vault, so the user can
safely close it and find it again later with the vault TUI.

## Iron rules

- **H1**: Never read `~/.claude/` or opencode's storage. Everything you write
  must come from this conversation, the environment, or the clipboard.
- **H2**: Never write a rendered resume command into the file. Only store the
  raw `harness` and `session_id` fields.
- **H4**: Fail loudly. If you cannot determine the session id with certainty,
  refuse to write the file and tell the user exactly why. A wrong session id
  is far worse than no capture.

## Step 1 — vault directory

```bash
VAULT="${SESSION_VAULT_DIR:-$HOME/session-vault}"; mkdir -p "$VAULT"; echo "$VAULT"
```

## Step 2 — detect harness

```bash
[ -n "$CLAUDECODE" ] && echo claude-code || echo opencode
```

## Step 3 — get the session id

### If harness is `claude-code`

Run in bash (primary path — do NOT transcribe a UUID by hand from anywhere):

```bash
printf '%s\n' "$CLAUDE_CODE_SESSION_ID"
```

Use the printed value verbatim from the tool output. If it is empty
(Claude Code < v2.1.132), fall back to the value substituted below by the
harness itself:

- Fallback session id: `${CLAUDE_SESSION_ID}`

If that line still shows the literal text `$`+`{CLAUDE_SESSION_ID}`
unsubstituted, both paths failed: **stop and tell the user** you cannot
capture without a trustworthy session id (H4).

### If harness is `opencode`

The user must run `/copy` in this session **immediately before** invoking this
skill. Read the clipboard:

```bash
pbpaste
```

The clipboard must be the `/copy` export, which contains lines like:

```
**Session ID:** ses_examplefixture0000000001
**Updated:** 7/15/2026, 3:17:25 PM
## Assistant (Build · Kimi-k2.6 · 3.9s)
```

Search the whole clipboard text for the `**Session ID:**` line — do not assume
it is at the top. Then run **all three checks**; if ANY fails, refuse to write
and ask the user to re-run `/copy` and invoke this skill again:

1. **Shape**: a line matching `**Session ID:** ses_…` exists.
2. **Freshness**: the `**Updated:**` timestamp is within 5 minutes of now
   (compare against `date`).
3. **Content match**: take the last `## User` message in the clipboard and
   compare it with the last user message you can see in this conversation.
   If they differ, the clipboard holds a *different* session — refuse.

Bonus: the last `## Assistant (<agent> · <model> · …)` heading gives you the
`model` field.

## Step 4 — write the card

Gather:

```bash
pwd                                            # → cwd field
basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"   # → repo field
date -Iseconds                                 # → captured_at field
```

Write `title` and `summary` yourself from the conversation:

- **title**: one line, specific enough to recognize three weeks later.
- **summary**: 2–5 sentences of prose in the body — what was discussed, where
  it is stuck / what it is waiting on, and the concrete next step.

Filename: `<vault>/YYYY-MM-DD-<slug>.md` where the slug is a short kebab-case
version of the title (ASCII). If the file already exists, append `-2`, `-3`, …
— never overwrite an existing card.

File format (exactly these frontmatter keys; `model` and `tags` are optional —
omit them rather than writing empty values; the key is `harness`, never
`agent`):

```markdown
---
harness: claude-code
session_id: <the id from step 3>
cwd: <pwd output>
repo: <repo name>
title: <your title>
captured_at: <date -Iseconds output>
model: <model, opencode only, optional>
tags: [<optional>]
---

<your summary>
```

## Step 5 — confirm

Tell the user the file path and the title you wrote, and that the session is
now safe to close.

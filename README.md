# Rewind

Deliberately archive an AI coding session, then "rewind" back to it anytime.

> **Naming**: the product name is **Rewind** (for the "return to an earlier
> session" verb). The internal codename was Session Vault; the CLI is now
> `rewind`, and the package is still `session_vault/` until the code is renamed
> to match.

## What it is

A session does just two things: **capture** and **rewind**.

```
[session-capture skill]  ──write──▶  [~/session-vault/]  ──read──▶  [rewind TUI]
```

- **Capture**: invoke the `session-capture` skill inside a conversation. It
  writes the current session as one markdown card (title / summary / the
  `harness` + `session_id` needed to get back).
- **Rewind**: run `rewind` in the vault directory, type to fuzzy-filter to the
  card, and click it to copy the resume command to the clipboard.

## Hard rules (from the spec — do not relitigate)

| # | Rule |
|---|------|
| H1 | The skill never reads `~/.claude/` or opencode storage; everything written comes only from the conversation, environment variables, or the clipboard |
| H2 | The vault stores only `harness` + `session_id`; the resume command is rendered at display time in the TUI (templates live in `COMMAND_TEMPLATES` in `session_vault/vault.py`) |
| H3 | The frontmatter key is `harness`, not `agent` |
| H4 | Rather write no capture than an uncertain session id; a broken `.md` shows as a red BROKEN card in the TUI and is never silently dropped |

## TUI

```bash
cd ~/session-vault
uv run --project ~/side-project/session-manager rewind
# or install once, then just run `rewind`:
# uv tool install --editable ~/side-project/session-manager
```

- Type in the top input to filter live: each token must subsequence-match some
  *word* (word-level, not a scan over the whole text — otherwise almost
  anything matches).
- Click a card (or press Enter) to copy that harness's resume command; the card
  flashes `Copied ✓`.
- Cards are sorted by `captured_at`, newest first.
- Cards that are broken, missing fields, or have an unknown harness are shown in
  red with the reason (H4).

The vault path comes from `$SESSION_VAULT_DIR`, defaulting to `~/session-vault/`.

## Capture skill

`skills/session-capture/SKILL.md`, symlinked to
`~/.claude/skills/session-capture` (shared by Claude Code and opencode).

- **Claude Code**: bash reads `$CLAUDE_CODE_SESSION_ID` (verified present on this
  machine); older versions fall back to the `CLAUDE_SESSION_ID` value the
  harness substitutes.
- **opencode**: run `/copy` in the session first, then the skill extracts the
  `Session ID` from the clipboard, passing three guards before writing — shape
  (a `**Session ID:** ses_…` line exists) / freshness (`Updated` within 5
  minutes) / content match (the clipboard's last user message matches this
  conversation's).

Card filename is `YYYY-MM-DD-<slug>.md`; on a collision it appends `-2`, `-3`, …
and never overwrites.

## Card format

```markdown
---
harness: claude-code
session_id: <id>
cwd: <pwd>
repo: <repo name>
title: <one line, still recognizable three weeks later>
captured_at: <date -Iseconds>
model: <optional, opencode>
tags: [<optional>]
---

<2–5 sentence summary: what was discussed, where it's stuck, the next step>
```

Required keys: `harness` `session_id` `cwd` `title` `captured_at`.
`repo` `model` `tags` are optional (omit entirely rather than writing empty
values).

## Tests

```bash
uv run pytest
```

## Known unverified

- opencode `/copy` output format on long conversations
- OSC 52 paste behavior inside cmux

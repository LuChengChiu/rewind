# Rewind

Deliberately archive an AI coding session, then "rewind" back to it anytime.

> **Naming**: the product is **Rewind** (for the "return to an earlier session"
> verb) — the CLI command, the Python package (`rewind/`), the distribution, and
> the archive directory (`~/rewind/`, `$REWIND_DIR`) all share that one name.
> "Vault" survives only as *vocabulary in the code* for the data store
> (`load_vault`, `resolve_vault_dir`, …). Session Vault was the old codename and
> no longer names anything.

## What it is

A session does just two things: **capture** and **rewind**.

```
[rewind-capture skill]  ──write──▶  [~/rewind/]  ──read──▶  [rewind TUI]
```

- **Capture**: invoke the `rewind-capture` skill inside a conversation. It
  writes the current session as one markdown card (title / summary / the
  `harness` + `session_id` needed to get back).
- **Rewind**: run `rewind` from anywhere, type to fuzzy-filter to the card, and
  click it to copy the resume command to the clipboard. Focus a card and press
  <kbd>space</kbd> to preview that session's conversation.

## Hard rules (from the spec — do not relitigate)

| # | Rule |
|---|------|
| H1 | The skill never reads `~/.claude/` or opencode storage; everything written comes only from the conversation, environment variables, or the clipboard |
| H2 | The vault stores only `harness` + `session_id`; the resume command is rendered at display time in the TUI (templates live in `COMMAND_TEMPLATES` in `rewind/vault.py`) |
| H3 | The frontmatter key is `harness`, not `agent` |
| H4 | Rather write no capture than an uncertain session id; a broken `.md` shows as a red BROKEN card in the TUI and is never silently dropped |

H1 binds **the skill**, and it is about *provenance*: a card may only contain
what the agent actually witnessed, so nothing it writes can be a guess. It is
not a blanket ban on Rewind reading anything — the TUI's preview
(`rewind/transcript.py`) reads a harness's own session storage at
display time, and that is fine because it writes nothing to the vault. Same
split as H2: storage stays raw, the TUI renders. Nothing read from a harness
may ever flow back into a `.md`.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/LuChengChiu/rewind/main/install.sh | bash
```

`install.sh` installs uv if it is missing, then `uv tool install`s Rewind from
this repo's `main`. uv brings its own Python and isolates `textual` /
`python-frontmatter`, so there is nothing else to set up. Re-running the
installer upgrades in place.

### Auto-update

Rewind tracks `main`: whatever is pushed here is what a new install, or the next
update, gets. Once a day, `rewind` re-runs its own `uv tool install` in a
detached background process — so the **next** launch is the updated one, the way
`claude` behaves.

The update deliberately runs *after* the TUI exits rather than at launch:
`uv tool install --force` rebuilds the venv in place, and swapping site-packages
under the live process would break any not-yet-executed import. It never blocks
and never reports failure — offline just means no update (`rewind/update.py`).

Silent, but not invisible. A broken `main` and an up-to-date install look the
same from the outside, so the update writes down how it went:

```bash
cat ~/.cache/rewind/last-update.json   # {"attempted_at":"…","finished_at":"…","exit_code":0}
cat ~/.cache/rewind/last-update.log    # stderr of the last attempt, if any
```

That record answers "why am I still on the old one", which nothing else can: a
failed update and an already-current one both leave the commit unmoved. It has
exactly three readings.

| What you see | What happened |
| --- | --- |
| `exit_code` 0 | The update ran and succeeded. You are current; `main` simply has nothing newer. |
| `exit_code` non-zero | It ran and failed — `last-update.log` has the stderr. |
| no `exit_code` at all | It was attempted at `attempted_at` and never finished: spawn refused, laptop shut, process killed. |

The last row is why the attempt is written down *before* the update starts
rather than after it ends. Only the detached child can report an outcome, and a
child that dies reports nothing — so if the file were written only on
completion, the previous run's `"exit_code": 0` would still be sitting there,
and "updated fine, moments ago" is precisely the lie the file exists to prevent.
An attempt with no ending is the truth in that case, and it is what you get.

- `REWIND_NO_UPDATE=1` disables it.
- Only a `uv tool install` of *this* repo's `main` updates itself. A dev
  checkout (`uv run`, `--editable`), a fork, or a pinned tag is left alone:
  Rewind reads its own recorded origin (PEP 610) rather than inferring one from
  where it is installed, so local work is never overwritten.

## TUI

```bash
rewind
# dev: uv run --project ~/side-project/session-manager rewind
```

- Type in the top input to filter live: each token must subsequence-match some
  *word* (word-level, not a scan over the whole text — otherwise almost
  anything matches).
- Click a card (or press Enter) to copy that harness's resume command; the card
  flashes `Copied ✓`.
- Cards are sorted by `captured_at`, newest first.
- Cards that are broken, missing fields, or have an unknown harness are shown in
  red with the reason (H4).
- Focus a card and press <kbd>space</kbd> to preview its conversation;
  <kbd>esc</kbd> closes. The hint only appears on cards that can preview.

The vault path comes from `$REWIND_DIR`, defaulting to `~/rewind/`
— resolved by `resolve_vault_dir` in `rewind/vault.py`, the same rule the
capture skill uses to *write*, so read and write always agree. `rewind` reads
that directory regardless of where you launch it; you no longer need to `cd`
into the vault first.

### Preview

`rewind/transcript.py` reads the harness's own session storage at
display time — read-only, never written back to the vault (see the H1 note
above). For Claude Code that is
`~/.claude/projects/<cwd-with-slashes-as-dashes>/<session_id>.jsonl`, which the
card's `cwd` + `session_id` already locate exactly.

Reconstructing the conversation is a graph walk, not a file read: the file is a
DAG whose `parentUuid` chain threads through non-message records, and it
retains branches abandoned by rewinds. The reader maps *every* record by uuid,
takes the newest message as the leaf, walks `parentUuid` back to the root, and
only then filters to messages — mapping just the messages dead-ends the walk at
the first `attachment` and yields a truncated transcript that looks complete
(`tests/test_transcript.py::test_chain_walks_through_non_message_records`).

**Adding a harness** is one reader plus one entry in `TRANSCRIPT_READERS`, the
same shape as `COMMAND_TEMPLATES` in `vault.py`:

```python
def _read_opencode(session_id: str, cwd: str) -> list[Message]: ...

TRANSCRIPT_READERS = {
    "claude-code": _read_claude_code,
    "opencode": _read_opencode,
}
```

A harness with no reader has no preview: the card offers no hint and space does
nothing. That is a supported state, not an error — only `claude-code` has a
reader today, so opencode cards simply do not preview.

Because this reads a format Anthropic does not promise to keep stable, it is
display-only by design: if the format shifts, the dialog shows a loud error and
nothing else in Rewind is affected. Failures never fall back to a partial
transcript (H4).

## Capture skill

`skills/rewind-capture/SKILL.md`, symlinked to
`~/.claude/skills/rewind-capture` (shared by Claude Code and opencode).

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

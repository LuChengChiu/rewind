# Rewind

Archive an AI coding session on purpose, then rewind back to it whenever you want.

You close a Claude Code or opencode session knowing you'll want it again — and
three weeks later you can't remember which one it was, let alone its id. Rewind
gives you a deliberate save point: one command at the end of a session, one TUI
to find it again.

```
[/rewind-capture]  ──write──▶  [~/rewind/*.md]  ──read──▶  [rewind]
```

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/LuChengChiu/rewind/main/install.sh | bash
```

The installer adds [uv](https://docs.astral.sh/uv/) if it's missing, then
`uv tool install`s Rewind. uv brings its own Python and isolates the
dependencies, so there's nothing else to set up. Re-running the installer
upgrades in place.

## Use it

**Capture** — at the end of a session worth keeping, run the skill inside the
conversation:

```
/rewind-capture
```

It writes one markdown card to `~/rewind/`: a title, a short summary of where
things stand, and the harness + session id needed to get back. Then the session
is safe to close.

**Rewind** — run `rewind` from anywhere:

```bash
rewind
```

Type to filter, click the card you want, and its resume command
(`claude --resume <id>`) lands on your clipboard.

### Keys

| Key | What it does |
| --- | --- |
| type | Filter live — each word must fuzzy-match a word on some card |
| click / <kbd>enter</kbd> | Copy that session's resume command; the card flashes `Copied ✓` |
| <kbd>space</kbd> | Preview the session's actual conversation (focused card only) |
| <kbd>d</kbd> | Delete the focused card — <kbd>y</kbd> confirms, <kbd>esc</kbd> / <kbd>n</kbd> cancels |
| <kbd>esc</kbd> | Close the preview, or cancel a delete |
| <kbd>ctrl+r</kbd> | Re-read the vault, keeping whatever you've typed in the filter |
| <kbd>ctrl+c</kbd> / <kbd>ctrl+q</kbd> | Quit — press twice within 2s |

The vault is read at startup and on <kbd>ctrl+r</kbd> — nothing watches the
directory, so a session captured while Rewind is open needs a reload to appear.

Cards are sorted newest-first and laid out in up to three columns depending on
terminal width. A card that's malformed, missing fields, or from an unknown
harness shows up in red with the reason — never silently dropped, and never
hidden by the filter.

Deleting never erases: the card moves into `.trash/` inside the vault
(suffixed `-2`, `-3`, … on a name collision, never overwriting), where Rewind
stops listing it but the file — and the resume command inside it — survives.

### Preview

Focus a card and press <kbd>space</kbd> to read back the conversation itself.
Rewind reads the harness's own session storage at display time, read-only —
for Claude Code, the JSONL transcript that the card's `cwd` + `session_id`
locate exactly.

Only harnesses with a reader can preview. Today that's `claude-code`; an
opencode card's hint row simply omits preview and <kbd>space</kbd> does
nothing. That's a supported state, not an error.

## Configuration

| | |
| --- | --- |
| `REWIND_DIR` | Where cards live. Defaults to `~/rewind/`. Read and write resolve it the same way, so the skill and the TUI always agree. |
| `REWIND_NO_UPDATE=1` | Disable auto-update. |

### Auto-update

Rewind tracks `main`. Once a day, after the TUI exits, it re-runs its own
`uv tool install` in a detached background process — so the *next* launch is
the updated one, the way `claude` behaves. It never blocks and never interrupts
you; offline just means no update.

Silent, but not invisible. A broken `main` and an already-current install look
identical from the outside, so each attempt is written down:

```bash
cat ~/.cache/rewind/last-update.json   # {"attempted_at":"…","finished_at":"…","exit_code":0}
cat ~/.cache/rewind/last-update.log    # stderr of the last attempt, if any
```

| What you see | What happened |
| --- | --- |
| `exit_code` 0 | Ran and succeeded — you're current. |
| `exit_code` non-zero | Ran and failed; the log has the stderr. |
| no `exit_code` | Attempted and never finished: spawn refused, laptop shut, process killed. |

Only a `uv tool install` of this repo's `main` updates itself. A dev checkout,
a fork, or a pinned tag is left alone — Rewind checks its own recorded origin
(PEP 610) rather than guessing from where it's installed.

## Harness support

|  | Capture | Resume | Preview |
| --- | --- | --- | --- |
| Claude Code | automatic | `claude --resume <id>` | ✅ |
| opencode | run `/copy` first | `opencode -s <id>` | — |

Claude Code capture reads the session id straight from the environment.
opencode has no equivalent, so you run `/copy` in the session first and the
skill takes the id from the clipboard — passing three guards before it writes
anything: the shape of the `Session ID` line, a freshness check on the
`Updated` timestamp, and a content match between the clipboard's last user
message and the live conversation.

**Adding a harness** takes two entries: a resume template in `COMMAND_TEMPLATES`
(`rewind/vault.py`) and, optionally, a transcript reader in `TRANSCRIPT_READERS`
(`rewind/transcript.py`).

## Card format

Cards are plain markdown with YAML frontmatter — editable by hand, greppable,
diffable.

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

<2–4 sentences: what this session was, what's still open, the next step>
```

Required: `harness` `session_id` `cwd` `title` `captured_at`. `repo` `model`
`tags` are optional — omitted entirely rather than left empty.

Filenames are `YYYY-MM-DD-<slug>.md`, suffixed `-2`, `-3`, … on collision. An
existing card is never overwritten.

## Design rules

Four constraints the implementation holds to, worth knowing before changing it:

- **The capture skill never reads a harness's storage.** Everything on a card
  comes from the conversation, the environment, or the clipboard — so nothing on
  it can be a guess. (The TUI's preview *does* read that storage, which is fine:
  it writes nothing back.)
- **Cards store `harness` + `session_id`, never a rendered command.** The resume
  command is built at display time from `COMMAND_TEMPLATES`.
- **The frontmatter key is `harness`, not `agent`.**
- **Better no capture than an uncertain session id.** Anything unparseable
  surfaces as a red BROKEN card instead of disappearing, and a failed preview
  shows the error rather than a partial transcript.

## Development

```bash
uv run --project . rewind   # run from a checkout
uv run pytest
```

The capture skill lives in `skills/rewind-capture/`, symlinked to
`~/.claude/skills/rewind-capture` (shared by Claude Code and opencode).

### Known unverified

- opencode `/copy` output format on long conversations
- OSC 52 paste behavior inside cmux

## License

MIT — see [LICENSE](LICENSE).

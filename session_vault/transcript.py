"""Read a harness's own session storage to render a read-only preview.

Scope (H1): H1 binds the *capture skill* — everything it writes into a card
must come from the conversation, the environment, or the clipboard. This module
writes nothing to the vault. It reads a harness's storage at display time and
renders it, exactly as H2 renders resume commands at display time instead of
storing them. Nothing read here may ever flow back into a `.md`.

Adding a harness is one reader function plus one `TRANSCRIPT_READERS` entry —
same shape as `COMMAND_TEMPLATES` in vault.py. A harness with no reader simply
has no preview; that is a supported state, not an error.

H4: every failure raises `TranscriptError` and is shown loudly in the dialog.
A truncated transcript that looks complete is the thing to avoid here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_EPOCH = datetime.fromtimestamp(0, tz=timezone.utc)

# Harness plumbing that rides inside user messages: slash-command dispatch and
# its captured stdout. Matched by name, never as generic tags — real prompts in
# this vault contain <id>, <vault> and <slug>, which are the user's own words.
_HARNESS_WRAPPERS = (
    "local-command-caveat",
    "local-command-stdout",
    "command-name",
    "command-message",
    "command-args",
    "system-reminder",
)
_WRAPPER_RE = re.compile(
    r"<(" + "|".join(_HARNESS_WRAPPERS) + r")>.*?</\1>", re.DOTALL
)
# Command stdout is captured raw, escape codes and all.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean(text: str) -> str:
    return _ANSI_RE.sub("", _WRAPPER_RE.sub("", text)).strip()

# Conversation lives in `user` / `assistant` records, but the parentUuid chain
# is threaded through *every* record type — `attachment` and `system` records
# sit between messages as parents. So the uuid map must hold all records and
# only the output is filtered to messages; mapping just the messages dead-ends
# the walk at the first attachment and yields a plausible-looking half.
_MESSAGE_TYPES = ("user", "assistant")


class TranscriptError(Exception):
    """A transcript could not be read or trusted. Always surfaced to the user."""


@dataclass(frozen=True)
class Message:
    role: str
    text: str
    timestamp: datetime | None = None


# (session_id, cwd) -> messages, oldest first.
TranscriptReader = Callable[[str, str], list[Message]]


def _claude_code_transcript_path(session_id: str, cwd: str) -> Path:
    # Claude Code slugifies the session's cwd into a project directory name and
    # names the file after the session id: both are already on the card.
    slug = cwd.replace("/", "-")
    return Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl"


# Per-tool: which input field best summarizes the call on one line. First key
# that is present wins; a tool absent here shows its name alone. Path-valued
# fields are basename'd by _tool_summary so the line stays short.
_TOOL_SUMMARY_KEYS: dict[str, tuple[str, ...]] = {
    "Bash": ("command",),
    "Read": ("file_path",),
    "Edit": ("file_path",),
    "Write": ("file_path",),
    "NotebookEdit": ("file_path",),
    "Glob": ("pattern",),
    "Grep": ("pattern",),
    "Agent": ("description",),
    "Task": ("description",),
    "Skill": ("skill",),
    "WebFetch": ("url",),
    "WebSearch": ("query",),
}
_PATH_KEYS = {"file_path", "path"}
_SUMMARY_WIDTH = 60  # max chars inside the parens; longer values end in "…"


def _tool_summary(name: str, tool_input: object) -> str:
    # A malformed block degrades to the bare name rather than erroring: this is
    # one line's label, not the transcript, so H4's loud-failure rule (which
    # guards against a *partial transcript* posing as complete) does not apply.
    if not isinstance(tool_input, dict):
        return f"⏺ {name}"
    for key in _TOOL_SUMMARY_KEYS.get(name, ()):
        value = tool_input.get(key)
        if not isinstance(value, str):
            continue
        summary = value.split("/")[-1] if key in _PATH_KEYS else value
        summary = " ".join(summary.split())  # collapse newlines/indent to one line
        if not summary:  # whitespace-only, or a path ending in "/"
            continue
        if len(summary) > _SUMMARY_WIDTH:
            summary = summary[: _SUMMARY_WIDTH - 1] + "…"
        return f"⏺ {name}({summary})"
    return f"⏺ {name}"


def _extract_text(message: dict) -> str:
    """Flatten a message's content to display text.

    `thinking` and `tool_result` blocks are dropped: this is a "which session
    was this?" preview, and both bury the conversation they are meant to locate.
    A `tool_use` becomes a one-line `⏺ Name(summary)` — the summary is the input
    field that most identifies the call, so the preview reads like the session
    did instead of a column of bare `⏺ Bash`.
    """
    content = message.get("content")
    if isinstance(content, str):
        return _clean(content)
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(_clean(str(block.get("text", ""))))
        elif block.get("type") == "tool_use":
            parts.append(_tool_summary(str(block.get("name", "tool")), block.get("input")))
    return "\n\n".join(p for p in parts if p)


def _parse_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _leaf_order(record: dict) -> datetime:
    """Sort key for leaf selection: undated records lose, naive ones get local tz."""
    ts = _parse_timestamp(record.get("timestamp"))
    if ts is None:
        return _EPOCH
    return ts if ts.tzinfo else ts.astimezone()


def _is_message(record: dict) -> bool:
    # Sidechain records are subagent turns, a separate chain hanging off the
    # main one. They are not what the user is looking for in a preview.
    return record.get("type") in _MESSAGE_TYPES and not record.get("isSidechain")


def _read_claude_code(session_id: str, cwd: str) -> list[Message]:
    path = _claude_code_transcript_path(session_id, cwd)
    if not path.is_file():
        raise TranscriptError(
            f"No transcript file at {path}\n\n"
            "The session may have been deleted/expired, or captured under a "
            "different working directory than it started in."
        )

    records: dict[str, dict] = {}
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # One bad line is a format surprise, not a transcript. Fail loud
            # rather than render whatever happened to parse (H4).
            raise TranscriptError(f"Unparseable JSON in {path.name}") from None
        uuid = record.get("uuid")
        if isinstance(uuid, str):
            records[uuid] = record

    leaves = [r for r in records.values() if _is_message(r)]
    if not leaves:
        raise TranscriptError(f"No conversation found in {path.name}")

    # Latest-timestamp leaf. Claude Code consults a leafUuids index to pick
    # precisely among forked tips; for display, newest-wins agrees with it on
    # unforked sessions and is off by one branch at worst.
    leaf = max(leaves, key=_leaf_order)

    chain: list[dict] = []
    seen: set[str] = set()
    current: dict | None = leaf
    while current is not None:
        uuid = current.get("uuid")
        if not isinstance(uuid, str) or uuid in seen:
            break  # cycle, or a record we cannot identify: stop, keep what we have
        seen.add(uuid)
        chain.append(current)
        parent = current.get("parentUuid")
        current = records.get(parent) if isinstance(parent, str) else None
    chain.reverse()

    messages: list[Message] = []
    for record in chain:
        if not _is_message(record):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        text = _extract_text(message)
        if not text:
            continue  # tool_result-only turns carry nothing to show
        messages.append(
            Message(
                role=str(record.get("type", "")),
                text=text,
                timestamp=_parse_timestamp(record.get("timestamp")),
            )
        )
    if not messages:
        raise TranscriptError(f"No displayable messages in {path.name}")
    return _coalesce(messages)


def _coalesce(messages: list[Message]) -> list[Message]:
    """Merge same-role runs into one turn.

    A harness records one assistant turn as several records — prose, then a
    record per tool call — so a raw chain reads as a wall of `⏺ Bash` with the
    prose lost in it. Rejoining the run restores the turn the user actually saw.
    """
    merged: list[Message] = []
    for message in messages:
        if merged and merged[-1].role == message.role:
            previous = merged[-1]
            merged[-1] = Message(
                role=previous.role,
                text=f"{previous.text}\n\n{message.text}",
                timestamp=previous.timestamp,
            )
            continue
        merged.append(message)
    return merged


TRANSCRIPT_READERS: dict[str, TranscriptReader] = {
    "claude-code": _read_claude_code,
    # "opencode": _read_opencode,  # needs its storage format reversed first
}


def supports_preview(harness: str) -> bool:
    return harness in TRANSCRIPT_READERS


def read_transcript(harness: str, session_id: str, cwd: str) -> list[Message]:
    reader = TRANSCRIPT_READERS.get(harness)
    if reader is None:
        raise TranscriptError(f"No transcript reader for harness {harness!r}")
    return reader(session_id, cwd)

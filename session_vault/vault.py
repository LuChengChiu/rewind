"""Vault loading and session model.

The vault is a directory of markdown files with YAML frontmatter (spec §5).
H2: resume commands are rendered from harness + session_id at display time;
the templates live here, never in the vault files.
H4: a file that fails to parse becomes a Session with `error` set — it is
shown loudly in the TUI, never silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

COMMAND_TEMPLATES: dict[str, str] = {
    "claude-code": "claude --resume {session_id}",
    "opencode": "opencode -s {session_id}",
}

REQUIRED_KEYS = ("harness", "session_id", "cwd", "title", "captured_at")


@dataclass
class Session:
    path: Path
    title: str = ""
    harness: str = ""
    session_id: str = ""
    cwd: str = ""
    repo: str = ""
    captured_at: datetime | None = None
    model: str = ""
    tags: list[str] = field(default_factory=list)
    summary: str = ""
    error: str | None = None

    @property
    def resume_command(self) -> str | None:
        template = COMMAND_TEMPLATES.get(self.harness)
        if template is None or not self.session_id:
            return None
        return template.format(session_id=self.session_id)

    @property
    def search_text(self) -> str:
        return " ".join(
            [
                self.title,
                self.harness,
                self.repo,
                self.cwd,
                self.model,
                " ".join(self.tags),
                self.summary,
                self.path.name,
            ]
        ).lower()


def _parse_captured_at(raw: object) -> datetime:
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, str):
        dt = datetime.fromisoformat(raw)
    else:
        raise ValueError(f"captured_at has unsupported type {type(raw).__name__}")
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def load_session(path: Path) -> Session:
    try:
        post = frontmatter.load(path)
        meta = post.metadata
        missing = [k for k in REQUIRED_KEYS if not meta.get(k)]
        if missing:
            raise ValueError(f"missing frontmatter keys: {', '.join(missing)}")
        harness = str(meta["harness"])
        session = Session(
            path=path,
            title=str(meta["title"]),
            harness=harness,
            session_id=str(meta["session_id"]),
            cwd=str(meta["cwd"]),
            repo=str(meta.get("repo", "")),
            captured_at=_parse_captured_at(meta["captured_at"]),
            model=str(meta.get("model") or ""),
            tags=[str(t) for t in (meta.get("tags") or [])],
            summary=post.content.strip(),
        )
        if harness not in COMMAND_TEMPLATES:
            session.error = (
                f"unknown harness {harness!r} — no command template for it"
            )
        return session
    except Exception as exc:  # noqa: BLE001 — every failure must surface as a card
        return Session(path=path, error=f"{type(exc).__name__}: {exc}")


def load_vault(directory: Path) -> list[Session]:
    sessions = [load_session(p) for p in sorted(directory.glob("*.md"))]
    epoch = datetime.fromtimestamp(0, tz=timezone.utc)
    sessions.sort(
        key=lambda s: s.captured_at or epoch,
        reverse=True,
    )
    return sessions


def fuzzy_match(needle: str, haystack: str) -> bool:
    """True if needle's characters appear in order within haystack."""
    it = iter(haystack)
    return all(ch in it for ch in needle)


def matches(query: str, session: Session) -> bool:
    """Every query token must fuzzy-match some word of the session's text.

    Word-level (not whole-text) matching: a subsequence scan across the full
    concatenated text matches almost any card, which makes the filter useless.
    """
    tokens = query.lower().split()
    if not tokens:
        return True
    words = session.search_text.split()
    return all(any(fuzzy_match(token, word) for word in words) for token in tokens)


def relative_time(dt: datetime | None) -> str:
    if dt is None:
        return "unknown time"
    now = datetime.now(timezone.utc)
    seconds = (now - dt).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = int(seconds // 3600)
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(seconds // 86400)
    if days < 14:
        return f"{days} day{'s' if days != 1 else ''} ago"
    weeks = days // 7
    if days < 60:
        return f"{weeks} weeks ago"
    return dt.strftime("%Y-%m-%d")

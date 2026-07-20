"""Vault loading and session model.

The vault is a directory of markdown files with YAML frontmatter (spec §5).
H2: resume commands are rendered from harness + session_id at display time;
the templates live here, never in the vault files.
H4: a file that fails to parse becomes a Session with `error` set — it is
shown loudly in the TUI, never silently dropped.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

COMMAND_TEMPLATES: dict[str, str] = {
    "claude-code": "claude --resume {session_id}",
    "opencode": "opencode -s {session_id}",
}

SECONDS_PER_DAY = 86400


def resolve_vault_dir() -> Path:
    """Where the vault lives, resolved the same way whether reading or writing.

    Mirrors the capture skill (skills/rewind-capture/SKILL.md): the raw value
    of ``$REWIND_DIR`` if set, otherwise ``~/rewind``. Capture writes there and
    Rewind reads there, so both must land on the same directory — that symmetry
    is the whole reason this is one shared rule and not a cwd default. An empty
    env var is treated as unset (bash ``:-``).

    The env value is used verbatim — no ``expanduser`` — because the skill's
    ``mkdir -p "$VAULT"`` does no tilde expansion either. Expanding here would
    reintroduce the exact divergence this function exists to close: a
    single-quoted ``REWIND_DIR='~/v'`` would have the skill write to a
    literal ``~/v`` while Rewind read ``$HOME/v``. In normal use the shell
    expands ``~`` at assignment, so both sides already see an absolute path.
    """
    env = os.environ.get("REWIND_DIR")
    if env:
        return Path(env)
    return Path.home() / "rewind"


def resolve_trash_days() -> int | None:
    """How many days a trashed capture is kept before `purge_trash` erases it.

    ``$REWIND_TRASH_DAYS`` if set, otherwise 14; empty means unset, like
    ``$REWIND_DIR``. Returning None turns purging off entirely — and that is
    where every unusable value lands: purging is the only destruction in the
    codebase, so a typo (``14d``) or a non-positive number must mean "keep
    everything", never "purge on the default schedule". ``0`` is therefore
    the documented off switch, not "purge immediately".
    """
    raw = os.environ.get("REWIND_TRASH_DAYS")
    if raw is None or not raw.strip():
        return 14
    try:
        days = int(raw)
    except ValueError:
        return None
    return days if days > 0 else None


SETTINGS_FILENAME = "settings.json"


def load_scope_default(vault_dir: Path) -> bool:
    """Whether the scope toggle should start on, per the vault's `settings.json`.

    Lives in the vault dir rather than a home-wide config path because the
    setting narrows *this* vault's cards, so it travels with the vault when
    `$REWIND_DIR` points elsewhere. `load_vault` globs `*.md`, so the file can
    never be mistaken for a card.

    Every failure — missing file, bad JSON, wrong shape, unreadable — returns
    False, i.e. show everything. This is a preference, not data: Rewind must
    open and show cards whatever state this file is in, and the failure
    direction is "show more", never "show nothing".
    """
    try:
        raw = json.loads((vault_dir / SETTINGS_FILENAME).read_text())
        return bool(raw["scope_cwd"])
    except Exception:  # noqa: BLE001 — see docstring: nothing here may block launch
        return False


def save_scope_default(vault_dir: Path, scope_cwd: bool) -> None:
    """Persist the scope toggle's startup state, rewriting the whole file.

    A whole-file rewrite is fine while `settings.json` carries one key, and
    that is the documented ceiling — merge on write as soon as a second key
    appears, or saving scope would silently drop it.
    """
    vault_dir.mkdir(parents=True, exist_ok=True)
    (vault_dir / SETTINGS_FILENAME).write_text(
        json.dumps({"scope_cwd": scope_cwd}) + "\n"
    )


def same_dir(a: str, b: str) -> bool:
    """Whether two path strings name the same directory.

    Exact match, not prefix and not fuzzy: `is_relative_to` would make
    launching from `~` show everything, and the fuzzy text matcher would let a
    folder named `web` match `cms-web-saku`.

    Both sides go through `realpath` because the two ends disagree on spelling.
    The capture skill fills `cwd` from shell `pwd` — the *logical* path — while
    the TUI reads `Path.cwd()`, which resolves symlinks; on macOS `/tmp` vs
    `/private/tmp` is the everyday case. Resolving both makes "the same folder"
    match whichever way it was reached, and also drops trailing slashes.

    An empty string is never a match. `realpath("")` returns the process cwd,
    so without this guard every broken card (`cwd=""` after a parse failure)
    would match the launch dir by accident rather than by the deliberate
    exemption the caller applies.
    """
    if not a or not b:
        return False
    return os.path.realpath(a) == os.path.realpath(b)


def purge_trash(
    vault_dir: Path, days: int, *, now: float | None = None
) -> tuple[list[Path], list[Path]]:
    """Erase captures older than ``days`` from `.trash/`.

    Returns ``(removed, failed)``: what was erased, and what should have been
    but could not be stat'ed or unlinked. Failures stay put — retention is the
    safe state, and the next launch retries — but they are the caller's to
    report: trash silently outliving its promised window would break "never
    silently dropped".

    Age runs from *deletion*, not capture: `trash_session` stamps the file's
    mtime the moment it enters the trash. ``max(mtime, ctime)`` covers files
    trashed before that stamp existed — `os.link` bumped their inode ctime at
    that moment — and where the reasoning fails, max errs toward the newer
    stamp, i.e. toward keeping. (On Windows ``st_ctime`` is creation time,
    not inode-change time, so there only the mtime stamp is meaningful and
    pre-stamp trash ages from capture at worst.)

    Only ``*.md`` is considered; anything else in `.trash/` was put there by
    someone else and is not ours to erase.
    """
    cutoff = (now if now is not None else time.time()) - days * SECONDS_PER_DAY
    removed: list[Path] = []
    failed: list[Path] = []
    for path in sorted((vault_dir / ".trash").glob("*.md")):
        try:
            st = path.stat()
            if max(st.st_mtime, st.st_ctime) < cutoff:
                path.unlink()
                removed.append(path)
        except OSError:
            failed.append(path)
    return removed, failed


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


def trash_session(session: Session, vault_dir: Path) -> Path:
    """Move a session's file into the vault's `.trash/`, returning where it went.

    A move rather than an unlink: a capture is the only record of a session, and
    the resume command inside it cannot be reconstructed once the file is gone.
    `.trash` sits inside the vault so everything stays on one filesystem, and
    because `load_vault` globs a single level it never re-reads what is in there.

    Names collide as soon as the same capture is deleted twice, and overwriting
    the older copy would defeat the point of not deleting in the first place —
    so the target is claimed with `os.link`, which refuses to replace an
    existing file, and a numeric suffix is tried until a claim sticks. Once
    the claim holds the file's mtime is stamped to now — `purge_trash` ages
    trash from that stamp, and the link would otherwise carry the capture's
    old mtime along — and only then is the original removed; a crash in
    between leaves two copies, never zero.
    """
    trash = vault_dir / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    target = trash / session.path.name
    counter = 2
    while True:
        try:
            os.link(session.path, target)
        except FileExistsError:
            target = trash / f"{session.path.stem}-{counter}{session.path.suffix}"
            counter += 1
        else:
            os.utime(target)
            session.path.unlink()
            return target


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

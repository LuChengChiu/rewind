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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

COMMAND_TEMPLATES: dict[str, str] = {
    "claude-code": "claude --resume {session_id}",
    "opencode": "opencode -s {session_id}",
}

SECONDS_PER_DAY = 86400

# mode -> the one-line description both pickers show. The order is the order
# the pickers list them in, and the first is the default everywhere.
SORT_MODES: dict[str, str] = {
    "recent": "newest first",
    "oldest": "oldest first",
    "grouped": "by folder, newest folder first",
}
SORT_DEFAULT = "recent"


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


@dataclass(frozen=True)
class Settings:
    """What a fresh launch starts as — the two rows of the ⚙ dialog.

    Field names match the keys in `settings.json`; the defaults here are the
    fallbacks every missing or unusable key silently yields.
    """

    scope_cwd: bool = False
    sort: str = SORT_DEFAULT


def load_settings(vault_dir: Path) -> Settings:
    """The vault's `settings.json`, with every missing or unusable key defaulted.

    Lives in the vault dir rather than a home-wide config path because these
    settings shape *this* vault's cards, so they travel with the vault when
    `$REWIND_DIR` points elsewhere. `load_vault` globs `*.md`, so the file can
    never be mistaken for a card.

    Every failure — missing file, bad JSON, wrong shape, unreadable, a wrongly
    typed value, an unknown sort mode — yields the default for that key. These
    are preferences, not data: Rewind must open and show cards whatever state
    this file is in, and the failure direction is "show more, in the usual
    order", never "show nothing". Validation is per-key so one bad key does not
    discard a good one.
    """
    try:
        raw = json.loads((vault_dir / SETTINGS_FILENAME).read_text())
        stored = raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001 — see docstring: nothing here may block launch
        return Settings()
    scope_cwd = stored.get("scope_cwd")
    sort = stored.get("sort")
    return Settings(
        scope_cwd=scope_cwd if isinstance(scope_cwd, bool) else Settings.scope_cwd,
        sort=sort if sort in SORT_MODES else Settings.sort,
    )


def save_settings(
    vault_dir: Path,
    *,
    scope_cwd: bool | None = None,
    sort: str | None = None,
) -> None:
    """Merge the given keys into the vault's `settings.json`; None leaves a key as stored.

    Merge rather than whole-file rewrite: the file carries more than one key,
    so writing only the key being edited would silently erase the others. The
    base is `load_settings`, so a corrupt file is replaced by defaults plus the
    update instead of propagating its corruption — the same "no state of this
    file blocks anything" rule the loader follows. Named parameters, not
    `**kwargs`: a typo'd key must be a TypeError here, never junk persisted
    into the file.
    """
    stored = load_settings(vault_dir)
    settings = Settings(
        scope_cwd=stored.scope_cwd if scope_cwd is None else scope_cwd,
        sort=stored.sort if sort is None else sort,
    )
    vault_dir.mkdir(parents=True, exist_ok=True)
    (vault_dir / SETTINGS_FILENAME).write_text(json.dumps(asdict(settings)) + "\n")


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


EPOCH = datetime.fromtimestamp(0, tz=timezone.utc)


def _captured_key(session: Session) -> datetime:
    """When a session was captured, with the epoch standing in for "unknown".

    Broken cards get no special-casing: falling back to the epoch puts them at
    the bottom in `recent` (which is what they already did), at the top in
    `oldest`, and — paired with their empty cwd — in one trailing bucket in
    `grouped`. They stay visible in every mode either way (H4).
    """
    return session.captured_at or EPOCH


def _bucket_key(cwd: str) -> str:
    """The folder a card's cwd names, spelled one way.

    `realpath` for the same reason `same_dir` uses it: capture writes the
    shell's logical path while symlinks resolve elsewhere, and on macOS
    `/tmp` vs `/private/tmp` is the everyday case — two spellings of one
    folder must land in one bucket, exactly as ctrl+f treats them as one.
    And the same empty-string guard: `realpath("")` is the process cwd, so a
    broken card (cwd="" after a parse failure) must keep its own key rather
    than join whatever folder Rewind was launched from.
    """
    return os.path.realpath(cwd) if cwd else ""


def sort_sessions(sessions: list[Session], sort_mode: str) -> list[Session]:
    """Order sessions by *sort_mode*, returning a new list.

    Never re-reads the vault, on purpose: the live sort re-orders the list
    already in memory, so changing the order can never double as a surprise
    vault sync (ctrl+r stays the only way to pick up new files). `grouped`
    does let `realpath` stat paths to normalize their spelling — but it reads
    no cards.

    `grouped` buckets on exact cwd equality — the same keying as `same_dir`
    and as Claude Code's own project storage — and ranks buckets by their
    newest member, so the folder just captured in leads and drags its siblings
    up with it. Exact match, not prefix: fusing two unrelated same-named
    projects is worse than splitting one project's subdirectories, which
    recency ranking parks next to each other anyway. An unknown mode falls
    back to `recent` rather than raising — no value of a settings key may
    block the vault from opening.
    """
    if sort_mode == "oldest":
        return sorted(sessions, key=_captured_key)
    if sort_mode == "grouped":
        # Normalized once per distinct spelling, not per card: realpath stats.
        bucket_of: dict[str, str] = {}
        newest: dict[str, datetime] = {}
        for session in sessions:
            if session.cwd not in bucket_of:
                bucket_of[session.cwd] = _bucket_key(session.cwd)
            bucket = bucket_of[session.cwd]
            captured = _captured_key(session)
            if bucket not in newest or captured > newest[bucket]:
                newest[bucket] = captured
        # The bucket key sits in the sort key so buckets stay contiguous even
        # when two folders share a newest timestamp; without it, equal-ranked
        # buckets interleave.
        return sorted(
            sessions,
            key=lambda s: (
                newest[bucket_of[s.cwd]],
                bucket_of[s.cwd],
                _captured_key(s),
            ),
            reverse=True,
        )
    return sorted(sessions, key=_captured_key, reverse=True)


def load_vault(directory: Path, sort_mode: str = SORT_DEFAULT) -> list[Session]:
    sessions = [load_session(p) for p in sorted(directory.glob("*.md"))]
    return sort_sessions(sessions, sort_mode)


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

"""Auto-update, the `claude`-style "it's just current when I open it" behaviour.

Rewind is installed with `uv tool install git+…@main`, so "update" is only ever
re-running that same install. Two things shape this module:

- It runs *after* the TUI exits, not at launch. `uv tool install --force`
  rebuilds the tool's venv in place, and swapping site-packages under a live
  process breaks any import Textual has not made yet. The update is detached, so
  it outlives us and the *next* `rewind` is the new one.
- It never blocks, never prompts, and never reports failure. An offline laptop
  must still open the vault.

Silence is not the same as invisibility, though: a broken `main` and a current
install look identical from the outside, so the update leaves a record behind.
CachePaths.result and .log answer "why am I still on the old one" with a `cat`.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

DIST = "rewind"
REPO = "https://github.com/LuChengChiu/rewind"
BRANCH = "main"
SOURCE = f"git+{REPO}@{BRANCH}"
CHECK_INTERVAL = 24 * 60 * 60


@dataclass(frozen=True)
class CachePaths:
    """The three files an update writes, and where they live.

    Three, and deliberately not one. `stamp` is written before we spawn; the
    detached child writes `result` and `log` long after we have exited. One
    writer each means no race and no lost back-off if the child never runs.
    """

    stamp: Path
    result: Path
    log: Path

    @classmethod
    def under(cls, cache_home: Path) -> CachePaths:
        home = cache_home / "rewind"
        return cls(
            stamp=home / "last-update-check",
            result=home / "last-update.json",
            log=home / "last-update.log",
        )


def _default_cache_paths() -> CachePaths:
    """Resolved per call rather than at import: XDG_CACHE_HOME is the caller's."""
    cache_home = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return CachePaths.under(Path(cache_home))


def _tool_dir(uv: str) -> Path | None:
    """Where `uv tool install` puts things.

    UV_TOOL_DIR, XDG_DATA_HOME and the built-in default all feed this, so ask uv
    instead of reconstructing the precedence and getting it subtly wrong.
    """
    try:
        done = subprocess.run(
            [uv, "tool", "dir"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return Path(done.stdout.strip())


def _same_repo(url: str) -> bool:
    def norm(u: str) -> str:
        return u.rstrip("/").removesuffix(".git").lower()

    return norm(url) == norm(REPO)


def _installed_from_source() -> bool:
    """True only if what is running was installed from SOURCE itself.

    The update reinstalls SOURCE over whatever is here, so every other
    provenance has to be left alone -- an editable dev checkout, a fork, a
    pinned tag. All of them live in the same tool dir as a real install, so
    location cannot tell them apart; PEP 610 records where a distribution
    actually came from, and that can.
    """
    try:
        raw = importlib.metadata.distribution(DIST).read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return False
    if not raw:  # installed from an index, not a direct URL
        return False
    try:
        info = json.loads(raw)
    except ValueError:
        return False
    vcs = info.get("vcs_info")  # absent for a local dir, editable or not
    if not vcs or vcs.get("requested_revision") != BRANCH:
        return False
    return _same_repo(info.get("url", ""))


def _is_managed_install(uv: str) -> bool:
    """True for an install this module is allowed to replace.

    Provenance alone is not enough: `uv pip install git+…@main` into some venv
    reports the same origin, but reinstalling would build a uv *tool* the user
    never asked for. It has to be SOURCE and it has to be a tool.

    Provenance is checked first because it only reads a metadata file, while
    _tool_dir spawns uv. A dev checkout answers False here on every launch --
    it never stamps, so it never stops being due -- and that path should stay
    free.
    """
    if not _installed_from_source():
        return False
    tools = _tool_dir(uv)
    if tools is None:
        return False
    try:
        return tools.resolve() in Path(sys.prefix).resolve().parents
    except OSError:
        return False


def _due(paths: CachePaths) -> bool:
    try:
        return (time.time() - paths.stamp.stat().st_mtime) > CHECK_INTERVAL
    except OSError:
        return True


def _utc_now() -> str:
    """The same instant the child's `date -u` would print, spelled the same way."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _record_attempt(paths: CachePaths, attempted_at: str) -> None:
    """Clobber the last result before we spawn, so it can never outlive its run.

    Only the child writes an outcome, and the child can die -- OOM-killed, laptop
    shut, spawn refused. Without this the previous run's `"exit_code": 0` would
    still be sitting there, and "updated fine, an hour ago" is exactly the lie
    this file exists to prevent. An attempt with no outcome is the truth here,
    so write that down first and let the child add the ending if it gets one.
    """
    try:
        paths.result.write_text(json.dumps({"attempted_at": attempted_at}) + "\n")
    except OSError:
        pass  # same bargain as everything else here: never fail the launch


def _update_script(uv: str, paths: CachePaths, attempted_at: str) -> str:
    """The install, plus the child recording how it went.

    We detach and never reap, so the exit code dies with the child unless the
    child writes it down itself. It is the one bit we cannot reconstruct later:
    a failed update and an already-current one both leave the commit unmoved,
    and telling them apart from here would mean asking the network at launch.

    The child rewrites the whole record rather than appending to it, carrying
    `attempted_at` back through: one file that is either an attempt or an
    attempt-with-an-ending, never a half-written mix of two runs.

    `rc` is captured before the $(date) substitution, which would otherwise
    overwrite $? with its own.
    """
    install = " ".join(
        shlex.quote(a)
        for a in [uv, "tool", "install", "--force", "--quiet", SOURCE]
    )
    log, result = shlex.quote(str(paths.log)), shlex.quote(str(paths.result))
    started = shlex.quote(attempted_at)
    return (
        f"{install} 2>{log}; rc=$?; "
        f"""printf '{{"attempted_at":"%s","finished_at":"%s","exit_code":%s}}\\n' """
        f'{started} "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$rc" >{result}'
    )


def maybe_update_in_background(paths: CachePaths | None = None) -> None:
    if os.environ.get("REWIND_NO_UPDATE"):
        return
    if paths is None:
        paths = _default_cache_paths()
    # Cheap checks first: _is_managed_install shells out to uv, and on all but
    # one launch a day the answer does not matter.
    if not _due(paths):
        return
    uv = shutil.which("uv")
    if uv is None:
        return
    if not _is_managed_install(uv):
        return

    # Stamp before spawning: if the update is going to fail (offline, main
    # broken), it must fail once a day, not on every single launch.
    try:
        paths.stamp.parent.mkdir(parents=True, exist_ok=True)
        paths.stamp.touch()
    except OSError:
        return

    attempted_at = _utc_now()
    _record_attempt(paths, attempted_at)

    try:
        subprocess.Popen(
            ["/bin/sh", "-c", _update_script(uv, paths, attempted_at)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass

import importlib.metadata
import json
import os
import subprocess
import time
import types

import pytest

from session_vault import update

REPO_URL = "https://github.com/LuChengChiu/rewind"

# direct_url.json payloads, PEP 610. Only the first may ever be overwritten.
GIT_MAIN = {
    "url": REPO_URL,
    "vcs_info": {"vcs": "git", "requested_revision": "main", "commit_id": "9f8e7d6"},
}
EDITABLE = {  # what `uv tool install --editable .` really records
    "url": "file:///Users/dev/side-project/session-manager",
    "dir_info": {"editable": True},
}
LOCAL_DIR = {"url": "file:///Users/dev/side-project/session-manager", "dir_info": {}}
PINNED_TAG = {
    "url": REPO_URL,
    "vcs_info": {"vcs": "git", "requested_revision": "v0.1.0", "commit_id": "9f8e7d6"},
}
FORK_MAIN = {
    "url": "https://github.com/someoneelse/rewind",
    "vcs_info": {"vcs": "git", "requested_revision": "main", "commit_id": "9f8e7d6"},
}
BARE_GIT = {  # git+URL with no @rev: tracks the default branch, not necessarily main
    "url": REPO_URL,
    "vcs_info": {"vcs": "git", "commit_id": "9f8e7d6"},
}


class FakeDist:
    def __init__(self, state):
        self._state = state

    def read_text(self, name):
        assert name == "direct_url.json"
        payload = self._state.direct_url
        if payload is None:  # installed from an index: no direct_url.json
            return None
        if isinstance(payload, str):
            return payload
        return json.dumps(payload)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A `uv tool install git+…@main` — the one shape allowed to update itself.

    Every test starts from this and breaks exactly one thing, so what makes an
    install (un)touchable stays legible.
    """
    tools = tmp_path / "tools"
    prefix = tools / "session-vault"
    prefix.mkdir(parents=True)

    state = types.SimpleNamespace(
        tool_dir=str(tools),
        tool_dir_rc=0,
        tool_dir_raises=None,
        direct_url=GIT_MAIN,
        not_found=False,
        popen_raises=None,
        spawns=[],
        run_calls=[],
        paths=update.CachePaths.under(tmp_path / "cache"),
        prefix=prefix,
        uv="/usr/bin/uv",
    )

    def fake_run(argv, **kwargs):
        assert argv[1:] == ["tool", "dir"]
        state.run_calls.append(argv)
        if state.tool_dir_raises is not None:
            raise state.tool_dir_raises
        return types.SimpleNamespace(
            returncode=state.tool_dir_rc, stdout=state.tool_dir + "\n"
        )

    def fake_popen(argv, **kwargs):
        if state.popen_raises is not None:
            raise state.popen_raises
        state.spawns.append((argv, kwargs))
        return types.SimpleNamespace(pid=4242)

    def fake_distribution(name):
        assert name == update.DIST
        if state.not_found:
            raise importlib.metadata.PackageNotFoundError(name)
        return FakeDist(state)

    monkeypatch.setattr(
        update,
        "subprocess",
        types.SimpleNamespace(
            run=fake_run,
            Popen=fake_popen,
            DEVNULL=subprocess.DEVNULL,
            SubprocessError=subprocess.SubprocessError,
        ),
    )
    monkeypatch.setattr(
        update,
        "importlib",
        types.SimpleNamespace(
            metadata=types.SimpleNamespace(
                distribution=fake_distribution,
                PackageNotFoundError=importlib.metadata.PackageNotFoundError,
            )
        ),
    )
    monkeypatch.setattr(
        update,
        "shutil",
        types.SimpleNamespace(which=lambda name: state.uv if name == "uv" else None),
    )
    monkeypatch.setattr(update, "sys", types.SimpleNamespace(prefix=str(prefix)))
    monkeypatch.delenv("REWIND_NO_UPDATE", raising=False)
    return state


def _stamp_aged(state, seconds):
    state.paths.stamp.parent.mkdir(parents=True, exist_ok=True)
    state.paths.stamp.touch()
    old = time.time() - seconds
    os.utime(state.paths.stamp, (old, old))


# --- provenance: who is allowed to be overwritten ----------------------------


def test_only_this_repos_main_is_installed_from_source(world):
    cases = [
        (GIT_MAIN, True),
        (EDITABLE, False),
        (LOCAL_DIR, False),
        (PINNED_TAG, False),
        (FORK_MAIN, False),
        (BARE_GIT, False),
        (None, False),
    ]
    for payload, expected in cases:
        world.direct_url = payload
        assert update._installed_from_source() is expected, payload


def test_unusable_metadata_is_not_installed_from_source(world):
    world.direct_url = "{ not json"
    assert update._installed_from_source() is False

    world.not_found = True
    assert update._installed_from_source() is False


def test_same_repo_ignores_url_spelling():
    assert update._same_repo(REPO_URL)
    assert update._same_repo(REPO_URL + ".git")
    assert update._same_repo(REPO_URL.upper() + ".git/")
    assert not update._same_repo("https://github.com/someoneelse/rewind")
    assert not update._same_repo("")


# --- managed install: provenance is necessary but not sufficient --------------


def test_managed_install_is_a_tool_install_of_source(world):
    assert update._is_managed_install(world.uv) is True


def test_venv_outside_the_tool_dir_is_not_managed(world, tmp_path):
    # `uv pip install git+…@main` into a project venv: same provenance, but
    # reinstalling would build a uv tool the user never asked for.
    venv = tmp_path / "project" / ".venv"
    venv.mkdir(parents=True)
    update.sys.prefix = str(venv)
    assert update._is_managed_install(world.uv) is False


def test_unreadable_tool_dir_is_not_managed(world):
    world.tool_dir_rc = 1
    assert update._is_managed_install(world.uv) is False

    world.tool_dir_rc = 0
    world.tool_dir_raises = OSError("uv vanished")
    assert update._is_managed_install(world.uv) is False

    world.tool_dir_raises = subprocess.TimeoutExpired("uv", 10)
    assert update._is_managed_install(world.uv) is False


# --- where the files land ----------------------------------------------------


def test_cache_paths_follow_xdg_then_fall_back_home(monkeypatch, tmp_path):
    # app.py calls maybe_update_in_background() with no paths, so this default
    # is the only one that ever runs for real.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert update._default_cache_paths() == update.CachePaths.under(tmp_path / "xdg")

    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setattr(update.Path, "home", classmethod(lambda cls: tmp_path / "h"))
    assert update._default_cache_paths() == update.CachePaths.under(
        tmp_path / "h" / ".cache"
    )


def test_cache_paths_share_one_directory(tmp_path):
    paths = update.CachePaths.under(tmp_path)
    parents = {p.parent for p in (paths.stamp, paths.result, paths.log)}
    assert parents == {tmp_path / "rewind"}


# --- the once-a-day gate -----------------------------------------------------


def test_due_only_after_the_interval(world):
    assert update._due(world.paths) is True  # no stamp yet

    _stamp_aged(world, 60)
    assert update._due(world.paths) is False

    _stamp_aged(world, update.CHECK_INTERVAL + 60)
    assert update._due(world.paths) is True


# --- maybe_update_in_background ----------------------------------------------


def test_updates_a_managed_install(world):
    update.maybe_update_in_background(world.paths)

    [(argv, kwargs)] = world.spawns
    assert argv[:2] == ["/bin/sh", "-c"]
    assert update.SOURCE in argv[2]
    # detached and silent: it has to outlive us and never touch the terminal
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert world.paths.stamp.exists()


def test_a_dead_child_leaves_no_stale_success(world):
    # The gap the record exists to close. Only the child writes an outcome, and
    # a child can die without writing one -- so yesterday's success must not be
    # left sitting there reading as "updated fine, moments ago".
    world.paths.result.parent.mkdir(parents=True, exist_ok=True)
    yesterday = {
        "attempted_at": "2026-07-15T09:00:00Z",
        "finished_at": "2026-07-15T09:00:04Z",
        "exit_code": 0,
    }
    world.paths.result.write_text(json.dumps(yesterday) + "\n")

    update.maybe_update_in_background(world.paths)  # spawns; the child never runs

    assert world.spawns  # it really did try
    record = json.loads(world.paths.result.read_text())
    assert "exit_code" not in record  # nothing finished, so nothing claims to have
    assert "finished_at" not in record
    assert record["attempted_at"] != yesterday["attempted_at"]


def test_update_script_records_how_it_went(world, tmp_path):
    # The real thing, run for real against a stub uv: we detach and never reap,
    # so if the child does not write the exit code down, nobody ever knows it.
    stub = tmp_path / "uv"
    stub.write_text("#!/bin/sh\necho 'fatal: could not read from remote' >&2\nexit 128\n")
    stub.chmod(0o755)
    world.paths.result.parent.mkdir(parents=True, exist_ok=True)

    script = update._update_script(str(stub), world.paths, "2026-07-16T09:00:00Z")
    subprocess.run(["/bin/sh", "-c", script], check=True)

    record = json.loads(world.paths.result.read_text())
    assert record["exit_code"] == 128
    assert record["attempted_at"] == "2026-07-16T09:00:00Z"  # carried through
    assert "could not read from remote" in world.paths.log.read_text()


def test_update_script_reports_success_and_truncates_the_log(world, tmp_path):
    stub = tmp_path / "uv"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    world.paths.log.parent.mkdir(parents=True, exist_ok=True)
    world.paths.log.write_text("noise from a previous failed run\n")

    script = update._update_script(str(stub), world.paths, "2026-07-16T09:00:00Z")
    subprocess.run(["/bin/sh", "-c", script], check=True)

    record = json.loads(world.paths.result.read_text())
    assert record["exit_code"] == 0
    assert record["finished_at"].endswith("Z")
    # `2>` truncates, so the log stays bounded without anyone rotating it
    assert world.paths.log.read_text() == ""


def test_dev_checkout_is_never_overwritten(world):
    world.direct_url = EDITABLE
    update.maybe_update_in_background(world.paths)
    assert world.spawns == []


def test_dev_checkout_costs_nothing_to_reject(world):
    # It never stamps, so it is due on every single launch, forever. Provenance
    # is a file read and `uv tool dir` is a subprocess: check the file first.
    world.direct_url = EDITABLE
    update.maybe_update_in_background(world.paths)
    assert world.run_calls == []


def test_env_var_opts_out(world, monkeypatch):
    monkeypatch.setenv("REWIND_NO_UPDATE", "1")
    update.maybe_update_in_background(world.paths)
    assert world.spawns == []


def test_no_update_before_the_interval_is_up(world):
    _stamp_aged(world, 60)
    update.maybe_update_in_background(world.paths)
    assert world.spawns == []


def test_no_uv_means_no_update(world):
    world.uv = None
    update.maybe_update_in_background(world.paths)
    assert world.spawns == []


def test_failure_to_spawn_backs_off_for_a_day(world):
    # Offline, broken main, whatever: stamped before the spawn, so a failure
    # costs one attempt a day rather than one per launch — and never raises.
    world.popen_raises = OSError("no such file")
    update.maybe_update_in_background(world.paths)

    assert world.spawns == []
    assert world.paths.stamp.exists()
    assert update._due(world.paths) is False
    # Refused at the door, so there is no outcome to report -- only the attempt.
    assert "exit_code" not in json.loads(world.paths.result.read_text())


def test_unwritable_stamp_means_no_update(world, monkeypatch):
    # Without a stamp there is no back-off, so don't start what can't be paced.
    def no_mkdir(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(type(world.paths.stamp), "mkdir", no_mkdir)
    update.maybe_update_in_background(world.paths)
    assert world.spawns == []

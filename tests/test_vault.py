import os
import time
from datetime import datetime
from pathlib import Path

from conftest import write_capture
from rewind.vault import (
    SECONDS_PER_DAY,
    fuzzy_match,
    load_scope_default,
    load_vault,
    matches,
    purge_trash,
    relative_time,
    resolve_trash_days,
    resolve_vault_dir,
    same_dir,
    save_scope_default,
    trash_session,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_resolve_vault_dir_defaults_to_home(monkeypatch):
    monkeypatch.delenv("REWIND_DIR", raising=False)
    assert resolve_vault_dir() == Path.home() / "rewind"


def test_resolve_vault_dir_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("REWIND_DIR", str(tmp_path))
    assert resolve_vault_dir() == tmp_path


def test_resolve_vault_dir_uses_env_verbatim(monkeypatch):
    # No expanduser: the skill's raw "$VAULT" does no tilde expansion, so an
    # unexpanded value must resolve identically on the read and write sides.
    monkeypatch.setenv("REWIND_DIR", "~/some/custom/path")
    assert resolve_vault_dir() == Path("~/some/custom/path")


def test_resolve_vault_dir_treats_empty_env_as_unset(monkeypatch):
    monkeypatch.setenv("REWIND_DIR", "")
    assert resolve_vault_dir() == Path.home() / "rewind"


def test_load_vault_sorted_newest_first():
    sessions = load_vault(FIXTURES)
    assert len(sessions) == 3
    dated = [s for s in sessions if s.captured_at is not None]
    assert dated == sorted(dated, key=lambda s: s.captured_at, reverse=True)
    assert sessions[0].title == "Logger facade PRD discussion"


def test_resume_commands_rendered_from_raw_fields():
    by_harness = {s.harness: s for s in load_vault(FIXTURES) if not s.error}
    assert (
        by_harness["claude-code"].resume_command
        == "claude --resume 11111111-2222-4333-8444-555555555555"
    )
    assert (
        by_harness["opencode"].resume_command
        == "opencode -s ses_examplefixture0000000001"
    )


def test_broken_file_surfaces_as_error_card():
    sessions = load_vault(FIXTURES)
    broken = [s for s in sessions if s.error]
    assert len(broken) == 1
    assert "session_id" in broken[0].error
    assert broken[0].resume_command is None


def test_unknown_harness_is_an_error(tmp_path):
    (tmp_path / "x.md").write_text(
        "---\nharness: cursor\nsession_id: abc\ncwd: /tmp\n"
        "title: t\ncaptured_at: 2026-07-01T00:00:00+08:00\n---\nbody\n"
    )
    [session] = load_vault(tmp_path)
    assert session.error is not None
    assert session.resume_command is None


def test_trash_session_moves_the_file(tmp_path):
    (tmp_path / "x.md").write_text(
        "---\nharness: claude-code\nsession_id: abc\ncwd: /tmp\n"
        "title: t\ncaptured_at: 2026-07-01T00:00:00+08:00\n---\nbody\n"
    )
    [session] = load_vault(tmp_path)
    target = trash_session(session, tmp_path)

    assert not session.path.exists()
    assert target == tmp_path / ".trash" / "x.md"
    assert "body" in target.read_text()
    # A single-level glob, so the vault no longer sees it.
    assert load_vault(tmp_path) == []


def test_trash_session_keeps_an_earlier_copy_of_the_same_name(tmp_path):
    # Same filename deleted twice: overwriting would erase the first capture,
    # which is exactly what moving instead of unlinking is meant to prevent.
    trashed = []
    for body in ("first", "second"):
        (tmp_path / "x.md").write_text(
            "---\nharness: claude-code\nsession_id: abc\ncwd: /tmp\n"
            f"title: t\ncaptured_at: 2026-07-01T00:00:00+08:00\n---\n{body}\n"
        )
        [session] = load_vault(tmp_path)
        trashed.append(trash_session(session, tmp_path))

    assert trashed[0].name == "x.md"
    assert trashed[1].name == "x-2.md"
    assert "first" in trashed[0].read_text()
    assert "second" in trashed[1].read_text()


def test_resolve_trash_days_defaults_to_14(monkeypatch):
    monkeypatch.delenv("REWIND_TRASH_DAYS", raising=False)
    assert resolve_trash_days() == 14


def test_resolve_trash_days_honors_env(monkeypatch):
    monkeypatch.setenv("REWIND_TRASH_DAYS", "30")
    assert resolve_trash_days() == 30


def test_resolve_trash_days_treats_empty_env_as_unset(monkeypatch):
    monkeypatch.setenv("REWIND_TRASH_DAYS", "")
    assert resolve_trash_days() == 14


def test_resolve_trash_days_fails_toward_keeping(monkeypatch):
    # Purging is destructive, so every unusable value must mean "never purge",
    # not "purge on the default schedule". 0 doubles as the off switch.
    for raw in ("0", "-3", "14d", "two weeks"):
        monkeypatch.setenv("REWIND_TRASH_DAYS", raw)
        assert resolve_trash_days() is None, raw


def test_purge_trash_erases_only_expired_captures(tmp_path):
    write_capture(tmp_path)
    [session] = load_vault(tmp_path)
    target = trash_session(session, tmp_path)

    # Freshly trashed: survives a purge today…
    assert purge_trash(tmp_path, 14) == ([], [])
    assert target.exists()
    # …but not one run 15 days from now.
    assert purge_trash(tmp_path, 14, now=time.time() + 15 * SECONDS_PER_DAY) == (
        [target],
        [],
    )
    assert not target.exists()


def test_trash_session_stamps_deletion_time(tmp_path):
    # The link would carry the capture's old mtime into .trash/; trash_session
    # re-stamps it so purge_trash ages from deletion on every platform.
    write_capture(tmp_path)
    stale = time.time() - 100 * SECONDS_PER_DAY
    os.utime(tmp_path / "x.md", (stale, stale))
    [session] = load_vault(tmp_path)

    target = trash_session(session, tmp_path)

    assert abs(target.stat().st_mtime - time.time()) < 60


def test_purge_trash_ages_by_deletion_time_not_capture_time(tmp_path):
    # Files trashed before trash_session stamped mtime have the capture's old
    # mtime but a ctime bumped at deletion — a months-old capture trashed just
    # now must survive a purge today. utime back-dates mtime while itself
    # refreshing ctime, which reproduces that legacy shape.
    trash = tmp_path / ".trash"
    trash.mkdir()
    write_capture(trash, "old.md")
    stale = time.time() - 100 * SECONDS_PER_DAY
    os.utime(trash / "old.md", (stale, stale))

    assert purge_trash(tmp_path, 14) == ([], [])
    assert (trash / "old.md").exists()


def test_purge_trash_leaves_non_captures_alone(tmp_path):
    trash = tmp_path / ".trash"
    trash.mkdir()
    (trash / "notes.txt").write_text("not a capture")
    write_capture(trash)

    removed, failed = purge_trash(tmp_path, 14, now=time.time() + 100 * SECONDS_PER_DAY)

    assert removed == [trash / "x.md"]
    assert failed == []
    assert (trash / "notes.txt").exists()


def test_purge_trash_reports_what_it_could_not_erase(tmp_path):
    # A read-only .trash/ denies the unlink; the failure must come back to the
    # caller instead of vanishing — trash outliving its window is not silent.
    trash = tmp_path / ".trash"
    trash.mkdir()
    write_capture(trash)
    os.chmod(trash, 0o500)
    try:
        removed, failed = purge_trash(
            tmp_path, 14, now=time.time() + 100 * SECONDS_PER_DAY
        )
    finally:
        os.chmod(trash, 0o700)

    assert removed == []
    assert failed == [trash / "x.md"]
    assert (trash / "x.md").exists()


def test_purge_trash_without_a_trash_dir(tmp_path):
    assert purge_trash(tmp_path, 14) == ([], [])


def test_fuzzy_filter():
    sessions = load_vault(FIXTURES)
    logger = next(s for s in sessions if "Logger" in s.title)
    assert fuzzy_match("lgfcd", "logger facade")
    assert matches("logger prd", logger)
    assert matches("pymnts", logger)
    assert not matches("opencode", logger)
    assert matches("", logger)


def test_relative_time():
    assert relative_time(None) == "unknown time"
    assert relative_time(datetime.now().astimezone()) == "just now"


def test_same_dir_matches_a_symlinked_spelling(tmp_path):
    # The capture skill writes `pwd` (logical), the TUI reads Path.cwd()
    # (resolved). On macOS that is /tmp vs /private/tmp every day, so the two
    # spellings of one folder have to compare equal.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert same_dir(str(link), str(real))


def test_same_dir_ignores_trailing_slashes(tmp_path):
    assert same_dir(f"{tmp_path}/", str(tmp_path))


def test_same_dir_is_exact_not_prefix(tmp_path):
    # Prefix matching would make launching from a parent show everything below
    # it, which defeats the point of narrowing.
    child = tmp_path / "child"
    child.mkdir()
    assert not same_dir(str(child), str(tmp_path))


def test_same_dir_never_matches_an_empty_path(tmp_path):
    # A broken card carries cwd="" and realpath("") is the process cwd, so
    # without the guard broken cards would match by accident.
    assert not same_dir("", str(tmp_path))
    assert not same_dir(str(tmp_path), "")


def test_scope_default_round_trips(tmp_path):
    save_scope_default(tmp_path, True)
    assert load_scope_default(tmp_path) is True
    save_scope_default(tmp_path, False)
    assert load_scope_default(tmp_path) is False


def test_scope_default_is_off_when_settings_are_missing(tmp_path):
    assert load_scope_default(tmp_path) is False


def test_scope_default_is_off_when_settings_are_corrupt(tmp_path):
    # A preference must never be able to stop Rewind opening, and the failure
    # direction is "show more" — never "show nothing".
    (tmp_path / "settings.json").write_text("{not json at all")
    assert load_scope_default(tmp_path) is False


def test_scope_default_is_off_when_settings_have_the_wrong_shape(tmp_path):
    (tmp_path / "settings.json").write_text('["scope_cwd"]')
    assert load_scope_default(tmp_path) is False


def test_settings_file_is_not_loaded_as_a_card(tmp_path):
    # load_vault globs *.md, so settings.json can never be mistaken for a
    # capture — which is why it is allowed to live in the vault dir at all.
    save_scope_default(tmp_path, True)
    write_capture(tmp_path, "a.md")
    sessions = load_vault(tmp_path)
    assert len(sessions) == 1
    assert sessions[0].error is None

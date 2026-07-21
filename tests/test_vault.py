import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import write_capture
from rewind.vault import (
    SECONDS_PER_DAY,
    Session,
    Settings,
    fuzzy_match,
    load_settings,
    load_vault,
    matches,
    purge_trash,
    relative_time,
    resolve_trash_days,
    resolve_vault_dir,
    same_dir,
    save_settings,
    sort_sessions,
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


def test_settings_round_trip(tmp_path):
    save_settings(tmp_path, scope_cwd=True, sort="oldest")
    assert load_settings(tmp_path) == Settings(scope_cwd=True, sort="oldest")
    save_settings(tmp_path, scope_cwd=False, sort="recent")
    assert load_settings(tmp_path) == Settings(scope_cwd=False, sort="recent")


def test_saving_one_setting_preserves_the_other(tmp_path):
    # The whole reason the single-key writer had to go: writing scope must not
    # silently erase the sort default, or vice versa.
    save_settings(tmp_path, sort="grouped")
    save_settings(tmp_path, scope_cwd=True)
    assert load_settings(tmp_path) == Settings(scope_cwd=True, sort="grouped")

    save_settings(tmp_path, sort="oldest")
    assert load_settings(tmp_path).scope_cwd is True


def test_settings_default_when_the_file_is_missing(tmp_path):
    assert load_settings(tmp_path) == Settings(scope_cwd=False, sort="recent")


@pytest.mark.parametrize(
    "content",
    [
        "{not json at all",
        '["scope_cwd"]',
        "null",
        '{"scope_cwd": "yes", "sort": "sideways"}',
        '{"sort": 7}',
    ],
)
def test_every_corruption_shape_yields_defaults(tmp_path, content):
    # A preference must never be able to stop Rewind opening, and the failure
    # direction is "show more, in the usual order" — never "show nothing".
    # That includes wrongly typed values: "yes" is not a scope default, it is
    # a malformed one, and malformed yields the default — not a coercion.
    (tmp_path / "settings.json").write_text(content)
    assert load_settings(tmp_path) == Settings(scope_cwd=False, sort="recent")


def test_one_bad_key_does_not_discard_a_good_one(tmp_path):
    (tmp_path / "settings.json").write_text('{"scope_cwd": true, "sort": "sideways"}')
    assert load_settings(tmp_path) == Settings(scope_cwd=True, sort="recent")


def test_saving_over_a_corrupt_file_replaces_it(tmp_path):
    (tmp_path / "settings.json").write_text("{broken")
    save_settings(tmp_path, sort="oldest")
    assert load_settings(tmp_path) == Settings(scope_cwd=False, sort="oldest")


def test_settings_file_is_not_loaded_as_a_card(tmp_path):
    # load_vault globs *.md, so settings.json can never be mistaken for a
    # capture — which is why it is allowed to live in the vault dir at all.
    save_settings(tmp_path, scope_cwd=True)
    write_capture(tmp_path, "a.md")
    sessions = load_vault(tmp_path)
    assert len(sessions) == 1
    assert sessions[0].error is None


BASE = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def _session(name: str, *, hours: float | None, cwd: str = "/proj/a") -> Session:
    """A session identified by its title, captured *hours* after BASE.

    hours=None fabricates a broken card: no timestamp, no cwd, exactly the
    fallbacks `load_session` leaves behind after a parse failure.
    """
    if hours is None:
        return Session(path=Path(f"{name}.md"), title=name, error="broken")
    return Session(
        path=Path(f"{name}.md"),
        title=name,
        cwd=cwd,
        captured_at=BASE + timedelta(hours=hours),
    )


def _titles(sessions: list[Session]) -> list[str]:
    return [s.title for s in sessions]


def test_recent_sorts_newest_first():
    sessions = [_session("old", hours=0), _session("new", hours=5)]
    assert _titles(sort_sessions(sessions, "recent")) == ["new", "old"]


def test_oldest_sorts_oldest_first():
    sessions = [_session("new", hours=5), _session("old", hours=0)]
    assert _titles(sort_sessions(sessions, "oldest")) == ["old", "new"]


def test_an_unknown_mode_falls_back_to_recent():
    # No value of a settings key may block the vault from opening.
    sessions = [_session("old", hours=0), _session("new", hours=5)]
    assert _titles(sort_sessions(sessions, "sideways")) == ["new", "old"]


def test_grouped_pulls_a_folders_cards_together():
    # Strictly by timestamp these would interleave a/b/a/b; grouped must not.
    sessions = [
        _session("a1", hours=4, cwd="/proj/a"),
        _session("b1", hours=3, cwd="/proj/b"),
        _session("a2", hours=2, cwd="/proj/a"),
        _session("b2", hours=1, cwd="/proj/b"),
    ]
    assert _titles(sort_sessions(sessions, "grouped")) == ["a1", "a2", "b1", "b2"]


def test_grouped_ranks_buckets_by_their_newest_member():
    # /proj/b holds the single newest card, so its whole bucket leads even
    # though /proj/a has more cards and an older-but-larger cluster.
    sessions = [
        _session("a1", hours=5, cwd="/proj/a"),
        _session("a2", hours=4, cwd="/proj/a"),
        _session("b1", hours=9, cwd="/proj/b"),
        _session("b2", hours=1, cwd="/proj/b"),
    ]
    assert _titles(sort_sessions(sessions, "grouped")) == ["b1", "b2", "a1", "a2"]


def test_grouped_keys_on_exact_cwd_not_prefix():
    # A subdirectory is its own bucket: a wrong merge is worse than a wrong
    # split, and recency ranking parks the two buckets adjacently anyway.
    sessions = [
        _session("parent", hours=2, cwd="/proj/a"),
        _session("child", hours=1, cwd="/proj/a/sub"),
    ]
    ordered = sort_sessions(sessions, "grouped")
    assert _titles(ordered) == ["parent", "child"]


def test_grouped_merges_two_spellings_of_one_folder(tmp_path):
    # Same folder, reached two ways: capture writes the shell's logical path,
    # so a symlinked spelling (macOS's /tmp vs /private/tmp) must land in the
    # bucket ctrl+f already treats as the same place — the `same_dir` keying.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    sessions = [
        _session("via-real", hours=3, cwd=str(real)),
        _session("elsewhere", hours=2, cwd=str(tmp_path / "other")),
        _session("via-link", hours=1, cwd=str(link)),
    ]
    # One bucket: via-link rides up with via-real instead of trailing
    # elsewhere in a bucket of its own.
    assert _titles(sort_sessions(sessions, "grouped")) == [
        "via-real",
        "via-link",
        "elsewhere",
    ]


def test_broken_cards_land_on_their_fallbacks_in_every_mode():
    sessions = [
        _session("good-old", hours=0),
        _session("good-new", hours=5),
        _session("broken", hours=None),
    ]
    # recent: bottom (unchanged from today). oldest: top. grouped: its own
    # trailing bucket, since cwd="" and the epoch rank it last.
    assert _titles(sort_sessions(sessions, "recent"))[-1] == "broken"
    assert _titles(sort_sessions(sessions, "oldest"))[0] == "broken"
    assert _titles(sort_sessions(sessions, "grouped"))[-1] == "broken"


def test_sorting_never_drops_or_duplicates_a_card():
    # H4 in the sort layer: every mode is a permutation, nothing else.
    sessions = [
        _session("a1", hours=4, cwd="/proj/a"),
        _session("b1", hours=3, cwd="/proj/b"),
        _session("broken", hours=None),
    ]
    for mode in ("recent", "oldest", "grouped", "nonsense"):
        assert sorted(_titles(sort_sessions(sessions, mode))) == [
            "a1",
            "b1",
            "broken",
        ]


def test_load_vault_applies_the_requested_sort(tmp_path):
    write_capture(tmp_path, "a.md", "first")
    write_capture(tmp_path, "b.md", "second")
    # Same captured_at in both, so this asserts the mode is threaded through
    # at all rather than any particular tie-break.
    assert len(load_vault(tmp_path, "grouped")) == 2
    assert len(load_vault(tmp_path, "oldest")) == 2

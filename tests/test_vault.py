from datetime import datetime
from pathlib import Path

from rewind.vault import (
    fuzzy_match,
    load_vault,
    matches,
    relative_time,
    resolve_vault_dir,
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

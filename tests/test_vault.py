from datetime import datetime
from pathlib import Path

from session_vault.vault import (
    fuzzy_match,
    load_vault,
    matches,
    relative_time,
)

FIXTURES = Path(__file__).parent / "fixtures"


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

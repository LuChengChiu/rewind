import json
from pathlib import Path

import pytest

from session_vault.transcript import (
    TranscriptError,
    read_transcript,
    supports_preview,
)

SESSION_ID = "11111111-2222-3333-4444-555555555555"
CWD = "/Users/someone/code/widget"


def _write(tmp_path: Path, records: list[dict]) -> None:
    project = tmp_path / ".claude" / "projects" / CWD.replace("/", "-")
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{SESSION_ID}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records)
    )


def _msg(uuid: str, parent: str | None, type_: str, text: str, minute: int, **extra):
    return {
        "uuid": uuid,
        "parentUuid": parent,
        "type": type_,
        "timestamp": f"2026-07-16T10:{minute:02d}:00.000Z",
        "message": {"role": type_, "content": text},
        **extra,
    }


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def test_reads_conversation_in_order(tmp_path):
    _write(tmp_path, [
        _msg("a", None, "user", "first question", 0),
        _msg("b", "a", "assistant", "first answer", 1),
        _msg("c", "b", "user", "second question", 2),
    ])
    messages = read_transcript("claude-code", SESSION_ID, CWD)
    assert [(m.role, m.text) for m in messages] == [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "second question"),
    ]


def test_chain_walks_through_non_message_records(tmp_path):
    """The parentUuid chain threads through attachment/system records.

    Claude Code writes these between messages, so a uuid map holding only
    user/assistant records dead-ends at the first one and returns a truncated
    transcript that looks complete. Regression guard for exactly that.
    """
    _write(tmp_path, [
        _msg("a", None, "user", "question", 0),
        {"uuid": "att", "parentUuid": "a", "type": "attachment", "timestamp":
         "2026-07-16T10:01:00.000Z"},
        _msg("b", "att", "assistant", "answer", 2),
    ])
    messages = read_transcript("claude-code", SESSION_ID, CWD)
    assert [m.text for m in messages] == ["question", "answer"]


def test_same_role_runs_are_coalesced(tmp_path):
    _write(tmp_path, [
        _msg("a", None, "user", "do it", 0),
        _msg("b", "a", "assistant", "sure", 1),
        _msg("c", "b", "assistant", "done", 2),
    ])
    messages = read_transcript("claude-code", SESSION_ID, CWD)
    assert len(messages) == 2
    assert messages[1].text == "sure\n\ndone"


def test_latest_leaf_wins_over_abandoned_branch(tmp_path):
    _write(tmp_path, [
        _msg("a", None, "user", "question", 0),
        _msg("old", "a", "assistant", "abandoned", 1),
        _msg("new", "a", "assistant", "kept", 5),
    ])
    messages = read_transcript("claude-code", SESSION_ID, CWD)
    assert [m.text for m in messages] == ["question", "kept"]


def test_sidechain_records_are_skipped(tmp_path):
    _write(tmp_path, [
        _msg("a", None, "user", "question", 0),
        _msg("s", "a", "assistant", "subagent chatter", 1, isSidechain=True),
        _msg("b", "s", "assistant", "real answer", 2),
    ])
    messages = read_transcript("claude-code", SESSION_ID, CWD)
    assert [m.text for m in messages] == ["question", "real answer"]


def test_tool_use_blocks_render_as_tool_lines(tmp_path):
    _write(tmp_path, [
        _msg("a", None, "user", "run it", 0),
        {
            "uuid": "b", "parentUuid": "a", "type": "assistant",
            "timestamp": "2026-07-16T10:01:00.000Z",
            "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hidden from preview"},
                {"type": "text", "text": "on it"},
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "git status"}},
            ]},
        },
    ])
    messages = read_transcript("claude-code", SESSION_ID, CWD)
    assert messages[1].text == "on it\n\n⏺ Bash(git status)"
    assert "hidden from preview" not in messages[1].text


def test_tool_summary_shows_key_argument(tmp_path):
    _write(tmp_path, [
        _msg("a", None, "user", "go", 0),
        {"uuid": "b", "parentUuid": "a", "type": "assistant",
         "timestamp": "2026-07-16T10:01:00.000Z",
         "message": {"role": "assistant", "content": [
             # path fields show the basename; command collapses to one line
             {"type": "tool_use", "name": "Write",
              "input": {"file_path": "/a/b/session_vault/app.py", "content": "x"}},
             {"type": "tool_use", "name": "Bash",
              "input": {"command": "git add -A\n  && git commit"}},
             {"type": "tool_use", "name": "AskUserQuestion",
              "input": {"questions": []}},  # no summary key -> bare name
         ]}},
    ])
    messages = read_transcript("claude-code", SESSION_ID, CWD)
    assert messages[1].text == (
        "⏺ Write(app.py)\n\n⏺ Bash(git add -A && git commit)\n\n⏺ AskUserQuestion"
    )


def test_tool_summary_truncates_only_past_60_chars(tmp_path):
    _write(tmp_path, [
        _msg("a", None, "user", "go", 0),
        {"uuid": "b", "parentUuid": "a", "type": "assistant",
         "timestamp": "2026-07-16T10:01:00.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Bash",
              "input": {"command": "x" * 60}},  # at the limit: shown whole
             {"type": "tool_use", "name": "Bash",
              "input": {"command": "y" * 61}},  # one past: 59 chars + ellipsis
         ]}},
    ])
    messages = read_transcript("claude-code", SESSION_ID, CWD)
    assert messages[1].text == (
        "⏺ Bash(" + "x" * 60 + ")\n\n⏺ Bash(" + "y" * 59 + "…)"
    )


def test_empty_summary_falls_back_to_bare_name(tmp_path):
    _write(tmp_path, [
        _msg("a", None, "user", "go", 0),
        {"uuid": "b", "parentUuid": "a", "type": "assistant",
         "timestamp": "2026-07-16T10:01:00.000Z",
         "message": {"role": "assistant", "content": [
             # a value that summarizes to "" must not render as "⏺ Name()"
             {"type": "tool_use", "name": "Bash",
              "input": {"command": "  \n  "}},  # whitespace-only
             {"type": "tool_use", "name": "Read",
              "input": {"file_path": "/a/b/"}},  # path ending in "/"
         ]}},
    ])
    messages = read_transcript("claude-code", SESSION_ID, CWD)
    assert messages[1].text == "⏺ Bash\n\n⏺ Read"


def test_malformed_tool_input_falls_back_to_bare_name(tmp_path):
    _write(tmp_path, [
        _msg("a", None, "user", "go", 0),
        {"uuid": "b", "parentUuid": "a", "type": "assistant",
         "timestamp": "2026-07-16T10:01:00.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Bash", "input": "not a dict"},
             {"type": "tool_use", "name": "Read"},  # input missing entirely
             {"type": "tool_use", "name": "Edit",
              "input": {"file_path": 42}},  # summary key holds a non-string
         ]}},
    ])
    messages = read_transcript("claude-code", SESSION_ID, CWD)
    assert messages[1].text == "⏺ Bash\n\n⏺ Read\n\n⏺ Edit"


def test_cycle_does_not_hang(tmp_path):
    _write(tmp_path, [
        _msg("a", "b", "user", "one", 0),
        _msg("b", "a", "assistant", "two", 1),
    ])
    messages = read_transcript("claude-code", SESSION_ID, CWD)
    assert len(messages) <= 2


def test_missing_file_fails_loudly():
    with pytest.raises(TranscriptError, match="No transcript file"):
        read_transcript("claude-code", SESSION_ID, CWD)


def test_bad_json_fails_loudly(tmp_path):
    project = tmp_path / ".claude" / "projects" / CWD.replace("/", "-")
    project.mkdir(parents=True)
    (project / f"{SESSION_ID}.jsonl").write_text('{"uuid": "a"}\nnot json at all\n')
    with pytest.raises(TranscriptError, match="Unparseable"):
        read_transcript("claude-code", SESSION_ID, CWD)


def test_unknown_harness_has_no_preview():
    assert supports_preview("claude-code")
    assert not supports_preview("opencode")
    with pytest.raises(TranscriptError, match="No transcript reader"):
        read_transcript("opencode", SESSION_ID, CWD)


def test_slash_command_plumbing_is_not_a_turn(tmp_path):
    # A /model invocation is harness machinery, not something the user said.
    _write(tmp_path, [
        _msg("a", None, "user",
             "<local-command-caveat>Caveat: ...</local-command-caveat>\n"
             "<command-name>/model</command-name>\n"
             "<command-message>model</command-message>\n"
             "<command-args></command-args>", 0),
        _msg("b", "a", "user", "now the real question", 1),
        _msg("c", "b", "assistant", "the answer", 2),
    ])
    messages = read_transcript("claude-code", SESSION_ID, CWD)
    assert [m.text for m in messages] == ["now the real question", "the answer"]


def test_command_stdout_and_ansi_are_stripped(tmp_path):
    _write(tmp_path, [
        _msg("a", None, "user",
             "<local-command-stdout>Set model to \x1b[1mFable 5\x1b[22m"
             "</local-command-stdout>", 0),
        _msg("b", "a", "user", "real prompt", 1),
    ])
    messages = read_transcript("claude-code", SESSION_ID, CWD)
    assert [m.text for m in messages] == ["real prompt"]


def test_user_prose_keeps_its_own_angle_bracket_tags(tmp_path):
    # Real prompts in this vault contain <vault> and <slug>; only known harness
    # wrappers may be stripped, never tags in general.
    _write(tmp_path, [
        _msg("a", None, "user", "write to <vault>/<slug>.md please", 0),
    ])
    messages = read_transcript("claude-code", SESSION_ID, CWD)
    assert messages[0].text == "write to <vault>/<slug>.md please"

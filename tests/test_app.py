import json
from pathlib import Path

import pytest
from textual.widgets import Input, Label

from session_vault.app import PreviewScreen, SessionCard, VaultApp, reflow

FIXTURES = Path(__file__).parent / "fixtures"


def _install_transcript(home: Path, session_id: str, cwd: str, records: list[dict]) -> None:
    project = home / ".claude" / "projects" / cwd.replace("/", "-")
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records)
    )


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _claude_card(app: VaultApp) -> SessionCard:
    return next(
        c for c in app.query(SessionCard)
        if not c.session.error and c.session.harness == "claude-code"
    )


@pytest.mark.asyncio
async def test_cards_render_and_filter():
    app = VaultApp(vault_dir=FIXTURES)
    async with app.run_test() as pilot:
        cards = app.query(SessionCard)
        assert len(cards) == 3

        app.query_one("#filter", Input).value = "logger"
        await pilot.pause()
        visible = [c for c in app.query(SessionCard) if c.display]
        # the broken card stays visible (H4), plus the matching one
        assert {c.session.error is None for c in visible} == {True, False}
        assert sum(1 for c in visible if not c.session.error) == 1


@pytest.mark.asyncio
async def test_click_copies_resume_command():
    app = VaultApp(vault_dir=FIXTURES)
    copied: list[str] = []
    app.copy_to_clipboard = copied.append
    async with app.run_test() as pilot:
        good = next(c for c in app.query(SessionCard) if not c.session.error)
        good.copy_command()
        await pilot.pause()
        assert copied == [good.session.resume_command]
        assert good.has_class("copied")


@pytest.mark.asyncio
async def test_broken_card_click_never_copies():
    app = VaultApp(vault_dir=FIXTURES)
    copied: list[str] = []
    app.copy_to_clipboard = copied.append
    async with app.run_test() as pilot:
        broken = next(c for c in app.query(SessionCard) if c.session.error)
        broken.copy_command()
        await pilot.pause()
        assert copied == []


@pytest.mark.parametrize(
    "authored, expected",
    [
        # CJK runs close up across the authored break
        ("一行\n第二行", "一行第二行"),
        # ASCII keeps the space the break stood for
        ("English one\ntwo", "English one two"),
        # no space after full-width punctuation
        ("格式、\nOSC 52", "格式、OSC 52"),
        # but CJK against Latin keeps one
        ("已 symlink 進\n~/.claude", "已 symlink 進 ~/.claude"),
        # blank lines are the author's paragraph breaks and survive
        ("a\n\nb\nc", "a\n\nb c"),
    ],
)
def test_reflow(authored, expected):
    assert reflow(authored) == expected


@pytest.mark.asyncio
async def test_card_width_is_capped_and_responsive():
    for term_width, expected in [(50, 43), (200, 80)]:
        app = VaultApp(vault_dir=FIXTURES)
        async with app.run_test(size=(term_width, 40)) as pilot:
            await pilot.pause()
            card = next(iter(app.query(SessionCard)))
            assert card.outer_size.width == expected


@pytest.mark.asyncio
async def test_space_opens_preview_on_claude_code_card(fake_home):
    app = VaultApp(vault_dir=FIXTURES)
    async with app.run_test() as pilot:
        card = _claude_card(app)
        _install_transcript(
            fake_home, card.session.session_id, card.session.cwd,
            [
                {"uuid": "a", "parentUuid": None, "type": "user",
                 "timestamp": "2026-07-15T10:00:00.000Z",
                 "message": {"role": "user", "content": "wrap the logger"}},
                {"uuid": "b", "parentUuid": "a", "type": "assistant",
                 "timestamp": "2026-07-15T10:01:00.000Z",
                 "message": {"role": "assistant", "content": "here is the facade"}},
            ],
        )
        card.focus()
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()

        assert isinstance(app.screen, PreviewScreen)
        shown = " ".join(str(label.content) for label in app.screen.query(Label))
        assert "wrap the logger" in shown
        assert "here is the facade" in shown

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, PreviewScreen)


@pytest.mark.asyncio
async def test_space_does_nothing_on_opencode_card(fake_home):
    # No reader for opencode: the card simply has no preview (not an error).
    app = VaultApp(vault_dir=FIXTURES)
    async with app.run_test() as pilot:
        card = next(
            c for c in app.query(SessionCard)
            if not c.session.error and c.session.harness == "opencode"
        )
        card.focus()
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        assert not isinstance(app.screen, PreviewScreen)


@pytest.mark.asyncio
async def test_missing_transcript_shows_error_not_empty_dialog(fake_home):
    # H4: nothing installed under fake_home, so the read must fail loudly.
    app = VaultApp(vault_dir=FIXTURES)
    async with app.run_test() as pilot:
        card = _claude_card(app)
        card.focus()
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        shown = " ".join(str(label.content) for label in app.screen.query(Label))
        assert "No transcript file" in shown


@pytest.mark.asyncio
async def test_preview_hint_only_on_previewable_cards():
    app = VaultApp(vault_dir=FIXTURES)
    async with app.run_test():
        for card in app.query(SessionCard):
            hints = card.query(".card-hint")
            expected = 1 if card.session.harness == "claude-code" and not card.session.error else 0
            assert len(hints) == expected, card.session.path.name

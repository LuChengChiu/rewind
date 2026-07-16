from pathlib import Path

import pytest
from textual.widgets import Input

from session_vault.app import SessionCard, VaultApp, reflow

FIXTURES = Path(__file__).parent / "fixtures"


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

from pathlib import Path

import pytest
from textual.widgets import Input

from session_vault.app import SessionCard, VaultApp

FIXTURES = Path(__file__).parent.parent / "fixtures"


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

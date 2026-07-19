import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Input, Label, Static

from conftest import write_capture
from rewind.app import ConfirmDeleteScreen, PreviewScreen, SessionCard, VaultApp, reflow
from rewind.vault import SECONDS_PER_DAY

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


@pytest.fixture
def live_vault(tmp_path):
    """A vault dir seeded from the fixtures that a test can write into."""
    for src in FIXTURES.glob("*.md"):
        (tmp_path / src.name).write_text(src.read_text())
    return tmp_path


@pytest.mark.asyncio
async def test_ctrl_r_picks_up_a_session_captured_while_running(live_vault):
    app = VaultApp(vault_dir=live_vault)
    async with app.run_test() as pilot:
        assert len(app.query(SessionCard)) == 3

        write_capture(live_vault, "2026-07-19-late-arrival.md", "late arrival")
        await pilot.press("ctrl+r")
        await pilot.pause()

        titles = [c.session.title for c in app.query(SessionCard)]
        assert "late arrival" in titles
        assert len(titles) == 4


@pytest.mark.asyncio
async def test_reload_keeps_the_active_filter(live_vault):
    # Rebuilding the grid remounts every card with display defaulting to True,
    # so the filter has to be re-applied or a reload silently clears it.
    app = VaultApp(vault_dir=live_vault)
    async with app.run_test() as pilot:
        app.query_one("#filter", Input).value = "logger"
        await pilot.pause()

        write_capture(live_vault, "2026-07-19-late-arrival.md", "late arrival")
        await pilot.press("ctrl+r")
        await pilot.pause()

        visible = [c for c in app.query(SessionCard) if c.display and not c.session.error]
        assert [c.session.title for c in visible] == ["Logger facade PRD discussion"]


@pytest.mark.asyncio
async def test_empty_vault_says_how_to_reload(tmp_path):
    # The empty state sends the reader off to capture a session, so it is the
    # one screen that must also say how to come back.
    app = VaultApp(vault_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "ctrl+r" in str(app.query_one("#empty", Static).content)


@pytest.mark.asyncio
async def test_reload_from_empty_vault_replaces_the_empty_state(tmp_path):
    app = VaultApp(vault_dir=tmp_path)
    async with app.run_test() as pilot:
        assert len(app.query(SessionCard)) == 0

        write_capture(tmp_path, "2026-07-19-first.md", "first one")
        await pilot.press("ctrl+r")
        await pilot.pause()

        assert len(app.query(SessionCard)) == 1
        assert len(app.query("#empty")) == 0


@pytest.mark.asyncio
async def test_reload_drops_a_session_deleted_from_the_vault(live_vault):
    app = VaultApp(vault_dir=live_vault)
    async with app.run_test() as pilot:
        gone = next(c for c in app.query(SessionCard) if not c.session.error).session
        gone.path.unlink()
        await pilot.press("ctrl+r")
        await pilot.pause()

        assert gone.title not in [c.session.title for c in app.query(SessionCard)]


@pytest.mark.asyncio
async def test_reload_of_an_emptied_vault_restores_the_empty_state(live_vault):
    app = VaultApp(vault_dir=live_vault)
    async with app.run_test() as pilot:
        for path in live_vault.glob("*.md"):
            path.unlink()
        await pilot.press("ctrl+r")
        await pilot.pause()

        assert len(app.query(SessionCard)) == 0
        assert len(app.query("#empty")) == 1


@pytest.mark.asyncio
async def test_reload_returns_focus_to_the_filter(live_vault):
    # Rebuilding removes the focused card, and Textual parks focus on the
    # scroll container — which is focusable but swallows typing, so the filter
    # looks dead until clicked.
    app = VaultApp(vault_dir=live_vault)
    async with app.run_test() as pilot:
        # Also covers launch, which goes through the same reload path.
        assert app.focused is app.query_one("#filter", Input)

        next(c for c in app.query(SessionCard) if not c.session.error).focus()
        await pilot.pause()

        await pilot.press("ctrl+r")
        await pilot.pause()

        assert app.focused is app.query_one("#filter", Input)


@pytest.mark.asyncio
async def test_reload_under_a_preview_survives_dismissing_it(live_vault):
    # ctrl+r is app-level, so it fires with the modal up. The rebuild must not
    # disturb the modal's own focus, and — because the reload destroyed the card
    # that was focused behind it — dismissing has to land on the filter rather
    # than the dead scroll container.
    app = VaultApp(vault_dir=live_vault)
    async with app.run_test() as pilot:
        card = next(c for c in app.query(SessionCard) if not c.session.error)
        card.focus()
        await pilot.pause()
        app.push_screen(PreviewScreen(card.session))
        await pilot.pause()
        modal_focus = app.focused

        await pilot.press("ctrl+r")
        await pilot.pause()
        assert isinstance(app.screen, PreviewScreen)
        assert app.focused is modal_focus

        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is app.query_one("#filter", Input)


@pytest.fixture
def clipboard(monkeypatch):
    """A VaultApp with both copy paths stubbed to record what they receive.

    Yields (app, osc52, native): the two lists capture the OSC 52 and native
    clipboard writes. The native stub reports success (a binary is present), so
    tests exercise the authoritative path; the fallback test re-stubs it to fail.
    """
    app = VaultApp(vault_dir=FIXTURES)
    osc52: list[str] = []
    native: list[str] = []

    def fake_native(text: str) -> bool:
        native.append(text)
        return True

    app.copy_to_clipboard = osc52.append
    monkeypatch.setattr("rewind.app._native_clipboard", fake_native)
    return app, osc52, native


@pytest.mark.asyncio
async def test_click_copies_resume_command(clipboard):
    app, osc52, native = clipboard
    async with app.run_test() as pilot:
        good = next(c for c in app.query(SessionCard) if not c.session.error)
        good.copy_command()
        await pilot.pause()
        # Native succeeded, so it is the only write — OSC 52 stays silent to
        # avoid a late, throttled sequence clobbering it.
        assert native == [good.session.resume_command]
        assert osc52 == []
        assert good.has_class("copied")


@pytest.mark.asyncio
async def test_rapid_clicks_copy_last_card(clipboard):
    app, osc52, native = clipboard
    async with app.run_test() as pilot:
        good = [c for c in app.query(SessionCard) if not c.session.error]
        assert len(good) >= 2
        good[0].copy_command()
        good[1].copy_command()
        await pilot.pause()
        # Two deliberate copies in a row: the last click wins deterministically
        # because the only writer is the synchronous native path — OSC 52, the
        # sole source of the race, is never sent.
        assert native == [good[0].session.resume_command, good[1].session.resume_command]
        assert osc52 == []


@pytest.mark.asyncio
async def test_falls_back_to_osc52_when_no_native(clipboard, monkeypatch):
    app, osc52, native = clipboard
    # No native binary present (e.g. over SSH): the write fails.
    monkeypatch.setattr("rewind.app._native_clipboard", lambda text: False)
    async with app.run_test() as pilot:
        good = next(c for c in app.query(SessionCard) if not c.session.error)
        good.copy_command()
        await pilot.pause()
        assert osc52 == [good.session.resume_command]
        assert native == []


@pytest.mark.asyncio
async def test_broken_card_click_never_copies(clipboard):
    app, osc52, native = clipboard
    async with app.run_test() as pilot:
        broken = next(c for c in app.query(SessionCard) if c.session.error)
        broken.copy_command()
        await pilot.pause()
        assert osc52 == []
        assert native == []


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
async def test_cards_reflow_into_columns_capped_by_max():
    # Column count tracks terminal width (width // CARD_WIDTH) but never
    # exceeds MAX_COLUMNS, so cards stay readable on very wide terminals.
    for term_width, expected_columns in [(50, 1), (130, 2), (200, 3)]:
        app = VaultApp(vault_dir=FIXTURES)
        async with app.run_test(size=(term_width, 40)) as pilot:
            await pilot.pause()
            cards = app.query_one("#cards")
            assert cards.styles.grid_size_columns == expected_columns


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
async def test_hint_row_advertises_only_what_the_card_can_do():
    # Every card gets a hint row, but it lists actions the card actually has:
    # preview needs a reader for the harness, copy needs a resume command.
    app = VaultApp(vault_dir=FIXTURES)
    async with app.run_test():
        for card in app.query(SessionCard):
            [hint] = card.query(".card-hint")
            shown = str(hint.content)
            assert "delete" in shown, card.session.path.name
            if card.session.error:
                assert "copy" not in shown
                assert "preview" not in shown
            else:
                assert "copy" in shown
                assert ("preview" in shown) is (card.session.harness == "claude-code")


@pytest.mark.asyncio
async def test_hint_row_is_not_clipped_at_the_narrowest_layout():
    # A one-column terminal is the tight case: uniform grid rows used to clip
    # the tallest card, and the hint row is last, so delete was what vanished.
    app = VaultApp(vault_dir=FIXTURES)
    async with app.run_test(size=(50, 40)) as pilot:
        card = _claude_card(app)
        card.focus()
        await pilot.pause()
        rendered = "\n".join(
            "".join(seg.text for seg in strip)
            for strip in app.screen._compositor.render_strips()
        )
        assert "delete d" in rendered


@pytest.mark.asyncio
async def test_d_moves_the_session_to_trash(live_vault):
    app = VaultApp(vault_dir=live_vault)
    async with app.run_test() as pilot:
        card = next(c for c in app.query(SessionCard) if not c.session.error)
        doomed = card.session
        card.focus()
        await pilot.pause()

        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDeleteScreen)
        await pilot.press("y")
        await pilot.pause()

        assert not doomed.path.exists()
        # Moved, not erased — the capture is the only record of the session.
        assert (live_vault / ".trash" / doomed.path.name).exists()
        assert doomed.title not in [c.session.title for c in app.query(SessionCard)]


@pytest.mark.asyncio
async def test_escape_cancels_the_delete(live_vault):
    app = VaultApp(vault_dir=live_vault)
    async with app.run_test() as pilot:
        card = next(c for c in app.query(SessionCard) if not c.session.error)
        spared = card.session
        card.focus()
        await pilot.pause()

        await pilot.press("d")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert spared.path.exists()
        assert not (live_vault / ".trash").exists()
        assert len(app.query(SessionCard)) == 3


@pytest.mark.asyncio
async def test_d_in_the_filter_is_typed_not_a_delete(live_vault):
    # The filter holds focus from launch, so a card-level binding is the only
    # thing keeping "docker" from deleting three sessions.
    app = VaultApp(vault_dir=live_vault)
    async with app.run_test() as pilot:
        assert app.focused is app.query_one("#filter", Input)
        await pilot.press("d")
        await pilot.pause()

        assert not isinstance(app.screen, ConfirmDeleteScreen)
        assert app.query_one("#filter", Input).value == "d"
        assert len(list(live_vault.glob("*.md"))) == 3


@pytest.mark.asyncio
async def test_broken_card_can_be_deleted(live_vault):
    # The main reason delete exists: a malformed file otherwise sits there
    # forever, since it cannot be copied or previewed either.
    app = VaultApp(vault_dir=live_vault)
    async with app.run_test() as pilot:
        card = next(c for c in app.query(SessionCard) if c.session.error)
        doomed = card.session
        card.focus()
        await pilot.pause()

        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

        assert not doomed.path.exists()
        assert (live_vault / ".trash" / doomed.path.name).exists()


@pytest.mark.asyncio
async def test_startup_purges_expired_trash(live_vault, monkeypatch):
    # Wiring check: launch runs the purge (vault-level behavior is covered in
    # test_vault). The clock is swapped only inside rewind.vault — shifting
    # time.time globally would reach Textual's own internals.
    import rewind.vault as vault_module

    trash = live_vault / ".trash"
    trash.mkdir()
    write_capture(trash, "expired.md", "long gone")
    monkeypatch.delenv("REWIND_TRASH_DAYS", raising=False)
    monkeypatch.setattr(
        vault_module, "time", SimpleNamespace(time=lambda: time.time() + 15 * SECONDS_PER_DAY)
    )

    app = VaultApp(vault_dir=live_vault)
    async with app.run_test():
        assert not (trash / "expired.md").exists()
        # Only .trash/ is purged; the vault itself is untouched.
        assert len(app.query(SessionCard)) == 3


@pytest.mark.asyncio
async def test_deleting_the_last_session_restores_the_empty_state(tmp_path):
    # Reusing the reload path is what makes this work; unmounting a lone card
    # would leave a blank grid with no empty state.
    write_capture(tmp_path, "2026-07-19-only.md", "only one")
    app = VaultApp(vault_dir=tmp_path)
    async with app.run_test() as pilot:
        card = next(iter(app.query(SessionCard)))
        card.focus()
        await pilot.pause()

        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

        assert len(app.query(SessionCard)) == 0
        assert len(app.query("#empty")) == 1

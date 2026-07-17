"""Session Vault TUI (spec §7).

Run inside the vault directory: it reads every *.md under cwd.
Click a card (or press Enter on it) to copy the resume command.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static

from .theme import BITCOIN_DEFI, PALETTE
from .transcript import TranscriptError, read_transcript, supports_preview
from .update import maybe_update_in_background
from .vault import Session, load_vault, matches, relative_time

# harness -> (badge label, reverse-video chip color). Colors come from the
# palette so the theme stays the single source of truth.
HARNESS_BADGES = {
    "claude-code": ("claude code", PALETTE["orange"]),
    "opencode": ("opencode", PALETTE["stardust"]),
}


def _is_wide(char: str) -> bool:
    return unicodedata.east_asian_width(char) in ("W", "F")


def _needs_space(left: str, right: str) -> bool:
    """Whether a line break between these two characters should become a space.

    CJK runs close up (行 + 第), and full-width punctuation never takes a
    trailing space (、+ OSC). But CJK against Latin does take one, because
    that is how the summaries are written: "已 symlink 進 ~/.claude".
    """
    if not _is_wide(left):
        return True
    if unicodedata.category(left).startswith("P"):
        return False
    return not _is_wide(right)


def reflow(text: str) -> str:
    """Drop the line breaks the capture skill wrote, keeping paragraph breaks.

    The vault file stores the summary exactly as authored, wrapped at whatever
    column the writer happened to use. The card is responsive, so it has to do
    its own wrapping — same split as H2, where storage stays raw and the TUI
    renders. A break after a full-width character joins with no space (the
    author never intended one there); everything else gets the space back.
    """
    paragraphs = re.split(r"\n\s*\n", text.strip())
    joined = []
    for paragraph in paragraphs:
        lines = [line.strip() for line in paragraph.split("\n") if line.strip()]
        if not lines:
            continue
        out = lines[0]
        for line in lines[1:]:
            out += (" " if _needs_space(out[-1], line[0]) else "") + line
        joined.append(out)
    return "\n\n".join(joined)


ROLE_LABELS = {"user": ("you", PALETTE["orange"]), "assistant": ("claude", PALETTE["stardust"])}


class PreviewScreen(ModalScreen[None]):
    """Read-only transcript of one session, read from its harness's storage.

    Display only: nothing here is written back to the vault (see transcript.py).
    """

    BINDINGS = [Binding("escape", "close", "Close", priority=True)]

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        s = self.session
        with Vertical(id="preview"):
            yield Label(f"[b]{escape(s.title)}[/b]", id="preview-title")
            yield Label(
                f"[dim]{escape(relative_time(s.captured_at))}  ·  {escape(s.cwd)}[/dim]",
                id="preview-meta",
            )
            yield VerticalScroll(id="preview-body")
            yield Label("[dim]esc close  ·  ↑↓ scroll[/dim]", id="preview-hint")

    def on_mount(self) -> None:
        body = self.query_one("#preview-body", VerticalScroll)
        try:
            messages = read_transcript(
                self.session.harness, self.session.session_id, self.session.cwd
            )
        except TranscriptError as exc:
            # H4: say exactly what failed rather than show an empty dialog.
            body.mount(Label(f"⚠ {escape(str(exc))}", classes="preview-error"))
            return
        for message in messages:
            label, color = ROLE_LABELS.get(message.role, (message.role, PALETTE["stardust"]))
            body.mount(Label(f"[b {color}]▌ {escape(label)}[/b {color}]", classes="preview-role"))
            body.mount(Label(escape(message.text), classes="preview-text"))
        body.focus()

    def action_close(self) -> None:
        self.dismiss()


class SessionCard(Static, can_focus=True):
    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session
        if session.error:
            self.add_class("broken")

    def compose(self) -> ComposeResult:
        s = self.session
        if s.error:
            yield Label(
                f"[b]⚠ BROKEN CARD[/b]  {escape(s.path.name)}", classes="card-title"
            )
            yield Label(escape(s.error), classes="card-error")
            yield Label(
                "This file could not be used. Fix it by hand — nothing was hidden.",
                classes="card-meta",
            )
            return

        badge_text, badge_color = HARNESS_BADGES.get(
            s.harness, (s.harness, PALETTE["stardust"])
        )
        yield Label(f"[b]{escape(s.title)}[/b]", classes="card-title")
        header = f"[reverse {badge_color}] {escape(badge_text)} [/reverse {badge_color}]"
        if s.repo:
            header += f"  {escape(s.repo)}"
        header += f"  [dim]{escape(relative_time(s.captured_at))}[/dim]"
        if s.model:
            header += f"  [dim]{escape(s.model)}[/dim]"
        yield Label(header, classes="card-meta")
        yield Label(f"cd {escape(s.cwd)}", classes="card-cwd")
        if s.summary:
            yield Label(escape(reflow(s.summary)), classes="card-summary")
        if s.tags:
            yield Label(
                " ".join(f"#{escape(t)}" for t in s.tags), classes="card-tags"
            )
        # Only harnesses with a reader can preview; the rest simply never offer
        # it. CSS reveals the hint on focus so idle cards stay uncluttered.
        if supports_preview(s.harness):
            yield Label("space preview", classes="card-hint")
        yield Label("", classes="card-flash")

    def on_click(self) -> None:
        self.copy_command()

    def key_enter(self) -> None:
        self.copy_command()

    def key_space(self) -> None:
        # Only fires while the card itself has focus, so it never competes with
        # a space typed into the filter Input.
        if self.session.error or not supports_preview(self.session.harness):
            return
        self.app.push_screen(PreviewScreen(self.session))

    def copy_command(self) -> None:
        command = self.session.resume_command
        if command is None:
            # H4: never hand over a command we can't render correctly.
            self.app.notify(
                f"Cannot build a resume command for {self.session.path.name}",
                severity="error",
                title="Not copied",
            )
            return
        self.app.copy_to_clipboard(command)
        flash = self.query_one(".card-flash", Label)
        flash.update(f"Copied ✓  [dim]{escape(command)}[/dim]")
        self.add_class("copied")
        self.set_timer(1.5, self._clear_flash)

    def _clear_flash(self) -> None:
        self.query_one(".card-flash", Label).update("")
        self.remove_class("copied")


class VaultApp(App):
    TITLE = "Session Vault"

    # priority=True so these fire even while the filter Input has focus, and so
    # ctrl+c reaches us instead of Textual's built-in handling.
    BINDINGS = [
        Binding("ctrl+c", "quit_with_toast", "Quit", priority=True),
        Binding("ctrl+q", "quit_with_toast", "Quit", priority=True),
    ]

    CSS = """
    Screen {
        layout: vertical;
    }
    #filter {
        dock: top;
        margin: 0 1;
    }
    #cards {
        padding: 0 1;
        align-horizontal: left;
    }
    SessionCard {
        background: $surface;
        border: round $surface-lighten-2;
        width: 90%;
        max-width: 80;
        padding: 0 1;
        margin-bottom: 1;
        height: auto;
    }
    SessionCard:hover {
        border: round $accent;
        background: $boost;
    }
    SessionCard:focus {
        border: round $accent;
    }
    SessionCard.copied {
        border: round $success;
    }
    SessionCard.broken {
        border: heavy $error;
    }
    .card-error {
        color: $error;
    }
    .card-cwd {
        color: $warning;
        text-style: bold;
    }
    .card-summary {
        color: $text-muted;
        margin-top: 1;
        /* Label is width:auto by default, which shrink-wraps and overflows.
           The summary has no authored line breaks left, so it needs a width
           to wrap into. */
        width: 100%;
    }
    .card-tags {
        color: $text-muted;
    }
    .card-flash {
        color: $success;
        text-style: bold;
    }
    .card-hint {
        display: none;
        color: $text-muted;
        margin-top: 1;
    }
    SessionCard:focus .card-hint {
        display: block;
    }
    #empty {
        padding: 1 2;
        color: $text-muted;
    }
    PreviewScreen {
        align: center middle;
    }
    #preview {
        width: 90%;
        max-width: 100;
        height: 85%;
        background: $surface;
        border: round $accent;
        padding: 0 1;
    }
    #preview-title {
        width: 100%;
    }
    #preview-meta {
        width: 100%;
        margin-bottom: 1;
    }
    #preview-body {
        height: 1fr;
    }
    #preview-hint {
        margin-top: 1;
    }
    .preview-role {
        margin-top: 1;
    }
    .preview-text {
        /* Same reason as .card-summary: Label shrink-wraps without a width. */
        width: 100%;
        color: $text-muted;
    }
    .preview-error {
        width: 100%;
        color: $error;
    }
    """

    def __init__(self, vault_dir: Path | None = None) -> None:
        super().__init__()
        self.vault_dir = vault_dir or Path.cwd()
        self.sessions: list[Session] = []

    def compose(self) -> ComposeResult:
        yield Input(placeholder="type to filter…", id="filter")
        yield VerticalScroll(id="cards")

    def on_mount(self) -> None:
        self.register_theme(BITCOIN_DEFI)
        self.theme = "bitcoin-defi"
        self.sessions = load_vault(self.vault_dir)
        cards = self.query_one("#cards", VerticalScroll)
        if not self.sessions:
            cards.mount(
                Static(
                    f"No sessions in {self.vault_dir} — capture one with the "
                    "session-capture skill.",
                    id="empty",
                )
            )
            return
        for session in self.sessions:
            cards.mount(SessionCard(session))
        self.query_one("#filter", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value
        for card in self.query(SessionCard):
            card.display = card.session.error is not None or matches(
                query, card.session
            )

    _quit_pending = False

    def action_quit_with_toast(self) -> None:
        # First press warns, second press within 2s actually quits. Both
        # ctrl+c and ctrl+q land here.
        if self._quit_pending:
            self.notify("Quitting…", title="Rewind")
            self.set_timer(0.4, self.exit)
            return
        self._quit_pending = True
        self.notify("Press again to quit", title="Rewind")
        self.set_timer(2.0, self._reset_quit_pending)

    def _reset_quit_pending(self) -> None:
        self._quit_pending = False


def main() -> None:
    VaultApp().run()
    # After the TUI is down, never before it — see session_vault/update.py.
    maybe_update_in_background()


if __name__ == "__main__":
    main()

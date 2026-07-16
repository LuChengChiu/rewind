"""Session Vault TUI (spec §7).

Run inside the vault directory: it reads every *.md under cwd.
Click a card (or press Enter on it) to copy the resume command.
"""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Input, Label, Static

from .theme import BITCOIN_DEFI, PALETTE
from .vault import Session, load_vault, matches, relative_time

# harness -> (badge label, reverse-video chip color). Colors come from the
# palette so the theme stays the single source of truth.
HARNESS_BADGES = {
    "claude-code": ("claude code", PALETTE["orange"]),
    "opencode": ("opencode", PALETTE["stardust"]),
}


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
            yield Label(escape(s.summary), classes="card-summary")
        if s.tags:
            yield Label(
                " ".join(f"#{escape(t)}" for t in s.tags), classes="card-tags"
            )
        yield Label("", classes="card-flash")

    def on_click(self) -> None:
        self.copy_command()

    def key_enter(self) -> None:
        self.copy_command()

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
    }
    SessionCard {
        background: $surface;
        border: round $surface-lighten-2;
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
    }
    .card-tags {
        color: $text-muted;
    }
    .card-flash {
        color: $success;
        text-style: bold;
    }
    #empty {
        padding: 1 2;
        color: $text-muted;
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


if __name__ == "__main__":
    main()

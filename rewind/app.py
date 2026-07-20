"""Rewind TUI (spec §7).

Reads every *.md in the vault directory, which `resolve_vault_dir` locates the
same way the capture skill does ($REWIND_DIR, else ~/rewind) — so
`rewind` works from anywhere, not only from inside the vault.
Click a card (or press Enter on it) to copy the resume command; ctrl+r re-reads
the vault (see `VaultApp._reload`); `d` on a focused card deletes it into
`.trash/` after a confirmation (see `VaultApp.delete_session`), where it is
kept for $REWIND_TRASH_DAYS days (default 14) before a launch-time purge
erases it for good (see `VaultApp._purge_trash`).

The vault stays global — finding "that thing I did in some other repo" is half
the point — so ctrl+f narrows the grid to cards captured in the launch
directory instead (see `VaultApp.action_toggle_scope`). ctrl+s (or the toolbar
button) picks the order the cards are laid out in, re-sorting what is already
loaded rather than re-reading the vault (see `VaultApp._resort`).

The ⚙ dialog sets only what those two *start* as, persisted per-vault in
`settings.json`; they are initial states, never modes or locks.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

from rich.markup import escape
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from .theme import BITCOIN_DEFI, PALETTE
from .transcript import TranscriptError, read_transcript, supports_preview
from .update import maybe_update_in_background
from .vault import (
    SORT_MODES,
    Session,
    Settings,
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

def _clipboard_candidates() -> tuple[tuple[list[str], str], ...]:
    """(argv, stdin encoding) clipboard writers to try, in order, for this platform."""
    if sys.platform == "darwin":
        return ((["pbcopy"], "utf-8"),)
    if sys.platform == "win32":
        # clip.exe decodes stdin in the console codepage, not UTF-8; UTF-16-LE
        # is what it reads back losslessly, so a non-ASCII cwd survives.
        return ((["clip"], "utf-16-le"),)
    return (
        (["wl-copy"], "utf-8"),
        (["xclip", "-selection", "clipboard"], "utf-8"),
        (["xsel", "-b", "-i"], "utf-8"),
    )


def _native_clipboard(text: str) -> bool:
    """Write to the OS clipboard synchronously; return whether it succeeded.

    A native binary is synchronous and last-write-wins, which is what a discrete
    copy action should be. The caller uses the result to decide the fallback: it
    must NOT also send OSC 52 when this succeeds. OSC 52 is applied by the
    terminal asynchronously, so a throttled, delayed OSC 52 for an earlier click
    could land *after* this write and clobber it — sending both races. OSC 52 is
    therefore the fallback only when no native binary is present (e.g. over SSH).

    The timeout bounds a hung writer (e.g. xclip against a dead X connection) so
    it can't freeze the TUI; stderr is discarded so a failing binary can't
    scribble over the screen.
    """
    for argv, encoding in _clipboard_candidates():
        if shutil.which(argv[0]) is None:
            continue
        try:
            subprocess.run(
                argv,
                input=text.encode(encoding),
                check=True,
                timeout=2,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


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
            # An error is a few lines and never scrolls, so the dialog drops its
            # transcript-sized viewport and shrinks to the message.
            self.query_one("#preview").add_class("compact")
            body.mount(Label(f"🚨 {escape(str(exc))}", classes="preview-error"))
            return
        for message in messages:
            label, color = ROLE_LABELS.get(message.role, (message.role, PALETTE["stardust"]))
            body.mount(Label(f"[b {color}]▌ {escape(label)}[/b {color}]", classes="preview-role"))
            body.mount(Label(escape(message.text), classes="preview-text"))
        body.focus()

    def action_close(self) -> None:
        self.dismiss()


class ConfirmDeleteScreen(ModalScreen[bool]):
    """Are-you-sure for a delete, returning True only on an explicit yes.

    `y` confirms rather than `enter`, and there is no default-accept: the reader
    arrived here by pressing `d`, so enter is exactly the key they might still
    have queued from copying a card. Every other route out — esc, `n` — cancels.
    """

    BINDINGS = [
        Binding("y", "confirm", "Delete", priority=True),
        Binding("escape,n", "cancel", "Cancel", priority=True),
    ]

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        # A broken card has no parsed title, so it is named by its filename —
        # which is also the only thing its own card shows.
        name = self.session.title or self.session.path.name
        with Vertical(id="confirm"):
            # The verb gets its own line in the error color rather than sharing
            # one with the title: titles are long and arbitrary, so inline they
            # wrap and push "Delete" away from the edge where it reads as the
            # question. Separated, the destructive word lands first every time.
            yield Label("Delete this session?", id="confirm-title")
            yield Label(f"[b]{escape(name)}[/b]", id="confirm-name")
            # When there is no title the heading is already the filename, so
            # naming it again below would print it twice in four lines.
            fate = (
                f"{escape(self.session.path.name)} moves"
                if self.session.title
                else "Moves"
            )
            # The promise must match what the purge actually does, so the
            # retention window is re-read here rather than hard-coded.
            days = resolve_trash_days()
            kept = (
                f"kept {days} days, then purged"
                if days is not None
                else "kept, not erased"
            )
            yield Label(
                f"[dim]{fate} to .trash/ — {kept}.[/dim]",
                id="confirm-body",
            )
            yield Label("delete [dim]y[/dim]   cancel [dim]esc/n[/dim]", id="confirm-hint")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


def _sort_options(marked: str) -> list[Option]:
    """The picker's rows, with `●` on *marked* only.

    Shared by both pickers so they read as one feature: single-line rows, a
    dim `—`-separated description, and a marker on exactly one row. The
    unmarked rows get a space in the marker's place rather than nothing, so
    the mode names stay in one column.
    """
    return [
        Option(
            f"{'●' if mode == marked else ' '} {mode}  [dim]— {description}[/dim]",
            id=mode,
        )
        for mode, description in SORT_MODES.items()
    ]


def _sort_index(sort_mode: str) -> int:
    """The row *sort_mode* sits on — both pickers list `SORT_MODES` in order."""
    return list(SORT_MODES).index(sort_mode)


class SortScreen(ModalScreen[str | None]):
    """Picks the live sort order, applying it on selection.

    Dismisses with the chosen mode, or None on esc — so opening the picker
    just to read what "grouped" means costs nothing. The cursor starts on the
    active mode, so the highlighted row and the marked row agree on open.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, sort_mode: str) -> None:
        super().__init__()
        self.sort_mode = sort_mode

    def compose(self) -> ComposeResult:
        with Vertical(id="sort"):
            yield Label("Sort", id="sort-title")
            yield OptionList(*_sort_options(self.sort_mode), id="sort-options")
            yield Label("[dim]esc close[/dim]", id="sort-hint")

    def on_mount(self) -> None:
        options = self.query_one("#sort-options", OptionList)
        options.highlighted = _sort_index(self.sort_mode)
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Both enter and a click arrive here, so the mouse path that opened the
        # dialog can also finish the job. Every row carries a mode as its id;
        # the guard only satisfies the `str | None` typing, and falls back to
        # "no change" rather than to any particular mode.
        self.dismiss(event.option.id or self.sort_mode)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SettingsScreen(ModalScreen[Settings | None]):
    """Sets what a fresh launch *starts* in — nothing else.

    Dismisses with the new defaults on Save, or None on any cancel, so the
    caller can tell "saved" from "did not save". The dialog deliberately never
    touches the current session's toggle or sort: making either jump from a
    settings dialog would change what is on screen for a reason the reader did
    not ask for. These are initial states, not modes and not locks.

    The sort row looks exactly like the live picker but commits differently:
    selecting only moves the pending marker, and the write happens on Save —
    the stage-then-commit contract the checkbox beside it already follows.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, scope_default: bool, sort_default: str) -> None:
        super().__init__()
        self.scope_default = scope_default
        self.sort_default = sort_default

    def compose(self) -> ComposeResult:
        with Vertical(id="settings"):
            yield Label("Settings", id="settings-title")
            yield Checkbox(
                'Start with "only here" on',
                value=self.scope_default,
                id="settings-scope",
            )
            yield Label("Start sorted by", id="settings-sort-label")
            yield OptionList(*_sort_options(self.sort_default), id="settings-sort")
            with Horizontal(id="settings-buttons"):
                yield Button("Save", id="settings-save", variant="primary")
                yield Button("Cancel", id="settings-cancel")

    def on_mount(self) -> None:
        self.query_one("#settings-sort", OptionList).highlighted = _sort_index(
            self.sort_default
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Staged, not applied: only the marker moves. Save writes it, Cancel
        # discards it. The `or` mirrors SortScreen's: id-less rows cannot
        # exist, and the fallback is "keep the pending choice".
        self.sort_default = event.option.id or self.sort_default
        options = self.query_one("#settings-sort", OptionList)
        for option in _sort_options(self.sort_default):
            options.replace_option_prompt(option.id, option.prompt)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "settings-save":
            self.dismiss(
                Settings(
                    scope_cwd=self.query_one("#settings-scope", Checkbox).value,
                    sort=self.sort_default,
                )
            )
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


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
            # A broken file is the likeliest thing to want gone, so this branch
            # gets the hint too — minus copy and preview, which it cannot do.
            yield Label(self._hints(), classes="card-hint")
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
        yield Label(self._hints(), classes="card-hint")
        yield Label("", classes="card-flash")

    def _hints(self) -> str:
        """The action row: what this card can do, and the key that does it.

        Action bright, key dim. Delete is the only entry every card has — copy
        needs a resume command and preview needs a reader for the harness, so a
        broken or unpreviewable card advertises only what it will actually do.
        CSS reveals the row on focus, keeping idle cards uncluttered.
        """
        s = self.session
        parts = []
        if not s.error:
            # Click is listed because it is how most readers find copy at all;
            # the whole card is the target (see on_click), not just this row.
            parts.append("copy [dim]enter/click[/dim]")
            # Only harnesses with a reader can preview; the rest never offer it.
            if supports_preview(s.harness):
                parts.append("preview [dim]space[/dim]")
        parts.append("delete [dim]d[/dim]")
        # Two spaces, not three: at CARD_WIDTH the full three-action row is a
        # couple of columns too wide, and the segment that falls off the end is
        # delete — the one action with no other affordance anywhere in the UI.
        return "  ".join(parts)

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

    def key_d(self) -> None:
        # Card-level for the same reason as key_space: it only fires while the
        # card itself has focus, so a `d` typed into the filter stays a search.
        def on_confirm(confirmed: bool | None) -> None:
            if confirmed:
                self.app.run_worker(self.app.delete_session(self.session))

        self.app.push_screen(ConfirmDeleteScreen(self.session), on_confirm)

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
        # Native clipboard is authoritative: synchronous and last-write-wins.
        # Only fall back to OSC 52 when there's no local binary (e.g. over SSH),
        # because sending both races — a throttled OSC 52 can land late and
        # clobber the native write.
        if not _native_clipboard(command):
            self.app.copy_to_clipboard(command)
        flash = self.query_one(".card-flash", Label)
        flash.update(f"Copied ✓  [dim]{escape(command)}[/dim]")
        self.add_class("copied")
        self.set_timer(1.5, self._clear_flash)

    def _clear_flash(self) -> None:
        self.query_one(".card-flash", Label).update("")
        self.remove_class("copied")


class VaultApp(App):
    TITLE = "Rewind"

    # priority=True so these fire even while the filter Input has focus, and so
    # ctrl+c reaches us instead of Textual's built-in handling.
    # ctrl+r and ctrl+f rather than bare `r` and `f`, for the same reason space
    # previews only from a focused card: plain letters belong to the filter
    # Input, which holds focus from launch. Both are app-level (not card-level)
    # so they still work on an empty vault, where there is no card to focus and
    # reloading is the whole point.
    BINDINGS = [
        Binding("ctrl+c", "quit_with_toast", "Quit", priority=True),
        Binding("ctrl+q", "quit_with_toast", "Quit", priority=True),
        Binding("ctrl+r", "reload", "Reload", priority=True),
        Binding("ctrl+f", "toggle_scope", "Scope", priority=True),
        # Safe as a binding because the terminal driver disables XON/XOFF flow
        # control, so ctrl+s never reaches us as "freeze the terminal".
        Binding("ctrl+s", "open_sort", "Sort", priority=True),
    ]

    CSS = """
    Screen {
        layout: vertical;
    }
    #toolbar {
        dock: top;
        height: auto;
        margin: 0 1;
    }
    #filter {
        /* Takes whatever the two buttons leave, so the search stays the widest
           thing in the row at any terminal width. */
        width: 1fr;
    }
    #sort-open, #scope, #settings-open {
        min-width: 0;
        height: 3;
        margin-left: 1;
    }
    #scope-notice {
        display: none;
        padding: 1 2;
        color: $text-muted;
    }
    #cards {
        padding: 0 1;
        align-horizontal: left;
        /* Column count is set from the terminal width in on_resize; Textual's
           grid has no auto-fill, so it can't reflow from CSS alone. */
        layout: grid;
        grid-gutter: 1;
        /* Without this, rows take a uniform default height and any card taller
           than it is clipped — losing the tags and the hint row, which is where
           delete is advertised. Cards are height:auto, so the rows must be too. */
        grid-rows: auto;
    }
    SessionCard {
        background: $surface;
        border: round $surface-lighten-2;
        width: 100%;
        max-width: 200;
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
        /* Normal foreground, not $text-muted: the keys are dimmed inline so the
           action names stay the brighter half of the pair. */
        color: $text;
        margin-top: 1;
        /* Same reason as .card-summary: without a width Label shrink-wraps and
           overflows, which clips the row's tail rather than wrapping it. */
        width: 100%;
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
    ConfirmDeleteScreen {
        align: center middle;
    }
    #confirm {
        width: 60;
        max-width: 90%;
        height: auto;
        background: $surface;
        border: round $error;
        padding: 1 2;
    }
    #confirm-title {
        width: 100%;
        color: $error;
        text-style: bold;
    }
    #confirm-name, #confirm-body {
        width: 100%;
    }
    #confirm-name {
        margin-bottom: 1;
    }
    #confirm-hint {
        width: 100%;
        margin-top: 1;
    }
    SortScreen {
        align: center middle;
    }
    #sort {
        width: 50;
        max-width: 90%;
        height: auto;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }
    #sort-title {
        width: 100%;
        text-style: bold;
        margin-bottom: 1;
    }
    #sort-hint {
        width: 100%;
        margin-top: 1;
    }
    SettingsScreen {
        align: center middle;
    }
    #settings {
        width: 50;
        max-width: 90%;
        height: auto;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }
    #settings-title {
        width: 100%;
        text-style: bold;
        margin-bottom: 1;
    }
    #settings-sort-label {
        width: 100%;
        margin-top: 1;
    }
    /* Both pickers: the rows are the content, so the list sizes to them and
       drops the border an OptionList draws by default — inside a dialog that
       would be a second frame within the dialog's own. */
    #sort-options, #settings-sort {
        height: auto;
        border: none;
        background: $surface;
        padding: 0;
    }
    #settings-buttons {
        height: auto;
        margin-top: 1;
        align-horizontal: right;
    }
    #settings-buttons Button {
        min-width: 0;
        margin-left: 1;
    }
    #preview {
        width: 90%;
        max-width: 100;
        height: 85%;
        background: $surface;
        border: round $accent;
        padding: 0 1;
    }
    /* A failed preview is a few lines that never scroll, so the dialog drops
       the transcript-sized viewport. Only this path may size to content: with
       a real transcript the `1fr` body below has nothing to divide up. */
    #preview.compact {
        height: auto;
    }
    #preview.compact #preview-body {
        height: auto;
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

    # Card content reads comfortably around this width; the grid fits as many
    # whole cards as the terminal allows, capped so cards never get too narrow.
    CARD_WIDTH = 46
    MAX_COLUMNS = 3

    def __init__(
        self, vault_dir: Path | None = None, launch_dir: str | None = None
    ) -> None:
        super().__init__()
        self.vault_dir = vault_dir or resolve_vault_dir()
        # Captured once, here, rather than re-read per filter pass: the scope is
        # "the folder Rewind was opened in", which cannot change while it runs.
        self.launch_dir = launch_dir or str(Path.cwd())
        self.sessions: list[Session] = []
        # Paths of the sessions captured in the launch dir, rebuilt by
        # `_reload` — see the comment there for why it is not computed per
        # filter pass.
        self._scoped_paths: set[Path] = set()
        # Two separate pieces of state per setting, on purpose. The `*_default`
        # halves are what settings.json holds and what the ⚙ dialog edits; the
        # live halves are seeded from them at startup and free to diverge for
        # the rest of the session. Collapsing them would turn the settings into
        # locks — and would make a temporary reorder accidentally permanent.
        settings = load_settings(self.vault_dir)
        self.scope_default = settings.scope_cwd
        self.scope_cwd = self.scope_default
        self.sort_default = settings.sort
        self.sort_mode = self.sort_default

    def compose(self) -> ComposeResult:
        with Horizontal(id="toolbar"):
            yield Input(placeholder="type to filter…", id="filter")
            yield Button(self._sort_label(), id="sort-open")
            yield Button(self._scope_label(), id="scope")
            yield Button("⚙", id="settings-open")
        # Outside #cards, which is a grid: a notice mounted in there would be
        # laid out as one narrow grid cell instead of a full-width line.
        yield Static("", id="scope-notice")
        yield VerticalScroll(id="cards")

    def _scope_label(self) -> str:
        """The scope button's text, which is also how the state is read.

        The label states the mode rather than the action, so the current scope
        is visible without opening or pressing anything.
        """
        return "◉ only here" if self.scope_cwd else "○ all folders"

    def _sort_label(self) -> str:
        """The sort button's text, which is also how the order is read.

        Spells out the active mode rather than showing an icon, matching the
        scope button beside it: a glyph cannot say which of three orders is on.
        """
        return f"sort: {self.sort_mode}"

    async def on_mount(self) -> None:
        self.register_theme(BITCOIN_DEFI)
        self.theme = "bitcoin-defi"
        self._purge_trash()
        await self._reload()

    def _purge_trash(self) -> None:
        """Expire old trash once per launch — never on ctrl+r.

        Startup is the one moment erasure is predictable; tying it to reload
        would turn a habitual ctrl+r into the thing that empties the trash.
        """
        days = resolve_trash_days()
        if days is None:
            return
        removed, failed = purge_trash(self.vault_dir, days)
        # H4's spirit applies to purging too: destruction — and destruction
        # that failed to happen on schedule — must say so; only "nothing was
        # due" stays silent.
        if removed:
            count = len(removed)
            self.notify(
                f"Purged {count} trashed session{'s' if count != 1 else ''} "
                f"older than {days} days",
                title="Rewind",
            )
        if failed:
            count = len(failed)
            self.notify(
                f"{count} expired trashed session{'s' if count != 1 else ''} "
                "could not be erased; will retry next launch",
                title="Rewind",
                severity="warning",
            )

    async def _reload(self) -> None:
        """Re-read the vault and rebuild the grid from scratch.

        The vault is only ever read here, so a session captured while the TUI
        is up appears on the next reload — nothing watches the directory.
        Removal and mounting are awaited so the cards exist before the filter
        is re-applied below; skipping that would leave a stale query showing
        every freshly mounted card.
        """
        self.sessions = load_vault(self.vault_dir, self.sort_mode)
        # Scope membership is settled here, not in `_apply_filter`: a card's
        # cwd can only change through a reload and the launch dir never
        # changes, so resolving it per keystroke would realpath — an lstat per
        # path component — every card on every character typed, for an answer
        # that cannot differ from the last one.
        self._scoped_paths = {
            s.path for s in self.sessions if same_dir(s.cwd, self.launch_dir)
        }
        await self._rebuild_grid()

    async def _rebuild_grid(self) -> None:
        """Re-mount the card grid from `self.sessions`, then restore the view.

        Split out of `_reload` so the sort path can share it: order is mount
        order, so changing the sort has to re-mount, and it needs exactly the
        same post-conditions — filter re-applied, focus back on the filter,
        empty state intact. One rebuild path means neither can drift.
        """
        cards = self.query_one("#cards", VerticalScroll)
        await cards.remove_children()
        if self.sessions:
            await cards.mount_all(SessionCard(s) for s in self.sessions)
            self._apply_filter(self.query_one("#filter", Input).value)
        else:
            # This is the one screen where reloading is the likely next action:
            # the message sends the reader off to capture a session, so it also
            # has to say how to come back.
            await cards.mount(
                Static(
                    f"No sessions in {self.vault_dir} — capture one with the "
                    "rewind-capture skill, then press ctrl+r to reload.",
                    id="empty",
                )
            )
            # An empty vault is already explained by the line above; blaming
            # the scope filter on top of it would be both redundant and wrong.
            self.query_one("#scope-notice", Static).display = False
        # Rebuilding destroys whatever card had focus, and Textual parks focus
        # on the scroll container — which swallows keys without being an input,
        # so the filter would look dead until clicked. Unconditional on purpose:
        # under an open preview this sets focus on the background screen without
        # touching the modal's own, which is what makes dismissing the preview
        # land on the filter instead of that same dead container.
        self.query_one("#filter", Input).focus()

    async def delete_session(self, session: Session) -> None:
        """Move one session to `.trash/` and rebuild the grid.

        Reuses the reload path rather than removing the single card by hand:
        one way to build the grid means the filter, the focus reset and the
        empty state all keep working after a delete for free — deleting the
        last session has to restore the empty state, which unmounting a lone
        card would not do.
        """
        try:
            target = trash_session(session, self.vault_dir)
        except OSError as exc:
            # H4: a delete that did not happen must say so, loudly.
            self.notify(
                f"Could not delete {session.path.name}: {exc}",
                severity="error",
                title="Not deleted",
            )
            return
        await self._reload()
        self.notify(f"Moved to {target.parent}", title="Deleted")

    def action_open_sort(self) -> None:
        """Open the live sort picker.

        Unconditional, including on an empty vault: a key that silently does
        nothing under some conditions reads as broken. Focus goes back to the
        filter *before* the push for the same reason as the ⚙ dialog — Textual
        restores focus to whatever held it before push_screen.
        """
        self.query_one("#filter", Input).focus()
        self.push_screen(SortScreen(self.sort_mode), self._sort_picked)

    def _sort_picked(self, sort_mode: str | None) -> None:
        """Apply a pick from the live picker; None (esc) changes nothing."""
        if sort_mode is None:
            return
        self.run_worker(self._resort(sort_mode))

    async def _resort(self, sort_mode: str) -> None:
        """Re-order the loaded sessions in place and rebuild the grid.

        Deliberately does not touch the disk in either direction: it re-sorts
        the list already in memory, so a sort change never doubles as a
        surprise sync, and it leaves the stored default alone, so a temporary
        reorder is never accidentally permanent.
        """
        self.sort_mode = sort_mode
        self.query_one("#sort-open", Button).label = self._sort_label()
        self.sessions = sort_sessions(self.sessions, sort_mode)
        await self._rebuild_grid()

    async def action_reload(self) -> None:
        await self._reload()
        # A silent reload over an unchanged vault is indistinguishable from a
        # dead keybinding, so it always reports the count.
        count = len(self.sessions)
        self.notify(
            f"Reloaded — {count} session{'s' if count != 1 else ''}",
            title="Rewind",
        )

    def on_resize(self, event: events.Resize) -> None:
        columns = min(self.MAX_COLUMNS, max(1, event.size.width // self.CARD_WIDTH))
        self.query_one("#cards", VerticalScroll).styles.grid_size_columns = columns

    def on_input_changed(self, event: Input.Changed) -> None:
        self._apply_filter(event.value)

    def _in_scope(self, session: Session) -> bool:
        return not self.scope_cwd or session.path in self._scoped_paths

    def _apply_filter(self, query: str) -> None:
        visible = 0
        for card in self.query(SessionCard):
            # The scope condition is ANDed with the text query — both are live
            # at once and neither clears the other — but the broken-card
            # exemption sits outside both. `load_session` leaves cwd="" on a
            # parse failure, so scoping a broken card would hide every one of
            # them, which is exactly what H4 forbids.
            card.display = card.session.error is not None or (
                self._in_scope(card.session) and matches(query, card.session)
            )
            # Count only healthy cards: broken cards are exempt from scoping,
            # so counting them would silence the notice exactly when the grid
            # shows nothing but an error card.
            if card.display and card.session.error is None:
                visible += 1
        self._update_scope_notice(visible, query)

    def _update_scope_notice(self, visible: int, query: str) -> None:
        """Explain an empty grid that the scope filter, not the reader, caused.

        `visible` counts healthy cards only — a grid holding nothing but a
        broken card still needs the explanation, since the reader's real
        sessions are just as hidden either way.

        Only when nothing was typed: a blank grid under a typed query is
        existing behaviour and needs no explanation, because the reader knows
        what they typed. With the setting on and no query, though, opening
        Rewind in a folder that has no captures is indistinguishable from a
        broken app — so the line names the folder and the way back out.
        """
        notice = self.query_one("#scope-notice", Static)
        show = self.scope_cwd and visible == 0 and not query.strip()
        if show:
            notice.update(
                f"No sessions captured in {escape(self.launch_dir)}. "
                "Press ctrl+f to show every folder."
            )
        notice.display = show

    def action_toggle_scope(self) -> None:
        self.scope_cwd = not self.scope_cwd
        self.query_one("#scope", Button).label = self._scope_label()
        self._apply_filter(self.query_one("#filter", Input).value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # A clicked Button keeps focus, which would leave the filter dead to
        # every later keystroke — the same trap `_reload` guards against — so
        # both branches hand focus straight back. For the settings screen the
        # hand-back happens *before* push_screen: Textual restores focus to the
        # previously focused widget on dismiss, and that must be the filter,
        # not this button.
        if event.button.id == "scope":
            self.action_toggle_scope()
            self.query_one("#filter", Input).focus()
        elif event.button.id == "sort-open":
            self.action_open_sort()
        elif event.button.id == "settings-open":
            self.query_one("#filter", Input).focus()
            self.push_screen(
                SettingsScreen(self.scope_default, self.sort_default),
                self._save_settings,
            )

    def _save_settings(self, defaults: Settings | None) -> None:
        """Write the new startup defaults, if the dialog was saved rather than cancelled.

        None means cancel, so the stored settings are left untouched. Note this
        never touches `self.scope_cwd` or `self.sort_mode`: the dialog sets what
        those start as next launch, not what they are right now.
        """
        if defaults is None:
            return
        try:
            save_settings(
                self.vault_dir, scope_cwd=defaults.scope_cwd, sort=defaults.sort
            )
        except OSError as exc:
            # H4: a write that did not happen must say so, rather than let the
            # reader believe the preference stuck.
            self.notify(
                f"Could not save settings: {exc}",
                severity="error",
                title="Not saved",
            )
            return
        self.scope_default = defaults.scope_cwd
        self.sort_default = defaults.sort
        state = "on" if self.scope_default else "off"
        self.notify(
            f'Rewind will start with "only here" {state}, sorted by '
            f"{self.sort_default}",
            title="Settings",
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
    # After the TUI is down, never before it — see rewind/update.py.
    maybe_update_in_background()


if __name__ == "__main__":
    main()

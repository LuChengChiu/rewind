#!/usr/bin/env bash
# Install Rewind:
#   curl -fsSL https://raw.githubusercontent.com/LuChengChiu/rewind/main/install.sh | bash
set -euo pipefail

SOURCE="git+https://github.com/LuChengChiu/rewind@main"

red() { printf '\033[31m%s\033[0m\n' "$1" >&2; }
say() { printf '\033[2m%s\033[0m\n' "$1"; }

if ! command -v curl >/dev/null 2>&1; then
    red "curl is required."
    exit 1
fi

# Rewind is a Python app, so it needs uv to manage its interpreter and deps.
if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv (Rewind uses it to manage Python)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv just installed itself somewhere not yet on this shell's PATH. Mirror the
    # installer's own precedence rather than guessing: UV_INSTALL_DIR, then
    # XDG_BIN_HOME, then $XDG_DATA_HOME/../bin, then ~/.local/bin. Empty entries
    # are skipped -- an empty PATH element means the *current directory*.
    for dir in "${UV_INSTALL_DIR:-}" "${XDG_BIN_HOME:-}" \
               "${XDG_DATA_HOME:+$XDG_DATA_HOME/../bin}" "$HOME/.local/bin"; do
        [ -n "$dir" ] && [ -d "$dir" ] && PATH="$dir:$PATH"
    done
    export PATH
fi

if ! command -v uv >/dev/null 2>&1; then
    red "uv was installed but is not on PATH. Open a new shell and re-run this script."
    exit 1
fi

say "Installing rewind from $SOURCE …"
# --force so re-running the installer upgrades an existing install in place.
uv tool install --force "$SOURCE"
uv tool update-shell >/dev/null 2>&1 || true

if command -v rewind >/dev/null 2>&1; then
    printf '\033[32m✓ rewind installed.\033[0m Run it in your vault:  cd ~/session-vault && rewind\n'
else
    printf '\033[32m✓ rewind installed\033[0m to %s\n' "$(uv tool dir)"
    say "Add it to PATH (or open a new shell), then run: cd ~/session-vault && rewind"
fi

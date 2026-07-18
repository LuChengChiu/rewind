"""The Bitcoin DeFi palette, translated to what a terminal cell grid can render.

Single source of truth: PALETTE feeds both the Textual ``Theme`` (so every
``$token`` in the app CSS resolves to these values) and the harness badge
colors. The web-only parts of the design system — glow shadows, glass blur,
orbital rings, gradient text — have no cell-grid equivalent and are dropped.
"""

from __future__ import annotations

from textual.theme import Theme

PALETTE = {
    "void": "#030304",  # True Void      — Screen background
    "matter": "#0F1115",  # Dark Matter    — card / panel surface
    "light": "#FFFFFF",  # Pure Light     — primary text
    "stardust": "#94A3B8",  # Stardust       — muted text / metadata
    "orange": "#F7931A",  # Bitcoin Orange — accent, focus, hover
    "burnt": "#EA580C",  # Burnt Orange   — cwd / secondary
    "gold": "#FFD600",  # Digital Gold   — copied / success
    "hazard": "#F8523A",  # (added) loud red — BROKEN cards, per H4.
    # The design system carries no red; H4 demands BROKEN be impossible to
    # miss, so one hazard tone was added deliberately.
}

BITCOIN_DEFI = Theme(
    name="bitcoin-defi",
    primary=PALETTE["orange"],
    secondary=PALETTE["burnt"],
    accent=PALETTE["orange"],
    foreground=PALETTE["light"],
    background=PALETTE["void"],
    surface=PALETTE["matter"],
    panel=PALETTE["matter"],
    success=PALETTE["gold"],
    warning=PALETTE["burnt"],
    error=PALETTE["hazard"],
    boost="#F7931A22",  # faint orange lift for hover backgrounds
    dark=True,
    variables={
        # Pin muted text to Stardust rather than the auto-derived grey.
        "text-muted": PALETTE["stardust"],
    },
)

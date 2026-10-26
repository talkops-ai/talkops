# Copyright talkops.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Welcome banner & first-run onboarding widget for TalkOps TUI.

Displays a two-panel "launch console" card:
  Left  — TalkOps logo + brand stack (wordmark, tagline, user, version, health).
          The logo is the real PNG when the terminal speaks Sixel / Kitty
          graphics (see ``logo_image.py``), otherwise 20x8 quadrant block art
          with a travelling light beam whose colour reflects server health.
  Right — Environment (model, cloud, project) and capabilities (skills,
          agents, MCP) with health dots on rows that have a live state.

Health colour language (shared by the beam and the row dots):
  amber  → starting / needs attention
  green  → healthy / ready
  red    → error

Lists that exceed the available width show a clickable "+N more" span that
opens a WelcomeDetailPopup modal with the full list and descriptions.

The banner stays in the scrollback and never collapses.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import ConsoleRenderable, Group
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget

from talkops.ui.theme import DC_CYAN, DC_PURPLE, DC_TEAL, ThemeColors, get_theme_colors
from talkops.ui.widgets.logo_image import create_logo_image_widget
from talkops.ui.widgets.welcome_popup import WelcomeDetailPopup
from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from talkops.mcp.mcp_info import MCPServerInfo

logger = get_logger(__name__)

# Maximum number of items shown inline before truncating with "+N more".
MAX_DISPLAY_ITEMS = 5

# Maximum character width for the inline list before truncating.
# Applied after MAX_DISPLAY_ITEMS to catch long names that wrap.
MAX_INLINE_CHARS = 38

# Terminal width below which the two panels stack vertically.
_STACK_BELOW_WIDTH = 104

# Label column width in the info panel ("project  ").
_LABEL_W = 9

# Count column width in the capabilities section.
_COUNT_W = 4

# Fallback info-panel width when rendering before layout (e.g. in tests).
_DEFAULT_INFO_WIDTH = 56

# Brand colours for cloud provider tags.
_AWS_ORANGE = "#FF9900"
_CLOUD_COLORS: dict[str, str] = {
    "aws": _AWS_ORANGE,
    "azure": "#0078D4",
    "gcp": "#4285F4",
    "k8s": "#326CE5",
}


def _is_ascii() -> bool:
    try:
        from talkops.config.settings import is_ascii_mode

        return is_ascii_mode()
    except Exception:
        return False


def _dot() -> str:
    """Return the health-dot glyph (geometric, never an emoji)."""
    return "*" if _is_ascii() else "●"


def _sep() -> str:
    return "|" if _is_ascii() else "·"


# ── Colour helpers ────────────────────────────────────────────────


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _blend(a: str, b: str, t: float) -> str:
    """Linear blend from colour ``a`` (t=0) to colour ``b`` (t=1)."""
    ra, ga, ba = _hex_to_rgb(a)
    rb, gb, bb = _hex_to_rgb(b)
    r = round(ra + (rb - ra) * t)
    g = round(ga + (gb - ga) * t)
    bl = round(ba + (bb - ba) * t)
    return f"#{r:02X}{g:02X}{bl:02X}"


def _gradient(stops: Sequence[str], t: float) -> str:
    """Sample a multi-stop gradient at position ``t`` in [0, 1]."""
    if t <= 0:
        return stops[0]
    if t >= 1:
        return stops[-1]
    span = len(stops) - 1
    idx = min(int(t * span), span - 1)
    local = t * span - idx
    return _blend(stops[idx], stops[idx + 1], local)


# ── TalkOps Brand Logo (Icon + Wordmark + Tagline + Authenticated User) ───
# Mirrors the TalkOps logo from talkops-agents-docs (talkops-logo-dark.svg):
#   - Speech bubble outline (#1F7ACB)
#   - Intertwined infinity wave (#1F7ACB blue + #7AC143 green)
#   - Wordmark "TalkOps.ai" (#FFFFFF) + version + tagline "DevOps on Demand" (#F3F4F6)
#   - Authenticated Control Plane user identity & license tier badge

_LOGO_BLUE = "#1F7ACB"
_LOGO_GREEN = "#7AC143"
_LOGO_WORDMARK = "#FFFFFF"
_LOGO_VERSION = "#22D3EE"
_LOGO_TAGLINE = "#94A3B8"
_LOGO_USER = "#CBD5E1"

# Block-art FALLBACK icon. Terminals that support Sixel / Kitty graphics show the
# real PNG instead (see ``talkops.ui.widgets.logo_image``); this art is used
# everywhere else (Terminal.app, Alacritty, Warp, VS Code without images, tests…).
#
# 40x16 2x2-quadrant sub-pixel canvas -> 20 cols x 8 terminal rows using
# Unicode Block Elements U+2580..U+259F: ▘ ▝ ▀ ▖ ▌ ▞ ▛ ▗ ▚ ▐ ▜ ▄ ▙ ▟ █
# Hand-tuned against a rasterisation of talkops-logo-dark.svg:
#   - horizontal strokes are 1 sub-pixel tall (= half a cell, ~ the SVG stroke)
#   - vertical strokes are 2 sub-pixels wide (= one full cell) so side walls
#     render as solid ``█`` with no line-spacing seams
#   - the infinity renders as ``▄▀▀▀▄▄▀▀▀▄`` / ``▀▄▄▄▀▀▄▄▄▀`` with the brand
#     colour split: top-left + bottom-right lobes blue, the other two green
#   'B' -> TalkOps Blue (#1F7ACB), 'G' -> TalkOps Green (#7AC143), '.' -> transparent
_ICON_SUBPIXELS: tuple[str, ...] = (
    "............BBBBBBBBBBBBBBBB............",  # y=0  top of bubble
    ".......BBBBB................BBBBB.......",  # y=1
    "....BBBB........................BBBB....",  # y=2
    "..BBB..............................BBB..",  # y=3
    ".BB..................................BB.",  # y=4
    "BB....................................BB",  # y=5  side walls
    "BB..........BBBBBB....GGGGGG..........BB",  # y=6  infinity top arches
    "BB........BB......BBGG......GG........BB",  # y=7  infinity sides + crossing
    "BB........GG......GGBB......BB........BB",  # y=8  infinity sides + crossing
    ".BB.........GGGGGG....BBBBBB.........BB.",  # y=9  infinity bottom arches
    "..BBB..............................BBB..",  # y=10
    "....BBBB........................BBBB....",  # y=11
    "......BBBB.....................BBB......",  # y=12
    "........BB...BBBBBBBBBBBBBBBBBB.........",  # y=13 tail wall + bottom of bubble
    "........BB.BBB..........................",  # y=14 tail diagonal
    "........BBB.............................",  # y=15 tail tip
)

_QUAD_GLYPHS: tuple[str, ...] = (
    " ", "▘", "▝", "▀",
    "▖", "▌", "▞", "▛",
    "▗", "▚", "▐", "▜",
    "▄", "▙", "▟", "█",
)

_ICON_COLS = 20
_ICON_ROWS = 8


def _subpixels_to_glyph_rows(subpixels: Sequence[str]) -> tuple[str, ...]:
    """Collapse a 2x2 sub-pixel canvas into plain quadrant-glyph rows (colour-less)."""
    rows: list[str] = []
    for top, bot in zip(subpixels[0::2], subpixels[1::2]):
        line = ""
        for col in range(len(top) // 2):
            quads = (top[2 * col], top[2 * col + 1], bot[2 * col], bot[2 * col + 1])
            mask = sum(bit for bit, q in zip((1, 2, 4, 8), quads) if q != ".")
            line += _QUAD_GLYPHS[mask]
        rows.append(line)
    return tuple(rows)


_ICON_UNICODE: tuple[str, ...] = _subpixels_to_glyph_rows(_ICON_SUBPIXELS)

# ASCII-mode icon (exactly _ICON_COLS wide) + matching colour mask.
_ICON_ASCII: tuple[str, ...] = (
    "    .----------.    ",
    "  .'            '.  ",
    " /                \\ ",
    "|    .--.  .--.    |",
    "|   (    ><    )   |",
    " \\   '--'  '--'   / ",
    "  '.  .---------'   ",
    "    |/              ",
)
_ICON_ASCII_COLORS: tuple[str, ...] = (
    "    BBBBBBBBBBBB    ",
    "  BB            BB  ",
    " B                B ",
    "B    BBBB  GGGG    B",
    "B   B    GG    G   B",
    " B   GGGG  BBBB   B ",
    "  BB  BBBBBBBBBBB   ",
    "    BB              ",
)

# ── Wordmark lockup ──────────────────────────────────────────────────────
# Terminals have a single font size, so the brand name is drawn as 3-row
# quadrant block letters (6 sub-pixel rows) to make it ~3x the height of the
# tagline — the same name/tagline scale relationship as talkops-logo-dark.svg
# (60px wordmark vs 30px tagline). Glyph rules: stems are 2 sub-pixels wide
# (one full cell, so they render as seam-free ``█``), horizontals 1 sub-pixel,
# caps/ascenders use rows 0-5, x-height uses rows 1-5 (5 px, enough for a
# legible double-storey 'a' and 's').
_WORDMARK_TEXT = "TalkOps.ai"
_WORDMARK_FONT: dict[str, tuple[str, ...]] = {
    "T": ("######", "..##..", "..##..", "..##..", "..##..", "..##.."),
    "a": ("......", "#####.", "....##", ".#####", "##..##", ".#####"),
    "l": ("##", "##", "##", "##", "##", "##"),
    "k": ("##....", "##..##", "##.##.", "####..", "##.##.", "##..##"),
    "O": (".####.", "##..##", "##..##", "##..##", "##..##", ".####."),
    "p": ("......", "#####.", "##..##", "##..##", "#####.", "##...."),
    "s": ("......", ".#####", "##....", ".####.", "....##", "#####."),
    ".": ("..", "..", "..", "..", "..", "##"),
    "i": ("##", "..", "##", "##", "##", "##"),
}
_WORDMARK_GAP = 2  # sub-pixels between letters (one cell keeps stems cell-aligned)
_WORDMARK_ROWS = 3


def _build_wordmark_subpixels(text: str) -> tuple[str, ...]:
    """Lay out ``text`` with ``_WORDMARK_FONT`` as a 6-row sub-pixel canvas ('W' = ink)."""
    rows = [""] * (_WORDMARK_ROWS * 2)
    for idx, ch in enumerate(text):
        glyph = _WORDMARK_FONT[ch]
        gap = "." * _WORDMARK_GAP if idx < len(text) - 1 else ""
        for r in range(len(rows)):
            rows[r] += glyph[r].replace("#", "W") + gap
    width = len(rows[0]) + (len(rows[0]) % 2)  # pad to whole cells
    return tuple(row.ljust(width, ".") for row in rows)


_WORDMARK_SUBPIXELS: tuple[str, ...] = _build_wordmark_subpixels(_WORDMARK_TEXT)
_WORDMARK_COLS = len(_WORDMARK_SUBPIXELS[0]) // 2  # 33 cells

# Tagline set as letter-spaced caps under the wordmark (classic logo lockup):
# small + wide + muted contrasts with big + bold + bright.
_TAGLINE_TEXT = "DevOps on Demand"
_TAGLINE_LOCKUP = " ".join(_TAGLINE_TEXT.upper())
_LOGO_RULE = "#1B2436"

_LOGO_WIDTH = 56
_MAX_USER_DISPLAY_CHARS = 36

_BRAND_STOPS: tuple[str, ...] = (DC_TEAL, DC_CYAN, DC_PURPLE)

# Pre-computed per-column brand gradient for fallback column lookups.
_BRAND_COLUMN_COLORS: list[str] = [
    _gradient(_BRAND_STOPS, x / max(_LOGO_WIDTH - 1, 1)) for x in range(_LOGO_WIDTH)
]


def _format_tier_label(raw_tier: str) -> str:
    """Format a Control Plane license/user tier for display in parentheses."""
    cleaned = (raw_tier or "").strip()
    if not cleaned:
        return ""
    return cleaned.replace("_", " ").replace("-", " ").title()


def _format_user_display(user_identity: str, user_tier: str = "") -> str:
    """Format ``email (Tier)`` for the welcome banner, fitting within banner width."""
    identity = (user_identity or "").strip()
    if not identity:
        return ""
    tier_label = _format_tier_label(user_tier)
    if tier_label and not identity.endswith(")"):
        full = f"{identity} ({tier_label})"
    else:
        full = identity
    if len(full) > _MAX_USER_DISPLAY_CHARS:
        if tier_label and not identity.endswith(")"):
            suffix = f" ({tier_label})"
            avail = max(_MAX_USER_DISPLAY_CHARS - len(suffix) - 1, 4)
            if avail + 1 + len(suffix) <= _MAX_USER_DISPLAY_CHARS:
                return f"{identity[:avail]}…{suffix}"
        return full[: _MAX_USER_DISPLAY_CHARS - 1] + "…"
    return full


def _collect_auth_user() -> tuple[str, str]:
    """Return ``(user_identity, tier_label)`` from the Control Plane session if authenticated."""
    try:
        from talkops.auth.models import StoredSession
        from talkops.auth.storage import get_credential_storage
        from talkops.state.remote_session import _can_use_disk_auth_session

        storage = get_credential_storage()
        is_custom_storage = type(storage).__name__ != "CompositeCredentialStorage"
        if _can_use_disk_auth_session() or is_custom_storage:
            session: StoredSession | None = None
            if hasattr(storage, "_read_auth_json") and not is_custom_storage:
                data = storage._read_auth_json()
                raw_session = data.get("session")
                if isinstance(raw_session, dict) and raw_session:
                    session = StoredSession.from_dict(raw_session)
            else:
                session = storage.load_session()

            if session is not None:
                has_auth = bool(
                    session.api_key
                    or session.api_key_prefix
                    or session.access_token
                    or session.email
                    or session.user_id
                    or os.environ.get("TALKOPS_API_KEY")
                    or os.environ.get("TALKOPS_SERVER_API_KEY")
                )
                if has_auth:
                    identity = (
                        (session.email or "").strip()
                        or (session.user_id or "").strip()
                        or os.environ.get("TALKOPS_USER_ID", "").strip()
                        or (session.api_key_prefix or "").strip()
                    )
                    if identity:
                        return identity, _format_tier_label(session.user_tier)

        env_user = os.environ.get("TALKOPS_USER_ID", "").strip()
        has_env_key = bool(
            os.environ.get("TALKOPS_API_KEY", "").strip()
            or os.environ.get("TALKOPS_SERVER_API_KEY", "").strip()
        )
        if env_user and has_env_key:
            return env_user, ""
    except Exception:
        logger.debug("Could not read Control Plane session for welcome banner", exc_info=True)
    return "", ""


def _beam_override_color(dist: int, status: str) -> str | None:
    """Return the light-beam highlight colour when ``dist`` is within the beam radius."""
    if dist > 1:
        return None
    if dist == 0:
        return "#FFFFFF"
    return _beam_accent(status)


def _beam_accent(status: str) -> str:
    if status == "connected":
        return "#34D399"
    if status in ("starting", "connecting", "warning", "degraded", "logged_out"):
        return "#FBBF24"
    return "#FB7185"


def _wordmark_beam_color(dist: int, status: str, base: str) -> str | None:
    """Beam over the (already white) wordmark: tint toward the status accent instead."""
    if dist > 1:
        return None
    accent = _beam_accent(status)
    return accent if dist == 0 else _blend(base, accent, 0.45)


def _is_light_background(color: str) -> bool:
    try:
        r, g, b = _hex_to_rgb(color)
    except (ValueError, IndexError):
        return False
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 > 0.6


def _append_quadrant_row(
    t: Text,
    top_sub: str,
    bot_sub: str,
    color_for: Any,
) -> None:
    """Append one terminal row of 2x2 quadrant cells built from two sub-pixel rows.

    ``color_for(code, col_idx)`` maps a sub-pixel code to a colour (``None`` =
    transparent). Partial cells use a foreground-only glyph; solid single-colour
    cells set the background too so they fill the terminal's line-spacing pads.
    """
    for col_idx in range(len(top_sub) // 2):
        q_codes = (
            top_sub[2 * col_idx],
            top_sub[2 * col_idx + 1],
            bot_sub[2 * col_idx],
            bot_sub[2 * col_idx + 1],
        )
        q_cols = tuple(color_for(c, col_idx) for c in q_codes)
        non_empty = [c for c in q_cols if c is not None]
        if not non_empty:
            t.append(" ")
        elif None in q_cols:
            fg_color = non_empty[0]
            mask = sum(bit for bit, c in zip((1, 2, 4, 8), q_cols) if c is not None)
            t.append(_QUAD_GLYPHS[mask], style=f"bold {fg_color}")
        else:
            fg_color = q_cols[0]
            assert fg_color is not None
            other_colors = [c for c in q_cols if c != fg_color]
            if not other_colors:
                # Set both fg and bg so full blocks span 100% of terminal line-height with zero gap
                t.append("█", style=f"bold {fg_color} on {fg_color}")
            else:
                mask = sum(bit for bit, c in zip((1, 2, 4, 8), q_cols) if c == fg_color)
                t.append(_QUAD_GLYPHS[mask], style=f"bold {fg_color} on {other_colors[0]}")


def _status_label_and_color(status: str, colors: ThemeColors) -> tuple[str, str]:
    """Return ``(label, color)`` for a banner connection status string."""
    return {
        "connected": ("ready", colors.success),
        "error": ("startup failed", colors.error),
        "warning": ("degraded", colors.warning),
        "degraded": ("degraded", colors.warning),
        "logged_out": ("logged out", colors.warning),
        "starting": ("connecting", colors.warning),
        "connecting": ("connecting", colors.warning),
    }.get(status, ("connecting", colors.warning))


def _collect_server_url(explicit_url: str | None = None) -> str:
    """Return the active Central Server URL if connected remotely, else ``""``."""
    raw = (explicit_url or "").strip().rstrip("/")
    if not raw:
        try:
            from talkops.state import remote_session

            raw = (getattr(remote_session, "_remote_server_url", None) or "").strip().rstrip("/")
        except Exception:
            raw = ""
    if not raw:
        raw = os.environ.get("TALKOPS_SERVER_URL", "").strip().rstrip("/")
    if raw.startswith("inprocess://"):
        return ""
    return raw


def _format_server_url_display(server_url: str, max_chars: int = 25) -> str:
    """Format a Central Server URL to fit within the left-panel session stack."""
    cleaned = (server_url or "").strip().rstrip("/")
    if not cleaned:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned
    # Strip scheme if needed to fit nicely before truncating
    for prefix in ("https://", "http://"):
        if cleaned.startswith(prefix):
            stripped = cleaned[len(prefix) :]
            if len(stripped) <= max_chars:
                return stripped
            return stripped[: max_chars - 1] + "…"
    return cleaned[: max_chars - 1] + "…"


@functools.lru_cache(maxsize=128)
def _build_logo_text_cached(
    beam_x: int,
    status: str,
    resolved_user: str,
    panel_bg: str,
    status_label: str,
    status_color: str,
    show_icon: bool,
    ascii_mode: bool,
    ver: str,
    resolved_server_url: str = "",
) -> Text:
    """Cached implementation of the TalkOps logo + brand lockup + session stack."""
    ver_clean = ver.lstrip("v") if ver else ""
    ver_str = f"v{ver_clean}" if ver_clean else ""
    dot = _dot()
    sep = _sep()
    is_light = _is_light_background(panel_bg)
    wordmark_color = "#0F172A" if is_light else _LOGO_WORDMARK
    tagline_color = "#475569" if is_light else _LOGO_TAGLINE
    rule_color = "#CBD5E1" if is_light else _LOGO_RULE
    user_color = "#334155" if is_light else _LOGO_USER
    version_color = "#0369A1" if is_light else _LOGO_VERSION
    stack_x = _ICON_COLS + 2 if show_icon else 0  # beam column where the stack starts

    # ── Right-hand stack: one list of (text, style) segments per row ──
    rows: list[list[tuple[str, str]] | None] = [None] * _ICON_ROWS
    tagline_pad = " " * max((_WORDMARK_COLS - len(_TAGLINE_LOCKUP)) // 2, 0)
    if ascii_mode:
        rows[1] = [(_WORDMARK_TEXT, f"bold {wordmark_color}")]
        rows[3] = [(_TAGLINE_TEXT, tagline_color)]
        rows[4] = [("-" * _WORDMARK_COLS, rule_color)]
    else:
        rows[3] = [(tagline_pad + _TAGLINE_LOCKUP, tagline_color)]
        rows[4] = [("─" * _WORDMARK_COLS, rule_color)]

    session_rows: list[list[tuple[str, str]]] = []
    if resolved_user:
        session_rows.append([(resolved_user, user_color)])
    health: list[tuple[str, str]] = []
    sep_pad = f" {sep} " if len(status_label) > 10 else f"  {sep}  "
    if ver_str:
        health.append((ver_str, version_color))
    if status_label:
        if health:
            health.append((sep_pad, rule_color))
        health.append((f"{dot} {status_label}", f"bold {status_color}"))
    if health:
        health.append((sep_pad, rule_color))
        if resolved_server_url:
            health.append(("central", f"bold {version_color}"))
        else:
            health.append(("local", tagline_color))
        session_rows.append(health)
    if resolved_server_url:
        url_display = _format_server_url_display(resolved_server_url)
        session_rows.append(
            [
                ("server", tagline_color),
                (f"  {sep}  ", rule_color),
                (url_display, user_color),
            ]
        )
    for offset, segs in enumerate(session_rows):
        if 5 + offset < _ICON_ROWS:
            rows[5 + offset] = segs

    def _icon_color(code: str, col_idx: int) -> str | None:
        if code not in ("B", "G"):
            return None
        beam_col = _beam_override_color(abs(col_idx - beam_x), status)
        if beam_col is not None:
            return beam_col
        return _LOGO_GREEN if code == "G" else _LOGO_BLUE

    def _wordmark_color(code: str, col_idx: int) -> str | None:
        if code != "W":
            return None
        beam = _wordmark_beam_color(abs(stack_x + col_idx - beam_x), status, wordmark_color)
        return beam or wordmark_color

    t = Text(no_wrap=True, overflow="ellipsis")
    for row_idx in range(_ICON_ROWS):
        # 1. 20-column x 8-row icon (skipped when the real PNG is shown instead).
        if not show_icon:
            pass
        elif ascii_mode:
            icon_line = _ICON_ASCII[row_idx]
            color_line = _ICON_ASCII_COLORS[row_idx]
            for col_idx, ch in enumerate(icon_line):
                if ch == " ":
                    t.append(" ")
                else:
                    beam_col = _beam_override_color(abs(col_idx - beam_x), status)
                    base_col = _LOGO_GREEN if color_line[col_idx] == "G" else _LOGO_BLUE
                    t.append(ch, style=f"bold {beam_col or base_col}")
        else:
            _append_quadrant_row(
                t, _ICON_SUBPIXELS[2 * row_idx], _ICON_SUBPIXELS[2 * row_idx + 1], _icon_color
            )

        # 2. Brand lockup + session stack.
        is_wordmark_row = not ascii_mode and row_idx < _WORDMARK_ROWS
        segs = rows[row_idx]
        if is_wordmark_row or segs:
            if show_icon:
                t.append("  ")
            if is_wordmark_row:
                _append_quadrant_row(
                    t,
                    _WORDMARK_SUBPIXELS[2 * row_idx],
                    _WORDMARK_SUBPIXELS[2 * row_idx + 1],
                    _wordmark_color,
                )
            for text_val, text_style in segs or ():
                t.append(text_val, style=text_style)

        if row_idx < _ICON_ROWS - 1:
            t.append("\n")
    return t


def _build_logo_text(
    beam_x: int = -10,
    status: str = "connected",
    *,
    user_display: str | None = None,
    panel_bg: str = "#07090E",
    status_label: str | None = None,
    status_color: str | None = None,
    show_icon: bool = True,
    server_url: str | None = None,
) -> Text:
    """Return the TalkOps logo + brand lockup + session stack with light beam.

    Left: the 40x16 sub-pixel icon rendered as a 20x8 grid of 2x2 Unicode
    quadrant blocks (``#1F7ACB`` speech bubble + blue/``#7AC143`` infinity).
    Right, top to bottom (8 rows, aligned with the icon):

      rows 0-2  ``TalkOps.ai`` as 3-row block letters (bold wordmark colour)
      row  3    ``D E V O P S   O N   D E M A N D`` letter-spaced tagline,
                centred under the wordmark (muted ``#94A3B8``)
      row  4    thin rule separating the brand lockup from session info
      row  5    authenticated Control Plane user & tier (``#CBD5E1``)
      row  6    ``vX.Y.Z  ·  ● ready  ·  local/central`` version + live health + mode
      row  7    ``server  ·  http://...`` when connected to a Central Server

    The light beam sweeps across the icon and then the wordmark. With
    ``show_icon=False`` only the right-hand stack is returned; the banner uses
    this when a graphics-protocol widget displays the real PNG icon.
    """
    ascii_mode = _is_ascii()
    ver = _version()
    if user_display is None:
        uid, utier = _collect_auth_user()
        resolved_user = _format_user_display(uid, utier)
    else:
        resolved_user = _format_user_display(user_display)

    resolved_server_url = _collect_server_url(server_url)

    if status_label is None or status_color is None:
        colors = get_theme_colors(None)
        default_label, default_color = _status_label_and_color(status, colors)
        status_label = status_label or default_label
        status_color = status_color or default_color

    # Normalize off-canvas negative beam positions so they share one static cache entry
    effective_beam_x = -10 if beam_x < -4 else beam_x
    return _build_logo_text_cached(
        effective_beam_x,
        status,
        resolved_user,
        panel_bg,
        status_label,
        status_color,
        show_icon,
        ascii_mode,
        ver,
        resolved_server_url,
    )


# ── Data collection ───────────────────────────────────────────────


def _collect_skills() -> list[tuple[str, str]]:
    """Return (name, description) for all discovered skills."""
    try:
        import talkops
        from talkops.config.settings import settings
        from talkops.skills.loader import list_skills

        built_in_dir = Path(talkops.__file__).parent / "built_in_skills"
        user_skills_dir = settings.get_user_skills_dir()
        project_skills_dir = settings.get_project_skills_dir()
        project_root = settings.project_root

        skills = list_skills(
            built_in_skills_dir=built_in_dir,
            user_skills_dir=user_skills_dir,
            project_skills_dir=project_skills_dir,
            include_plugins=True,
            project_root=project_root,
        )
        return sorted(
            [(s.get("name", ""), s.get("description", "")) for s in skills if s.get("name")],
            key=lambda x: x[0],
        )
    except Exception:
        logger.debug("Could not discover skills for welcome banner", exc_info=True)
        return []


def _collect_subagents() -> list[tuple[str, str]]:
    """Return (name, description) for all loaded subagents."""
    try:
        from talkops.config.settings import settings
        from talkops.subagents import list_subagents

        effective_root = settings.effective_project_root
        user_dir = settings.get_user_agents_dir() if hasattr(settings, "get_user_agents_dir") else None
        proj_dir = settings.get_project_agents_dir() if hasattr(settings, "get_project_agents_dir") else None

        metas = list_subagents(
            user_agents_dir=user_dir,
            project_agents_dir=proj_dir,
            project_root=effective_root,
            include_plugins=True,
        )
        seen = {meta["name"]: meta.get("description", "") for meta in metas if meta.get("name")}
        return sorted(seen.items(), key=lambda x: x[0])
    except Exception:
        logger.debug("Could not discover subagents for welcome banner", exc_info=True)
        return []


def _project_display() -> str:
    """Return the project directory with the home prefix collapsed to ``~``."""
    try:
        from talkops.config.settings import settings

        root = Path(getattr(settings, "project_root", None) or Path.cwd())
    except Exception:
        root = Path.cwd()
    text = str(root)
    home = os.path.expanduser("~")
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text


def _version() -> str:
    try:
        from talkops._version import __version__

        return __version__
    except Exception:
        return ""


def _truncate_to_width(
    names: list[str], max_chars: int, max_items: int,
) -> tuple[list[str], int]:
    """Return (shown_names, remaining_count) fitting within both limits.

    Truncates by *item count* first (MAX_DISPLAY_ITEMS) then by cumulative
    *character width* so long agent names don't wrap the line.
    """
    candidates = names[:max_items]
    result: list[str] = []
    current_len = 0
    for name in candidates:
        sep = 2 if result else 0  # ", " separator
        if current_len + sep + len(name) > max_chars and result:
            break
        result.append(name)
        current_len += sep + len(name)
    remaining = len(names) - len(result)
    return result, remaining


# ── Banner state ──────────────────────────────────────────────────


@dataclass
class _BannerInfo:
    """Everything the info panel renders. Mutated in place by the banner."""

    model_spec: str = ""
    effort: str = ""
    cloud_provider: str = "aws"
    cloud_profile: str = ""
    cloud_region: str = ""
    project: str = ""
    skills: list[tuple[str, str]] = field(default_factory=list)
    subagents: list[tuple[str, str]] = field(default_factory=list)
    mcp_servers: list[MCPServerInfo] | None = None
    """``None`` while discovery is still running; ``[]`` when none configured."""
    mcp_disabled: bool = False


def _health_style(state: str, colors: ThemeColors) -> str:
    return {
        "ok": colors.success,
        "warn": colors.warning,
        "error": colors.error,
    }.get(state, colors.muted)


def _fit(text: Text, width: int) -> Text:
    """Truncate ``text`` to ``width`` cells with an ellipsis."""
    if width <= 0:
        return Text("")
    if text.cell_len > width:
        text = text.copy()
        text.truncate(width, overflow="ellipsis")
    return text


def _short_name(name: str) -> str:
    """Strip plugin/marketplace qualifiers for compact inline display.

    ``aws-compute@talkops-devops-plugins:amazon-ec2-image-builder``
    becomes ``amazon-ec2-image-builder``; ``agent@marketplace`` becomes
    ``agent``. The full identifier is still shown in the detail popup.
    """
    if ":" in name:
        tail = name.rsplit(":", 1)[1]
        if tail:
            return tail
    if "@" in name:
        head = name.split("@", 1)[0]
        if head:
            return head
    return name


def _name_list(
    names: list[str],
    width: int,
    name_style: str,
    more_style: Style,
) -> Text:
    """Comma-joined names that fit ``width``, with a clickable ``+N more``."""
    out = Text()
    if not names:
        return out
    shown: list[str] = []
    used = 0
    for idx, name in enumerate(names):
        remaining_after = len(names) - (idx + 1)
        more_len = len(f"  +{remaining_after} more") if remaining_after else 0
        sep = 2 if shown else 0
        if used + sep + len(name) + more_len > width:
            break
        shown.append(name)
        used += sep + len(name)
    if not shown:
        # Not even one full name fits: show a truncated first name.
        remaining = len(names) - 1
        more_len = len(f"  +{remaining} more") if remaining else 0
        first = _fit(Text(names[0], style=name_style), max(width - more_len, 4))
        out.append_text(first)
        shown = [names[0]]
    else:
        for i, name in enumerate(shown):
            if i:
                out.append(", ", style=name_style)
            out.append(name, style=name_style)
    remaining = len(names) - len(shown)
    if remaining:
        out.append("  ")
        out.append(f"+{remaining} more", style=more_style)
    return out


def _build_info_text(
    info: _BannerInfo,
    *,
    width: int,
    status: str,
    colors: ThemeColors,
) -> Text:
    """Render the environment + capabilities panel as a single ``Text``."""
    dot = _dot()
    sep = _sep()
    label_style = _blend(colors.foreground, colors.background, 0.45)
    header_style = f"bold {colors.primary}"
    value_style = colors.foreground
    secondary_style = _blend(colors.foreground, colors.background, 0.35)

    value_w = max(width - 2 - _LABEL_W, 8)
    out = Text(no_wrap=True, overflow="ellipsis")

    def header(title: str) -> None:
        """Execute header."""
        out.append(title, style=header_style)
        out.append("\n")

    def row(health: str | None, label: str, value: Text) -> None:
        """Execute row."""
        if health is None:
            out.append("  ")
        else:
            out.append(f"{dot} ", style=_health_style(health, colors))
        out.append(f"{label:<{_LABEL_W}}", style=label_style)
        out.append_text(_fit(value, value_w))
        out.append("\n")

    # ── Environment ────────────────────────────────────────────
    header("Environment")

    # Model — health follows server connection state.
    model_health = {"connected": "ok", "error": "error"}.get(status, "warn")
    model_val = Text()
    if info.model_spec:
        provider, _, model = info.model_spec.partition(":")
        if not model:
            provider, model = "", provider
        if provider:
            model_val.append(f"{provider}:", style=secondary_style)
        model_val.append(model, style=f"bold {value_style}")
        if info.effort:
            model_val.append(f"  {info.effort}", style=colors.skill)
    else:
        model_health = "warn"
        model_val.append("not configured", style=colors.warning)
        model_val.append(f"  {sep}  /model to choose", style=secondary_style)
    row(model_health, "model", model_val)

    # Cloud — healthy when a profile/target is active.
    cloud_val = Text()
    if info.cloud_profile:
        cloud_health = "ok"
        prov_tag = (info.cloud_provider or "aws").lower()
        prov_color = _CLOUD_COLORS.get(prov_tag, _AWS_ORANGE)
        cloud_val.append(f"{prov_tag} ", style=f"bold {prov_color}")
        cloud_val.append(info.cloud_profile, style=value_style)
        if info.cloud_region:
            cloud_val.append(f" {sep} {info.cloud_region}", style=secondary_style)
    else:
        cloud_health = "warn"
        cloud_val.append("no profile", style=colors.warning)
        cloud_val.append(f"  {sep}  /cloud to select", style=secondary_style)
    row(cloud_health, "cloud", cloud_val)

    # Project — static, no health.
    project = info.project or _project_display()
    if len(project) > value_w:
        project = "…" + project[-(value_w - 1):]
    row(None, "project", Text(project, style=secondary_style))

    out.append("\n")

    # ── Capabilities ───────────────────────────────────────────
    header("Capabilities")
    list_w = max(value_w - _COUNT_W, 8)

    def count_cell(n: int) -> Text:
        """Count cell."""
        return Text(f"{n:<{_COUNT_W}}", style=f"bold {value_style}")

    skills_val = count_cell(len(info.skills))
    if info.skills:
        skills_val.append_text(
            _name_list(
                [_short_name(n) for n, _ in info.skills],
                list_w,
                colors.skill,
                Style(underline=True, color=colors.secondary, meta={"@click": "show_skills_popup()"}),
            )
        )
    else:
        skills_val.append("none discovered", style=secondary_style)
    row(None, "skills", skills_val)

    agents_val = count_cell(len(info.subagents))
    if info.subagents:
        agents_val.append_text(
            _name_list(
                [_short_name(n) for n, _ in info.subagents],
                list_w,
                colors.secondary,
                Style(underline=True, color=colors.secondary, meta={"@click": "show_agents_popup()"}),
            )
        )
    else:
        agents_val.append("none loaded", style=secondary_style)
    row(None, "agents", agents_val)

    # MCP — health reflects server probe results.
    if info.mcp_disabled:
        mcp_health: str | None = None
        mcp_val = Text("disabled", style=secondary_style)
    elif info.mcp_servers is None:
        mcp_health = "warn"
        mcp_val = Text(f"{'':<{_COUNT_W}}", style=value_style)
        mcp_val.append("discovering servers…", style=secondary_style)
    elif not info.mcp_servers:
        mcp_health = None
        mcp_val = count_cell(0)
        mcp_val.append("none configured", style=secondary_style)
    else:
        active = [s for s in info.mcp_servers if s.enabled]
        attention = [s for s in active if s.needs_attention()]
        tools = sum(s.tool_count for s in active if not s.needs_attention())
        mcp_health = "warn" if attention else "ok"
        mcp_val = count_cell(len(active))
        mcp_val.append("servers", style=secondary_style)
        mcp_val.append(f" {sep} ", style=secondary_style)
        mcp_val.append(str(tools), style=f"bold {value_style}")
        mcp_val.append(" tools", style=secondary_style)
        if attention:
            mcp_val.append(f" {sep} ", style=secondary_style)
            mcp_val.append(f"{len(attention)} need attention", style=colors.warning)
    row(mcp_health, "mcp", mcp_val)

    out.rstrip()
    return out


# ── Left panel widget ─────────────────────────────────────────────


class _LeftPanel(Widget):
    """Reactive left panel: animated TalkOps logo + authenticated user + health line.

    The ``status`` reactive property drives the health colour and
    light-beam animation:
    ``"starting"``  → warm amber pulse, amber dot.
    ``"connected"`` → green beam over the brand gradient, green dot.
    ``"error"``     → red alert scan, red dot.
    """

    DEFAULT_CSS = """
    _LeftPanel {
        width: 60;
        height: auto;
        padding: 1 0 0 1;
    }
    """

    status: reactive[str] = reactive("starting")
    beam_x: reactive[int] = reactive(-6)

    def __init__(
        self,
        *,
        user_display: str | None = None,
        show_icon: bool = True,
        server_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize _LeftPanel."""
        super().__init__(**kwargs)
        self._cycle_count = 0
        self._anim_timer: Timer | None = None
        # False when a graphics-protocol widget shows the real PNG icon beside us;
        # the panel then renders only the wordmark lockup + session stack.
        self._show_icon = show_icon
        self._server_url: str | None = server_url
        if user_display is None:
            uid, utier = _collect_auth_user()
            self._user_display: str = _format_user_display(uid, utier)
        else:
            self._user_display = _format_user_display(user_display)

    def refresh_user(self, user_display: str | None = None) -> None:
        """Refresh the cached Control Plane user display string."""
        if user_display is None:
            uid, utier = _collect_auth_user()
            self._user_display = _format_user_display(uid, utier)
        else:
            self._user_display = _format_user_display(user_display)
        self.refresh()

    def refresh_server(self, server_url: str | None = None) -> None:
        """Refresh the cached Central Server URL display string."""
        self._server_url = server_url
        self.refresh()

    def on_mount(self) -> None:
        """Start the light-beam travelling animation (icon + wordmark)."""
        self._anim_timer = self.set_interval(0.08, self._advance_beam)

    def on_unmount(self) -> None:
        """Stop animation timer on unmount."""
        if self._anim_timer is not None:
            self._anim_timer.stop()
            self._anim_timer = None

    def _advance_beam(self) -> None:
        """Advance the light beam position from left to right."""
        next_x = self.beam_x + 2
        # Icon (20) + gap (2) + wordmark (33) = 55 cols. Travel past the end (up to 64)
        # for a short pause between sweeps.
        if next_x > 64:
            self.beam_x = -6
            self._cycle_count += 1
            # Once connected and initial welcome sweeps have completed, pause the 80ms
            # timer so idle sessions consume 0.0% CPU and zero GC pressure.
            if self.status == "connected" and self._cycle_count >= 4 and self._anim_timer is not None:
                self._anim_timer.pause()
        else:
            self.beam_x = next_x
        if not self.is_attached:
            self.refresh()

    def _health_line(self) -> Text:
        """Render the status indicator line (e.g. ``● ready``)."""
        colors = get_theme_colors(self) if self.is_attached else get_theme_colors(None)
        label, color = _status_label_and_color(self.status, colors)

        line = Text(no_wrap=True)
        line.append(f"{_dot()} ", style=color)
        line.append(label, style=f"bold {color}")
        return line

    def render(self) -> Group:
        """Build the left-panel content."""
        colors = get_theme_colors(self) if self.is_attached else get_theme_colors(None)
        status_label, status_color = _status_label_and_color(self.status, colors)
        logo = _build_logo_text(
            beam_x=self.beam_x,
            status=self.status,
            user_display=self._user_display,
            panel_bg=colors.background,
            status_label=status_label,
            status_color=status_color,
            show_icon=self._show_icon,
            server_url=self._server_url,
        )
        return Group(logo)

    def watch_status(self, _old: str, new: str) -> None:
        """Auto-refresh when status changes and ensure animation timer is active."""
        self._cycle_count = 0
        if self._anim_timer is None and self.is_attached:
            try:
                self._anim_timer = self.set_interval(0.08, self._advance_beam)
            except Exception:
                pass
        elif self._anim_timer is not None:
            try:
                self._anim_timer.resume()
            except Exception:
                pass
        self.refresh()


# ── Right panel widget ────────────────────────────────────────────


class _InfoPanel(Widget):
    """Environment + capabilities panel with health dots.

    Renders at its actual width so long lists never wrap mid-word; overflow
    collapses into a clickable ``+N more`` that opens a detail popup.
    """

    DEFAULT_CSS = """
    _InfoPanel {
        width: 1fr;
        height: auto;
        padding: 1 2 1 2;
    }
    """

    status: reactive[str] = reactive("starting")

    def __init__(self, info: _BannerInfo, **kwargs: Any) -> None:
        """Initialize _InfoPanel."""
        super().__init__(**kwargs)
        self._info = info

    def render(self) -> Text:
        """Render the widget content."""
        width = self.content_size.width or _DEFAULT_INFO_WIDTH
        return _build_info_text(
            self._info,
            width=width,
            status=self.status,
            colors=get_theme_colors(self),
        )

    def get_content_height(self, container: Any, viewport: Any, width: int) -> int:
        """Calculate and return the preferred content height."""
        return len(
            _build_info_text(
                self._info,
                width=width or _DEFAULT_INFO_WIDTH,
                status=self.status,
                colors=get_theme_colors(self),
            ).plain.splitlines()
        )

    def watch_status(self, _old: str, _new: str) -> None:
        """React to changes in the status reactive attribute."""
        self.refresh()

    async def action_show_skills_popup(self) -> None:
        """Push the skills detail popup."""
        await self.app.push_screen(
            WelcomeDetailPopup(title="Discovered Skills", items=self._info.skills)
        )

    async def action_show_agents_popup(self) -> None:
        """Push the subagents detail popup."""
        await self.app.push_screen(
            WelcomeDetailPopup(title="Loaded Subagents", items=self._info.subagents)
        )


# ── Main banner ───────────────────────────────────────────────────


class WelcomeBanner(Widget):
    """Two-panel welcome banner card shown on app startup.

    Left panel: animated TalkOps logo ("DevOps on Demand"), authenticated Control Plane user, health line, version.
    Right panel: environment and capabilities with health dots.

    The banner stays in the scrollback; there is no dismiss button.
    """

    DEFAULT_CSS = """
    WelcomeBanner {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
        align-horizontal: center;
        background: transparent;
    }

    WelcomeBanner #welcome-card {
        width: 120;
        max-width: 100%;
        height: auto;
        border: round $primary 55%;
        background: transparent;
        border-subtitle-align: left;
        border-subtitle-color: $text-muted;
    }

    WelcomeBanner _LeftPanel {
        width: 60;
        height: auto;
        padding: 1 0 0 1;
    }

    /* Graphics-protocol mode: real PNG icon + text-only _LeftPanel side by side. */
    WelcomeBanner #welcome-brand {
        width: 60;
        height: auto;
        padding: 1 0 0 1;
    }

    WelcomeBanner #welcome-logo-image {
        width: auto;
        height: 8;
        max-width: 24;
    }

    WelcomeBanner #welcome-brand _LeftPanel {
        width: 1fr;
        padding: 0 0 0 2;
    }

    WelcomeBanner #welcome-right {
        width: 1fr;
        height: auto;
        border-left: solid $primary 25%;
    }

    WelcomeBanner #welcome-card.-stacked {
        layout: vertical;
    }

    WelcomeBanner #welcome-card.-stacked #welcome-right {
        width: 100%;
        border-left: none;
        border-top: solid $primary 25%;
    }
    """

    def __init__(
        self,
        *,
        model_spec: str = "",
        effort: str = "",
        cloud_profile: str = "",
        cloud_region: str = "",
        mcp_servers: Sequence[MCPServerInfo] | None = None,
        mcp_disabled: bool = False,
        user_email: str = "",
        user_tier: str = "",
        server_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize WelcomeBanner."""
        super().__init__(**kwargs)
        self.border_title = None
        self._info = _BannerInfo(
            model_spec=model_spec,
            effort=effort,
            cloud_profile=cloud_profile,
            cloud_region=cloud_region,
            mcp_servers=list(mcp_servers) if mcp_servers else None,
            mcp_disabled=mcp_disabled,
        )
        self._user_display: str | None = (
            _format_user_display(user_email, user_tier) if user_email else None
        )
        self._server_url: str | None = server_url
        self._status = "starting"

    # Backwards-compatible accessors (used by tests / callers).
    @property
    def _skills_data(self) -> list[tuple[str, str]]:
        return self._info.skills

    @_skills_data.setter
    def _skills_data(self, value: list[tuple[str, str]]) -> None:
        self._info.skills = list(value)

    @property
    def _subagents_data(self) -> list[tuple[str, str]]:
        return self._info.subagents

    @_subagents_data.setter
    def _subagents_data(self, value: list[tuple[str, str]]) -> None:
        self._info.subagents = list(value)

    def compose(self) -> ComposeResult:
        """Compose and yield child widgets for this component."""
        self._info.skills = _collect_skills()
        self._info.subagents = _collect_subagents()
        self._info.project = _project_display()

        card = Horizontal(id="welcome-card")
        card.border_subtitle = self._hint_markup()
        with card:
            logo_image = create_logo_image_widget()
            if logo_image is not None:
                # Terminal speaks Sixel / Kitty graphics: show the real logo PNG.
                with Horizontal(id="welcome-brand"):
                    yield logo_image
                    yield _LeftPanel(
                        user_display=self._user_display,
                        show_icon=False,
                        server_url=self._server_url,
                        id="welcome-left",
                    )
            else:
                yield _LeftPanel(
                    user_display=self._user_display,
                    server_url=self._server_url,
                    id="welcome-left",
                )
            yield _InfoPanel(self._info, id="welcome-right")

    def on_resize(self, event: events.Resize) -> None:
        """Stack the panels on narrow terminals so nothing gets clipped."""
        try:
            card = self.query_one("#welcome-card", Horizontal)
        except Exception:
            return
        card.set_class(event.size.width < _STACK_BELOW_WIDTH, "-stacked")

    @staticmethod
    def _hint_markup() -> str:
        sep = " [dim]" + _sep() + "[/dim] "
        items = [
            ("/", "commands"),
            ("!", "shell"),
            ("ctrl+o", "expand output"),
            ("esc", "interrupt"),
        ]
        body = sep.join(f"[b]{key}[/b] {label}" for key, label in items)
        return f" {body} "

    # ── Public API ────────────────────────────────────────────────

    def update_cloud(self, profile: str = "", region: str = "", provider: str = "aws") -> None:
        """Reflect the active cloud provider / profile / region in the environment rows."""
        self._info.cloud_provider = provider or "aws"
        self._info.cloud_profile = profile or ""
        self._info.cloud_region = region or ""
        self._refresh_info()

    def update_model(self, model_spec: str, effort: str = "") -> None:
        """Reflect the active model (``provider:model``) and reasoning effort."""
        self._info.model_spec = model_spec or ""
        self._info.effort = effort or ""
        self._refresh_info()

    def update_mcp(self, servers: Sequence[MCPServerInfo] | None) -> None:
        """Reflect probed MCP servers. ``None`` keeps the 'discovering' state."""
        self._info.mcp_servers = list(servers) if servers is not None else None
        self._refresh_info()

    def refresh_capabilities(self) -> None:
        """Re-discover skills and subagents and refresh the info panel."""
        self._info.skills = _collect_skills()
        self._info.subagents = _collect_subagents()
        self._refresh_info()

    def update_user(self, user: str = "", tier: str = "") -> None:
        """Explicitly set or clear the Control Plane authenticated user display."""
        self._user_display = _format_user_display(user, tier) if user else ""
        try:
            left = self.query_one(_LeftPanel)
            left.refresh_user(self._user_display)
        except Exception:
            pass

    def update_server(self, server_url: str | None = None) -> None:
        """Explicitly set or clear the Central Server URL displayed on the left panel."""
        self._server_url = server_url
        try:
            left = self.query_one(_LeftPanel)
            left.refresh_server(self._server_url)
        except Exception:
            pass

    def refresh_auth(self) -> None:
        """Re-read the Control Plane session from storage, refresh the left panel, and restore ready status if authenticated."""
        self._user_display = None
        try:
            left = self.query_one(_LeftPanel)
            left.refresh_user(None)
        except Exception:
            pass
        if self._status in ("warning", "degraded", "logged_out"):
            has_cp_session = False
            try:
                from talkops.auth.storage import get_credential_storage

                sess = get_credential_storage().load_session()
                has_cp_session = bool(
                    (
                        sess
                        and (
                            sess.api_key
                            or sess.access_token
                            or sess.email
                            or sess.user_id
                        )
                    )
                    or os.environ.get("TALKOPS_API_KEY", "").strip()
                    or os.environ.get("TALKOPS_SERVER_API_KEY", "").strip()
                )
            except Exception:
                pass
            app = getattr(self, "app", None)
            has_live_client = bool(
                self._status != "logged_out"
                and app is not None
                and getattr(app, "_client", None) is not None
                and not getattr(app, "_connecting", False)
                and getattr(app, "_server_startup_error", None) is None
            )
            if has_cp_session or has_live_client:
                self.set_connected()
                if app is not None:
                    try:
                        from talkops.ui.widgets.status import StatusBar

                        sb = app.query_one("#status-bar", StatusBar)
                        if "logged out" in (sb.status_message or "").lower():
                            sb.set_status("Ready")
                    except Exception:
                        pass

    def set_connected(self) -> None:
        """Transition the health indicators to green 'ready'."""
        self._set_status("connected")

    def set_starting(self) -> None:
        """Transition the health indicators to yellow 'connecting'."""
        self._set_status("starting")

    def set_warning(self) -> None:
        """Transition the health indicators to yellow 'warning'."""
        self._set_status("warning")

    def set_logged_out(self) -> None:
        """Transition the banner to 'logged out' state with sign-in instructions."""
        self.update_user("Not signed in · /login to sign in", "")
        self._set_status("logged_out")

    def set_error(self) -> None:
        """Transition the health indicators to red 'error'."""
        self._set_status("error")

    # ── Private helpers ───────────────────────────────────────────

    def _set_status(self, status: str) -> None:
        self._status = status
        for widget_type in (_LeftPanel, _InfoPanel):
            try:
                self.query_one(widget_type).status = status
            except Exception:
                logger.debug("Could not update banner status", exc_info=True)

    def _refresh_info(self) -> None:
        try:
            self.query_one(_InfoPanel).refresh(layout=True)
        except Exception:
            pass

    def _build_right_panel(self, width: int = _DEFAULT_INFO_WIDTH) -> Text:
        """Render the info panel text (used by tests and for previews)."""
        return _build_info_text(
            self._info,
            width=width,
            status=self._status,
            colors=get_theme_colors(self) if self.is_attached else get_theme_colors(None),
        )

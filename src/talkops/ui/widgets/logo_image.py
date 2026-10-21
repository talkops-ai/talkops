"""Pixel-perfect TalkOps icon for the welcome banner via terminal graphics.

Character cells cannot reproduce the smooth TalkOps mark (see
``docs/welcome_logo_handover.md``), so when the terminal supports a real
graphics protocol we show the bundled PNG instead of block art:

* **Sixel**  — iTerm2, WezTerm, VS Code (``terminal.integrated.enableImages``),
  Windows Terminal, foot, Konsole, mlterm, xterm ``-ti vt340`` …
* **Kitty TGP** (Unicode placeholders) — kitty and Ghostty.

Everything else keeps the quadrant-block fallback rendered by
``welcome._build_logo_text``.

Detection must happen *before* Textual owns stdin, so :func:`probe_graphics_protocol`
is called once from the CLI launcher. Widgets never probe; they only read the
cached result, which keeps tests, pipes, and headless runs on the fallback.

Environment override ``TALKOPS_LOGO_IMAGE``:
``0/off/false/no`` disables the image, ``force`` also trusts Kitty TGP replies
from terminals other than kitty/Ghostty; anything else (default) is auto.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from textual.widget import Widget

logger = get_logger(__name__)

GraphicsProtocol = Literal["sixel", "tgp"]

LOGO_ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "talkops-icon.png"

_ENV_VAR = "TALKOPS_LOGO_IMAGE"
_DISABLED_VALUES = frozenset({"0", "off", "false", "no", "none", "disable", "disabled"})

_probed = False
_protocol: GraphicsProtocol | None = None


def _env_mode() -> str:
    return os.environ.get(_ENV_VAR, "auto").strip().lower()


def _is_ascii_mode() -> bool:
    try:
        from talkops.config.settings import is_ascii_mode

        return bool(is_ascii_mode())
    except Exception:
        return False


def _tgp_placeholders_trusted() -> bool:
    """Kitty-protocol Unicode placeholders only render correctly in kitty and Ghostty.

    WezTerm, Konsole and Warp answer the TGP query with ``OK`` but do not draw
    placeholder cells, which would leave garbage glyphs in the banner.
    """
    term = os.environ.get("TERM", "").lower()
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    return bool(
        os.environ.get("KITTY_WINDOW_ID")
        or "kitty" in term
        or "ghostty" in term
        or term_program == "ghostty"
        or os.environ.get("GHOSTTY_RESOURCES_DIR")
    )


def _eligible_for_probe() -> bool:
    mode = _env_mode()
    if mode in _DISABLED_VALUES:
        return False
    if _is_ascii_mode():
        return False
    if os.environ.get("TERM", "").lower() in ("", "dumb"):
        return False
    if not LOGO_ICON_PATH.is_file():
        return False
    try:
        return bool(
            sys.__stdin__ is not None
            and sys.__stdout__ is not None
            and sys.__stdin__.isatty()
            and sys.__stdout__.isatty()
        )
    except Exception:
        return False


def probe_graphics_protocol() -> GraphicsProtocol | None:
    """Detect Sixel / Kitty graphics support. Call once before ``App.run()``.

    Safe to call repeatedly (cached) and never raises. Importing
    ``textual_image`` sends a single batched terminal query (TGP + primary DA)
    and reads the reply, which is only possible while we still own the TTY.
    """
    global _probed, _protocol
    if _probed:
        return _protocol
    _probed = True
    _protocol = None

    if not _eligible_for_probe():
        return None

    try:
        import textual_image.renderable as ti_renderable
        from textual_image.renderable.sixel import Image as SixelRenderable
        from textual_image.renderable.tgp import Image as TGPRenderable

        # Prime the cell-size cache used for scaling (needs the TTY too).
        import textual_image.widget  # noqa: F401
    except Exception:
        logger.debug("textual-image unavailable; using block-art logo", exc_info=True)
        return None

    detected = ti_renderable.Image
    if detected is SixelRenderable:
        _protocol = "sixel"
    elif detected is TGPRenderable and (_tgp_placeholders_trusted() or _env_mode() == "force"):
        _protocol = "tgp"
    logger.debug("Welcome logo graphics protocol: %s", _protocol or "block-art fallback")
    return _protocol


def graphics_protocol() -> GraphicsProtocol | None:
    """Return the cached protocol without probing (``None`` if never probed)."""
    return _protocol if _probed else None


def create_logo_image_widget(*, widget_id: str = "welcome-logo-image") -> Widget | None:
    """Build the image widget for the detected protocol, or ``None`` for fallback."""
    protocol = graphics_protocol()
    if protocol is None:
        return None
    try:
        if protocol == "sixel":
            from textual_image.widget import SixelImage as ImageWidget
        else:
            from textual_image.widget import TGPImage as ImageWidget

        return ImageWidget(LOGO_ICON_PATH, id=widget_id)
    except Exception:
        logger.debug("Could not create welcome logo image widget", exc_info=True)
        return None


def _reset_for_tests(protocol: GraphicsProtocol | None = None, *, probed: bool = False) -> None:
    """Testing hook: set the cached probe result."""
    global _probed, _protocol
    _probed = probed
    _protocol = protocol

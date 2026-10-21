"""Message widgets and preamble UI helpers."""

from __future__ import annotations

from talkops.utils.logger import get_logger
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from talkops.config.settings import (
    get_glyphs,
    is_ascii_mode,
)
from talkops.ui import theme
from talkops.ui.widgets.diff import compose_diff_lines
from talkops.ui.widgets.loading import format_duration
from talkops.ui.widgets.tool_display import (
    format_tool_display,
)
from talkops.ui.widgets._js_eval_display import (
    JsEvalBlock,
    JsEvalError,
    JsEvalResult,
    JsEvalStdout,
    parse_js_eval_blocks,
)

if TYPE_CHECKING:
    from textual.widget import Widget

logger = get_logger(__name__)


def _mode_color(mode: str | None, widget_or_app: object | None = None) -> str:
    """Return the hex color string for a mode, falling back to primary.

    Args:
        mode: Mode name (e.g. `'shell'`, `'command'`) or `None`.
        widget_or_app: Textual widget or `App` for theme-aware lookup.

    Returns:
        Color string from the active theme's `ThemeColors`.
    """
    colors = theme.get_theme_colors(widget_or_app)
    if not mode:
        return colors.primary
    if mode == "shell_incognito":
        return colors.mode_incognito
    if mode == "shell":
        return colors.mode_bash
    if mode == "command":
        return colors.mode_command
    logger.warning("Missing color for mode '%s'; falling back to primary.", mode)
    return colors.primary

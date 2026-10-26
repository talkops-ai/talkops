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

"""Message widgets and preamble UI helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from talkops.config.settings import (
    get_glyphs,
    is_ascii_mode,
)
from talkops.ui import theme
from talkops.ui.widgets._js_eval_display import (
    JsEvalBlock,
    JsEvalError,
    JsEvalResult,
    JsEvalStdout,
    parse_js_eval_blocks,
)
from talkops.ui.widgets.diff import compose_diff_lines
from talkops.ui.widgets.loading import format_duration
from talkops.ui.widgets.tool_display import (
    format_tool_display,
)
from talkops.utils.logger import get_logger

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

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

"""Notification settings modal screen for `/notifications` command."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import VerticalGroup
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Static

from talkops.config.settings import get_glyphs
from talkops.ui.widgets.modal_base import OpsModalScreen
from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from textual.app import ComposeResult

logger = get_logger(__name__)

WARNING_TOGGLES: list[tuple[str, str]] = [
    ("ripgrep", "Warn when ripgrep is not installed"),
    ("tavily", "Warn when TAVILY_API_KEY is not set (web search)"),
    ("docker", "Warn when Docker engine is not running"),
    ("kubectl", "Warn when kubectl CLI is not found"),
    ("terraform", "Warn when Terraform / OpenTofu is not installed"),
    ("ansible", "Warn when Ansible is not installed"),
]


class NotificationSettingsScreen(OpsModalScreen[None]):
    """Modal dialog for managing startup warning preferences.

    Toggling a checkbox enables or suppresses startup warning notices.
    """

    DIALOG_CLASSES: ClassVar[str] = "-compact"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("up", "app.focus_previous", "Previous", show=False, priority=True),
        Binding("down", "app.focus_next", "Next", show=False, priority=True),
        Binding("tab", "app.focus_next", "Next", show=False, priority=True),
        Binding("shift+tab", "app.focus_previous", "Previous", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    NotificationSettingsScreen .ns-title {
        display: none;
    }

    NotificationSettingsScreen .ns-help {
        display: none;
    }

    NotificationSettingsScreen Checkbox {
        margin: 0;
        border: none;
        height: auto;
    }
    """

    def __init__(self, suppressed: set[str] | None = None) -> None:
        """Initialize NotificationSettingsScreen."""
        super().__init__()
        self._suppressed: set[str] = suppressed or set()

    def compose_modal(self):
        """Execute compose modal on NotificationSettingsScreen."""
        yield Static("Notification Settings", classes="ns-title")
        for key, label in WARNING_TOGGLES:
            yield Checkbox(
                label,
                value=key not in self._suppressed,
                id=f"ns-{key}",
            )
        yield Static("↑/↓ or Tab navigate • Space toggle • Esc close", classes="ns-help")

    def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        super().on_mount()
        glyphs = get_glyphs()
        self.set_modal_title("Notification Settings", counter=f"{len(WARNING_TOGGLES)} rules")
        self.set_modal_subtitle([
            (f"{glyphs.arrow_up}{glyphs.arrow_down}", "navigate"),
            ("space", "toggle"),
            ("esc", "close"),
        ])
        try:
            self.query(Checkbox).first().focus()
        except Exception:
            pass

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Handle checkbox changed events."""
        event.stop()
        checkbox_id = event.checkbox.id
        if not checkbox_id or not checkbox_id.startswith("ns-"):
            return
        key = checkbox_id.removeprefix("ns-")
        enabled = event.value

        from talkops.model.config import suppress_warning, unsuppress_warning

        if enabled:
            self._suppressed.discard(key)
            unsuppress_warning(key)
        else:
            self._suppressed.add(key)
            suppress_warning(key)

        if hasattr(self.app, "_suppressed_warnings"):
            setattr(self.app, "_suppressed_warnings", self._suppressed)

    def action_cancel(self) -> None:
        """Execute the cancel action."""
        self.dismiss(None)


__all__ = ["NotificationSettingsScreen", "WARNING_TOGGLES"]

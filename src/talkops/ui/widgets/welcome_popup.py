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

"""Lightweight modal popup for displaying full skill / subagent lists.

Pushed by the WelcomeBanner when the user clicks a "…more" span.
Dismissed on Escape, Enter, or backdrop click.
"""

from __future__ import annotations

from typing import Any, ClassVar, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.content import Content
from textual.widgets import Static

from talkops.ui.widgets.modal_base import OpsModalScreen
from talkops.ui.widgets.modal_base import escape_markup as escape


class WelcomeDetailPopup(OpsModalScreen[None]):
    """Scrollable popup listing names + descriptions for skills or subagents."""

    DIALOG_CLASSES: ClassVar[str] = "-compact"

    BINDINGS = [
        Binding("escape", "dismiss_popup", "Close", show=False),
        Binding("enter", "dismiss_popup", "Close", show=False),
    ]

    DEFAULT_CSS = """
    WelcomeDetailPopup #welcome-detail-title {
        display: none;
    }

    WelcomeDetailPopup #welcome-detail-scroll {
        height: 1fr;
        scrollbar-gutter: stable;
    }

    WelcomeDetailPopup .welcome-detail-row {
        height: auto;
        margin-bottom: 1;
    }

    WelcomeDetailPopup #welcome-detail-hint {
        display: none;
    }
    """

    def __init__(
        self,
        title: str,
        items: Sequence[tuple[str, str]],
        **kwargs: Any,
    ) -> None:
        """Initialize the welcome info popup.

        Args:
            title: Popup heading, e.g. "Skills" or "Subagents".
            items: Sequence of (name, description) pairs.
            **kwargs: Additional Textual ModalScreen keyword arguments.
        """
        super().__init__(**kwargs)
        self._title = title
        self._items = list(items)

    def compose_modal(self) -> ComposeResult:
        """Execute compose modal on WelcomeDetailPopup."""
        yield Static(self._title, id="welcome-detail-title")
        with VerticalScroll(id="welcome-detail-scroll"):
            for name, desc in self._items:
                desc_part = f"  [dim]—  {escape(desc)}[/dim]" if desc else ""
                yield Static(
                    Content.from_markup(f"  [bold $primary]{escape(name)}[/bold $primary]{desc_part}"),
                    classes="welcome-detail-row",
                )
        yield Static("", id="welcome-detail-hint")

    def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        super().on_mount()
        self.set_modal_title(self._title, counter=f"{len(self._items)} items")
        self.set_modal_subtitle([
            ("enter", "close"),
            ("esc", "close"),
        ])

    def action_dismiss_popup(self) -> None:
        """Execute the dismiss popup action."""
        self.dismiss(None)

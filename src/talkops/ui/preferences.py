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

"""User preferences & persistent configuration manager.

Parses and writes `~/.talkops/config.toml` for UI theme, scrollbar, timestamps,
and auto-approval preferences.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

from talkops.ui.theme import (
    CONFIG_PATH,
    get_registry,
    load_theme_preference,
    save_theme_preference,
)


@dataclass
class UserPreferences:
    """Represent user preferences."""
    theme: str = "talkops-dark"
    scrollbar: bool = True
    timestamps: bool = True
    auto_approve_level: str = "off"


def load_preferences() -> UserPreferences:
    """Load preferences from ~/.talkops/config.toml if present."""
    if not CONFIG_PATH.exists():
        return UserPreferences(theme=load_theme_preference())
    try:
        with open(CONFIG_PATH, "rb") as f:
            data = tomllib.load(f)
        ui = data.get("ui", {})
        return UserPreferences(
            theme=load_theme_preference(),
            scrollbar=ui.get("scrollbar", True),
            timestamps=ui.get("timestamps", True),
            auto_approve_level=ui.get("auto_approve_level", "off"),
        )
    except Exception:
        return UserPreferences(theme=load_theme_preference())


def save_preferences(prefs: UserPreferences) -> bool:
    """Save preferences to ~/.talkops/config.toml."""
    return save_theme_preference(prefs.theme)



class ThemeSelector(Widget):
    """Modal widget allowing live selection of visual themes."""

    DEFAULT_CSS = """
    ThemeSelector {
        layer: overlay;
        dock: right;
        width: 45;
        height: 100%;
        background: $surface;
        border-left: tall $panel;
        padding: 1;
    }
    ThemeSelector .title {
        color: $primary;
        text-style: bold;
        margin-bottom: 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss_selector", "Close", show=True),
    ]

    def __init__(self, **kwargs: Any) -> None:
        """Initialize ThemeSelector."""
        super().__init__(**kwargs)
        self._registry = get_registry()

    def compose(self):
        """Compose and yield child widgets for this component."""
        yield Static("Select Color Theme", classes="title")
        option_list = OptionList(id="theme-options")
        for key, entry in self._registry.items():
            option_list.add_option(Option(f"{entry.label} (`{key}`)", id=key))
        yield option_list
        yield Button("Apply Theme", variant="success", id="btn-apply")

    @on(Button.Pressed, "#btn-apply")
    def action_apply(self) -> None:
        """Execute the apply action."""
        opt_list = self.query_one("#theme-options", OptionList)
        if opt_list.highlighted is not None:
            option = opt_list.get_option_at_index(opt_list.highlighted)
            theme_name = option.id or "talkops-dark"
            if self.app is not None:
                self.app.theme = theme_name
                prefs = load_preferences()
                prefs.theme = theme_name
                save_preferences(prefs)
        self.remove()

    def action_dismiss_selector(self) -> None:
        """Execute the dismiss selector action."""
        self.remove()

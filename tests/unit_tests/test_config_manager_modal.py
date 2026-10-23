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

"""Unit tests for ConfigManagerScreen and _InlineEditScreen modal migration."""

from __future__ import annotations

from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Input, OptionList

from talkops.ui.widgets.config_manager import ConfigManagerScreen, _InlineEditScreen


class DummyConfigApp(App[None]):
    """Minimal textual app to mount and test ConfigManagerScreen."""

    def __init__(self, screen_instance: ConfigManagerScreen | _InlineEditScreen) -> None:
        """Initialize DummyConfigApp."""
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        """Compose and yield child widgets for this component."""
        yield from ()

    async def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_config_manager_screen_mount_and_search() -> None:
    """Verify config manager screen mount and search."""
    screen = ConfigManagerScreen()
    app = DummyConfigApp(screen)

    async with app.run_test() as pilot:
        tabs: Any = screen.query_one("#config-tabs")
        assert tabs is not None
        search = screen.query_one("#config-search", Input)
        opt_list = screen.query_one("#config-options", OptionList)
        assert opt_list is not None
        assert opt_list.has_focus
        assert opt_list.option_count > 0

        # Cycle through config tabs (All -> Cloud & Infra -> DevOps & Tools)
        screen.action_cycle_tab()
        await pilot.pause()
        assert screen._active_tab == "cloud"
        assert opt_list.option_count > 0

        screen.action_cycle_tab()
        await pilot.pause()
        assert screen._active_tab == "devops"
        assert opt_list.option_count > 0

        # Reset back to All tab
        tabs.activate_tab("all")
        await pilot.pause()

        # Type search filter
        search.value = "theme"
        await pilot.pause()
        # OptionList filtered
        assert opt_list.option_count >= 1

        # Clear search filter
        search.value = ""
        await pilot.pause()
        assert opt_list.option_count > 5

        # Close
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1


@pytest.mark.asyncio
async def test_inline_edit_screen() -> None:
    """Verify inline edit screen."""
    screen = _InlineEditScreen(label="Test Option", current_value="current_val")
    app = DummyConfigApp(screen)

    async with app.run_test() as pilot:
        inp = screen.query_one("#edit-input", Input)
        assert inp.has_focus
        assert inp.value == "current_val"

        # Cancel
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1

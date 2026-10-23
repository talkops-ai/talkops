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

"""Unit tests for PermissionsManagerScreen OpsModalScreen migration."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList

from talkops.ui.permission_store import PermissionStore
from talkops.ui.widgets.permissions_manager import (
    PermissionsManagerScreen,
    _AddRuleScreen,
)


class DummyPermApp(App[None]):
    """Minimal textual app to mount and test PermissionsManagerScreen."""

    def __init__(self, screen_instance: PermissionsManagerScreen | _AddRuleScreen) -> None:
        """Initialize DummyPermApp."""
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        """Compose and yield child widgets for this component."""
        yield from ()

    async def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_permissions_manager_screen_mount_and_tabs() -> None:
    """Verify permissions manager screen mount and tabs."""
    store = PermissionStore()
    screen = PermissionsManagerScreen(store=store)
    app = DummyPermApp(screen)

    async with app.run_test() as pilot:
        opt_list = screen.query_one("#perm-options", OptionList)
        assert opt_list is not None
        assert opt_list.has_focus
        assert opt_list.option_count > 0

        # Switch tab next
        screen.action_next_tab()
        await pilot.pause()
        assert screen._active_tab == "recently_denied"

        # Switch tab prev
        screen.action_prev_tab()
        await pilot.pause()
        assert screen._active_tab == "permissions"

        # Cycle mode
        initial_mode = store.mode
        screen.action_cycle_mode()
        await pilot.pause()
        assert store.mode != initial_mode

        # Close
        screen.action_close()
        await pilot.pause()
        assert len(app._screen_stack) == 1


@pytest.mark.asyncio
async def test_add_rule_screen() -> None:
    """Verify add rule screen."""
    screen = _AddRuleScreen(category="allow")
    app = DummyPermApp(screen)

    async with app.run_test() as pilot:
        inp = screen.query_one("#add-rule-input")
        assert inp.has_focus

        # Cancel
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1

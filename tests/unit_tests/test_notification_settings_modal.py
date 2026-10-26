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

"""Unit tests for NotificationSettingsScreen OpsModalScreen migration."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Checkbox

from talkops.ui.widgets.notification_settings import (
    WARNING_TOGGLES,
    NotificationSettingsScreen,
)


class DummyNotifApp(App[None]):
    """Minimal textual app to mount and test NotificationSettingsScreen."""

    def __init__(self, screen_instance: NotificationSettingsScreen) -> None:
        """Initialize DummyNotifApp."""
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        """Compose and yield child widgets for this component."""
        yield from ()

    async def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_notification_settings_screen_mount_and_toggle() -> None:
    """Verify notification settings screen mount and toggle."""
    suppressed = {"docker"}
    screen = NotificationSettingsScreen(suppressed=suppressed)
    app = DummyNotifApp(screen)

    async with app.run_test() as pilot:
        boxes = list(screen.query(Checkbox))
        assert len(boxes) == len(WARNING_TOGGLES)

        docker_box = screen.query_one("#ns-docker", Checkbox)
        assert docker_box.value is False

        ripgrep_box = screen.query_one("#ns-ripgrep", Checkbox)
        assert ripgrep_box.value is True

        # Toggle ripgrep
        ripgrep_box.value = False
        await pilot.pause()
        assert "ripgrep" in screen._suppressed

        # Close
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1

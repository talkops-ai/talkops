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

"""Unit tests for AutoModeNoticeScreen OpsModalScreen migration."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from talkops.ui.widgets.auto_mode_notice import AutoModeNoticeScreen


class DummyNoticeApp(App[None]):
    """Minimal textual app to mount and test AutoModeNoticeScreen."""

    def __init__(self, screen_instance: AutoModeNoticeScreen) -> None:
        """Initialize DummyNoticeApp."""
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        """Compose and yield child widgets for this component."""
        yield from ()

    async def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_auto_mode_notice_screen() -> None:
    """Verify auto mode notice screen."""
    screen = AutoModeNoticeScreen()
    app = DummyNoticeApp(screen)

    async with app.run_test() as pilot:
        # Cancel action dismisses with False
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1

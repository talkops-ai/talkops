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

"""Unit tests for DebugConsoleScreen OpsModalScreen migration."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Select

from talkops.ui.widgets.debug_console import (
    DebugConsoleScreen,
    SnapshotField,
    _DebugLogView,
)


class DummyDebugApp(App[None]):
    """Minimal textual app to mount and test DebugConsoleScreen."""

    def __init__(self, screen_instance: DebugConsoleScreen) -> None:
        """Initialize DummyDebugApp."""
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        """Compose and yield child widgets for this component."""
        yield from ()

    async def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_debug_console_screen_mount_and_controls() -> None:
    """Verify debug console screen mount and controls."""
    snapshot = [
        SnapshotField(label="Model", value="claude-3-7-sonnet"),
        SnapshotField(label="Thread", value="test-thread-id", copyable=True),
    ]
    screen = DebugConsoleScreen(snapshot=snapshot)
    app = DummyDebugApp(screen)

    async with app.run_test() as pilot:
        log_view = screen.query_one("#debug-log", _DebugLogView)
        assert log_view is not None

        level_select = screen.query_one("#debug-level-filter", Select)
        assert level_select is not None
        assert level_select.value == "all"

        # Close
        screen.action_close()
        await pilot.pause()
        assert len(app._screen_stack) == 1


@pytest.mark.asyncio
async def test_debug_console_displays_emitted_agent_logs() -> None:
    """DebugConsoleScreen renders logs emitted through talkops.utils.logger.get_logger."""
    from talkops._debug_buffer import get_log_buffer
    from talkops.utils.logger import configure_logging, get_logger

    configure_logging(level="INFO", log_to_console=False, log_to_file=False)
    buf = get_log_buffer()
    assert buf is not None
    cursor = buf.total_emitted

    test_logger = get_logger("talkops.ui.debug_test")
    test_logger.info("Live log line for debug console")

    screen = DebugConsoleScreen(
        snapshot=[SnapshotField(label="Thread", value="t-123")],
        cleared_upto=cursor,
    )
    app = DummyDebugApp(screen)

    async with app.run_test() as pilot:
        await pilot.pause()
        log_view = screen.query_one("#debug-log", _DebugLogView)
        assert any("Live log line for debug console" in r.message for r in log_view._records)


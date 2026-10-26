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

"""Unit tests for ThreadSelectorScreen OpsModalScreen migration."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from textual.app import App, ComposeResult

from talkops.ui.widgets.thread_selector import (
    CustomOptionList,
    DeleteThreadConfirmScreen,
    SearchInput,
    ThreadSelectorScreen,
    _format_relative_time,
)


class DummyThreadApp(App):
    """Minimal textual app to mount and test ThreadSelectorScreen."""

    def __init__(self, screen_instance: ThreadSelectorScreen) -> None:
        """Initialize DummyThreadApp."""
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        """Compose and yield child widgets for this component."""
        yield from ()

    async def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        await self.push_screen(self._test_screen)


def test_format_relative_time() -> None:
    """Verify format relative time."""
    assert _format_relative_time(None) == ""
    assert _format_relative_time("") == ""
    # Test recent seconds
    now_iso = datetime.now(timezone.utc).isoformat()
    assert _format_relative_time(now_iso) == "just now"


@pytest.mark.asyncio
async def test_thread_selector_screen_mount_and_options() -> None:
    """Verify thread selector screen mount and options."""
    threads = [
        {
            "thread_id": "thread-12345678-abcd",
            "message_count": 5,
            "initial_prompt": "Investigate ECS CPU spike",
            "created_at": "2026-10-12T05:00:00Z",
            "cost": 0.0425,
        },
        {
            "thread_id": "thread-87654321-efgh",
            "message_count": 2,
            "initial_prompt": "Deploy redis cluster",
            "created_at": "2026-10-12T07:00:00Z",
        },
    ]

    screen = ThreadSelectorScreen(threads=threads, current_thread_id="thread-12345678-abcd")
    app = DummyThreadApp(screen)

    async with app.run_test() as pilot:
        # SearchInput is mounted and focused
        search = screen.query_one(SearchInput)
        assert search.has_focus

        # Option list is mounted
        opt_list = screen.query_one(CustomOptionList)
        assert opt_list is not None
        # 1 ("+ Start New Thread") + 2 threads = 3 options
        assert opt_list.option_count == 3

        # Test search filter
        search.value = "redis"
        await pilot.pause()
        assert opt_list.option_count == 2  # New thread + matching thread

        # Clear filter
        search.value = ""
        await pilot.pause()
        assert opt_list.option_count == 3

        # Test down navigation
        screen.action_cursor_down()
        await pilot.pause()
        assert opt_list.highlighted == 1

        # Test cancel
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1


@pytest.mark.asyncio
async def test_thread_search_editing_keys_do_not_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify thread search editing keys do not delete."""
    screen = ThreadSelectorScreen(
        threads=[{"thread_id": "thread-12345678-abcd", "initial_prompt": "Deployment"}],
    )
    app = DummyThreadApp(screen)
    deleted: list[str] = []

    async def fake_delete(thread_id: str) -> None:
        """Provide fake delete test helper or fixture."""
        deleted.append(thread_id)

    monkeypatch.setattr(screen, "_delete_thread_async", fake_delete)

    async with app.run_test(size=(100, 30)) as pilot:
        search = screen.query_one(SearchInput)
        search.focus()
        await pilot.press("d", "x", "backspace", "d")
        await pilot.pause()

        assert deleted == []
        assert search.value == "dd"


@pytest.mark.asyncio
async def test_thread_row_click_does_not_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify thread row click does not delete."""
    screen = ThreadSelectorScreen(
        threads=[{"thread_id": "thread-12345678-abcd", "initial_prompt": "Deployment"}],
    )
    app = DummyThreadApp(screen)
    deleted: list[str] = []

    async def fake_delete(thread_id: str) -> None:
        """Provide fake delete test helper or fixture."""
        deleted.append(thread_id)

    monkeypatch.setattr(screen, "_delete_thread_async", fake_delete)

    dismiss_result: list[object] = []
    screen.dismiss = lambda result=None: dismiss_result.append(result)  # type: ignore[method-assign]

    async with app.run_test(size=(100, 30)) as pilot:
        opt_list = screen.query_one(CustomOptionList)
        screen.action_cursor_down()
        await pilot.pause()
        opt_list.action_select()
        await pilot.pause()

        assert deleted == []
        assert dismiss_result == ["thread-12345678-abcd"]


@pytest.mark.asyncio
async def test_thread_ctrl_d_and_confirm_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify thread ctrl d and confirm flow."""
    screen = ThreadSelectorScreen(
        threads=[{"thread_id": "thread-12345678-abcd", "initial_prompt": "Deployment"}],
    )
    app = DummyThreadApp(screen)
    deleted: list[str] = []

    async def fake_delete(thread_id: str) -> None:
        """Provide fake delete test helper or fixture."""
        deleted.append(thread_id)

    monkeypatch.setattr(screen, "_delete_thread_async", fake_delete)

    async with app.run_test(size=(100, 30)) as pilot:
        opt_list = screen.query_one(CustomOptionList)
        # Navigate to the thread option
        screen.action_cursor_down()
        await pilot.pause()
        assert opt_list.highlighted == 1

        # Press ctrl+d to trigger delete
        await pilot.press("ctrl+d")
        await pilot.pause()

        # Confirmation screen should now be top screen
        confirm_screen = app.screen
        assert isinstance(confirm_screen, DeleteThreadConfirmScreen)
        assert confirm_screen.thread_id == "thread-12345678-abcd"

        # Cancel should not delete
        await pilot.click("#cancel")
        await pilot.pause()
        assert deleted == []

        # Press ctrl+d again and confirm
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert isinstance(app.screen, DeleteThreadConfirmScreen)

        await pilot.click("#confirm")
        await pilot.pause()
        assert deleted == ["thread-12345678-abcd"]


@pytest.mark.asyncio
async def test_thread_row_del_click_triggers_confirm_modal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify thread row del click triggers confirm modal."""
    screen = ThreadSelectorScreen(
        threads=[{"thread_id": "thread-12345678-abcd", "initial_prompt": "Deployment"}],
    )
    app = DummyThreadApp(screen)
    deleted: list[str] = []

    async def fake_delete(thread_id: str) -> None:
        """Provide fake delete test helper or fixture."""
        deleted.append(thread_id)

    monkeypatch.setattr(screen, "_delete_thread_async", fake_delete)

    async with app.run_test(size=(100, 30)) as pilot:
        opt_list = screen.query_one(CustomOptionList)
        screen.action_cursor_down()
        await pilot.pause()

        # Simulate click on [del] zone at the right edge
        opt_list.last_click_x = 95
        opt_list.action_select()
        await pilot.pause()

        # Confirm screen opened, no immediate deletion
        assert isinstance(app.screen, DeleteThreadConfirmScreen)
        assert deleted == []


@pytest.mark.asyncio
async def test_thread_selector_displays_title_with_fallback_and_dual_search() -> None:
    """Verify ThreadSelectorScreen displays LLM title when present, falls back to initial_prompt, and searches both."""
    threads = [
        {
            "thread_id": "thread-11112222-aaaa",
            "message_count": 4,
            "title": "Refining SageMaker Agent Plugin",
            "initial_prompt": "can you help me refine the sagemaker plugin manifest",
            "created_at": "2026-10-12T05:00:00Z",
        },
        {
            "thread_id": "thread-33334444-bbbb",
            "message_count": 1,
            "title": None,
            "initial_prompt": "what is yours capability ?",
            "created_at": "2026-10-12T07:00:00Z",
        },
    ]

    screen = ThreadSelectorScreen(threads=threads, current_thread_id="thread-11112222-aaaa")
    app = DummyThreadApp(screen)

    async with app.run_test(size=(120, 30)) as pilot:
        opt_list = screen.query_one(CustomOptionList)
        assert opt_list.option_count == 3

        first_thread_opt = str(opt_list.get_option_at_index(1).prompt)
        second_thread_opt = str(opt_list.get_option_at_index(2).prompt)
        assert "Refining SageMaker Agent Plugin" in first_thread_opt
        assert "what is yours capability ?" in second_thread_opt

        search = screen.query_one(SearchInput)

        # Search by LLM-generated title keyword ("Refining")
        search.value = "Refining"
        await pilot.pause()
        assert opt_list.option_count == 2
        assert opt_list.get_option_at_index(1).id == "thread-11112222-aaaa"

        # Search by underlying initial_prompt keyword ("manifest") not in the title
        search.value = "manifest"
        await pilot.pause()
        assert opt_list.option_count == 2
        assert opt_list.get_option_at_index(1).id == "thread-11112222-aaaa"

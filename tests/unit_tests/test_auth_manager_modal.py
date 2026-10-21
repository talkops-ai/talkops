"""Unit tests for AuthManagerScreen OpsModalScreen migration."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList

from talkops.ui.widgets.auth import AuthPromptScreen
from talkops.ui.widgets.auth_manager import AuthManagerScreen, _KNOWN_PROVIDERS


class DummyAuthApp(App[None]):
    """Minimal textual app to mount and test AuthManagerScreen."""

    def __init__(self, screen_instance: AuthManagerScreen) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_auth_manager_screen_mount_and_options() -> None:
    screen = AuthManagerScreen(initial_provider="openai")
    app = DummyAuthApp(screen)

    async with app.run_test() as pilot:
        opt_list = screen.query_one("#auth-mgr-options", OptionList)
        assert opt_list is not None
        assert opt_list.has_focus
        assert opt_list.option_count == len(_KNOWN_PROVIDERS)

        # Highlighted index matches initial_provider
        openai_idx = _KNOWN_PROVIDERS.index("openai")
        assert opt_list.highlighted == openai_idx

        # Close
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1


class DummyAuthPromptApp(App[None]):
    """Minimal textual app to mount and test AuthPromptScreen."""

    def __init__(self, screen_instance: AuthPromptScreen) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_auth_prompt_screen_mount_and_cancel() -> None:
    from textual.widgets import Input, Static
    from talkops.ui.widgets.auth import AuthCheckbox, AuthPromptScreen

    screen = AuthPromptScreen(provider="google_genai")
    app = DummyAuthPromptApp(screen)

    async with app.run_test() as pilot:
        assert screen.dialog is not None
        assert screen.dialog.border_title is not None
        assert "Google Gemini" in str(screen.dialog.border_title)

        # Verify key input and instructions render even when reason is None
        key_input = screen.query_one("#auth-prompt-input", Input)
        assert key_input is not None
        assert key_input.display is True
        instructions = screen.query_one("#auth-prompt-key-instructions", Static)
        assert instructions is not None

        # Verify F2 toggles advanced controls including Vertex AI checkbox
        vertex_cb = screen.query_one("#auth-prompt-vertexai-cb", AuthCheckbox)
        assert vertex_cb.display is False
        screen.action_toggle_advanced()
        await pilot.pause()
        assert vertex_cb.display is True

        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1

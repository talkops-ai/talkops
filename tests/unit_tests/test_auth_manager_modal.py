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

"""Unit tests for AuthManagerScreen OpsModalScreen migration."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList, Static

from talkops.ui.widgets.auth import AuthPromptScreen
from talkops.ui.widgets.auth_manager import _KNOWN_PROVIDERS, AuthManagerScreen


class DummyAuthApp(App[None]):
    """Minimal textual app to mount and test AuthManagerScreen."""

    def __init__(self, screen_instance: AuthManagerScreen) -> None:
        """Initialize DummyAuthApp."""
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        """Compose and yield child widgets for this component."""
        yield from ()

    async def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_auth_manager_screen_mount_and_options() -> None:
    """Verify auth manager screen mount and options."""
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
        """Initialize DummyAuthPromptApp."""
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        """Compose and yield child widgets for this component."""
        yield from ()

    async def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_auth_prompt_screen_mount_and_cancel() -> None:
    """Verify auth prompt screen mount and cancel."""
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


@pytest.mark.asyncio
async def test_auth_manager_does_not_show_unconfigured_cloud_providers_as_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify AuthManagerScreen marks Gemini, Bedrock, and Vertex AI as MISSING when not configured."""
    from talkops.config.settings import get_settings

    s = get_settings()
    for k in (
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_CLOUD_PROJECT",
        "GCP_PROJECT_ID",
        "VERTEXAI_PROJECT",
        "VERTEXAI_API_KEY",
        "GOOGLE_VERTEXAI_API_KEY",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "TALKOPS_AWS_ACCESS_KEY_ID",
        "AWS_BEARER_TOKEN_BEDROCK",
        "BEDROCK_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)

    # Simulate ambient shell AWS CLI keys present at startup
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA_AMBIENT")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SECRET_AMBIENT")
    monkeypatch.setattr(
        "talkops.model.config._INITIAL_SHELL_AWS_CREDS",
        {"AWS_ACCESS_KEY_ID": "AKIA_AMBIENT", "AWS_SECRET_ACCESS_KEY": "SECRET_AMBIENT"},
    )
    monkeypatch.setattr("talkops.config.toml_config.load_default_model", lambda: None)
    monkeypatch.setattr("talkops.config.toml_config.load_recent_model", lambda: None)

    monkeypatch.setattr(s, "google_api_key", None, raising=False)
    monkeypatch.setattr(s, "google_cloud_project", None, raising=False)
    monkeypatch.setattr(s, "google_genai_use_vertexai", False, raising=False)
    monkeypatch.setattr(s, "model_name", None, raising=False)
    monkeypatch.setattr(s, "model", None, raising=False)

    screen = AuthManagerScreen()
    options = {opt.id: str(opt.prompt) for opt in screen._build_options(width=70)}

    for prov in ("google_genai", "bedrock", "google_vertexai", "google_anthropic_vertex"):
        assert "MISSING" in options[prov], f"Expected {prov} to be MISSING, got: {options[prov]}"
        assert "Not set" in options[prov], f"Expected {prov} to be Not set, got: {options[prov]}"


@pytest.mark.asyncio
async def test_selecting_auto_jev_in_model_selector_prompts_typesafe_auth_not_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify selecting Autonomous Tier Routing (TypeSafe Jev System 1) prompts for TypeSafe AI (Jev) key, not OpenAI."""
    from talkops.config.settings import get_settings
    from talkops.model.config import resolve_model_spec
    from talkops.ui.app import TalkOpsApp
    from talkops.ui.widgets.auth import AuthPromptScreen
    from talkops.ui.widgets.model_selector import ModelSelectorScreen

    s = get_settings()
    for k in ("TYPESAFE_API_KEY", "JEV_API_KEY", "TALKOPS_TYPESAFE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(s, "typesafe_api_key", None, raising=False)

    assert resolve_model_spec("auto") == ("typesafe", "auto")
    assert resolve_model_spec("dynamic") == ("typesafe", "dynamic")

    app = TalkOpsApp()
    async with app.run_test(headless=True) as pilot:
        s = get_settings()
        for k in ("TYPESAFE_API_KEY", "JEV_API_KEY", "TALKOPS_TYPESAFE_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr(s, "typesafe_api_key", None, raising=False)

        screen = ModelSelectorScreen()
        app.push_screen(screen)
        await pilot.pause()

        screen._select_with_auth_check("auto", "dynamic", None)
        await pilot.pause()

        top_screen = app.screen
        assert isinstance(top_screen, AuthPromptScreen)
        assert top_screen._provider == "typesafe"
        assert top_screen._env_var == "TYPESAFE_API_KEY"
        assert "TypeSafe AI (Jev)" in str(top_screen.dialog.border_title)
        assert "OpenAI" not in str(top_screen.dialog.border_title)


@pytest.mark.asyncio
async def test_auth_prompt_screen_vertex_ai_saves_all_env_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Verify AuthPromptScreen for google_vertexai renders project/location/api_version inputs and persists env vars."""
    import os

    from talkops.auth.storage import CompositeCredentialStorage
    from talkops.ui.widgets.auth import AuthPromptScreen

    env_file = tmp_path / ".env"
    auth_file = tmp_path / "auth.json"
    storage = CompositeCredentialStorage(auth_file_path=auth_file, env_file_path=env_file)
    monkeypatch.setattr("talkops.auth.storage.get_credential_storage", lambda: storage)
    monkeypatch.setattr("talkops.ui.widgets.auth.upsert_env_vars", lambda d: storage.set_provider_key("google_vertexai", d.get("VERTEXAI_API_KEY") or d.get("GOOGLE_CLOUD_PROJECT", ""), extra_env=d, sync_dotenv=True))

    for k in (
        "VERTEXAI_API_KEY",
        "GOOGLE_VERTEXAI_API_KEY",
        "GOOGLE_CLOUD_PROJECT",
        "VERTEXAI_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "VERTEXAI_LOCATION",
        "VERTEXAI_API_VERSION",
        "GOOGLE_CLOUD_QUOTA_PROJECT",
    ):
        monkeypatch.delenv(k, raising=False)

    screen = AuthPromptScreen(provider="google_vertexai")
    app = DummyAuthPromptApp(screen)

    async with app.run_test() as pilot:
        key_inp = screen.query_one("#auth-prompt-input", Input)
        proj_inp = screen.query_one("#auth-prompt-vertex-project", Input)
        loc_inp = screen.query_one("#auth-prompt-vertex-location", Input)
        ver_inp = screen.query_one("#auth-prompt-vertex-api-version", Input)
        quota_inp = screen.query_one("#auth-prompt-vertex-quota-project", Input)

        assert key_inp.display is True
        assert proj_inp.display is True
        assert loc_inp.display is True
        assert loc_inp.value == "global"
        assert ver_inp.display is False
        assert quota_inp.display is False

        # Toggle F2 Advanced
        screen.action_toggle_advanced()
        await pilot.pause()
        assert ver_inp.display is True
        assert quota_inp.display is True

        key_inp.value = "AQ.test-vertex-key-123"
        proj_inp.value = "otel-demo-499213"
        loc_inp.value = "global"
        ver_inp.value = "v1"
        quota_inp.value = "otel-demo-499213"

        screen.action_save()
        await pilot.pause()

    assert os.environ.get("VERTEXAI_API_KEY") == "AQ.test-vertex-key-123"
    assert os.environ.get("GOOGLE_CLOUD_PROJECT") == "otel-demo-499213"
    assert os.environ.get("VERTEXAI_PROJECT") == "otel-demo-499213"
    assert os.environ.get("GOOGLE_CLOUD_LOCATION") == "global"
    assert os.environ.get("VERTEXAI_LOCATION") == "global"
    assert os.environ.get("VERTEXAI_API_VERSION") == "v1"
    assert os.environ.get("GOOGLE_CLOUD_QUOTA_PROJECT") == "otel-demo-499213"
    env_text = env_file.read_text(encoding="utf-8")
    assert "GOOGLE_CLOUD_PROJECT=otel-demo-499213" in env_text
    assert "GOOGLE_CLOUD_LOCATION=global" in env_text
    assert "VERTEXAI_API_KEY=AQ.test-vertex-key-123" in env_text
    assert "VERTEXAI_API_VERSION=v1" in env_text




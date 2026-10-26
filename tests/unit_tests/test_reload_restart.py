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

"""Unit tests for /reload and /restart slash commands, formatting, and lifecycles."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from talkops.commands._base import CommandContext
from talkops.commands.power.runtime import ReloadHandler, RestartHandler
from talkops.mcp.mcp_info import MCPServerInfo, MCPToolInfo
from talkops.server import ServerConfig, ServerProcess
from talkops.ui.app import (
    TalkOpsApp,
    _format_mcp_server_changes,
    _ServerRespawnResult,
)
from talkops.ui.command_registry import (
    COMMANDS,
    BypassTier,
    get_all_entries,
)
from talkops.ui.widgets.autocomplete import CompletionView, SlashCommandController
from talkops.ui.widgets.messages import SystemMessage

# ── 1. Autocomplete Order & SlashCommand Metadata Tests ──────────


class MockCompletionView(CompletionView):
    """Stub view to capture autocomplete render calls."""

    def __init__(self) -> None:
        """Initialize MockCompletionView."""
        self.suggestions: list[tuple[str, str]] = []
        self.selected_index: int = 0
        self.visible: bool = False

    def render_completion_suggestions(
        self, suggestions: list[tuple[str, str]], selected_index: int
    ) -> None:
        """Provide render completion suggestions test helper or fixture."""
        self.suggestions = suggestions
        self.selected_index = selected_index
        self.visible = True

    def clear_completion_suggestions(self) -> None:
        """Provide clear completion suggestions test helper or fixture."""
        self.suggestions = []
        self.visible = False

    def replace_completion_range(self, start: int, end: int, replacement: str) -> None:
        """Provide replace completion range test helper or fixture."""
        pass


def test_autocomplete_re_order_and_descriptions():
    """Verify typing `/re` displays remember, reload, and restart in exact order."""
    entries = get_all_entries()
    view = MockCompletionView()
    controller = SlashCommandController(entries, view)

    # User types "/re" with cursor at index 3
    controller.on_text_changed("/re", 3)

    assert len(controller._suggestions) >= 3
    top_three = controller._suggestions[:3]

    expected = [
        ("/remember", "Save useful context to memory or skills"),
        ("/reload", "Reload environment and config"),
        ("/restart", "Restart the agent server"),
    ]
    assert top_three == expected


def test_slash_command_registry_metadata():
    """Verify SlashCommand specifications for /remember, /reload, /restart."""
    by_name = {cmd.name: cmd for cmd in COMMANDS}

    assert "/remember" in by_name
    remember_cmd = by_name["/remember"]
    assert remember_cmd.description == "Save useful context to memory or skills"
    assert remember_cmd.bypass_tier == BypassTier.QUEUED
    assert remember_cmd.argument_hint == "[<text>]"

    assert "/reload" in by_name
    reload_cmd = by_name["/reload"]
    assert reload_cmd.description == "Reload environment and config"
    assert reload_cmd.bypass_tier == BypassTier.QUEUED
    assert "skills" in reload_cmd.hidden_keywords
    assert "themes" in reload_cmd.hidden_keywords

    assert "/restart" in by_name
    restart_cmd = by_name["/restart"]
    assert restart_cmd.description == "Restart the agent server"
    assert restart_cmd.bypass_tier == BypassTier.ALWAYS
    assert "server" in restart_cmd.hidden_keywords


# ── 2. Command Handlers Execution Tests ─────────────────────────


@pytest.mark.asyncio
async def test_reload_handler_delegation_to_app():
    """Verify ReloadHandler schedules reload on TalkOpsApp."""
    mock_app = MagicMock()
    mock_app._schedule_reload = MagicMock()

    ctx = CommandContext(app=mock_app, raw_command="/reload", args="")
    handler = ReloadHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    assert result.mount_as_app_message is False
    assert mock_app._schedule_reload.called


@pytest.mark.asyncio
async def test_reload_handler_fallback_without_app():
    """Verify ReloadHandler fallback when app is not provided."""
    mock_settings = MagicMock()
    mock_settings.reload_from_environment.return_value = ["OPENAI_API_KEY changed"]

    ctx = CommandContext(app=None, settings=mock_settings, raw_command="/reload", args="")
    handler = ReloadHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    assert result.mount_as_app_message is True
    assert result.message is not None
    assert "Configuration reloaded. Changes:" in result.message
    assert "OPENAI_API_KEY changed" in result.message


@pytest.mark.asyncio
async def test_restart_handler_delegation_to_app():
    """Verify RestartHandler delegates to app._handle_restart_command."""
    mock_app = MagicMock()
    mock_app._handle_restart_command = AsyncMock()

    ctx = CommandContext(app=mock_app, raw_command="/restart", args="")
    handler = RestartHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    assert result.mount_as_app_message is False
    mock_app._handle_restart_command.assert_awaited_once_with(command="/restart")


@pytest.mark.asyncio
async def test_restart_handler_fallback_without_app():
    """Verify RestartHandler fallback when app is not provided."""
    ctx = CommandContext(app=None, raw_command="/restart", args="")
    handler = RestartHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    assert result.mount_as_app_message is True
    assert result.message == "Restart complete."


# ── 3. _format_mcp_server_changes Tests ──────────────────────────


def test_format_mcp_server_changes_when_current_is_none():
    """Verify formatting when current server list is None."""
    # Without error
    msg = _format_mcp_server_changes(previous=None, current=None)
    assert "MCP server changes couldn't be determined; use /mcp to check." == msg

    # With error
    msg_err = _format_mcp_server_changes(
        previous=None, current=None, error="ConnectionRefusedError"
    )
    assert "(ConnectionRefusedError)" in msg_err


def test_format_mcp_server_changes_no_baseline():
    """Verify formatting when previous is None but current servers are loaded."""
    current = [
        MCPServerInfo(name="srv1", status="ok"),
        MCPServerInfo(name="srv2", status="unauthenticated"),
        MCPServerInfo(name="err_cfg", transport="config"),
    ]
    msg = _format_mcp_server_changes(previous=None, current=current)
    assert "no baseline metadata" in msg
    assert "currently loaded: srv1" in msg
    assert "unavailable: srv2 (unauthenticated)" in msg
    assert "config errors: err_cfg" in msg


def test_format_mcp_server_changes_no_changes():
    """Verify formatting when baseline and current are identical and healthy."""
    servers = [
        MCPServerInfo(name="filesystem", status="ok"),
        MCPServerInfo(name="github", status="ok"),
    ]
    msg = _format_mcp_server_changes(previous=servers, current=servers)
    assert msg == "MCP server changes: no changes detected."


def test_format_mcp_server_changes_stuck_needing_attention():
    """Verify formatting when unchanged but a server still needs attention."""
    servers = [
        MCPServerInfo(name="filesystem", status="ok"),
        MCPServerInfo(name="github", status="unauthenticated"),
    ]
    msg = _format_mcp_server_changes(previous=servers, current=servers)
    assert "MCP server changes: no changes detected (still needs attention: github; use /mcp)." == msg


def test_format_mcp_server_changes_diffs():
    """Verify formatting for added, removed, failing, and reconfigured servers."""
    previous = [
        MCPServerInfo(name="unchanged", status="ok"),
        MCPServerInfo(name="to_remove", status="ok"),
        MCPServerInfo(name="now_failing", status="ok"),
        MCPServerInfo(
            name="tool_changed",
            status="ok",
            tools=(MCPToolInfo(name="t1"),),
        ),
    ]
    current = [
        MCPServerInfo(name="unchanged", status="ok"),
        MCPServerInfo(name="newly_added", status="ok"),
        MCPServerInfo(name="now_failing", status="error"),
        MCPServerInfo(
            name="tool_changed",
            status="ok",
            tools=(MCPToolInfo(name="t1"), MCPToolInfo(name="t2")),
        ),
    ]
    msg = _format_mcp_server_changes(previous=previous, current=current)
    assert "  - Loaded: newly_added" in msg
    assert "  - Removed: to_remove" in msg
    assert "  - Failed to load: now_failing" in msg
    assert "  - Reconfigured: tool_changed" in msg


# ── 4. TalkOpsApp /reload and /restart Lifecycle Tests ─────────


@pytest.mark.asyncio
async def test_run_reload_unlocked_report_matches_expected():
    """Verify /reload outputs the expected structured reload report."""
    app = TalkOpsApp(defer_server_start=True)

    # Mock settings reload (no changes)
    app._settings = MagicMock()
    app._settings.reload_from_environment.return_value = []

    # Mock theme reload
    with patch("talkops.ui.theme.reload_registry"), patch(
        "talkops.ui.theme.register_app_themes"
    ):
        # Mock skill discovery (13 plugin skills)
        app.get_discovered_skills = MagicMock(
            return_value=[{"name": f"test_plugin:skill_{i}"} for i in range(13)]
        )

        # Mock plugin discovery (1 plugin, 0 hooks, 0 MCP)
        mock_plugin = MagicMock()
        mock_plugin.manifest = MagicMock()
        mock_plugin.manifest.hooks = ()
        mock_plugin_result = MagicMock(plugins=(mock_plugin,))

        # Mock server restart for plugin MCP
        app._server_proc = MagicMock()
        app._restart_server_manual_result = AsyncMock(
            return_value=_ServerRespawnResult(
                restarted=True,
                mcp_server_info=[],
                mcp_status="fresh",
                mcp_error=None,
            )
        )
        app._mcp_server_info = []

        mounted_messages = []
        app._mount_message = AsyncMock(side_effect=lambda msg: mounted_messages.append(msg))

        with patch(
            "talkops.plugins.discovery.discover_plugins",
            return_value=mock_plugin_result,
        ), patch(
            "talkops.plugins.adapters.mcp.plugin_mcp_configs",
            return_value=[],
        ), patch(
            "talkops.model.config.clear_caches"
        ):
            await app._run_reload_unlocked()

        assert len(mounted_messages) == 1
        msg = mounted_messages[0]
        assert isinstance(msg, SystemMessage)
        content = getattr(msg, "_raw_content", "")

        expected_lines = [
            "Configuration reloaded. No changes detected.",
            "Model config caches cleared.",
            "Theme registry reloaded.",
            "Plugins: 1 plugin · 13 skills · 0 plugin MCP servers · 0 hooks",
            "Agent server restarted for plugin MCP.",
            "MCP server changes: no changes detected.",
        ]
        for line in expected_lines:
            assert line in content, f"Expected line {line!r} not in:\n{content}"


@pytest.mark.asyncio
async def test_run_restart_respawn_flow():
    """Verify _run_restart_respawn success emits 'Restart complete.'."""
    app = TalkOpsApp(defer_server_start=True)

    app._restart_server_manual_result = AsyncMock(
        return_value=_ServerRespawnResult(restarted=True)
    )

    mounted_messages = []
    app._mount_message = AsyncMock(side_effect=lambda msg: mounted_messages.append(msg))

    transient_mock = MagicMock()
    transient_mock.remove = AsyncMock()
    app._mount_transient_app_message = AsyncMock(return_value=transient_mock)

    restarted = await app._run_restart_respawn(propagate_errors=False)

    assert restarted is True
    assert any(
        isinstance(m, SystemMessage) and getattr(m, "_raw_content", "") == "Restart complete."
        for m in mounted_messages
    )


# ── 5. ServerProcess restart() Tests ─────────────────────────────


@pytest.mark.asyncio
async def test_server_process_restart():
    """Verify ServerProcess.restart() calls _stop_process and start."""
    cfg = ServerConfig()
    proc = ServerProcess(cfg, port=9999)

    proc._stop_process = MagicMock()
    proc.start = MagicMock()

    await proc.restart(timeout=5.0)

    assert proc._stop_process.called
    assert proc.start.called


@pytest.mark.asyncio
async def test_on_server_start_failed_missing_credentials_message():
    """Verify _on_server_start_failed does not hardcode OpenAI and provides provider hint."""
    from talkops.exceptions import MissingCredentialsError
    from talkops.ui.widgets.messages import ErrorMessage

    app = TalkOpsApp(defer_server_start=True)
    mounted: list[Any] = []
    app._mount_message = AsyncMock(side_effect=lambda msg: mounted.append(msg))

    err = MissingCredentialsError("API key missing", provider="google_genai", env_var="GEMINI_API_KEY")
    event = TalkOpsApp.ServerStartFailed(err)

    await app._on_server_start_failed(event)

    assert app._connecting is False
    assert app._server_startup_error == err
    assert len(mounted) == 1
    assert isinstance(mounted[0], ErrorMessage)
    content = mounted[0]._raw_content
    assert "google_genai" in content
    assert "GEMINI_API_KEY" in content
    assert "OPENAI_API_KEY" not in content


@pytest.mark.asyncio
async def test_on_server_start_failed_generic_error_message():
    """Verify general server startup errors do not contain hardcoded OpenAI/Anthropic references."""
    from talkops.ui.widgets.messages import ErrorMessage

    app = TalkOpsApp(defer_server_start=True)
    mounted: list[Any] = []
    app._mount_message = AsyncMock(side_effect=lambda msg: mounted.append(msg))

    err = TimeoutError("Server at http://127.0.0.1:51314 failed to become healthy within 60.0s")
    event = TalkOpsApp.ServerStartFailed(err)

    await app._on_server_start_failed(event)

    assert len(mounted) == 1
    assert isinstance(mounted[0], ErrorMessage)
    content = mounted[0]._raw_content
    assert "51314" in content
    assert "OPENAI_API_KEY" not in content


@pytest.mark.asyncio
async def test_restart_lifecycle_deferred_server_guidance():
    """Verify running restart when server is deferred tells user it's waiting for a model."""
    app = TalkOpsApp(
        defer_server_start=True,
        server_kwargs={"assistant_id": "talkops", "interactive": True},
    )

    restored_notices: list[str] = []
    app._restore_queue_to_input = AsyncMock(
        side_effect=lambda notice, empty_notice=None: restored_notices.append(notice)
    )

    success = await app._run_restart_command()
    assert success is False
    assert len(restored_notices) == 1
    assert "Server startup is waiting for a model" in restored_notices[0]
    assert "remote LangGraph server" not in restored_notices[0]


@pytest.mark.asyncio
async def test_restart_lifecycle_remote_detection():
    """Verify running restart when server_kwargs is None indicates remote server."""
    app = TalkOpsApp(server_url="http://remote-server:2024", server_kwargs=None)

    restored_notices: list[str] = []
    app._restore_queue_to_input = AsyncMock(
        side_effect=lambda notice, empty_notice=None: restored_notices.append(notice)
    )

    success = await app._run_restart_command()
    assert success is False
    assert len(restored_notices) == 1
    assert "connected to a remote LangGraph server" in restored_notices[0]


@pytest.mark.asyncio
async def test_run_reload_reports_aws_and_pool_state():
    """Verify /reload report includes active AWS configuration and Agent model pool lines."""
    app = TalkOpsApp(defer_server_start=True)
    app._settings = MagicMock()
    app._settings.reload_from_environment.return_value = []
    app._settings.aws_profile = "test-profile"
    app._settings.aws_region = "us-west-2"

    mounted_messages = []
    app._mount_message = AsyncMock(side_effect=lambda msg: mounted_messages.append(msg))
    app.get_discovered_skills = MagicMock(return_value=[])

    with patch("talkops.ui.theme.reload_registry"), patch("talkops.ui.theme.register_app_themes"), patch(
        "talkops.plugins.discovery.discover_plugins",
        return_value=MagicMock(plugins=()),
    ), patch(
        "talkops.plugins.adapters.mcp.plugin_mcp_configs",
        return_value=[],
    ), patch(
        "talkops.model.config.clear_caches"
    ), patch(
        "talkops.config.toml_config.load_agent_pool",
        return_value={"fast": "gpt-4o-mini", "standard": "gpt-4o", "powerful": "o3", "provider": "openai"},
    ), patch(
        "talkops.config.cloud_profiles.get_active_aws_profile",
        return_value="test-profile",
    ), patch(
        "talkops.config.cloud_profiles.get_active_aws_region",
        return_value="us-west-2",
    ):
        await app._run_reload_unlocked()

    assert len(mounted_messages) == 1
    content = getattr(mounted_messages[0], "_raw_content", "")
    assert "**AWS configuration:** profile `test-profile` (us-west-2)" in content
    assert "**Agent model pool:** fast=`gpt-4o-mini`, standard=`gpt-4o`, powerful=`o3` (provider: openai)" in content
    assert "Model config caches cleared." in content



@pytest.mark.asyncio
async def test_run_reload_detects_aws_profile_and_pool_changes():
    """Verify /reload report highlights changes when AWS profile or agent pool are updated."""
    app = TalkOpsApp(defer_server_start=True)
    app._settings = MagicMock()
    app._settings.reload_from_environment.return_value = []

    # Prior snapshot was default / None
    app._last_reload_snapshot = {
        "aws_profile": "default",
        "aws_region": "us-east-1",
        "agent_pool": None,
        "default_model": None,
        "default_agent": None,
        "theme": "dark",
    }

    mounted_messages = []
    app._mount_message = AsyncMock(side_effect=lambda msg: mounted_messages.append(msg))
    app.get_discovered_skills = MagicMock(return_value=[])

    new_pool = {"fast": "fast-m", "standard": "std-m", "powerful": "pow-m", "provider": "multi"}

    with patch("talkops.ui.theme.reload_registry"), patch("talkops.ui.theme.register_app_themes"), patch(
        "talkops.plugins.discovery.discover_plugins",
        return_value=MagicMock(plugins=()),
    ), patch(
        "talkops.plugins.adapters.mcp.plugin_mcp_configs",
        return_value=[],
    ), patch(
        "talkops.model.config.clear_caches"
    ), patch(
        "talkops.config.toml_config.load_agent_pool",
        return_value=new_pool,
    ), patch(
        "talkops.config.cloud_profiles.get_active_aws_profile",
        return_value="prod-profile",
    ), patch(
        "talkops.config.cloud_profiles.get_active_aws_region",
        return_value="eu-west-1",
    ):
        await app._run_reload_unlocked()

    assert len(mounted_messages) == 1
    content = getattr(mounted_messages[0], "_raw_content", "")
    assert "Configuration reloaded. Changes:" in content
    assert "aws_profile: default -> prod-profile" in content
    assert "aws_region: us-east-1 -> eu-west-1" in content
    assert "agent_pool: default (dynamic discovery) -> fast='fast-m', standard='std-m', powerful='pow-m' (provider: multi)" in content


def test_clear_caches_flushes_pool_and_aws_caches():
    """Verify clear_caches flushes model pool manager instances and AWS profile cache."""
    from talkops.config import cloud_profiles
    from talkops.model.config import clear_caches
    from talkops.model.pool import get_model_pool_manager

    pool_mgr = get_model_pool_manager()
    mock_instance = MagicMock()
    pool_mgr._instances["test_key"] = mock_instance  # type: ignore[assignment]
    assert "test_key" in pool_mgr._instances


    cloud_profiles._cached_profiles = [MagicMock()]
    assert cloud_profiles._cached_profiles is not None

    clear_caches()

    # Model pool manager singleton reset or instances cleared
    new_pool_mgr = get_model_pool_manager()
    assert "test_key" not in new_pool_mgr._instances
    assert cloud_profiles._cached_profiles is None


def test_settings_reload_from_environment_picks_up_toml_aws_and_pool(monkeypatch):
    """Verify settings.reload_from_environment reloads AWS profile/region and pool from config.toml."""
    from talkops.config.settings import Settings

    # Isolate environment from outer shell environment variables (e.g. AWS_REGION)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    s = Settings(aws_profile="old-prof", aws_region="us-east-1", agent_pool=None)

    with patch(
        "talkops.config.settings.load_config_toml",
        return_value={
            "aws": {"profile": "new-prof", "region": "eu-central-1"},
            "agent_pool": {"fast": "gpt-5-mini", "standard": "gpt-5", "powerful": "gpt-5-max", "provider": "openai"},
        },
    ):
        changes = s.reload_from_environment()

    assert s.aws_profile == "new-prof"
    assert s.aws_region == "eu-central-1"
    assert s.agent_pool == {"fast": "gpt-5-mini", "standard": "gpt-5", "powerful": "gpt-5-max", "provider": "openai"}
    assert any("aws_profile: old-prof -> new-prof" in c for c in changes)
    assert any("aws_region: us-east-1 -> eu-central-1" in c for c in changes)
    assert any("agent_pool: default (dynamic discovery) -> fast='gpt-5-mini'" in c for c in changes)


# ── 6. Status Bar Connection & Queue Sync & Lifecycle Guard Tests ──


def test_sync_status_connection_and_queued_do_not_clobber_each_other():
    """Verify _sync_status_connection and _sync_status_queued drive set_connection and set_queued independently."""
    from talkops.ui.app import QueuedMessage

    app = TalkOpsApp(defer_server_start=True)
    mock_bar = MagicMock()
    mock_bar._busy_message = ""
    app._status_bar = mock_bar

    # Initial connecting state
    app._connecting = True
    app._reconnecting = False
    app._sync_status_connection()
    mock_bar.set_connection.assert_called_with("connecting")

    # Reconnecting state (during /restart or /reload)
    app._connecting = True
    app._reconnecting = True
    app._sync_status_connection()
    mock_bar.set_connection.assert_called_with("reconnecting")

    # Queue updates during reconnect must use set_queued and never call set_status("Ready")
    app._pending_messages.append(QueuedMessage(text="hello", mode="normal"))
    app._sync_status_queued()
    mock_bar.set_queued.assert_called_with(1)
    mock_bar.set_status.assert_not_called()

    # Clearing queue during reconnect keeps connection indicator intact
    app._pending_messages.clear()
    app._sync_status_queued()
    mock_bar.set_queued.assert_called_with(0)
    mock_bar.set_status.assert_not_called()

    # Connection finished
    app._connecting = False
    app._reconnecting = False
    app._sync_status_connection()
    mock_bar.set_connection.assert_called_with("")


@pytest.mark.asyncio
async def test_finalize_connection_preserves_thread_on_respawn():
    """Verify finalize_connection initializes thread and startup hooks only once across respawns."""
    app = TalkOpsApp(
        defer_server_start=True,
        startup_cmd="echo hi",
        initial_prompt="hello world",
    )
    app._mount_message = AsyncMock()
    app._submit_input = AsyncMock()

    # First connection initializes thread_id and runs startup hooks
    await app._finalize_connection(MagicMock())
    first_thread_id = app._agent_thread_id
    assert first_thread_id is not None
    assert app._initial_session_started is True
    assert app._submit_input.await_count == 2

    # Subsequent ServerReady (after /reload or /restart) preserves thread_id and skips startup hooks
    app._submit_input.reset_mock()
    await app._finalize_connection(MagicMock())
    assert app._agent_thread_id == first_thread_id
    assert app._submit_input.await_count == 0


@pytest.mark.asyncio
async def test_submit_input_coalesces_duplicate_reload_and_queues_during_reload():
    """Verify duplicate /reload bypasses queue to coalesce while normal prompts queue during _reloading."""
    app = TalkOpsApp(defer_server_start=True)
    app._reloading = True
    app._mount_message = AsyncMock()
    app._process_message = AsyncMock()

    # Duplicate /reload bypasses the queue so _schedule_reload can coalesce it
    await app._submit_input("/reload", "command")
    app._process_message.assert_awaited_once_with("/reload", "command")
    assert len(app._pending_messages) == 0

    # Normal prompt submitted while _reloading is queued
    app._process_message.reset_mock()
    await app._submit_input("check cluster health", "normal")
    app._process_message.assert_not_awaited()
    assert len(app._pending_messages) == 1
    assert app._pending_messages[0].text == "check cluster health"


@pytest.mark.asyncio
async def test_handle_restart_command_sets_defer_flag_before_awaiting_echo():
    """Verify handle_restart_command records _restart_requested_during_reload before awaiting UserMessage mount."""
    app = TalkOpsApp(defer_server_start=True)
    app._reloading = True
    mock_task = MagicMock()
    mock_task.done.return_value = False
    app._reload_task = mock_task

    flag_at_first_mount: list[bool] = []

    async def _on_mount(_widget: Any) -> None:
        flag_at_first_mount.append(app._restart_requested_during_reload)

    app._mount_message = AsyncMock(side_effect=_on_mount)

    await app._handle_restart_command("/restart")
    assert flag_at_first_mount[0] is True
    assert app._restart_requested_during_reload is True


@pytest.mark.asyncio
async def test_run_reload_unlocked_sets_and_clears_busy_status_and_reports_deferred_server():
    """Verify /reload sets Reloading... busy status and reports deferred local server state."""
    app = TalkOpsApp(
        defer_server_start=True,
        server_kwargs={"assistant_id": "talkops", "interactive": True},
    )
    app._settings = MagicMock()
    app._settings.reload_from_environment.return_value = []

    mock_bar = MagicMock()
    mock_bar._busy_message = ""

    def _set_busy(msg: str) -> None:
        mock_bar._busy_message = msg

    mock_bar.set_busy.side_effect = _set_busy
    app._status_bar = mock_bar

    mounted_messages: list[Any] = []
    app._mount_message = AsyncMock(side_effect=lambda msg: mounted_messages.append(msg))
    app.get_discovered_skills = MagicMock(return_value=[])

    with patch("talkops.ui.theme.reload_registry"), patch(
        "talkops.ui.theme.register_app_themes"
    ), patch(
        "talkops.plugins.discovery.discover_plugins",
        return_value=MagicMock(plugins=()),
    ), patch(
        "talkops.plugins.adapters.mcp.plugin_mcp_configs",
        return_value=[],
    ), patch(
        "talkops.model.config.clear_caches"
    ):
        await app._run_reload_unlocked()

    assert mock_bar.set_busy.call_args_list[0].args == ("Reloading...",)
    assert mock_bar.set_busy.call_args_list[-1].args == ("",)
    assert len(mounted_messages) == 1
    content = getattr(mounted_messages[0], "_raw_content", "")
    assert "local server hasn't started yet" in content


@pytest.mark.asyncio
async def test_respawn_server_passes_preload_kwargs_to_preload_mcp_server_info():
    """Verify _respawn_server forwards _mcp_preload_kwargs to preload_mcp_server_info without TypeError."""
    app = TalkOpsApp(
        defer_server_start=True,
        mcp_preload_kwargs={
            "mcp_config_path": None,
            "no_mcp": False,
            "trust_project_mcp": None,
        },
    )
    mock_proc = MagicMock()
    mock_proc.is_in_process = True
    mock_proc.restart = AsyncMock(return_value="inprocess://local")
    mock_proc.create_client = MagicMock(return_value=MagicMock())
    app._server_proc = mock_proc

    mock_info = MCPServerInfo(
        name="aws_docs",
        transport="stdio",
        tools=[MCPToolInfo(name="search", description="search docs")],
        status="ok",
    )
    with patch(
        "talkops.mcp.preload.preload_mcp_server_info",
        AsyncMock(return_value=[mock_info]),
    ) as mock_preload:
        res = await app._respawn_server()

    assert res.restarted is True
    assert res.mcp_status == "fresh"
    assert res.mcp_server_info == [mock_info]
    mock_preload.assert_awaited_once_with(
        mcp_config_path=None,
        no_mcp=False,
        trust_project_mcp=None,
    )



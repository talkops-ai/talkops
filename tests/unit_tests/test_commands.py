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

"""Unit tests for TalkOps slash command system and CommandRegistry."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from textual.app import App
from textual.widgets import Select

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.commands.core.exit_cmd import ExitHandler
from talkops.commands.registry import CommandRegistry


@pytest.fixture(autouse=True)
def reset_registry():
    """Provide reset registry test helper or fixture."""
    CommandRegistry.reset()
    yield
    CommandRegistry.reset()


def test_command_registry_discovery():
    """Verify command registry discovery."""
    registry = CommandRegistry.get_instance()
    all_cmds = registry.list_all_commands()
    assert "/exit" in all_cmds
    assert "/help" in all_cmds
    assert "/model" in all_cmds
    assert "/clear" in all_cmds


def test_exit_command_and_aliases():
    """Verify exit command and aliases."""
    registry = CommandRegistry.get_instance()
    exit_handler = registry.get_handler("/exit")
    assert exit_handler is not None
    assert isinstance(exit_handler, ExitHandler)

    quit_handler = registry.get_handler("/quit")
    assert quit_handler is not None
    assert isinstance(quit_handler, ExitHandler)

    q_handler = registry.get_handler("/q")
    assert q_handler is not None
    assert isinstance(q_handler, ExitHandler)


@pytest.mark.asyncio
async def test_exit_command_execution():
    """Verify exit command execution."""
    mock_app = MagicMock()
    mock_app.exit = MagicMock()
    mock_app.prepare_exit = MagicMock()

    ctx = CommandContext(app=mock_app, raw_command="/quit", args="")
    handler = ExitHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    assert mock_app.prepare_exit.called
    assert mock_app.exit.called


@pytest.mark.asyncio
async def test_dispatch_slash_command():
    """Verify dispatch slash command."""
    registry = CommandRegistry.get_instance()
    mock_app = MagicMock()
    mock_app.exit = MagicMock()

    ctx = CommandContext(app=mock_app, raw_command="/q", args="")
    result = await registry.dispatch("/q", ctx)
    assert result.success is True
    assert mock_app.exit.called


def test_plugin_command_registration_and_source():
    """Verify plugin command registration and source."""
    registry = CommandRegistry.get_instance()

    class CustomPluginHandler(BaseCommandHandler):
        """Test suite or helper class for custom plugin handler."""
        @property
        def name(self) -> str:
            """Return the primary slash command name."""
            return "/mycmd"

        @property
        def aliases(self) -> tuple[str, ...]:
            """Return alternative slash command aliases."""
            return ("/mc",)

        @property
        def category(self) -> CommandCategory:
            """Return the command category classification."""
            return CommandCategory.DEVOPS

        @property
        def safety_level(self) -> SafetyLevel:
            """Return the safety level required for this command."""
            return SafetyLevel.READ_ONLY

        @property
        def bypass_tier(self) -> BypassTier:
            """Return the execution queue bypass tier for this command."""
            return BypassTier.NORMAL

        async def execute(self, ctx: CommandContext) -> CommandResult:
            """Execute the command handler with the provided command context."""
            return CommandResult(success=True, feedback_message="custom output")

    custom_handler = CustomPluginHandler()
    registry.register(custom_handler, source="plugin:custom-aws-plugin")

    handler = registry.get_handler("/mycmd")
    assert handler is custom_handler
    alias_handler = registry.get_handler("/mc")
    assert alias_handler is custom_handler

    assert registry.get_source("/mycmd") == "plugin:custom-aws-plugin"
    assert registry.get_source("/mc") == "plugin:custom-aws-plugin"
    assert registry.get_source("/exit") == "built-in"

    plugin_cmds = registry.list_plugin_commands()
    assert "custom-aws-plugin" in plugin_cmds
    assert custom_handler in plugin_cmds["custom-aws-plugin"]


@pytest.mark.asyncio
async def test_config_command_execution():
    """Verify config command execution."""
    from talkops.commands.core.config_cmd import ConfigHandler
    from talkops.config.settings import Settings

    handler = ConfigHandler()
    test_settings = Settings(aws_region="us-east-1")

    # 1. /config path
    ctx_path = CommandContext(app=None, raw_command="/config path", args="path", settings=test_settings)
    res_path = await handler.execute(ctx_path)
    assert res_path.success is True
    assert res_path.message is not None
    assert "config.toml" in res_path.message

    # 2. /config show (text fallback)
    ctx_show = CommandContext(app=None, raw_command="/config show", args="show", settings=test_settings)
    res_show = await handler.execute(ctx_show)
    assert res_show.success is True
    assert res_show.message is not None
    assert "Current Configuration" in res_show.message
    assert "aws.region" in res_show.message

    # 3. /config set
    ctx_set = CommandContext(app=None, raw_command="/config set aws_region eu-central-1", args="set aws_region eu-central-1", settings=test_settings)
    res_set = await handler.execute(ctx_set)
    assert res_set.success is True
    assert test_settings.aws_region == "eu-central-1"

    # 4. /config reset
    ctx_reset = CommandContext(app=None, raw_command="/config reset aws_region", args="reset aws_region", settings=test_settings)
    res_reset = await handler.execute(ctx_reset)
    assert res_reset.success is True
    assert test_settings.aws_region == "us-east-1"


@pytest.mark.asyncio
async def test_pool_command_execution():
    """Verify pool command execution."""
    from talkops.commands.core.pool import PoolHandler
    from talkops.config.toml_config import clear_agent_pool, load_agent_pool

    handler = PoolHandler()
    clear_agent_pool()


    # 1. /pool status
    ctx_status = CommandContext(app=None, raw_command="/pool status", args="status")
    res_status = await handler.execute(ctx_status)
    assert res_status.success is True
    assert res_status.message is not None
    assert "Fast (Tier 0" in res_status.message

    # 2. /pool set fast=openai:fast standard=openai:std powerful=openai:pow
    ctx_set = CommandContext(
        app=None,
        raw_command="/pool set fast=openai:fast standard=openai:std powerful=openai:pow",
        args="set fast=openai:fast standard=openai:std powerful=openai:pow",
    )
    res_set = await handler.execute(ctx_set)
    assert res_set.success is True
    assert res_set.message is not None
    assert "Agent Model Pool Updated" in res_set.message
    saved = load_agent_pool()
    assert saved is not None
    assert saved["fast"] == "openai:fast"
    assert saved["standard"] == "openai:std"
    assert saved["powerful"] == "openai:pow"

    # 3. /pool clear
    ctx_clear = CommandContext(app=None, raw_command="/pool clear", args="clear")
    res_clear = await handler.execute(ctx_clear)
    assert res_clear.success is True
    assert load_agent_pool() is None

    # 4. /pool (interactive modal invocation)
    mock_app = MagicMock()
    mock_app._show_pool_selector = AsyncMock()
    ctx_ui = CommandContext(app=mock_app, raw_command="/pool", args="")
    res_ui = await handler.execute(ctx_ui)
    assert res_ui.success is True
    mock_app._show_pool_selector.assert_awaited_once()


@pytest.mark.asyncio
async def test_pool_selector_screen_mount():
    """Verify pool selector screen mount."""
    from talkops.config.toml_config import clear_agent_pool
    from talkops.ui.widgets.pool_selector import PoolSelectorScreen

    clear_agent_pool()


    class TestApp(App[None]):
        """Test suite or helper class for test app."""
        def on_mount(self) -> None:
            """Initialize component state when mounted into the DOM."""
            self.push_screen(PoolSelectorScreen(active_provider="openai"))

    test_app = TestApp()
    async with test_app.run_test(size=(120, 50)) as pilot:
        screen = test_app.screen
        assert isinstance(screen, PoolSelectorScreen)
        s_fast = screen.query_one("#select-fast", Select)
        s_std = screen.query_one("#select-standard", Select)
        assert s_fast is not None
        assert s_std is not None
        assert screen.query_one("#select-powerful") is not None

        # 1. Expand s_fast
        await pilot.click("#select-fast")
        assert s_fast.expanded

        # 2. Escape collapses dropdown without dismissing screen
        await pilot.press("escape")
        assert not s_fast.expanded
        assert isinstance(test_app.screen, PoolSelectorScreen)

        # 3. Outside click collapses dropdown
        await pilot.click("#select-fast")
        assert s_fast.expanded
        await pilot.click(".pool-title")
        assert not s_fast.expanded

        # 4. Click cancel button dismisses screen
        await pilot.click("#btn-cancel")
        assert not isinstance(test_app.screen, PoolSelectorScreen)


async def test_pool_selector_buttons_visible_in_constrained_terminal():
    """Verify pool selector buttons visible in constrained terminal."""
    from talkops.config.toml_config import clear_agent_pool
    from talkops.ui.widgets.pool_selector import PoolSelectorScreen

    clear_agent_pool()


    class TestApp(App[None]):
        """Test suite or helper class for test app."""
        def on_mount(self) -> None:
            """Initialize component state when mounted into the DOM."""
            self.push_screen(PoolSelectorScreen(active_provider="google_genai"))

    test_app = TestApp()
    # Test on a compact 80x26 terminal (standard laptop/split-pane height)
    async with test_app.run_test(size=(80, 26)):
        screen = test_app.screen
        assert isinstance(screen, PoolSelectorScreen)

        dialog = screen.query_one("PoolSelectorScreen > Vertical")
        btn_save = screen.query_one("#btn-save")
        btn_reset = screen.query_one("#btn-reset")
        btn_cancel = screen.query_one("#btn-cancel")
        help_bar = screen.query_one(".pool-help")

        dialog_bottom = dialog.region.y + dialog.region.height

        # Ensure all action buttons and help footer are within the visible dialog boundary
        assert btn_save.visible
        assert btn_reset.visible
        assert btn_cancel.visible
        assert (btn_save.region.y + btn_save.region.height) <= dialog_bottom
        assert (btn_reset.region.y + btn_reset.region.height) <= dialog_bottom
        assert (btn_cancel.region.y + btn_cancel.region.height) <= dialog_bottom
        assert (help_bar.region.y + help_bar.region.height) <= dialog_bottom


async def test_pool_selector_save_and_reset_actions():
    """Verify pool selector save and reset actions."""
    from talkops.config.toml_config import clear_agent_pool, load_agent_pool
    from talkops.ui.widgets.pool_selector import PoolSelectorScreen

    clear_agent_pool()


    saved_result = None

    def on_dismiss(res):
        """Provide on dismiss test helper or fixture."""
        nonlocal saved_result
        saved_result = res

    class TestApp(App[None]):
        """Test suite or helper class for test app."""
        def on_mount(self) -> None:
            """Initialize component state when mounted into the DOM."""
            self.push_screen(PoolSelectorScreen(active_provider="google_genai"), on_dismiss)

    test_app = TestApp()
    async with test_app.run_test(size=(100, 35)) as pilot:
        screen = test_app.screen
        assert isinstance(screen, PoolSelectorScreen)

        # Click save button
        await pilot.click("#btn-save")
        await pilot.pause()

        # Modal dismissed and saved pool persisted to config
        assert saved_result is not None
        assert "fast" in saved_result
        assert "standard" in saved_result
        assert "powerful" in saved_result

        persisted = load_agent_pool()
        assert persisted is not None
        assert persisted["fast"] == saved_result["fast"]

        # Reopen and test reset
        reset_result = None

        def on_reset_dismiss(res):
            """Provide on reset dismiss test helper or fixture."""
            nonlocal reset_result
            reset_result = res

        screen2 = PoolSelectorScreen(active_provider="google_genai")
        test_app.push_screen(screen2, on_reset_dismiss)
        await pilot.pause()

        await pilot.click("#btn-reset")
        await pilot.pause()

        assert reset_result == {"_cleared": True}
        assert load_agent_pool() is None


async def test_pool_selector_dismiss_idempotency():
    """Verify PoolSelectorScreen dismissal is idempotent and avoids ScreenStackError on double dismiss."""
    from talkops.ui.widgets.pool_selector import PoolSelectorScreen

    dismissed_results = []

    class TestApp(App[None]):
        """Test suite or helper class for test app."""
        def on_mount(self) -> None:
            """Initialize component state when mounted into the DOM."""
            self.push_screen(
                PoolSelectorScreen(active_provider="google_genai"),
                lambda res: dismissed_results.append(res),
            )

    test_app = TestApp()
    async with test_app.run_test() as pilot:
        screen = test_app.screen
        assert isinstance(screen, PoolSelectorScreen)

        # 1st dismiss
        screen.dismiss(None)
        await pilot.pause()
        assert screen._dismissed is True
        assert len(dismissed_results) == 1

        # 2nd dismiss (simulating double click or rapid repeat)
        screen.dismiss(None)
        await pilot.pause()
        assert len(dismissed_results) == 1



@pytest.mark.asyncio
async def test_goal_command_conversational_amendment_when_active():
    """Verify goal command conversational amendment when active."""
    from talkops.commands.power.goal import GoalHandler, get_goal_state

    mock_app = MagicMock()
    mock_app._run_goal_criteria_request = AsyncMock()
    state = get_goal_state(mock_app)
    state.objective = "Create a file named nginx.conf with http and server blocks"
    state.status = "active"
    state.rubric = (
        "- nginx.conf is created in working directory\n"
        "- nginx.conf contains http block\n"
        "- The nginx.conf configuration syntax is valid."
    )

    ctx = CommandContext(
        app=mock_app,
        raw_command="/goal lets skip the nginx.conf configuration check",
        args="lets skip the nginx.conf configuration check",
    )
    handler = GoalHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    assert mock_app._run_goal_criteria_request.called
    call_req = mock_app._run_goal_criteria_request.call_args[0][0]
    assert call_req["kind"] == "amend"
    assert call_req["objective"] == "Create a file named nginx.conf with http and server blocks"
    assert "nginx.conf is created in working directory" in call_req["criteria"]
    assert call_req["feedback"] == "lets skip the nginx.conf configuration check"


@pytest.mark.asyncio
async def test_goal_command_conversational_amendment_when_blocked():
    """Verify goal command conversational amendment when blocked."""
    from talkops.commands.power.goal import GoalHandler, get_goal_state

    mock_app = MagicMock()
    mock_app._run_goal_criteria_request = AsyncMock()
    state = get_goal_state(mock_app)
    state.objective = "Deploy ECS service"
    state.status = "blocked"
    state.rubric = "- Cluster healthy\n- Service running\n- ALB target healthy"

    ctx = CommandContext(
        app=mock_app,
        raw_command="/goal skip ALB target check",
        args="skip ALB target check",
    )
    handler = GoalHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    assert mock_app._run_goal_criteria_request.called
    call_req = mock_app._run_goal_criteria_request.call_args[0][0]
    assert call_req["kind"] == "amend"
    assert call_req["objective"] == "Deploy ECS service"
    assert call_req["feedback"] == "skip ALB target check"


@pytest.mark.asyncio
async def test_goal_command_explicit_new_replaces_active():
    """Verify goal command explicit new replaces active."""
    from talkops.commands.power.goal import GoalHandler, get_goal_state

    mock_app = MagicMock()
    mock_app._run_goal_criteria_request = AsyncMock()
    state = get_goal_state(mock_app)
    state.objective = "Old nginx goal"
    state.status = "active"
    state.rubric = "- Old criteria"

    ctx = CommandContext(
        app=mock_app,
        raw_command="/goal new Deploy Postgres RDS database",
        args="new Deploy Postgres RDS database",
    )
    handler = GoalHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    assert mock_app._run_goal_criteria_request.called
    call_req = mock_app._run_goal_criteria_request.call_args[0][0]
    assert call_req["kind"] == "create"
    assert call_req["objective"] == "Deploy Postgres RDS database"


@pytest.mark.asyncio
async def test_goal_command_create_when_no_active_goal():
    """Verify goal command create when no active goal."""
    from talkops.commands.power.goal import GoalHandler, get_goal_state

    mock_app = MagicMock()
    mock_app._run_goal_criteria_request = AsyncMock()
    state = get_goal_state(mock_app)
    state.clear()

    ctx = CommandContext(
        app=mock_app,
        raw_command="/goal Create a file named nginx.conf",
        args="Create a file named nginx.conf",
    )
    handler = GoalHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    assert mock_app._run_goal_criteria_request.called
    call_req = mock_app._run_goal_criteria_request.call_args[0][0]
    assert call_req["kind"] == "create"
    assert call_req["objective"] == "Create a file named nginx.conf"


@pytest.mark.asyncio
async def test_context_command_no_emojis_and_complete_metrics():
    """Verify context command no emojis and complete metrics."""
    from talkops.commands.core.context import ContextHandler

    mock_app = MagicMock()
    mock_app._model = "google_genai:gemini-3.5-flash"
    mock_app.get_context_tokens = MagicMock(return_value=14074)
    mock_app.get_conversation_token_count = AsyncMock(return_value=119)
    mock_app._cumulative_session_tokens = 37658
    mock_app._session_cost_usd = 0.03579855
    mock_adapter = MagicMock()
    mock_adapter._stats = MagicMock(request_count=3, input_tokens=36420, output_tokens=1238, total_cost_usd=0.03579855)
    mock_app._adapter = mock_adapter

    mock_app.get_active_tools = MagicMock(return_value=["fetch_url", "bash", "edit_file"])
    mock_app.get_mcp_servers = MagicMock(return_value=[])
    mock_app.get_discovered_skills = MagicMock(return_value=["skill-creator", "cloud-doctor"])

    ctx = CommandContext(app=mock_app, raw_command="/context", args="")
    handler = ContextHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    msg = result.message
    assert msg is not None

    # Verify NO emojis in message
    emojis = ["📊", "🧩", "☁️", "🤖", "⚡", "✨", "🔥"]
    for e in emojis:
        assert e not in msg, f"Found emoji {e} in context command output"

    # Verify context window and breakdown
    assert "**Context Window:**" in msg
    assert "14,074" in msg
    assert "System Prompt + Tools:" in msg
    assert "Conversation History:" in msg
    assert "Remaining Space:" in msg

    # Verify session totals
    assert "**Session Total:**" in msg
    assert "37,658 tokens" in msg
    assert "$0.04" in msg
    assert "(3 requests)" in msg
    assert "Input Tokens:" in msg
    assert "36,420" in msg
    assert "Output Tokens:" in msg
    assert "1,238" in msg

    # Verify active resources
    assert "**Active Resources:**" in msg
    assert "3 tools" in msg
    assert "2 skills" in msg
    assert "**Tools:** fetch_url, bash, edit_file" in msg
    assert "**Skills:** skill-creator, cloud-doctor" in msg


@pytest.mark.asyncio
async def test_multi_provider_pool_command_set_and_status():
    """Verify multi provider pool command set and status."""
    from talkops.commands._base import CommandContext
    from talkops.commands.core.pool import PoolHandler
    from talkops.config.toml_config import clear_agent_pool, load_agent_pool

    clear_agent_pool()

    handler = PoolHandler()

    # 1. Set multi-provider pool
    ctx_set = CommandContext(
        app=None,
        raw_command="/pool set fast=openai:gpt-4o-mini standard=google_genai:gemini-3.7-flash powerful=anthropic:claude-sonnet-4-5",
        args="set fast=openai:gpt-4o-mini standard=google_genai:gemini-3.7-flash powerful=anthropic:claude-sonnet-4-5",
    )
    res_set = await handler.execute(ctx_set)
    assert res_set.success is True
    assert res_set.message is not None
    assert "Agent Model Pool Updated" in res_set.message

    saved = load_agent_pool()
    assert saved is not None
    assert saved["fast"] == "openai:gpt-4o-mini"
    assert saved["standard"] == "google_genai:gemini-3.7-flash"
    assert saved["powerful"] == "anthropic:claude-sonnet-4-5"
    assert saved.get("provider") == "multi"

    # 2. Status inspection detects multi-provider badge
    ctx_status = CommandContext(app=None, raw_command="/pool status", args="status")
    res_status = await handler.execute(ctx_status)
    assert res_status.success is True
    assert res_status.message is not None
    assert "Multi-Provider" in res_status.message
    assert "openai:gpt-4o-mini" in res_status.message
    assert "google_genai:gemini-3.7-flash" in res_status.message
    assert "anthropic:claude-sonnet-4-5" in res_status.message

    clear_agent_pool()



@pytest.mark.asyncio
async def test_pool_selector_screen_multi_provider_ui():
    """Verify pool selector screen multi provider ui."""
    from talkops.config.toml_config import clear_agent_pool, load_agent_pool
    from talkops.ui.widgets.pool_selector import PoolSelectorScreen

    clear_agent_pool()


    saved_result = None

    def on_dismiss(res):
        """Provide on dismiss test helper or fixture."""
        nonlocal saved_result
        saved_result = res

    class TestApp(App[None]):
        """Test suite or helper class for test app."""
        def on_mount(self) -> None:
            """Initialize component state when mounted into the DOM."""
            self.push_screen(PoolSelectorScreen(active_provider="google_genai"), on_dismiss)

    test_app = TestApp()
    async with test_app.run_test(size=(120, 50)) as pilot:
        screen = test_app.screen
        assert isinstance(screen, PoolSelectorScreen)

        # 1. Verify individual provider selectors exist
        p_fast = screen.query_one("#select-provider-fast", Select)
        p_std = screen.query_one("#select-provider-standard", Select)
        p_pow = screen.query_one("#select-provider-powerful", Select)
        assert p_fast is not None
        assert p_std is not None
        assert p_pow is not None

        # 2. Change Fast provider to openai
        p_fast.value = "openai"
        await pilot.pause()

        # Fast model selector options are now OpenAI models
        s_fast = screen.query_one("#select-fast", Select)
        assert any("openai" in str(opt[1]) for opt in s_fast._options)
        s_fast.value = "openai:gpt-4o-mini"

        # 3. Change Powerful provider to anthropic
        p_pow.value = "anthropic"
        await pilot.pause()

        s_pow = screen.query_one("#select-powerful", Select)
        assert any("anthropic" in str(opt[1]) for opt in s_pow._options)
        s_pow.value = "anthropic:claude-sonnet-4-5"

        # 4. Standard remains google_genai
        s_std = screen.query_one("#select-standard", Select)
        assert any("google_genai" in str(opt[1]) for opt in s_std._options)
        assert not any("gemini-2.5" in str(opt[1]) for opt in s_std._options)
        s_std.value = "google_genai:gemini-3.7-flash"

        # 5. Save multi-provider pool
        await pilot.click("#btn-save")
        await pilot.pause()

        assert saved_result is not None
        assert saved_result["fast"] == "openai:gpt-4o-mini"
        assert saved_result["standard"] == "google_genai:gemini-3.7-flash"
        assert saved_result["powerful"] == "anthropic:claude-sonnet-4-5"
        assert saved_result["provider"] == "multi"

        persisted = load_agent_pool()
        assert persisted is not None
        assert persisted["fast"] == "openai:gpt-4o-mini"
        assert persisted["standard"] == "google_genai:gemini-3.7-flash"
        assert persisted["powerful"] == "anthropic:claude-sonnet-4-5"
        assert persisted["provider"] == "multi"

    clear_agent_pool()


@pytest.mark.asyncio
async def test_logout_command_mode_guards(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    """Verify /logout mode guards for TalkOps ADC, TalkOps Enterprise Gateway, and Direct TalkOps API Key modes."""
    from talkops.auth.models import StoredSession
    from talkops.auth.storage import CompositeCredentialStorage
    from talkops.commands.core.auth import LogoutHandler

    auth_file = tmp_path / "auth.json"
    env_file = tmp_path / ".env"
    storage = CompositeCredentialStorage(auth_file_path=auth_file, env_file_path=env_file)
    monkeypatch.setattr("talkops.auth.storage.get_credential_storage", lambda: storage)
    monkeypatch.setattr("talkops.config.paths.AUTH_PATH", auth_file)
    monkeypatch.setattr("talkops.config.paths.GLOBAL_ENV_PATH", env_file)

    for k in (
        "TALKOPS_ADC_AUTH",
        "TALKOPS_LLM_GATEWAY_URL",
        "TALKOPS_GATEWAY_URL",
        "TALKOPS_API_KEY",
        "TALKOPS_SERVER_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)

    handler = LogoutHandler()
    assert handler.bypass_tier == BypassTier.IMMEDIATE_UI

    # 1. TalkOps ADC Mode Guard (`TALKOPS_ADC_AUTH`)
    monkeypatch.setenv("TALKOPS_ADC_AUTH", "1")
    res_adc = await handler.execute(CommandContext(app=None, raw_command="/logout", args=""))
    assert res_adc.success is True
    assert res_adc.data["logged_out"] is False
    assert res_adc.data["alert_title"] == "ADC Logout Instructions"
    assert "unset TALKOPS_ADC_AUTH" in res_adc.data["alert_body"]
    assert res_adc.notify_severity == "warning"
    monkeypatch.delenv("TALKOPS_ADC_AUTH", raising=False)

    # 2. TalkOps Enterprise Gateway Mode Guard (`TALKOPS_LLM_GATEWAY_URL`)
    monkeypatch.setenv("TALKOPS_LLM_GATEWAY_URL", "https://gateway.internal.talkops.ai")
    res_gw = await handler.execute(CommandContext(app=None, raw_command="/logout", args=""))
    assert res_gw.success is True
    assert res_gw.data["logged_out"] is False
    assert res_gw.data["alert_title"] == "Enterprise Gateway Mode"
    assert "unset TALKOPS_LLM_GATEWAY_URL" in res_gw.data["alert_body"]
    assert res_gw.notify_severity == "warning"
    monkeypatch.delenv("TALKOPS_LLM_GATEWAY_URL", raising=False)

    # 3. Direct TalkOps API Key Mode Guard (unstored env-only TALKOPS_API_KEY)
    monkeypatch.setenv("TALKOPS_API_KEY", "oc_live_direct_env_key_123456")
    res_tk = await handler.execute(CommandContext(app=None, raw_command="/logout", args=""))
    assert res_tk.success is True
    assert res_tk.data["logged_out"] is False
    assert res_tk.data["alert_title"] == "TalkOps API Key Mode"
    assert "unset TALKOPS_API_KEY" in res_tk.data["alert_body"]
    assert res_tk.notify_severity == "warning"
    monkeypatch.delenv("TALKOPS_API_KEY", raising=False)

    # 4. Stored session with auth_method="gateway" also triggers Enterprise Gateway Mode guard
    storage.save_session(
        StoredSession(
            api_key="oc_live_gw_1234567890",
            auth_method="gateway",
            server_url="https://gw.talkops.ai",
        )
    )
    res_sess_gw = await handler.execute(CommandContext(app=None, raw_command="/logout", args=""))
    assert res_sess_gw.data["logged_out"] is False
    assert res_sess_gw.data["alert_title"] == "Enterprise Gateway Mode"


@pytest.mark.asyncio
async def test_logout_command_full_cleanup_conversation_reset_and_tui_transition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    """Verify bare /logout logs out of TalkOps Auth (preserving 3rd-party provider keys & WIF hint), cancels streaming, resets thread, and pushes AuthManagerScreen."""
    from talkops.auth.models import StoredSession
    from talkops.auth.oauth_acquirer import LoopbackCallbackServer
    from talkops.auth.storage import CompositeCredentialStorage
    from talkops.commands.core.auth import LogoutHandler
    from talkops.state.remote_session import (
        get_remote_client_config,
        set_remote_client_context,
    )
    from talkops.ui.widgets.auth_manager import AuthManagerScreen

    auth_file = tmp_path / "auth.json"
    env_file = tmp_path / ".env"
    storage = CompositeCredentialStorage(auth_file_path=auth_file, env_file_path=env_file)
    monkeypatch.setattr("talkops.auth.storage.get_credential_storage", lambda: storage)
    monkeypatch.setattr("talkops.config.paths.AUTH_PATH", auth_file)
    monkeypatch.setattr("talkops.config.paths.GLOBAL_ENV_PATH", env_file)

    for k in (
        "TALKOPS_ADC_AUTH",
        "TALKOPS_LLM_GATEWAY_URL",
        "TALKOPS_GATEWAY_URL",
        "TALKOPS_API_KEY",
        "TALKOPS_SERVER_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)

    # Save an enterprise WIF/SSO TalkOps Control Plane session and a 3rd-party OpenAI provider key
    storage.save_session(
        StoredSession(
            access_token="eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c3JfMSJ9.sig",
            api_key="oc_live_k9x2m4p1_secret123456",
            server_url="http://127.0.0.1:2024",
            control_plane_url="https://cp.talkops.ai",
            user_id="usr_1",
            email="engineer@corp.com",
            org_id="corp-wif-provider",
            auth_method="wif_sso",
            expires_at=9999999999.0,
        ),
        sync_dotenv=True,
    )
    storage.set_provider_key("openai", "sk-proj-testkey999", sync_dotenv=True)
    set_remote_client_context("http://127.0.0.1:2024", api_key="oc_live_k9x2m4p1_secret123456", user_id="usr_1")

    # Start an ephemeral loopback server to verify /logout cancels in-flight TalkOps OAuth listeners
    loopback = LoopbackCallbackServer(expected_state="state123")
    await loopback.start()

    mock_app = MagicMock()
    mock_app._agent_thread_id = "old-thread-111"
    mock_app._session_state = MagicMock()
    mock_app._session_state.reset_thread = MagicMock(return_value="new-thread-222")
    mock_app._pending_messages = ["queued_msg"]
    mock_app._queued_widgets = ["queued_widget"]
    mock_banner = MagicMock()
    mock_messages = MagicMock()
    mock_status_bar = MagicMock()

    def _query_one(selector, *args, **kwargs):
        if selector == "#messages":
            return mock_messages
        if selector == "#status-bar":
            return mock_status_bar
        return mock_banner

    mock_app.query_one = MagicMock(side_effect=_query_one)

    ctx = CommandContext(
        app=mock_app,
        raw_command="/logout",
        args="",
        thread_id="old-thread-111",
        model_spec="openai:gpt-4o",
    )
    res = await LogoutHandler().execute(ctx)

    assert res.success is True
    assert res.data["logged_out"] is True
    assert res.data["session_cleared"] is True
    assert res.data["providers_cleared"] == []
    assert res.data["cancelled_oauth_flows"] >= 1
    assert res.data["new_thread_id"] == "new-thread-222"
    assert res.data["transitioned_to_auth_screen"] is True
    assert res.data["saved_wif_provider"] == "corp-wif-provider"

    # TalkOps Auth session secrets are wiped from storage & remote context, while saved_wif_provider and 3rd-party OpenAI key remain intact
    assert storage.load_session() is None
    assert storage.get_saved_wif_provider() == "corp-wif-provider"
    assert storage.get_provider_key("openai") == "sk-proj-testkey999"
    assert get_remote_client_config() == (None, None, None)

    # Active streaming work interrupted, messages cleared, thread reset, banner & status bar updated, AuthManagerScreen pushed with "talkops"
    mock_app._force_interrupt_active_work.assert_called_once()
    mock_messages.clear.assert_called_once()
    assert mock_app._agent_thread_id == "new-thread-222"
    mock_banner.refresh_auth.assert_called_once()
    mock_banner.set_logged_out.assert_called_once()
    mock_status_bar.set_status.assert_any_call("● Logged out · /login to sign in")
    assert res.mount_as_app_message is True
    assert res.message is not None
    assert "Logged Out of TalkOps" in res.message
    assert "What's next to log back in:" in res.message
    mock_app.push_screen.assert_called_once()
    pushed_screen = mock_app.push_screen.call_args[0][0]
    assert isinstance(pushed_screen, AuthManagerScreen)
    assert pushed_screen._initial_provider == "talkops"

    # Explicit `/logout all` also clears 3rd-party LLM provider credentials
    res_all = await LogoutHandler().execute(
        CommandContext(app=None, raw_command="/logout all", args="all")
    )
    assert res_all.success is True
    assert "openai" in res_all.data["providers_cleared"]
    assert storage.get_provider_key("openai") is None






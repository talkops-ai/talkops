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

"""Unit tests for PluginManagerScreen modal UI, pill tabs, and border styling."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from talkops.ui.widgets.modal_base import PillTabs
from talkops.ui.widgets.plugin_manager import (
    PluginManagerScreen,
    _ManagerState,
    _MarketplaceRow,
    _PluginRow,
)


class DummyModalApp(App):
    """Test app to mount and interact with PluginManagerScreen."""

    def __init__(self, screen_instance: PluginManagerScreen) -> None:
        """Initialize DummyModalApp."""
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        """Compose and yield child widgets for this component."""
        yield from ()

    async def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        await self.push_screen(self._test_screen)


def _mock_state() -> _ManagerState:
    plugins = (
        _PluginRow(
            plugin_id="aws-core@talkops",
            description="Core AWS skills and tools.",
            enabled=True,
            version="1.0.0",
            author="TalkOps",
            display_name="AWS Core",
            skill_count=5,
        ),
        _PluginRow(
            plugin_id="aws-sre@talkops",
            description="Site reliability operations for AWS.",
            enabled=False,
            version="0.9.0",
            author="TalkOps",
            display_name="AWS SRE",
            skill_count=2,
        ),
    )
    installed = (
        _PluginRow(
            plugin_id="aws-core@talkops",
            description="Core AWS skills and tools.",
            enabled=True,
            version="1.0.0",
            author="TalkOps",
            display_name="AWS Core",
            skill_count=5,
        ),
    )
    marketplaces = (
        _MarketplaceRow(
            name="talkops",
            source="local",
            plugin_count=2,
            installed_count=1,
        ),
    )
    errors = ("Plugin xyz failed to load manifest.",)
    return _ManagerState(
        available_plugins=plugins,
        installed_plugins=installed,
        marketplaces=marketplaces,
        errors=errors,
    )


@pytest.mark.asyncio
async def test_plugin_manager_modal_chrome_and_counters() -> None:
    """Verify dialog border title has counter and subtitle has keycap hints."""
    screen = PluginManagerScreen()
    mock_state = _mock_state()

    with patch("talkops.ui.widgets.plugin_manager._load_manager_state", return_value=mock_state):
        app = DummyModalApp(screen)
        async with app.run_test() as pilot:
            await pilot.pause()

            # Check border title
            assert screen.dialog is not None
            assert screen.dialog.border_title is not None
            assert "Plugins" in str(screen.dialog.border_title)
            assert "2 available" in str(screen.dialog.border_title)

            # Check border subtitle has keycaps
            subtitle_text = str(screen.dialog.border_subtitle)
            assert "navigate" in subtitle_text
            assert "details" in subtitle_text
            assert "search" in subtitle_text
            assert "close" in subtitle_text

            # Check tab counts
            tabs = screen.query_one(PillTabs)
            assert tabs.tabs[0].tab_id == "discover"
            assert tabs.tabs[1].tab_id == "installed"
            assert tabs.tabs[1].count == 1
            assert tabs.tabs[2].tab_id == "marketplaces"
            assert tabs.tabs[2].count == 1
            assert tabs.tabs[3].tab_id == "errors"
            assert tabs.tabs[3].count == 1


@pytest.mark.asyncio
async def test_plugin_manager_tab_navigation_and_subtitles() -> None:
    """Verify keyboard tab navigation switches views and updates chrome."""
    screen = PluginManagerScreen()
    mock_state = _mock_state()

    with patch("talkops.ui.widgets.plugin_manager._load_manager_state", return_value=mock_state):
        app = DummyModalApp(screen)
        async with app.run_test() as pilot:
            await pilot.pause()

            # Switch to installed tab
            screen.action_next_tab()
            await pilot.pause()
            assert screen._tab == "installed"
            assert screen.dialog is not None
            assert screen.dialog.border_title is not None
            assert "1 installed" in str(screen.dialog.border_title)

            # Switch to marketplaces tab
            screen.action_next_tab()
            await pilot.pause()
            assert screen._tab == "marketplaces"
            assert screen.dialog is not None
            assert screen.dialog.border_title is not None
            assert "1 marketplaces" in str(screen.dialog.border_title)

            # Search is not active in marketplaces, subtitle hints update
            assert screen.dialog.border_subtitle is not None
            subtitle_text = str(screen.dialog.border_subtitle)
            assert "search" not in subtitle_text

            # Switch back with previous_tab
            screen.action_previous_tab()
            await pilot.pause()
            assert screen._tab == "installed"


@pytest.mark.asyncio
async def test_plugin_manager_details_mode_and_escape_back() -> None:
    """Verify selecting a plugin opens details and Escape returns to list mode."""
    screen = PluginManagerScreen()
    mock_state = _mock_state()

    with patch("talkops.ui.widgets.plugin_manager._load_manager_state", return_value=mock_state):
        app = DummyModalApp(screen)
        async with app.run_test() as pilot:
            await pilot.pause()

            # Select first available plugin (detail:aws-core@talkops)
            from textual.widgets._option_list import Option

            class DummyOptionSelected:
                """Test suite or helper class for dummy option selected."""
                option = Option("AWS Core", id="detail:aws-core@talkops")

            await screen.on_option_list_option_selected(cast(Any, DummyOptionSelected()))
            await pilot.pause()

            # Should be in plugin_details mode
            assert screen._mode == "plugin_details"
            assert screen.dialog is not None
            assert screen.dialog.border_title is not None
            assert "AWS Core" in str(screen.dialog.border_title)
            assert "back" in str(screen.dialog.border_subtitle)

            # Press escape to return to list mode
            screen.action_cancel()
            await pilot.pause()
            assert screen._mode == "list"
            assert screen._selected_plugin is None
            assert screen.dialog is not None
            assert screen.dialog.border_title is not None
            assert "Plugins" in str(screen.dialog.border_title)


@pytest.mark.asyncio
async def test_plugin_manager_close_reload_prompt_flow() -> None:
    """Verify that when mutated, closing enters reload prompt and enter dismisses with 'reload'."""
    dismiss_result = None

    class TrackingApp(App):
        """Test suite or helper class for tracking app."""
        def compose(self) -> ComposeResult:
            """Compose and yield child widgets for this component."""
            yield from ()

        async def on_mount(self) -> None:
            """Initialize component state when mounted into the DOM."""
            nonlocal dismiss_result

            def on_close(res: Any) -> None:
                """Provide on close test helper or fixture."""
                nonlocal dismiss_result
                dismiss_result = res

            await self.push_screen(screen, on_close)

    screen = PluginManagerScreen()
    mock_state = _mock_state()

    with patch("talkops.ui.widgets.plugin_manager._load_manager_state", return_value=mock_state):
        app = TrackingApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            # Mark screen as mutated (e.g. plugin installed)
            screen._mutated = True

            # Trigger close (action_cancel)
            screen.action_cancel()
            await pilot.pause()

            # Should transition to reload prompt
            assert screen._close_phase == "reload_prompt"
            assert screen.dialog is not None
            assert "Reload plugins?" in str(screen.dialog.border_title)

            # Pressing Enter triggers reload
            screen.action_reload_plugins()
            await pilot.pause()

            assert dismiss_result == "reload"


@pytest.mark.asyncio
async def test_plugin_manager_check_reload_required_detects_changes() -> None:
    """Verify that check_reload_required callback detecting changes prompts for reload."""
    dismiss_result = None

    async def mock_check() -> bool:
        """Provide mock check test helper or fixture."""
        return True

    screen = PluginManagerScreen(check_reload_required=mock_check)
    mock_state = _mock_state()

    class TrackingApp(App):
        """Test suite or helper class for tracking app."""
        def compose(self) -> ComposeResult:
            """Compose and yield child widgets for this component."""
            yield from ()

        async def on_mount(self) -> None:
            """Initialize component state when mounted into the DOM."""
            nonlocal dismiss_result

            def on_close(res: Any) -> None:
                """Provide on close test helper or fixture."""
                nonlocal dismiss_result
                dismiss_result = res

            await self.push_screen(screen, on_close)

    with patch("talkops.ui.widgets.plugin_manager._load_manager_state", return_value=mock_state):
        app = TrackingApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            # Cancel while browsing
            screen.action_cancel()
            await pilot.pause()

            assert screen._close_phase == "reload_prompt"

            # Pressing Esc again chooses "later"
            screen.action_cancel()
            await pilot.pause()

            assert dismiss_result == "later"


@pytest.mark.asyncio
async def test_refresh_all_marketplaces_shows_intermediate_status_and_completes() -> None:
    """Verify selecting 'refresh-all-marketplaces' immediately shows intermediate feedback and then completes."""
    screen = PluginManagerScreen()
    mock_state = _mock_state()

    def fake_refresh(**kw: Any) -> dict[str, Any]:
        """Provide fake refresh test helper or fixture."""
        return {}

    class DummyEvent:
        """Test suite or helper class for dummy event."""
        option = Option("Refresh", id="refresh-all-marketplaces")

    with patch("talkops.ui.widgets.plugin_manager._load_manager_state", return_value=mock_state), \
         patch("talkops.plugins.discovery.refresh_all_marketplaces", side_effect=fake_refresh):
        app = DummyModalApp(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen._select_tab("marketplaces")
            await pilot.pause()

            ev1: Any = DummyEvent()
            await screen.on_option_list_option_selected(ev1)
            # Intermediate state must be immediate:
            assert screen._is_refreshing_marketplaces is True
            assert screen._status == "Refreshing all marketplaces..."
            status_widget = screen.query_one("#plugin-manager-status", Static)
            assert str(status_widget.content) == "Refreshing all marketplaces..."

            # After background worker finishes:
            await pilot.pause()
            assert screen._is_refreshing_marketplaces is False
            assert screen._status == "All marketplaces refreshed."
            assert str(status_widget.content) == "All marketplaces refreshed."


@pytest.mark.asyncio
async def test_refresh_single_marketplace_shows_intermediate_status_and_completes() -> None:
    """Verify selecting single marketplace refresh shows intermediate feedback and completes."""
    screen = PluginManagerScreen()
    mock_state = _mock_state()

    mock_mp = MagicMock()
    mock_mp.plugins = []

    class DummyEvent:
        """Test suite or helper class for dummy event."""
        option = Option("Refresh", id="action:refresh-marketplace")

    with patch("talkops.ui.widgets.plugin_manager._load_manager_state", return_value=mock_state), \
         patch("talkops.plugins.discovery.refresh_marketplace", return_value=mock_mp):
        app = DummyModalApp(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen._select_tab("marketplaces")
            await pilot.pause()

            screen._selected_marketplace = mock_state.marketplaces[0]
            screen._mode = "marketplace_details"
            screen._refresh_view()
            await pilot.pause()

            ev2: Any = DummyEvent()
            await screen.on_option_list_option_selected(ev2)
            # Intermediate state must be immediate:
            assert screen._is_refreshing_marketplaces is True
            assert "Refreshing marketplace talkops..." in str(screen._status)
            status_widget = screen.query_one("#plugin-manager-status", Static)
            assert "Refreshing marketplace talkops..." in str(status_widget.content)

            # After background worker finishes:
            await pilot.pause()
            assert screen._is_refreshing_marketplaces is False
            assert "Marketplace refreshed" in str(screen._status)
            assert "Marketplace refreshed" in str(status_widget.content)


@pytest.mark.asyncio
async def test_large_plugin_details_keep_install_scope_and_uninstall_options_visible() -> None:
    """Regression test: rich plugin manifests must not push Install/Uninstall options off-screen."""
    long_description = (
        "Principal Kubernetes Application Onboarding, GitOps & Progressive Delivery Engineer: "
        "end-to-end workload packaging and release management with Helm (chart discovery, values "
        "validation, security scanning, atomic upgrades, rollbacks), GitOps continuous delivery "
        "with ArgoCD (repository/project onboarding, sync orchestration, drift remediation, "
        "history rollbacks), progressive delivery with Argo Rollouts (Deployment-to-Rollout "
        "migration, canary/blue-green strategies, Prometheus AnalysisTemplate gating, experiments), "
        "multi-stage freight promotion pipelines with Kargo (Warehouse discovery, Stage DAGs, "
        "freight promotion/approval, stage verification), and ingress/traffic routing with "
        "Traefik v3 (HTTP/TCP/UDP IngressRoutes, weighted canary services, traffic mirroring, "
        "middlewares, TLS/mTLS policies, NGINX annotation migration, and security posture auditing)."
    )
    many_skills = tuple(f"kubernetes-delivery-skill-{idx:02d}-long-identifier" for idx in range(24))

    discover_row = _PluginRow(
        plugin_id="kubernetes-observability-engineer@talkops-devops-plugins",
        description=long_description,
        enabled=False,
        version="0.2.0",
        author="TalkOps",
        display_name="Kubernetes Observability Engineer",
        skill_count=len(many_skills),
        agent_names=("kubernetes-observability-engineer",),
        skill_names=many_skills,
        command_names=("alerts", "metrics", "traces", "logs"),
        mcp_server_names=("prometheus", "alertmanager", "tempo", "loki"),
    )
    installed_row = _PluginRow(
        plugin_id="kubernetes-delivery-engineer@talkops-devops-plugins",
        description=long_description,
        enabled=False,
        version="0.2.0",
        author="TalkOps",
        display_name="Kubernetes Delivery Engineer",
        scope="local",
        project_path="/tmp/devops-codebase",
        skill_count=len(many_skills),
        agent_names=("kubernetes-delivery-engineer",),
        skill_names=many_skills,
        command_names=("canary", "deploy", "onboard", "rollback"),
        mcp_server_names=("helm", "argocd", "argo-rollout", "kargo", "traefik"),
    )
    rich_state = _ManagerState(
        available_plugins=(discover_row,),
        installed_plugins=(installed_row,),
        marketplaces=(
            _MarketplaceRow(
                name="talkops-devops-plugins",
                source="local",
                plugin_count=2,
                installed_count=1,
            ),
        ),
        errors=(),
    )

    screen = PluginManagerScreen(project_root=Path("/tmp/devops-codebase"))

    class SelectEvent:
        """Synthetic OptionList selection event."""

        def __init__(self, opt_id: str) -> None:
            self.option = Option(opt_id, id=opt_id)

    with patch("talkops.ui.widgets.plugin_manager._load_manager_state", return_value=rich_state):
        app = DummyModalApp(screen)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert screen.dialog is not None

            # 1. Discover -> Install details must keep user/project/local scope options on-screen
            await screen.on_option_list_option_selected(
                cast(Any, SelectEvent(f"detail:{discover_row.plugin_id}"))
            )
            await pilot.pause()

            options = screen.query_one("#plugin-manager-options", OptionList)
            details_scroll = screen.query_one("#plugin-manager-details-scroll", VerticalScroll)

            option_ids = [options.get_option_at_index(i).id for i in range(options.option_count)]
            assert option_ids == [
                "action:install-user",
                "action:install-project",
                "action:install-local",
                "details-back",
            ]
            option_prompts = [
                str(options.get_option_at_index(i).prompt) for i in range(options.option_count)
            ]
            assert any("user / global" in p for p in option_prompts)
            assert any("project scope" in p for p in option_prompts)
            assert any("workspace" in p and "local scope" in p for p in option_prompts)

            assert options.display is True
            assert options.highlighted == 0
            assert options.region.height >= 4
            assert options.region.y >= screen.dialog.region.y
            assert options.region.bottom <= screen.dialog.region.bottom

            # Details body overflows inside its scroll container and PgDn scrolls it without losing option focus
            assert details_scroll.max_scroll_y > 0
            await pilot.press("pagedown")
            await pilot.pause()
            assert details_scroll.scroll_y > 0
            assert options.has_focus

            # Return to list and open Installed -> plugin details
            screen.action_cancel()
            await pilot.pause()
            screen._select_tab("installed")
            await pilot.pause()

            await screen.on_option_list_option_selected(
                cast(Any, SelectEvent(f"installed:{installed_row.plugin_id}"))
            )
            await pilot.pause()

            installed_option_ids = [
                options.get_option_at_index(i).id for i in range(options.option_count)
            ]
            assert "action:toggle-enabled" in installed_option_ids
            assert "action:uninstall" in installed_option_ids
            assert "details-back" in installed_option_ids

            assert options.display is True
            assert options.highlighted == 0
            assert options.region.height >= 4
            assert options.region.y >= screen.dialog.region.y
            assert options.region.bottom <= screen.dialog.region.bottom


@pytest.mark.asyncio
async def test_installed_details_toggle_enabled_updates_selected_plugin_row() -> None:
    """Toggling Enable/Disable in installed_details updates Status and button label immediately."""
    disabled_row = _PluginRow(
        plugin_id="kubernetes-delivery-engineer@talkops-devops-plugins",
        description="Kubernetes delivery plugin.",
        enabled=False,
        version="0.2.0",
        author="TalkOps",
        display_name="Kubernetes Delivery Engineer",
        scope="local",
        project_path="/tmp/devops-codebase",
    )
    enabled_row = _PluginRow(
        plugin_id="kubernetes-delivery-engineer@talkops-devops-plugins",
        description="Kubernetes delivery plugin.",
        enabled=True,
        version="0.2.0",
        author="TalkOps",
        display_name="Kubernetes Delivery Engineer",
        scope="local",
        project_path="/tmp/devops-codebase",
    )
    states = [
        _ManagerState(available_plugins=(), installed_plugins=(disabled_row,), marketplaces=(), errors=()),
        _ManagerState(available_plugins=(), installed_plugins=(enabled_row,), marketplaces=(), errors=()),
    ]

    screen = PluginManagerScreen(project_root=Path("/tmp/devops-codebase"))

    class SelectEvent:
        """Synthetic OptionList selection event."""

        def __init__(self, opt_id: str) -> None:
            self.option = Option(opt_id, id=opt_id)

    with (
        patch("talkops.ui.widgets.plugin_manager._load_manager_state", side_effect=states),
        patch("talkops.ui.widgets.plugin_manager.set_installed_plugin_enabled"),
    ):
        app = DummyModalApp(screen)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen._select_tab("installed")
            await pilot.pause()

            await screen.on_option_list_option_selected(
                cast(Any, SelectEvent(f"installed:{disabled_row.plugin_id}"))
            )
            await pilot.pause()

            options = screen.query_one("#plugin-manager-options", OptionList)
            assert str(options.get_option_at_index(0).prompt) == "Enable plugin"

            await screen.on_option_list_option_selected(
                cast(Any, SelectEvent("action:toggle-enabled"))
            )
            await pilot.pause()

            assert screen._selected_plugin is not None
            assert screen._selected_plugin.enabled is True
            assert str(options.get_option_at_index(0).prompt) == "Disable plugin"


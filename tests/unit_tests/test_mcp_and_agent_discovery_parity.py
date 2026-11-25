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

"""Unit tests for MCP, plugin, subagent, and TUI capability discovery parity."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


def test_load_mcp_json_bare_map_with_schema_metadata(tmp_path: Path) -> None:
    """Bare server maps with $schema or non-server metadata must not drop valid entries."""
    from talkops.mcp.discovery import _load_mcp_json

    bare_with_schema = tmp_path / "mcp.json"
    bare_with_schema.write_text(
        json.dumps(
            {
                "$schema": "https://modelcontextprotocol.io/schema/mcp.json",
                "rea": {
                    "command": "rea-mcp",
                    "args": ["--stdio"],
                },
            }
        ),
        encoding="utf-8",
    )

    parsed = _load_mcp_json(bare_with_schema)
    assert "rea" in parsed
    assert parsed["rea"]["command"] == "rea-mcp"
    assert "$schema" not in parsed

    codex_wrapped = tmp_path / "codex_mcp.json"
    codex_wrapped.write_text(
        json.dumps(
            {
                "mcp_servers": {
                    "aws-docs": {"url": "https://example.com/mcp"},
                }
            }
        ),
        encoding="utf-8",
    )
    parsed_codex = _load_mcp_json(codex_wrapped)
    assert "aws-docs" in parsed_codex
    assert parsed_codex["aws-docs"]["url"] == "https://example.com/mcp"


def test_mcp_discovery_non_git_workspace_falls_back_to_cwd_and_keeps_global(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workspaces without .git/.talkops (e.g. OTEL-APP) still load global and cwd MCP configs."""
    from talkops.config import paths
    from talkops.config.settings import settings
    from talkops.mcp.discovery import MCPDiscovery

    global_agents = tmp_path / "home" / ".agents"
    user_talkops = tmp_path / "home" / ".talkops"
    otel_app = tmp_path / "OTEL-APP"
    global_agents.mkdir(parents=True)
    user_talkops.mkdir(parents=True)
    otel_app.mkdir(parents=True)

    monkeypatch.setattr(paths, "AGENTS_SHARED_DIR", global_agents)
    monkeypatch.setattr(paths, "DATA_DIR", user_talkops)
    monkeypatch.setattr(paths, "GLOBAL_MCP_PATH", user_talkops / ".mcp.json")
    monkeypatch.setattr(settings, "project_root", None)
    monkeypatch.setattr(settings, "cwd", otel_app)

    (global_agents / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": "https://example.com/mcp-schema.json",
                "rea": {"command": "rea-mcp"},
            }
        ),
        encoding="utf-8",
    )
    (otel_app / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "otel-collector": {"command": "otel-mcp"},
                }
            }
        ),
        encoding="utf-8",
    )

    with patch("talkops.plugins.adapters.mcp.discover_plugin_mcp_configs", return_value={}):
        discovered = MCPDiscovery().discover()

    assert "rea" in discovered
    assert discovered["rea"]["source"] == "global"
    assert "otel-collector" in discovered
    assert discovered["otel-collector"]["source"] == "project"


@pytest.mark.asyncio
async def test_preload_mcp_server_info_forwards_project_root(tmp_path: Path) -> None:
    """preload_mcp_server_info resolves ProjectContext and forwards project_root."""
    from talkops.mcp.preload import MCPServerInfo, MCPToolInfo, preload_mcp_server_info

    otel_app = tmp_path / "OTEL-APP"
    otel_app.mkdir()

    fake_info = MCPServerInfo(
        name="rea",
        transport="stdio",
        tools=[MCPToolInfo(name="open_binary", description="Open binary")],
        status="ok",
    )

    with (
        patch(
            "talkops.mcp.discovery.MCPDiscovery.discover_and_sync_async",
            new=AsyncMock(return_value={"rea": {"command": "rea-mcp"}}),
        ) as mock_discover,
        patch(
            "talkops.plugins.adapters.mcp.discover_all_subagents_mcp_configs",
            return_value={},
        ) as mock_subagents,
        patch(
            "talkops.mcp.preload.preload_mcp_metadata",
            new=AsyncMock(return_value=[fake_info]),
        ),
        patch.dict("talkops.mcp.preload._PROBED_CACHE", {"rea": fake_info}, clear=False),
    ):
        infos = await preload_mcp_server_info(
            project_root=otel_app,
            include_subagents=True,
        )

    assert len(infos) == 1
    assert infos[0].name == "rea"
    mock_discover.assert_awaited_once_with(project_root=otel_app.resolve())
    mock_subagents.assert_called_once_with(project_root=otel_app.resolve())


def test_plugin_manifest_and_subagent_bundle_support_mcp_json_without_dot(
    tmp_path: Path,
) -> None:
    """Plugins and subagent bundles using mcp.json (no dot) are discovered cleanly."""
    from talkops.plugins.discovery import _is_likely_plugin_dir
    from talkops.plugins.manifest import (
        build_inventory,
        inspect_plugin_components,
        load_manifest,
    )
    from talkops.subagents.subagents_parser import parse_subagent_bundle

    plugin_dir = tmp_path / "aws-platform-plugin"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "aws-platform-plugin", "version": "0.2.0"}),
        encoding="utf-8",
    )
    (plugin_dir / "agents").mkdir()
    (plugin_dir / "agents" / "aws-platform-engineer.md").write_text(
        "---\nname: aws-platform-engineer\ndescription: Principal AWS engineer\n---\nPrompt.",
        encoding="utf-8",
    )
    (plugin_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "aws-knowledge-mcp": {"url": "https://knowledge-mcp.global.api.aws"},
                }
            }
        ),
        encoding="utf-8",
    )

    manifest, _, _ = load_manifest(plugin_dir, fallback_name=plugin_dir.name)
    inventory = build_inventory(plugin_dir, manifest)
    assert (plugin_dir / "mcp.json").resolve() in inventory.mcp_files

    comp = inspect_plugin_components(plugin_dir, manifest)
    assert "aws-knowledge-mcp" in comp["mcp_server_names"]
    assert "aws-platform-engineer" in comp["agent_names"]

    mcp_only_dir = tmp_path / "mcp-only-plugin"
    mcp_only_dir.mkdir()
    (mcp_only_dir / "mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    assert _is_likely_plugin_dir(mcp_only_dir) is True

    bundle_agents = parse_subagent_bundle(plugin_dir, source="plugin:aws-platform-plugin")
    assert len(bundle_agents) == 1
    assert bundle_agents[0]["mcp_files"] == [str(plugin_dir / "mcp.json")]


def test_list_subagents_scans_dot_agents_when_project_agents_dir_is_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing project_agents_dir must not skip .agents/ or .agents/agents/, while ignoring .agents/skills."""
    from talkops.subagents.loader import list_subagents

    repo = tmp_path / "repo"
    (repo / ".agents" / "agents").mkdir(parents=True)
    (repo / ".agents" / "skills" / "my-skill").mkdir(parents=True)
    (repo / ".agents" / "commands").mkdir(parents=True)

    # Non-agent markdown files inside reserved subdirs must NOT be loaded as subagents
    (repo / ".agents" / "skills" / "my-skill" / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Not an agent\n---\nSkill body.",
        encoding="utf-8",
    )
    (repo / ".agents" / "commands" / "deploy.md").write_text(
        "---\nname: deploy\ndescription: Slash command, not an agent\n---\nCommand body.",
        encoding="utf-8",
    )

    # Real subagent inside .agents/agents/
    (repo / ".agents" / "agents" / "reverse-engineer.md").write_text(
        "---\nname: reverse-engineer\ndescription: Binary analysis specialist\n---\nAnalyze.",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "talkops.plugins.adapters.agents.discover_plugin_subagents",
        lambda project_root=None: [],
    )

    results = list_subagents(
        user_agents_dir=tmp_path / "no_user_agents",
        project_agents_dir=repo / ".talkops" / "agents",  # does not exist, matching welcome.py
        project_root=repo,
        include_plugins=True,
    )

    names = [r["name"] for r in results]
    assert names == ["reverse-engineer"]


def test_welcome_collectors_use_cwd_when_project_root_is_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Welcome banner skill/agent collectors resolve workspace capabilities when project_root is None."""
    from talkops.config.settings import settings
    from talkops.ui.widgets.welcome import WelcomeBanner

    otel_app = tmp_path / "OTEL-APP"
    (otel_app / ".agents" / "agents").mkdir(parents=True)
    (otel_app / ".agents" / "agents" / "otel-helper.md").write_text(
        "---\nname: otel-helper\ndescription: OTEL tracing helper\n---\nHelp with traces.",
        encoding="utf-8",
    )

    monkeypatch.setattr(settings, "project_root", None)
    monkeypatch.setattr(settings, "cwd", otel_app)
    monkeypatch.setattr(
        "talkops.plugins.adapters.agents.discover_plugin_subagents",
        lambda project_root=None: [],
    )
    monkeypatch.setattr(
        settings,
        "get_user_agents_dir",
        lambda *a, **kw: tmp_path / "empty_user_agents",
    )
    monkeypatch.setattr(
        "talkops.config.paths.AGENTS_SHARED_DIR",
        tmp_path / "empty_shared_agents",
    )

    banner = WelcomeBanner()
    banner.refresh_capabilities()
    agent_names = [name for name, _ in banner._info.subagents]
    assert "otel-helper" in agent_names
    assert banner._info.project.endswith("OTEL-APP")


def test_plugin_manager_labels_foreign_project_install_clearly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Project-scoped plugins from another repo are clearly labeled in /plugins."""
    from talkops.config import paths
    from talkops.plugins import store
    from talkops.plugins.store import add_installed_plugin
    from talkops.ui.widgets.plugin_manager import (
        _installed_plugin_details_content,
        _load_manager_state,
        _plugin_prompt,
    )

    state_dir = tmp_path / "state"
    cache_root = tmp_path / "plugins_cache"
    state_dir.mkdir()
    cache_root.mkdir()
    monkeypatch.setattr(store, "_state_dir", lambda: state_dir)
    monkeypatch.setattr(store, "_installed_plugins_path", lambda: state_dir / "installed_plugins.json")
    monkeypatch.setattr(store, "_marketplaces_path", lambda: state_dir / "marketplaces.json")
    monkeypatch.setattr(store, "_plugin_state_path", lambda: state_dir / "state.json")
    monkeypatch.setattr(store, "plugin_storage_root", lambda: cache_root)
    monkeypatch.setattr(store, "_user_settings_path", lambda: tmp_path / "user_settings.json")
    monkeypatch.setattr(store, "load_marketplace_records", lambda project_root=None: {})
    monkeypatch.setattr(paths, "project_settings_path", lambda pr: pr / ".talkops" / "settings.json")
    monkeypatch.setattr(paths, "project_local_settings_path", lambda pr: pr / ".talkops" / "settings.local.json")

    devops_repo = tmp_path / "devops-codebase"
    otel_repo = tmp_path / "OTEL-APP"
    devops_repo.mkdir()
    otel_repo.mkdir()

    plugin_cache = cache_root / "aws_platform"
    plugin_cache.mkdir()
    (plugin_cache / "plugin.json").write_text(
        json.dumps({"name": "aws-platform-engineer", "displayName": "AWS Platform Engineer"}),
        encoding="utf-8",
    )

    pid = "aws-platform-engineer@talkops-devops-plugins"
    add_installed_plugin(
        pid,
        install_path=str(plugin_cache),
        version="0.2.0",
        scope="project",
        project_root=devops_repo,
    )

    state = _load_manager_state(project_root=otel_repo)
    rows = [r for r in state.installed_plugins if r.plugin_id == pid]
    assert len(rows) == 1
    row = rows[0]
    assert row.scope == "project"
    assert row.project_path == str(devops_repo)
    assert row.foreign_project is True

    prompt_plain = _plugin_prompt(row, width=100).plain
    assert "project: devops-codebase" in prompt_plain
    assert "other workspace" in prompt_plain

    details_plain = _installed_plugin_details_content(row).plain
    assert "Not active in this workspace (installed in devops-codebase)" in details_plain

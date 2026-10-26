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

"""Unit tests for TalkOps local filesystem plugin management.

Tests cover:
- Plugin ID parsing and marketplace source parsing
- Manifest and component inventory detection
- Scoped enablement (user / project / local settings files)
- Scoped install registry (array-per-plugin in installed_plugins.json)
- Scope-aware uninstall (remove single scope, cache only when orphaned)
- Merged enablement across scopes
- FileLock concurrency safety
- CLI parser and execution handlers
- MCP adapter with resilient JSON repair
- Commands adapter and Subagent adapter
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from talkops.commands._base import CommandContext
from talkops.plugins.adapters.agents import plugin_subagents
from talkops.plugins.adapters.commands import plugin_commands
from talkops.plugins.adapters.mcp import (
    _try_repair_mcp_json,
    discover_plugin_mcp_configs,
)
from talkops.plugins.commands_cli import execute_plugin_command, setup_plugin_parser
from talkops.plugins.manifest import build_inventory, load_manifest
from talkops.plugins.marketplace import parse_marketplace_source
from talkops.plugins.models import (
    ComponentInventory,
    InstalledPluginEntry,
    LocalMarketplaceSource,
    PluginInstance,
    RepositoryMarketplaceSource,
    UrlMarketplaceSource,
    split_plugin_id,
)
from talkops.plugins.store import (
    _load_settings_enabled_plugins,
    _write_settings_enabled_plugins,
    add_installed_plugin,
    load_all_enabled_plugin_ids,
    load_enabled_plugin_ids,
    load_installed_plugin_entries,
    load_installed_plugins,
    load_local_enabled_plugin_ids,
    load_project_enabled_plugin_ids,
    load_user_enabled_plugin_ids,
    plugin_mutation_lock,
    remove_installed_plugin,
    set_plugin_enabled,
    set_plugin_enabled_for_scope,
    uninstall_plugin,
)

# ── Plugin ID and Source Parsing ─────────────────────────────


def test_split_plugin_id():
    """Verify split plugin id."""
    name, mp = split_plugin_id("financial-analysis@official")
    assert name == "financial-analysis"
    assert mp == "official"

    with pytest.raises(ValueError):
        split_plugin_id("invalid-id-without-at")


def test_parse_marketplace_source():
    """Verify parse marketplace source."""
    src_gh = parse_marketplace_source("talkops/plugins")
    assert isinstance(src_gh, RepositoryMarketplaceSource)
    assert src_gh.value == "talkops/plugins"

    src_url = parse_marketplace_source("https://example.com/marketplace.json")
    assert isinstance(src_url, UrlMarketplaceSource)

    src_dir = parse_marketplace_source("./")
    assert isinstance(src_dir, LocalMarketplaceSource)


def test_load_manifest_and_inventory(tmp_path: Path):
    """Verify load manifest and inventory."""
    plugin_dir = tmp_path / "my_plugin"
    plugin_dir.mkdir()
    manifest_file = plugin_dir / "plugin.json"
    manifest_file.write_text(
        json.dumps(
            {
                "name": "aws-security-audit",
                "version": "1.2.0",
                "displayName": "AWS Security Audit",
                "skills": "./skills",
            }
        ),
        encoding="utf-8",
    )
    skills_dir = plugin_dir / "skills"
    skills_dir.mkdir()
    (skills_dir / "SKILL.md").write_text("# Skill", encoding="utf-8")

    manifest, path, warnings = load_manifest(plugin_dir)
    assert manifest is not None
    assert manifest.name == "aws-security-audit"
    assert manifest.version == "1.2.0"
    assert manifest.display_name == "AWS Security Audit"

    inventory = build_inventory(plugin_dir, manifest, warnings)
    assert len(inventory.skills) == 1


def test_build_inventory_detects_all_components(tmp_path: Path):
    """Verify build inventory detects all components."""
    plugin_dir = tmp_path / "comprehensive_plugin"
    plugin_dir.mkdir()
    agents_dir = plugin_dir / "agents"
    agents_dir.mkdir()
    (agents_dir / "auditor.md").write_text(
        "---\nname: auditor\ndescription: Audit cloud resources\n---\nAuditor prompt",
        encoding="utf-8",
    )
    commands_dir = plugin_dir / "commands"
    commands_dir.mkdir()
    (commands_dir / "scan.md").write_text("# Scan\nScan infrastructure.", encoding="utf-8")

    mcp_file = plugin_dir / ".mcp.json"
    mcp_file.write_text('{"mcpServers": {"aws-mcp": {"command": "uvx"}}}', encoding="utf-8")

    inventory = build_inventory(plugin_dir, None)
    assert len(inventory.agents) == 1
    assert len(inventory.commands) == 1
    assert len(inventory.mcp) == 1


# ── Scoped Enablement Tests ──────────────────────────────────


class TestScopedEnablement:
    """Tests for scoped settings.json enablement files."""

    def test_write_and_read_settings_enabled_plugins(self, tmp_path: Path):
        """Verify write and read settings enabled plugins."""
        settings_file = tmp_path / "settings.json"
        _write_settings_enabled_plugins(settings_file, {"a@mp", "b@mp"})

        result = _load_settings_enabled_plugins(settings_file)
        assert result == frozenset({"a@mp", "b@mp"})

    def test_read_nonexistent_settings_file(self, tmp_path: Path):
        """Verify read nonexistent settings file."""
        result = _load_settings_enabled_plugins(tmp_path / "missing.json")
        assert result == frozenset()

    def test_read_corrupt_settings_file(self, tmp_path: Path):
        """Verify read corrupt settings file."""
        bad_file = tmp_path / "settings.json"
        bad_file.write_text("not-json!!!", encoding="utf-8")
        result = _load_settings_enabled_plugins(bad_file)
        assert result == frozenset()

    def test_preserves_other_keys(self, tmp_path: Path):
        """Verify preserves other keys."""
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(
            json.dumps({"model": "gpt-4o", "enabledPlugins": {"old@mp": True}}),
            encoding="utf-8",
        )
        _write_settings_enabled_plugins(settings_file, {"new@mp"})
        data = json.loads(settings_file.read_text(encoding="utf-8"))
        assert data["model"] == "gpt-4o"
        assert data["enabledPlugins"] == {"new@mp": True}

    def test_user_scope_enablement(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Verify user scope enablement."""
        monkeypatch.setattr(
            "talkops.plugins.store._user_settings_path", lambda: tmp_path / "settings.json"
        )
        set_plugin_enabled("test@mp", True)
        assert "test@mp" in load_user_enabled_plugin_ids()

        set_plugin_enabled("test@mp", False)
        assert "test@mp" not in load_user_enabled_plugin_ids()

    def test_project_scope_enablement(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Verify project scope enablement."""
        from talkops.config import paths

        project_root = tmp_path / "myproject"
        project_root.mkdir()
        (project_root / ".talkops").mkdir()

        monkeypatch.setattr(
            paths,
            "project_settings_path",
            lambda pr: pr / ".talkops" / "settings.json",
        )
        set_plugin_enabled_for_scope("test@mp", True, scope="project", project_root=project_root)
        result = load_project_enabled_plugin_ids(project_root)
        assert "test@mp" in result

    def test_local_scope_enablement(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Verify local scope enablement."""
        from talkops.config import paths

        project_root = tmp_path / "myproject"
        project_root.mkdir()
        (project_root / ".talkops").mkdir()

        monkeypatch.setattr(
            paths,
            "project_local_settings_path",
            lambda pr: pr / ".talkops" / "settings.local.json",
        )
        monkeypatch.setattr(
            "talkops.plugins.store._ensure_local_settings_gitignored", lambda _: None
        )
        set_plugin_enabled_for_scope("test@mp", True, scope="local", project_root=project_root)
        result = load_local_enabled_plugin_ids(project_root)
        assert "test@mp" in result

    def test_merged_enablement(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Verify merged enablement."""
        from talkops.config import paths

        user_settings = tmp_path / "user" / "settings.json"
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / ".talkops").mkdir()

        monkeypatch.setattr(
            "talkops.plugins.store._user_settings_path", lambda: user_settings
        )
        monkeypatch.setattr(
            paths,
            "project_settings_path",
            lambda pr: pr / ".talkops" / "settings.json",
        )
        monkeypatch.setattr(
            paths,
            "project_local_settings_path",
            lambda pr: pr / ".talkops" / "settings.local.json",
        )

        _write_settings_enabled_plugins(user_settings, {"user-plugin@mp"})
        _write_settings_enabled_plugins(
            project_root / ".talkops" / "settings.json", {"project-plugin@mp"}
        )
        _write_settings_enabled_plugins(
            project_root / ".talkops" / "settings.local.json", {"local-plugin@mp"}
        )

        merged = load_all_enabled_plugin_ids(project_root=project_root)
        assert merged == frozenset({"user-plugin@mp", "project-plugin@mp", "local-plugin@mp"})

    def test_scope_validation(self):
        """Verify scope validation."""
        with pytest.raises(ValueError, match="project_root required"):
            set_plugin_enabled_for_scope("x@mp", True, scope="project")
        with pytest.raises(ValueError, match="project_root required"):
            set_plugin_enabled_for_scope("x@mp", True, scope="local")


# ── Scoped Install Registry Tests ────────────────────────────


class TestScopedInstallRegistry:
    """Tests for array-per-plugin install registry with scope metadata."""

    def test_add_scoped_install_entry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Verify add scoped install entry."""
        state_dir = tmp_path / "state"
        monkeypatch.setattr(
            "talkops.plugins.store._installed_plugins_path",
            lambda: state_dir / "installed_plugins.json",
        )

        entry = add_installed_plugin(
            "test@mp",
            install_path=str(tmp_path / "cache" / "test"),
            version="1.0.0",
            scope="user",
        )
        assert entry.scope == "user"
        assert entry.installed_at is not None

        all_entries = load_installed_plugin_entries()
        assert "test@mp" in all_entries
        assert len(all_entries["test@mp"]) == 1
        assert all_entries["test@mp"][0].scope == "user"

    def test_multi_scope_install(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Verify multi scope install."""
        state_dir = tmp_path / "state"
        monkeypatch.setattr(
            "talkops.plugins.store._installed_plugins_path",
            lambda: state_dir / "installed_plugins.json",
        )

        project_root = tmp_path / "myproject"
        project_root.mkdir()

        add_installed_plugin(
            "test@mp",
            install_path=str(tmp_path / "cache" / "test"),
            version="1.0.0",
            scope="user",
        )
        add_installed_plugin(
            "test@mp",
            install_path=str(tmp_path / "cache" / "test"),
            version="1.0.0",
            scope="project",
            project_root=project_root,
        )

        all_entries = load_installed_plugin_entries()
        assert len(all_entries["test@mp"]) == 2
        scopes = {e.scope for e in all_entries["test@mp"]}
        assert scopes == {"user", "project"}

    def test_replace_same_scope_entry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Verify replace same scope entry."""
        state_dir = tmp_path / "replace_test_state"
        monkeypatch.setattr(
            "talkops.plugins.store._installed_plugins_path",
            lambda: state_dir / "installed_plugins.json",
        )

        add_installed_plugin(
            "test@mp",
            install_path=str(tmp_path / "cache" / "v1"),
            version="1.0.0",
            scope="user",
        )
        add_installed_plugin(
            "test@mp",
            install_path=str(tmp_path / "cache" / "v2"),
            version="2.0.0",
            scope="user",
        )

        all_entries = load_installed_plugin_entries()
        assert len(all_entries["test@mp"]) == 1
        assert all_entries["test@mp"][0].version == "2.0.0"

    def test_backward_compat_load_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Verify backward compat load installed."""
        state_dir = tmp_path / "compat_test_state"
        monkeypatch.setattr(
            "talkops.plugins.store._installed_plugins_path",
            lambda: state_dir / "installed_plugins.json",
        )

        project_root = tmp_path / "proj"
        project_root.mkdir()

        add_installed_plugin(
            "test@mp",
            install_path="/cache/user",
            version="1.0.0",
            scope="user",
        )
        add_installed_plugin(
            "test@mp",
            install_path="/cache/user",
            version="1.0.0",
            scope="project",
            project_root=project_root,
        )

        installed = load_installed_plugins()
        assert "test@mp" in installed
        assert installed["test@mp"].scope == "user"


# ── Scope-Aware Uninstall Tests ──────────────────────────────


class TestScopedUninstall:
    """Tests for scope-aware uninstall logic."""

    def test_remove_single_scope(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Verify remove single scope."""
        state_dir = tmp_path / "remove_single_state"
        monkeypatch.setattr(
            "talkops.plugins.store._installed_plugins_path",
            lambda: state_dir / "installed_plugins.json",
        )

        project_root = tmp_path / "proj"
        project_root.mkdir()

        add_installed_plugin(
            "test@mp", install_path="/cache/test", version="1.0.0", scope="user"
        )
        add_installed_plugin(
            "test@mp",
            install_path="/cache/test",
            version="1.0.0",
            scope="project",
            project_root=project_root,
        )

        removed = remove_installed_plugin(
            "test@mp", scope="project", project_root=project_root
        )
        assert removed is not None
        assert removed.scope == "project"

        remaining = load_installed_plugin_entries()
        assert len(remaining["test@mp"]) == 1
        assert remaining["test@mp"][0].scope == "user"

    def test_remove_all_scopes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Verify remove all scopes."""
        state_dir = tmp_path / "remove_all_state"
        monkeypatch.setattr(
            "talkops.plugins.store._installed_plugins_path",
            lambda: state_dir / "installed_plugins.json",
        )

        add_installed_plugin(
            "test@mp", install_path="/cache/test", version="1.0.0", scope="user"
        )

        removed = remove_installed_plugin("test@mp")
        assert removed is not None

        remaining = load_installed_plugin_entries()
        assert "test@mp" not in remaining

    def test_uninstall_deletes_cache_only_when_orphaned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Verify uninstall deletes cache only when orphaned."""
        state_dir = tmp_path / "uninstall_orphan_state"
        monkeypatch.setattr(
            "talkops.plugins.store._installed_plugins_path",
            lambda: state_dir / "installed_plugins.json",
        )
        monkeypatch.setattr(
            "talkops.plugins.store._user_settings_path",
            lambda: tmp_path / "settings.json",
        )

        cache_dir = tmp_path / "cache" / "test"
        cache_dir.mkdir(parents=True)
        (cache_dir / "plugin.json").write_text("{}", encoding="utf-8")

        project_root = tmp_path / "proj"
        project_root.mkdir()

        add_installed_plugin(
            "test@mp", install_path=str(cache_dir), version="1.0.0", scope="user"
        )
        add_installed_plugin(
            "test@mp",
            install_path=str(cache_dir),
            version="1.0.0",
            scope="project",
            project_root=project_root,
        )

        from talkops.config import paths

        monkeypatch.setattr(
            paths,
            "project_settings_path",
            lambda pr: pr / ".talkops" / "settings.json",
        )
        (project_root / ".talkops").mkdir(exist_ok=True)

        uninstall_plugin("test@mp", scope="project", project_root=project_root)
        assert cache_dir.is_dir(), "Cache should survive when user scope still references it"

        uninstall_plugin("test@mp", scope="user")
        assert not cache_dir.is_dir(), "Cache should be deleted when no scopes reference it"


def test_plugin_mutation_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify plugin_mutation_lock executes cleanly and protects critical sections."""
    lock_dir = tmp_path / "lock_dir"
    lock_dir.mkdir()
    monkeypatch.setenv("TALKOPS_PLUGIN_DIR", str(lock_dir))

    executed = []

    @plugin_mutation_lock()
    def _critical_func(x: int) -> int:
        executed.append(x)
        return x * 2

    res = _critical_func(21)
    assert res == 42
    assert executed == [21]
    assert (lock_dir / ".mutation.lock").exists()


# ── CLI Flag and Command Execution Tests ──────────────────────


class TestCLIPluginCommands:
    """Tests for talkops plugin CLI parsing and execution."""

    def test_setup_parser_subcommands(self):
        """Verify setup parser subcommands."""
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        setup_plugin_parser(sub)

        args_install = parser.parse_args(
            ["plugin", "install", "my-plugin@official", "--scope", "project"]
        )
        assert args_install.plugin_id == "my-plugin@official"
        assert args_install.scope == "project"

        args_enable = parser.parse_args(
            ["plugin", "enable", "my-plugin@official", "--scope", "local"]
        )
        assert args_enable.plugin_id == "my-plugin@official"
        assert args_enable.scope == "local"

    def test_execute_plugin_list_empty(self, monkeypatch: pytest.MonkeyPatch):
        """Verify execute plugin list empty."""
        monkeypatch.setattr(
            "talkops.plugins.commands_cli.list_available_plugins", lambda **kwargs: []
        )
        args = argparse.Namespace(plugin_command="list", output_format="text")
        output = execute_plugin_command(args)
        assert output == "No plugin marketplaces configured."

    def test_execute_plugin_list_json(self, monkeypatch: pytest.MonkeyPatch):
        """Verify execute plugin list json."""
        monkeypatch.setattr(
            "talkops.plugins.commands_cli.list_available_plugins",
            lambda **kwargs: [("p1@mp", "Test Plugin", True)],
        )
        args = argparse.Namespace(plugin_command="list", output_format="json")
        output = execute_plugin_command(args)
        assert output is None  # Printed JSON to stdout

    def test_execute_plugin_enable_disable(self, monkeypatch: pytest.MonkeyPatch):
        """Verify execute plugin enable disable."""
        enabled_calls = []

        def mock_set_enabled(plugin_id, enabled, **kwargs):
            """Provide mock set enabled test helper or fixture."""
            enabled_calls.append((plugin_id, enabled))

        monkeypatch.setattr(
            "talkops.plugins.commands_cli.set_installed_plugin_enabled", mock_set_enabled
        )

        args_en = argparse.Namespace(
            plugin_command="enable", plugin_id="my-plugin@official", scope="user"
        )
        out_en = execute_plugin_command(args_en)
        assert "Enabled plugin my-plugin@official" in (out_en or "")

        args_dis = argparse.Namespace(
            plugin_command="disable", plugin_id="my-plugin@official", scope="user"
        )
        out_dis = execute_plugin_command(args_dis)
        assert "Disabled plugin my-plugin@official" in (out_dis or "")
        assert enabled_calls == [("my-plugin@official", True), ("my-plugin@official", False)]


# ── Adapters Tests (Commands, Subagents, MCP with Repair) ──────


def test_plugin_subagents_adapter(tmp_path: Path):
    """Verify plugin subagents adapter."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    agent_file = agents_dir / "cloud_architect.md"
    agent_file.write_text(
        "---\nname: cloud_architect\ndescription: AWS Architecture agent\ntools:\n  - aws_describe\nskills:\n  - aws-infra:vpc-analyzer\n---\n\nYou are an AWS architect.",
        encoding="utf-8",
    )

    inventory = ComponentInventory(agents=(agents_dir,))
    plugin = PluginInstance(
        plugin_id="aws-pack@official",
        name="aws-pack",
        marketplace="official",
        version="1.0.0",
        root=tmp_path,
        data_dir=tmp_path / "data",
        manifest=None,
        inventory=inventory,
    )

    subagents = plugin_subagents((plugin,))
    assert len(subagents) == 1
    assert subagents[0]["name"] == "aws-pack@official:cloud_architect"
    assert subagents[0].get("tools") == ["aws_describe"]
    assert subagents[0].get("skills") == ["aws-infra:vpc-analyzer"]


def test_plugin_commands_adapter(tmp_path: Path):
    """Verify plugin commands adapter."""
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    cmd_file = commands_dir / "dcf.md"
    cmd_file.write_text("# DCF Command\nRun discounted cash flow valuation.", encoding="utf-8")

    inventory = ComponentInventory(commands=(commands_dir,))
    plugin = PluginInstance(
        plugin_id="finance@official",
        name="finance",
        marketplace="official",
        version="1.0.0",
        root=tmp_path,
        data_dir=tmp_path / "data",
        manifest=None,
        inventory=inventory,
    )

    handlers = plugin_commands((plugin,))
    assert len(handlers) == 1
    assert handlers[0].name == "/dcf"
    assert "/finance:dcf" in handlers[0].aliases


def test_mcp_json_repair_logic():
    # Corrupt JSON with missing comma between object keys and missing closing brace
    """Verify mcp json repair logic."""
    corrupt_content = """{
      "mcpServers": {
        "daloopa": {
          "command": "npx",
          "args": ["-y", "daloopa-mcp"]
        }
        "box": {
          "command": "npx",
          "args": ["-y", "box-mcp"]
        }
    """
    repaired = _try_repair_mcp_json(corrupt_content)
    assert repaired is not None
    assert "daloopa" in repaired["mcpServers"]
    assert "box" in repaired["mcpServers"]


def test_discover_plugin_mcp_configs_with_repair(tmp_path: Path):
    """Verify discover plugin mcp configs with repair."""
    mcp_file = tmp_path / ".mcp.json"
    mcp_file.write_text(
        """{
          "mcpServers": {
            "server1": {
              "command": "echo"
            }
            "server2": {
              "command": "cat"
            }
        """,
        encoding="utf-8",
    )

    inventory = ComponentInventory(mcp_files=(mcp_file,))
    plugin = PluginInstance(
        plugin_id="repaired-pack@official",
        name="repaired-pack",
        marketplace="official",
        version="1.0.0",
        root=tmp_path,
        data_dir=tmp_path / "data",
        manifest=None,
        inventory=inventory,
    )

    from talkops.plugins.adapters.mcp import plugin_mcp_configs

    configs = plugin_mcp_configs([plugin])
    assert len(configs) == 1
    server_keys = list(configs[0].keys())
    assert any("server1" in k for k in server_keys)
    assert any("server2" in k for k in server_keys)


@pytest.mark.asyncio
async def test_plugins_handler_cli_fallback():
    """Verify plugins handler cli fallback."""
    from talkops.commands.core.plugins import PluginsHandler

    ctx = CommandContext(
        app=None,
        settings=MagicMock(),
        raw_command="/plugins",
        args="",
    )
    res = await PluginsHandler().execute(ctx)
    assert res.success
    assert res.message is not None
    assert "Plugins" in res.message or "No plugins" in res.message


def test_hierarchical_scope_override_and_workspace_exclude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify project/local scopes and plugins.json exclude override global user enablement per workspace."""
    from talkops.config import paths
    from talkops.plugins.store import load_all_disabled_plugin_ids

    user_settings = tmp_path / "user" / "settings.json"
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    (repo_a / ".talkops").mkdir(parents=True)
    (repo_a / ".agents").mkdir(parents=True)
    (repo_b / ".talkops").mkdir(parents=True)

    monkeypatch.setattr("talkops.plugins.store._user_settings_path", lambda: user_settings)
    monkeypatch.setattr(
        paths, "project_settings_path", lambda pr: pr / ".talkops" / "settings.json"
    )
    monkeypatch.setattr(
        paths,
        "project_local_settings_path",
        lambda pr: pr / ".talkops" / "settings.local.json",
    )
    monkeypatch.setattr(
        "talkops.plugins.store._ensure_local_settings_gitignored", lambda _: None
    )

    # Enable two plugins globally for user
    set_plugin_enabled("global-one@mp", True)
    set_plugin_enabled("global-two@mp", True)
    set_plugin_enabled("global-disabled@mp", False)

    # Disable global-one in repo_a via project settings, and enable global-disabled in repo_a
    set_plugin_enabled_for_scope(
        "global-one@mp", False, scope="project", project_root=repo_a
    )
    set_plugin_enabled_for_scope(
        "global-disabled@mp", True, scope="local", project_root=repo_a
    )

    # Exclude global-two in repo_a via .agents/plugins.json
    (repo_a / ".agents" / "plugins.json").write_text(
        json.dumps({"exclude": ["global-two"]}),
        encoding="utf-8",
    )

    enabled_a = load_all_enabled_plugin_ids(project_root=repo_a)
    disabled_a = load_all_disabled_plugin_ids(project_root=repo_a)
    assert "global-one@mp" not in enabled_a
    assert "global-one@mp" in disabled_a
    assert "global-two@mp" not in enabled_a
    assert "global-two@mp" in disabled_a
    assert "global-disabled@mp" in enabled_a
    assert "global-disabled@mp" not in disabled_a

    # In repo_b, global-one and global-two remain enabled and global-disabled remains disabled
    enabled_b = load_all_enabled_plugin_ids(project_root=repo_b)
    disabled_b = load_all_disabled_plugin_ids(project_root=repo_b)
    assert "global-one@mp" in enabled_b
    assert "global-two@mp" in enabled_b
    assert "global-disabled@mp" not in enabled_b
    assert "global-disabled@mp" in disabled_b


def test_workspace_plugin_isolation_and_agents_manifest_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify .agents/plugins, .talkops/plugins, and plugins.json entries are isolated to their workspace."""
    from talkops.config import paths
    from talkops.plugins.discovery import discover_plugins

    monkeypatch.setattr(
        "talkops.plugins.store._user_settings_path", lambda: tmp_path / "user_settings.json"
    )
    monkeypatch.setattr(
        paths, "project_settings_path", lambda pr: pr / ".talkops" / "settings.json"
    )
    monkeypatch.setattr(
        paths,
        "project_local_settings_path",
        lambda pr: pr / ".talkops" / "settings.local.json",
    )

    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()

    # 1. Create a plugin in repo_a/.agents/plugins/agy-style-plugin using .agents-plugin/plugin.json
    agy_plugin = repo_a / ".agents" / "plugins" / "agy-style-plugin"
    (agy_plugin / ".agents-plugin").mkdir(parents=True)
    (agy_plugin / ".agents-plugin" / "plugin.json").write_text(
        json.dumps({"name": "agy-style-plugin", "version": "2.1.0"}),
        encoding="utf-8",
    )
    (agy_plugin / "skills" / "agy-skill").mkdir(parents=True)
    (agy_plugin / "skills" / "agy-skill" / "SKILL.md").write_text(
        "---\nname: agy-skill\ndescription: Agy skill\n---\nInstructions",
        encoding="utf-8",
    )

    # 2. Create a plugin referenced via repo_a/.talkops/plugins.json entries
    ext_plugin = repo_a / "custom_ bundles" / "declared-plugin"
    ext_plugin.mkdir(parents=True)
    (ext_plugin / "plugin.json").write_text(
        json.dumps({"name": "declared-plugin", "version": "1.0.0"}),
        encoding="utf-8",
    )
    (ext_plugin / "skills" / "declared-skill").mkdir(parents=True)
    (ext_plugin / "skills" / "declared-skill" / "SKILL.md").write_text(
        "---\nname: declared-skill\ndescription: Declared skill\n---\nInstructions",
        encoding="utf-8",
    )
    (repo_a / ".talkops").mkdir(parents=True, exist_ok=True)
    (repo_a / ".talkops" / "plugins.json").write_text(
        json.dumps({"entries": ["../custom_ bundles/declared-plugin"]}),
        encoding="utf-8",
    )

    res_a = discover_plugins(project_root=repo_a)
    ids_a = {p.plugin_id for p in res_a.plugins}
    assert "agy-style-plugin@project" in ids_a
    assert "declared-plugin@project" in ids_a

    # Verify Repo B cannot see Repo A's plugins
    res_b = discover_plugins(project_root=repo_b)
    assert len(res_b.plugins) == 0

    # Verify disabling agy-style-plugin@project in repo_a suppresses it
    set_plugin_enabled_for_scope(
        "agy-style-plugin@project", False, scope="project", project_root=repo_a
    )
    res_a_after = discover_plugins(project_root=repo_a)
    ids_a_after = {p.plugin_id for p in res_a_after.plugins}
    assert "agy-style-plugin@project" not in ids_a_after
    assert "declared-plugin@project" in ids_a_after


def test_load_installed_plugins_excludes_foreign_projects_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify load_installed_plugins(include_foreign_projects=False) isolates project-scoped entries."""
    state_dir = tmp_path / "isolated_state"
    monkeypatch.setattr(
        "talkops.plugins.store._installed_plugins_path",
        lambda: state_dir / "installed_plugins.json",
    )

    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()

    add_installed_plugin(
        "repo-a-only@mp",
        install_path="/cache/repo-a",
        version="1.0.0",
        scope="project",
        project_root=repo_a,
    )
    add_installed_plugin(
        "global-plugin@mp",
        install_path="/cache/global",
        version="1.0.0",
        scope="user",
    )

    # In repo_a: both are visible
    installed_a = load_installed_plugins(
        project_root=repo_a, include_foreign_projects=False
    )
    assert set(installed_a) == {"repo-a-only@mp", "global-plugin@mp"}

    # In repo_b with include_foreign_projects=False: repo-a-only@mp is hidden
    installed_b = load_installed_plugins(
        project_root=repo_b, include_foreign_projects=False
    )
    assert set(installed_b) == {"global-plugin@mp"}

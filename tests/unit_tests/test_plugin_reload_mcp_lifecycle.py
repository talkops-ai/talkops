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

"""Regression tests for marketplace plugin install/uninstall MCP reload & subagent binding."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.language_models.chat_models import SimpleChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

import talkops.mcp.preload as preload_mod
from talkops.agent.factory import create_talkops_agent_async
from talkops.commands.power.agents import get_agent_session_state
from talkops.mcp.mcp_info import MCPServerInfo, MCPToolInfo
from talkops.mcp.preload import (
    clear_cached_mcp_server_infos,
    get_cached_mcp_server_infos,
    is_mcp_server_info_cached,
    preload_mcp_metadata,
    preload_mcp_server_info,
    probe_one_mcp_server,
)
from talkops.mcp.session_manager import MCPSessionManager, _MCPSessionEntry
from talkops.plugins.discovery import uninstall_plugin
from talkops.plugins.store import add_installed_plugin, set_plugin_enabled_for_scope
from talkops.ui.app import TalkOpsApp


class _RecordingChatModel(SimpleChatModel):
    """Deterministic fake chat model that records bound tools and system prompts."""

    bound_tool_names: list[list[str]] = []
    seen_system_prompts: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "recording-chat-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        names = [
            str(getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else t))
            for t in (tools or ())
        ]
        self.bound_tool_names.append(names)
        return self

    def _call(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> str:
        sys_parts = [
            str(m.content)
            for m in messages
            if getattr(m, "type", "") == "system"
        ]
        self.seen_system_prompts.append("\n".join(sys_parts))
        return "Checked Kargo via MCP."

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        text = self._call(messages, stop=stop, run_manager=run_manager, **kwargs)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])


def _create_k8s_delivery_plugin(root: Path) -> Path:
    """Create a realistic marketplace agent plugin mirroring kubernetes-delivery-engineer."""
    bundle = root / "kubernetes-delivery-engineer"
    (bundle / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (bundle / "agents").mkdir(parents=True, exist_ok=True)
    (bundle / "skills" / "kargo-ops").mkdir(parents=True, exist_ok=True)

    (bundle / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "kubernetes-delivery-engineer",
                "version": "1.0.0",
                "description": "Kubernetes progressive delivery specialist",
            }
        ),
        encoding="utf-8",
    )
    (bundle / "skills" / "kargo-ops" / "SKILL.md").write_text(
        "---\nname: kargo-ops\ndescription: Kargo progressive delivery skill\n---\nUse kargo MCP tools.\n",
        encoding="utf-8",
    )
    (bundle / "agents" / "kubernetes-delivery-engineer.md").write_text(
        "---\n"
        "name: kubernetes-delivery-engineer\n"
        "description: Progressive delivery engineer for Helm and Kargo\n"
        "tools: Read, Bash, mcp__plugin_kubernetes-delivery-engineer_kargo__*, "
        "mcp__plugin_kubernetes-delivery-engineer_helm__*\n"
        "skills:\n"
        "  - kargo-ops\n"
        "---\n"
        "You are the Kubernetes Delivery Engineer.\n",
        encoding="utf-8",
    )
    (bundle / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "kargo": {
                        "command": "uv",
                        "args": ["run", "kargo-mcp-server"],
                    },
                    "helm": {
                        "command": "uv",
                        "args": ["run", "helm-mcp-server"],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return bundle


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Isolate TalkOps config, state, MCP caches, schema dir, and plugin storage."""
    config_dir = tmp_path / ".talkops"
    state_dir = config_dir / ".state"
    mcp_schema_dir = config_dir / "mcp"
    plugin_storage = tmp_path / "plugin_storage"
    workspace = tmp_path / "workspace"
    agents_shared = tmp_path / ".agents"

    for d in (config_dir, state_dir, mcp_schema_dir, plugin_storage, workspace, agents_shared):
        d.mkdir(parents=True, exist_ok=True)

    cache_file = state_dir / "mcp_metadata_cache.json"
    monkeypatch.setenv("TALKOPS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("TALKOPS_PLUGIN_DIR", str(plugin_storage))
    monkeypatch.setenv("TALKOPS_MCP_METADATA_CACHE", str(cache_file))
    monkeypatch.setenv("TALKOPS_MCP_SCHEMA_DIR", str(mcp_schema_dir))

    from talkops.config import paths
    from talkops.plugins import store

    monkeypatch.setattr(paths, "DATA_DIR", config_dir)
    monkeypatch.setattr(paths, "STATE_DIR", state_dir)
    monkeypatch.setattr(paths, "GLOBAL_MCP_PATH", config_dir / ".mcp.json")
    monkeypatch.setattr(paths, "AGENTS_SHARED_DIR", agents_shared)
    monkeypatch.setattr(paths, "user_agents_dir", lambda assistant_id="talkops": config_dir / assistant_id / "agents")
    monkeypatch.setattr(paths, "project_settings_path", lambda pr: pr / ".talkops" / "settings.json")
    monkeypatch.setattr(paths, "project_local_settings_path", lambda pr: pr / ".talkops" / "settings.local.json")

    monkeypatch.setattr(store, "_state_dir", lambda: state_dir)
    monkeypatch.setattr(store, "_installed_plugins_path", lambda: state_dir / "installed_plugins.json")
    monkeypatch.setattr(store, "plugin_storage_root", lambda: plugin_storage)
    monkeypatch.setattr(store, "_user_settings_path", lambda: config_dir / "settings.json")
    monkeypatch.setattr(store, "load_marketplace_records", lambda project_root=None: {})

    clear_cached_mcp_server_infos()
    preload_mod._PERSISTENT_CACHE_LOADED = False
    preload_mod._PERSISTENT_CACHE_SUPPRESSED = False
    old_mgr = MCPSessionManager._instance
    MCPSessionManager._instance = None

    yield {
        "tmp": tmp_path,
        "config_dir": config_dir,
        "state_dir": state_dir,
        "mcp_schema_dir": mcp_schema_dir,
        "cache_file": cache_file,
        "plugin_storage": plugin_storage,
        "workspace": workspace,
    }

    clear_cached_mcp_server_infos()
    MCPSessionManager._instance = old_mgr


@pytest.mark.asyncio
async def test_cancelled_probe_does_not_poison_cache_and_subsequent_preload_succeeds(
    isolated_env: dict[str, Path],
) -> None:
    """Cancelling an MCP probe (e.g. via wait_for timeout) must NOT cache a bogus error."""
    slow_script = (
        "import sys, time\n"
        "time.sleep(5)\n"
    )
    srv_cfg = {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-c", slow_script],
    }

    # 1. Direct cancellation of probe_one_mcp_server raises CancelledError (not error MCPServerInfo)
    probe_task = asyncio.create_task(probe_one_mcp_server("kargo", srv_cfg))
    await asyncio.sleep(0.08)
    probe_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe_task

    assert not is_mcp_server_info_cached("kargo", srv_cfg)
    assert get_cached_mcp_server_infos() == []

    # 2. Timeout cancellation during preload_mcp_metadata leaves cache unpoisoned
    async def _slow_then_fast_probe(name: str, cfg: Any) -> MCPServerInfo:
        await asyncio.sleep(0.25)
        return MCPServerInfo(
            name=name,
            transport="stdio",
            status="ok",
            tools=(
                MCPToolInfo(
                    name=f"{name}:kargo_list_projects",
                    description="List Kargo projects",
                    input_schema={"type": "object", "properties": {}},
                    original_name="kargo_list_projects",
                ),
            ),
            supports_resources=True,
            instructions="Use kargo://projects to inspect Kargo state.",
        )

    configs = {"subagent__k8s__kargo": {"command": "uv", "args": ["run", "kargo-mcp"]}}
    with patch.object(preload_mod, "probe_one_mcp_server", side_effect=_slow_then_fast_probe):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(preload_mcp_metadata(configs), timeout=0.05)

        # Must NOT have cached an error stub!
        assert not is_mcp_server_info_cached("subagent__k8s__kargo", configs["subagent__k8s__kargo"])
        assert get_cached_mcp_server_infos() == []

        # Subsequent full preload succeeds and populates tools + resources + schema files
        infos = await preload_mcp_metadata(configs)
        assert len(infos) == 1
        assert infos[0].status == "ok"
        assert infos[0].supports_resources is True
        assert [t.original_name for t in infos[0].tools] == ["kargo_list_projects"]
        schema_path = isolated_env["mcp_schema_dir"] / "subagent__k8s__kargo" / "kargo_list_projects.json"
        instr_path = isolated_env["mcp_schema_dir"] / "subagent__k8s__kargo" / "instructions.md"
        assert schema_path.is_file()
        assert instr_path.is_file()


@pytest.mark.asyncio
async def test_concurrent_preload_deduplicates_inflight_probes(
    isolated_env: dict[str, Path],
) -> None:
    """Overlapping preload_mcp_metadata calls share a single in-flight subprocess probe."""
    probe_calls = 0

    async def _fake_probe(name: str, cfg: Any) -> MCPServerInfo:
        nonlocal probe_calls
        probe_calls += 1
        await asyncio.sleep(0.08)
        return MCPServerInfo(
            name=name,
            transport="stdio",
            status="ok",
            tools=(
                MCPToolInfo(
                    name=f"{name}:helm_list_releases",
                    description="List Helm releases",
                    original_name="helm_list_releases",
                ),
            ),
            supports_resources=True,
        )

    configs = {"subagent__k8s__helm": {"command": "uv", "args": ["run", "helm-mcp"]}}
    with patch.object(preload_mod, "probe_one_mcp_server", side_effect=_fake_probe):
        res_a, res_b = await asyncio.gather(
            preload_mcp_metadata(configs),
            preload_mcp_metadata(configs),
        )

    assert probe_calls == 1
    assert len(res_a) == 1 and res_a[0].status == "ok"
    assert len(res_b) == 1 and res_b[0].status == "ok"


@pytest.mark.asyncio
async def test_marketplace_agent_plugin_install_and_reload_binds_mcp_tools_and_resources_immediately(
    isolated_env: dict[str, Path],
) -> None:
    """Installing an agent plugin and reloading binds its MCP tools & resources before graph rebuild."""
    workspace = isolated_env["workspace"]
    plugin_bundle = _create_k8s_delivery_plugin(isolated_env["tmp"] / "marketplace_cache")
    plugin_id = "kubernetes-delivery-engineer@talkops-devops-plugins"

    async def _fake_probe(name: str, cfg: Any) -> MCPServerInfo:
        base = name.rsplit("__", 1)[-1]
        tool_orig = f"{base}_list_projects" if base == "kargo" else f"{base}_list_releases"
        return MCPServerInfo(
            name=name,
            transport="stdio",
            status="ok",
            tools=(
                MCPToolInfo(
                    name=f"{name}:{tool_orig}",
                    description=f"Query {base}",
                    input_schema={"type": "object", "properties": {"namespace": {"type": "string"}}},
                    original_name=tool_orig,
                ),
            ),
            supports_resources=True,
            instructions=f"Prefer {base}:// resources and {base} MCP tools over kubectl.",
        )

    # Track ordering: preload_mcp_server_info MUST complete BEFORE server_proc.restart() compiles the graph
    lifecycle_events: list[str] = []
    compiled_graph_holder: dict[str, Any] = {}
    recording_model = _RecordingChatModel()

    async def _rebuild_inprocess_graph(timeout: float = 60.0) -> str:
        lifecycle_events.append("server_restart")
        graph, _ = await create_talkops_agent_async(
            model=recording_model,
            cwd=workspace,
            auto_approve=True,
            interactive=False,
            defer_mcp_preload=True,
            checkpointer=InMemorySaver(),
        )
        compiled_graph_holder["graph"] = graph
        return "inprocess://local"

    mock_proc = MagicMock()
    mock_proc.is_in_process = True
    mock_proc.restart = AsyncMock(side_effect=_rebuild_inprocess_graph)
    mock_proc.create_client = MagicMock(return_value=MagicMock())

    app = TalkOpsApp(
        defer_server_start=True,
        mcp_preload_kwargs={
            "mcp_config_path": None,
            "no_mcp": False,
            "trust_project_mcp": True,
            "include_subagents": True,
            "project_root": workspace,
        },
    )
    app._server_proc = mock_proc
    app._settings.reload_from_environment = MagicMock(return_value=[])
    mounted_messages: list[Any] = []
    app._mount_message = AsyncMock(side_effect=lambda m: mounted_messages.append(m))

    # Install marketplace plugin (simulating PluginManagerScreen install)
    add_installed_plugin(
        plugin_id,
        install_path=str(plugin_bundle),
        version="1.0.0",
        scope="user",
    )
    set_plugin_enabled_for_scope(plugin_id, scope="user", enabled=True)

    orig_preload_fn = preload_mod.preload_mcp_server_info

    async def _tracked_preload(**kwargs: Any) -> list[MCPServerInfo]:
        lifecycle_events.append("mcp_preload_start")
        res = await orig_preload_fn(**kwargs)
        lifecycle_events.append("mcp_preload_done")
        return res

    with (
        patch.object(preload_mod, "probe_one_mcp_server", side_effect=_fake_probe),
        patch.object(preload_mod, "preload_mcp_server_info", side_effect=_tracked_preload),
        patch("talkops.ui.theme.reload_registry"),
        patch("talkops.ui.theme.register_app_themes"),
        patch("talkops.model.config.clear_caches"),
    ):
        await app._run_reload_unlocked()

    # 1. Preload completed BEFORE graph restart!
    assert lifecycle_events == ["mcp_preload_start", "mcp_preload_done", "server_restart"]

    # 2. Reload report reflects the 2 subagent MCP servers and Loaded status
    assert len(mounted_messages) == 1
    reload_report = getattr(mounted_messages[0], "_raw_content", "")
    assert "2 plugin MCP servers" in reload_report
    assert "Loaded:" in reload_report
    assert "kargo" in reload_report and "helm" in reload_report

    # 3. Enter the dynamic subagent console immediately (without restarting TUI) and invoke a task!
    graph = compiled_graph_holder["graph"]
    recording_model.bound_tool_names.clear()
    recording_model.seen_system_prompts.clear()

    thread_cfg = {"configurable": {"thread_id": "reload-install-thread"}}
    await graph.ainvoke(
        {
            "messages": [],
            "agent_session_request": {
                "kind": "start",
                "agent_name": plugin_id,
                "plugin_id": plugin_id,
                "prompt": "Check Kargo stages in namespace kargo",
            },
        },
        config=thread_cfg,
    )

    state_snap = await graph.aget_state(thread_cfg)
    assert state_snap.values.get("_active_agent_id") == plugin_id
    assert len(recording_model.bound_tool_names) >= 1
    bound_tools = set(recording_model.bound_tool_names[-1])

    # MCP tools and MCP resource tools MUST be bound on the very first turn!
    assert any("kargo_list_projects" in t for t in bound_tools) or "call_mcp_tool" in bound_tools
    assert any("helm_list_releases" in t for t in bound_tools) or "call_mcp_tool" in bound_tools
    assert "list_resources" in bound_tools
    assert "read_resource" in bound_tools

    # System prompt MUST contain active MCP inventory & resource guidance, NOT UNAVAILABLE
    sys_prompt = recording_model.seen_system_prompts[-1]
    assert "UNAVAILABLE" not in sys_prompt
    assert "Connection timed out" not in sys_prompt
    assert "kargo_list_projects" in sys_prompt
    assert "helm_list_releases" in sys_prompt
    assert "list_resources" in sys_prompt
    assert "read_resource" in sys_prompt

    # Schema files & instructions materialized on disk
    kargo_dirs = list(isolated_env["mcp_schema_dir"].glob("*kargo*"))
    assert len(kargo_dirs) == 1
    assert (kargo_dirs[0] / "kargo_list_projects.json").is_file()
    assert (kargo_dirs[0] / "instructions.md").is_file()


@pytest.mark.asyncio
async def test_agent_session_first_turn_awaits_uncached_mcp_when_compiled_with_deferred_preload(
    isolated_env: dict[str, Path],
) -> None:
    """Even when compiled cold (defer_mcp_preload=True), entering /agent probes MCP before Turn 1."""
    workspace = isolated_env["workspace"]
    plugin_bundle = _create_k8s_delivery_plugin(isolated_env["tmp"] / "cold_cache")
    plugin_id = "kubernetes-delivery-engineer@talkops-devops-plugins"

    add_installed_plugin(
        plugin_id,
        install_path=str(plugin_bundle),
        version="1.0.0",
        scope="user",
    )
    set_plugin_enabled_for_scope(plugin_id, scope="user", enabled=True)

    recording_model = _RecordingChatModel()
    recording_model.bound_tool_names.clear()
    recording_model.seen_system_prompts.clear()

    # Compile graph BEFORE any MCP probes have run (simulating cold TUI startup with defer_mcp_preload=True)
    graph, _ = await create_talkops_agent_async(
        model=recording_model,
        cwd=workspace,
        auto_approve=True,
        interactive=False,
        defer_mcp_preload=True,
        checkpointer=InMemorySaver(),
    )
    assert get_cached_mcp_server_infos() == []

    async def _fake_probe(name: str, cfg: Any) -> MCPServerInfo:
        base = name.rsplit("__", 1)[-1]
        tool_orig = f"{base}_list_projects" if base == "kargo" else f"{base}_list_releases"
        return MCPServerInfo(
            name=name,
            transport="stdio",
            status="ok",
            tools=(
                MCPToolInfo(
                    name=f"{name}:{tool_orig}",
                    description=f"Query {base}",
                    original_name=tool_orig,
                ),
            ),
            supports_resources=True,
        )

    with patch.object(preload_mod, "probe_one_mcp_server", side_effect=_fake_probe) as mock_probe:
        await graph.ainvoke(
            {
                "messages": [],
                "agent_session_request": {
                    "kind": "start",
                    "agent_name": plugin_id,
                    "plugin_id": plugin_id,
                    "prompt": "Inspect Kargo promotions",
                },
            },
            config={"configurable": {"thread_id": "cold-deferred-thread"}},
        )

    assert mock_probe.await_count == 2
    bound_tools = set(recording_model.bound_tool_names[-1])
    assert any("kargo_list_projects" in t for t in bound_tools)
    assert any("helm_list_releases" in t for t in bound_tools)
    assert "list_resources" in bound_tools
    assert "read_resource" in bound_tools
    assert "kargo_list_projects" in recording_model.seen_system_prompts[-1]


@pytest.mark.asyncio
async def test_plugin_uninstall_and_reload_closes_sessions_purges_caches_schema_dirs_and_detaches_agent(
    isolated_env: dict[str, Path],
) -> None:
    """Uninstalling a plugin and reloading closes live MCP sessions, purges caches/dirs, and detaches /agent."""
    workspace = isolated_env["workspace"]
    plugin_bundle = _create_k8s_delivery_plugin(isolated_env["tmp"] / "uninstall_cache")
    plugin_id = "kubernetes-delivery-engineer@talkops-devops-plugins"

    add_installed_plugin(
        plugin_id,
        install_path=str(plugin_bundle),
        version="1.0.0",
        scope="user",
    )
    set_plugin_enabled_for_scope(plugin_id, scope="user", enabled=True)

    async def _fake_probe(name: str, cfg: Any) -> MCPServerInfo:
        base = name.rsplit("__", 1)[-1]
        return MCPServerInfo(
            name=name,
            transport="stdio",
            status="ok",
            tools=(
                MCPToolInfo(
                    name=f"{name}:{base}_status",
                    description=f"{base} status",
                    original_name=f"{base}_status",
                ),
            ),
            supports_resources=True,
            instructions=f"{base} instructions",
        )

    # Initial preload populates _PROBED_CACHE, mcp_metadata_cache.json, schema dirs, and MCPSessionManager
    with patch.object(preload_mod, "probe_one_mcp_server", side_effect=_fake_probe):
        await preload_mcp_server_info(include_subagents=True, project_root=workspace)

    probed_names = [s.name for s in get_cached_mcp_server_infos()]
    assert len(probed_names) == 2
    kargo_srv_name = [n for n in probed_names if n.endswith("__kargo")][0]
    assert (isolated_env["mcp_schema_dir"] / kargo_srv_name / "kargo_status.json").is_file()

    persisted_before = json.loads(isolated_env["cache_file"].read_text(encoding="utf-8"))
    assert kargo_srv_name in persisted_before["entries"]

    # Simulate an active live MCP session in MCPSessionManager
    mgr = MCPSessionManager.get_instance()
    mock_entry = MagicMock(spec=_MCPSessionEntry)
    mock_entry.session = MagicMock()
    mock_entry.aclose = AsyncMock()
    mgr._sessions[kargo_srv_name] = mock_entry
    assert kargo_srv_name in mgr._config

    # Simulate TUI with active `/agent` session attached to kubernetes-delivery-engineer
    mock_proc = MagicMock()
    mock_proc.is_in_process = True
    mock_proc.restart = AsyncMock(return_value="inprocess://local")
    mock_proc.create_client = MagicMock(return_value=MagicMock())

    app = TalkOpsApp(
        defer_server_start=True,
        mcp_preload_kwargs={
            "mcp_config_path": None,
            "no_mcp": False,
            "trust_project_mcp": True,
            "include_subagents": True,
            "project_root": workspace,
        },
    )
    app._server_proc = mock_proc
    app._settings.reload_from_environment = MagicMock(return_value=[])
    mounted_messages: list[Any] = []
    app._mount_message = AsyncMock(side_effect=lambda m: mounted_messages.append(m))

    ag_state = get_agent_session_state(app)
    ag_state.agent_id = plugin_id
    ag_state.short_name = "kubernetes-delivery-engineer"
    ag_state.plugin_id = plugin_id
    ag_state.attached = True
    assert ag_state.is_active is True

    # Uninstall plugin and run reload
    uninstall_plugin(plugin_id, scope="user", project_root=workspace)

    with (
        patch("talkops.ui.theme.reload_registry"),
        patch("talkops.ui.theme.register_app_themes"),
        patch("talkops.model.config.clear_caches"),
    ):
        await app._run_reload_unlocked()

    # 1. Live MCP session was closed and server configs were purged from MCPSessionManager
    mock_entry.aclose.assert_awaited_once()
    assert kargo_srv_name not in mgr._sessions
    assert kargo_srv_name not in mgr._config

    # 2. In-memory _PROBED_CACHE and on-disk mcp_metadata_cache.json purged
    assert get_cached_mcp_server_infos() == []
    persisted_after = json.loads(isolated_env["cache_file"].read_text(encoding="utf-8"))
    assert kargo_srv_name not in persisted_after["entries"]
    assert persisted_after["entries"] == {}

    # 3. Materialized ~/.talkops/mcp/subagent__* directories removed from disk
    assert not (isolated_env["mcp_schema_dir"] / kargo_srv_name).exists()
    assert list(isolated_env["mcp_schema_dir"].glob("subagent__*")) == []

    # 4. Active TUI `/agent` session detached cleanly & reload report reflects removal
    assert ag_state.is_active is False
    assert ag_state.agent_id is None
    reload_report = getattr(mounted_messages[0], "_raw_content", "")
    assert "Agent session detached" in reload_report
    assert "0 plugin MCP servers" in reload_report
    assert "Removed:" in reload_report
    assert kargo_srv_name in reload_report


@pytest.mark.asyncio
async def test_dotenv_argocd_env_update_restarts_stdio_mcp_subprocess_on_reload_and_restart(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editing `.env` and running `/reload` or `/restart` must respawn live stdio MCP subprocesses with fresh env."""
    import importlib

    from talkops.cli.server_manager import InProcessServerHandle
    from talkops.config import paths
    from talkops.mcp.session_manager import build_mcp_tools_from_server_infos
    from talkops.server._server_config import ServerConfig

    settings_mod = importlib.import_module("talkops.config.settings")

    for key in ("ARGOCD_SERVER_URL", "ARGOCD_INSECURE", "ARGOCD_USERNAME", "ARGOCD_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    global_env_file = isolated_env["config_dir"] / ".env"
    config_toml_file = isolated_env["config_dir"] / "config.toml"
    monkeypatch.setattr(paths, "GLOBAL_ENV_PATH", global_env_file)
    monkeypatch.setattr(settings_mod, "GLOBAL_ENV_PATH", global_env_file)
    monkeypatch.setattr(settings_mod, "CONFIG_PATH", config_toml_file)
    settings_mod.reset_settings_for_testing()

    # Inline stdio MCP server that freezes ARGOCD_* env vars at process startup (mirroring argocd-mcp-server)
    argocd_server_script = isolated_env["tmp"] / "fake_argocd_mcp.py"
    argocd_server_script.write_text(
        "import json, os\n"
        "from mcp.server.fastmcp import FastMCP\n"
        "FROZEN_URL = os.environ.get('ARGOCD_SERVER_URL', 'https://argocd-server.argocd.svc:443')\n"
        "FROZEN_INSECURE = os.environ.get('ARGOCD_INSECURE', 'false')\n"
        "FROZEN_USER = os.environ.get('ARGOCD_USERNAME', '')\n"
        "FROZEN_PID = os.getpid()\n"
        "mcp = FastMCP('argocd')\n"
        "@mcp.tool()\n"
        "def list_applications() -> str:\n"
        "    return json.dumps({'server_url': FROZEN_URL, 'insecure': FROZEN_INSECURE, 'user': FROZEN_USER, 'pid': FROZEN_PID})\n"
        "if __name__ == '__main__':\n"
        "    mcp.run(transport='stdio')\n",
        encoding="utf-8",
    )

    workspace = isolated_env["workspace"]
    plugin_bundle = _create_k8s_delivery_plugin(isolated_env["tmp"] / "argocd_plugin_cache")
    (plugin_bundle / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "argocd": {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [str(argocd_server_script)],
                        "env": {
                            "FASTMCP_LOG_LEVEL": "ERROR",
                            "MCP_ALLOW_WRITE": "false",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    plugin_id = "kubernetes-delivery-engineer@talkops-devops-plugins"
    add_installed_plugin(
        plugin_id,
        install_path=str(plugin_bundle),
        version="0.2.0",
        scope="user",
    )
    set_plugin_enabled_for_scope(plugin_id, scope="user", enabled=True)

    # Initial settings bootstrap BEFORE .env has ARGOCD_* credentials
    test_settings = settings_mod.get_settings()
    await preload_mcp_server_info(include_subagents=True, project_root=workspace)
    infos = [s for s in get_cached_mcp_server_infos() if s.name.endswith("__argocd")]
    assert len(infos) == 1 and infos[0].status == "ok"
    argocd_srv_name = infos[0].name

    def _extract_tool_json(raw: Any) -> dict[str, Any]:
        val = raw[0] if isinstance(raw, tuple) else raw
        if isinstance(val, list) and val and isinstance(val[0], dict) and "text" in val[0]:
            return json.loads(str(val[0]["text"]))
        return json.loads(str(val))

    mgr = MCPSessionManager.get_instance()
    try:
        tools = build_mcp_tools_from_server_infos(infos, mgr, configs={argocd_srv_name: mgr._config[argocd_srv_name]})
        list_apps_tool = next(t for t in tools if "list_applications" in t.name)

        # 1. Before `.env` update: picks default cluster DNS URL
        payload_before = _extract_tool_json(await list_apps_tool.ainvoke({}))
        assert payload_before["server_url"] == "https://argocd-server.argocd.svc:443"
        assert payload_before["insecure"] == "false"
        pid_initial = payload_before["pid"]
        assert argocd_srv_name in mgr._sessions

        # Set up InProcessServerHandle + TalkOpsApp
        srv_cfg = ServerConfig(cwd=str(workspace), model="openai:gpt-4o-mini")
        inproc_handle = InProcessServerHandle(srv_cfg)

        async def _fake_inproc_astart(timeout: float = 60.0) -> str:
            return "inprocess://local"

        monkeypatch.setattr(inproc_handle, "astart", _fake_inproc_astart)
        monkeypatch.setattr(inproc_handle, "create_client", lambda: MagicMock())

        app = TalkOpsApp(
            defer_server_start=True,
            cwd=str(workspace),
            server_kwargs={"assistant_id": "talkops", "interactive": True, "cwd": str(workspace)},
            mcp_preload_kwargs={
                "mcp_config_path": None,
                "no_mcp": False,
                "trust_project_mcp": True,
                "include_subagents": True,
                "project_root": workspace,
            },
        )
        app._server_proc = inproc_handle
        app._settings = test_settings
        mounted_messages: list[Any] = []
        app._mount_message = AsyncMock(side_effect=lambda m: mounted_messages.append(m))
        transient_mock = MagicMock()
        transient_mock.remove = AsyncMock()
        app._mount_transient_app_message = AsyncMock(return_value=transient_mock)

        # 2. User writes ARGOCD_* into ~/.talkops/.env and runs `/reload`
        global_env_file.write_text(
            "ARGOCD_SERVER_URL=https://localhost:8443\n"
            "ARGOCD_INSECURE=true\n"
            "ARGOCD_USERNAME=admin\n"
            "ARGOCD_PASSWORD=super-secret-pass\n",
            encoding="utf-8",
        )

        with (
            patch("talkops.ui.theme.reload_registry"),
            patch("talkops.ui.theme.register_app_themes"),
            patch("talkops.model.config.clear_caches"),
        ):
            await app._run_reload_unlocked()

        reload_report = getattr(mounted_messages[-1], "_raw_content", "")
        assert "ARGOCD_SERVER_URL: added (.env)" in reload_report
        assert "ARGOCD_PASSWORD: added (.env)" in reload_report
        assert "super-secret-pass" not in reload_report

        payload_reload = _extract_tool_json(await list_apps_tool.ainvoke({}))
        assert payload_reload["server_url"] == "https://localhost:8443"
        assert payload_reload["insecure"] == "true"
        assert payload_reload["user"] == "admin"
        assert payload_reload["pid"] != pid_initial

        # 3. User updates ARGOCD_SERVER_URL in ~/.talkops/.env and runs `/restart`
        pid_after_reload = payload_reload["pid"]
        global_env_file.write_text(
            "ARGOCD_SERVER_URL=https://localhost:9443\n"
            "ARGOCD_INSECURE=true\n"
            "ARGOCD_USERNAME=admin\n"
            "ARGOCD_PASSWORD=super-secret-pass\n",
            encoding="utf-8",
        )

        with patch("talkops.model.config.clear_caches"):
            restarted = await app._run_restart_command()
        assert restarted is True

        payload_restart = _extract_tool_json(await list_apps_tool.ainvoke({}))
        assert payload_restart["server_url"] == "https://localhost:9443"
        assert payload_restart["pid"] != pid_after_reload
    finally:
        await mgr.close_all()
        settings_mod.reset_settings_for_testing()


@pytest.mark.asyncio
async def test_reload_evicts_failed_mcp_probes_and_invalidates_interpolated_env_fingerprints(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit reload evicts negative-cached failed probes and `${VAR}` changes invalidate fingerprints."""
    from talkops.mcp.preload import (
        _config_fingerprint,
        _connection_fingerprint,
        evict_failed_mcp_server_infos,
        set_cached_mcp_server_info,
    )

    cfg_with_var = {
        "transport": "stdio",
        "command": "uvx",
        "args": ["argocd-mcp-server"],
        "env": {"MCP_ALLOW_WRITE": "${MCP_ALLOW_WRITE:-false}"},
    }
    monkeypatch.delenv("MCP_ALLOW_WRITE", raising=False)
    fp_false = _config_fingerprint(cfg_with_var)
    conn_fp_false = _connection_fingerprint(cfg_with_var)

    monkeypatch.setenv("MCP_ALLOW_WRITE", "true")
    assert _config_fingerprint(cfg_with_var) != fp_false
    assert _connection_fingerprint(cfg_with_var) != conn_fp_false

    err_info = MCPServerInfo(
        name="subagent__k8s__argocd",
        transport="stdio",
        status="error",
        error="Missing ARGOCD_AUTH_TOKEN",
        tools=(),
    )
    ok_info = MCPServerInfo(
        name="subagent__k8s__helm",
        transport="stdio",
        status="ok",
        tools=(MCPToolInfo(name="subagent__k8s__helm:helm_list", original_name="helm_list"),),
    )
    set_cached_mcp_server_info(err_info, cfg_with_var)
    set_cached_mcp_server_info(ok_info, {"transport": "stdio", "command": "uvx", "args": ["helm-mcp"]})
    preload_mod._write_persistent_cache()

    evicted = evict_failed_mcp_server_infos()
    assert evicted == ["subagent__k8s__argocd"]
    assert not is_mcp_server_info_cached("subagent__k8s__argocd", cfg_with_var)
    assert is_mcp_server_info_cached(
        "subagent__k8s__helm",
        {"transport": "stdio", "command": "uvx", "args": ["helm-mcp"]},
    )

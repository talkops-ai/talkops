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

"""Comprehensive unit tests for TalkOps MCP subsystem.

Covers:
- Environment interpolation and alias synchronization (AWS/K8s)
- Argument normalization and schema cleanup
- Cloud & AWS error diagnostic hints
- Local and DB-backed configuration discovery
- SQLite persistence of MCP server configurations
- Raw MCP configuration import and export (.mcp.json / Claude Desktop format)
- MCP server probing, preloading, and caching
- Semantic Profiler 4-tier risk classification for AWS/Cloud tools
- Headless MCP Guard middleware
- MCP Context middleware prompt formatting
- Session manager and cached tool invocation
- CLI mcp command execution
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException, tool

from talkops.cli.commands.mcp import run_mcp_command
from talkops.config.adapters.sqlite import SqliteConfigAdapter
from talkops.config.store import ConfigStore
from talkops.mcp.config import (
    _interpolate_env,
    resolve_mcp_server_env,
    sync_mcp_env_aliases,
)
from talkops.mcp.discovery import MCPDiscovery, discover_mcp_configs
from talkops.mcp.mcp_info import MCPServerInfo, MCPToolInfo
from talkops.mcp.preload import (
    _mcp_tool_name,
    clear_cached_mcp_server_infos,
    get_cached_mcp_server_infos,
    is_mcp_server_info_cached,
    preload_mcp_metadata,
    preload_mcp_server_info,
    probe_one_mcp_server,
)
from talkops.mcp.raw_config import export_raw_mcp_config, import_raw_mcp_config
from talkops.mcp.semantic_profiler import MCPSemanticProfiler, ToolSafetyProfile
from talkops.mcp.session_manager import (
    MCPSessionManager,
    _build_cached_mcp_tool,
    _clean_mcp_schema,
    _enhance_mcp_error_diagnostics,
    _is_transient_session_error,
    _normalize_mcp_arguments,
    create_mcp_connection,
)
from talkops.middleware.headless_mcp_guard import (
    HeadlessMCPGuardMiddleware,
    gated_mcp_tool_names,
    mcp_tool_is_coherently_read_only,
)
from talkops.middleware.mcp_context import (
    MCPContextMiddleware,
    _build_mcp_context_from_infos,
)

# ── Config and Environment Interpolation ──────────────────────────────────────


def test_interpolate_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify interpolate env default."""
    monkeypatch.delenv("TEST_AWS_REGION", raising=False)
    # Default fallback
    assert _interpolate_env("${TEST_AWS_REGION:-us-west-2}", field="test.region") == "us-west-2"

    # Set value
    monkeypatch.setenv("TEST_AWS_REGION", "eu-central-1")
    assert _interpolate_env("${TEST_AWS_REGION:-us-west-2}", field="test.region") == "eu-central-1"

    # Unset without default raises RuntimeError
    monkeypatch.delenv("MISSING_VAR", raising=False)
    with pytest.raises(RuntimeError):
        _interpolate_env("${MISSING_VAR}", field="test.missing")


def test_resolve_mcp_server_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify resolve mcp server env."""
    monkeypatch.setenv("AWS_PROFILE_NAME", "production")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    raw_config = {
        "command": "uvx",
        "args": ["mcp-server-aws", "--profile", "${AWS_PROFILE_NAME:-default}"],
        "env": {"AWS_REGION": "${AWS_REGION:-us-east-1}"},
    }
    resolved = resolve_mcp_server_env("aws-mcp", raw_config)
    assert resolved["args"] == ["mcp-server-aws", "--profile", "production"]
    assert resolved["env"]["AWS_REGION"] == "us-east-1"


def test_sync_mcp_env_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    # AWS_REGION <-> AWS_DEFAULT_REGION
    """Verify sync mcp env aliases."""
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    sync_mcp_env_aliases()
    assert os.environ.get("AWS_DEFAULT_REGION") == "ap-south-1"

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    monkeypatch.delenv("AWS_REGION", raising=False)
    sync_mcp_env_aliases()
    assert os.environ.get("AWS_REGION") == "us-east-2"

    # AWS_PROFILE <-> AWS_DEFAULT_PROFILE
    monkeypatch.setenv("AWS_PROFILE", "stage")
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    sync_mcp_env_aliases()
    assert os.environ.get("AWS_DEFAULT_PROFILE") == "stage"


# ── Argument Normalization & Schema Cleanup ───────────────────────────────────


def test_normalize_mcp_arguments() -> None:
    """Verify normalize mcp arguments."""
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "tag": {"type": "string"},
        },
        "required": ["query"],
    }
    raw_args = {
        "query": "active instances",
        "tag": "",  # non-required string with empty string should be stripped
        "limit": 10,
    }
    normalized = _normalize_mcp_arguments(raw_args, schema)
    assert "tag" not in normalized
    assert normalized["query"] == "active instances"
    assert normalized["limit"] == 10


def test_clean_mcp_schema() -> None:
    """Verify clean mcp schema."""
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "instance_id": {
                "type": "string",
                "additionalProperties": False,
                "$schema": "...",
            }
        },
    }
    cleaned = _clean_mcp_schema(schema)
    assert "$schema" not in cleaned
    assert "additionalProperties" not in cleaned
    assert "properties" in cleaned
    assert "$schema" not in cleaned["properties"]["instance_id"]
    assert "additionalProperties" not in cleaned["properties"]["instance_id"]

    # Verify dereferencing of nested $defs and $ref (e.g. PricingFilter in AWS pricing tools)
    schema_with_defs = {
        "type": "object",
        "properties": {
            "service_code": {"type": "string"},
            "filters": {
                "type": "array",
                "items": {"$ref": "#/$defs/PricingFilter"},
            },
        },
        "required": ["service_code"],
        "$defs": {
            "PricingFilter": {
                "type": "object",
                "properties": {
                    "Field": {"type": "string"},
                    "Value": {"type": "string"},
                },
                "required": ["Field", "Value"],
            }
        },
    }
    cleaned_defs = _clean_mcp_schema(schema_with_defs)
    assert "$defs" not in cleaned_defs
    assert "$ref" not in str(cleaned_defs)
    # The referenced definition was inlined directly into properties.filters.items
    items_schema = cleaned_defs["properties"]["filters"]["items"]
    assert items_schema["type"] == "object"
    assert "Field" in items_schema["properties"]
    assert "Value" in items_schema["properties"]

    # Verify circular references and bounded recursion break safely without empty keys or infinite loop
    circular_schema = {
        "type": "object",
        "properties": {"node": {"$ref": "#/$defs/Node"}},
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {"child": {"$ref": "#/$defs/Node"}},
            }
        },
    }
    cleaned_circ = _clean_mcp_schema(circular_schema)
    assert "$defs" not in cleaned_circ
    assert "$ref" not in str(cleaned_circ)
    assert "" not in cleaned_circ
    assert cleaned_circ["properties"]["node"]["properties"]["child"] == {"type": "object"}

    # Verify nullable anyOf and default: null compaction
    nullable_schema = {
        "type": "object",
        "properties": {
            "start_date": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
                "description": "Start date",
            },
            "filter": {
                "anyOf": [{"type": "string"}, {"type": "object"}, {"type": "null"}],
                "default": None,
                "description": "Filter expression",
            },
        },
    }
    cleaned_nullable = _clean_mcp_schema(nullable_schema)
    assert cleaned_nullable["properties"]["start_date"] == {
        "type": "string",
        "description": "Start date",
    }
    assert cleaned_nullable["properties"]["filter"] == {
        "anyOf": [{"type": "string"}, {"type": "object"}],
        "description": "Filter expression",
    }


def test_is_transient_session_error() -> None:
    """Verify is transient session error."""
    assert _is_transient_session_error(BrokenPipeError()) is True
    assert _is_transient_session_error(ConnectionResetError()) is True
    assert _is_transient_session_error(EOFError()) is True
    assert _is_transient_session_error(ValueError("Bad input")) is False


# ── Error Diagnostics ────────────────────────────────────────────────────────


def test_enhance_mcp_error_diagnostics_aws() -> None:
    """Verify enhance mcp error diagnostics aws."""
    sso_err = _enhance_mcp_error_diagnostics("botocore.exceptions.ClientError: The security token included in the request is expired (ExpiredToken)")
    assert "aws sso login" in sso_err

    cred_err = _enhance_mcp_error_diagnostics("botocore.exceptions.NoCredentialsError: Unable to locate credentials")
    assert "No AWS credentials found" in cred_err

    denied_err = _enhance_mcp_error_diagnostics("AccessDenied: User is not authorized to perform: ec2:DescribeInstances")
    assert "AWS IAM Access Denied" in denied_err

    endpoint_err = _enhance_mcp_error_diagnostics("EndpointConnectionError: Could not connect to the endpoint URL")
    assert "AWS endpoint URL" in endpoint_err

    crash_err = _enhance_mcp_error_diagnostics("Process failed with Connection closed unexpectedly")
    assert "MCP server process terminated" in crash_err


# ── Pure File-Based Discovery & Precedence ────────────────────────────────────


def test_mcp_discovery_from_files(tmp_path: Path) -> None:
    """Verify pure file-based discovery with no database connection."""
    mcp_file = tmp_path / ".mcp.json"
    mcp_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "aws-cloudwatch": {
                        "command": "uvx",
                        "args": ["mcp-server-aws-cloudwatch"],
                        "env": {"AWS_REGION": "us-west-2"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    discovery = MCPDiscovery()
    configs = discovery.discover(project_root=tmp_path)
    assert "aws-cloudwatch" in configs
    assert configs["aws-cloudwatch"].get("command") == "uvx"
    assert configs["aws-cloudwatch"].get("args") == ["mcp-server-aws-cloudwatch"]
    assert configs["aws-cloudwatch"].get("source") == "project"
    assert configs["aws-cloudwatch"].get("enabled") is True


def test_mcp_discovery_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify precedence hierarchy: project > user > global."""
    global_dir = tmp_path / "global_agents"
    user_dir = tmp_path / "user_talkops"
    project_dir = tmp_path / "project_workspace"

    global_dir.mkdir()
    user_dir.mkdir()
    project_dir.mkdir()

    from talkops.config import paths

    monkeypatch.setattr(paths, "AGENTS_SHARED_DIR", global_dir)
    monkeypatch.setattr(paths, "DATA_DIR", user_dir)
    monkeypatch.setattr(paths, "GLOBAL_MCP_PATH", user_dir / ".mcp.json")

    # 1. Global config (~/.agents/mcp.json)
    (global_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "shared-server": {"command": "global-cmd", "env": {"REGION": "global-reg"}},
                    "global-only": {"command": "global-only-cmd"},
                }
            }
        ),
        encoding="utf-8",
    )

    # 2. User config (~/.talkops/.mcp.json)
    (user_dir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "shared-server": {"command": "user-cmd", "env": {"REGION": "user-reg"}},
                    "user-only": {"command": "user-only-cmd"},
                }
            }
        ),
        encoding="utf-8",
    )

    # 3. Project config ({project_root}/.mcp.json)
    (project_dir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "shared-server": {"command": "project-cmd", "env": {"REGION": "project-reg"}},
                    "project-only": {"command": "project-only-cmd"},
                }
            }
        ),
        encoding="utf-8",
    )

    discovery = MCPDiscovery()
    configs = discovery.discover(project_root=project_dir)

    # Project wins for shared-server
    assert configs["shared-server"].get("command") == "project-cmd"
    shared_env = configs["shared-server"].get("env")
    assert shared_env is not None
    assert shared_env["REGION"] == "project-reg"
    assert configs["shared-server"].get("source") == "project"

    # All tiers are discovered
    assert configs["global-only"].get("command") == "global-only-cmd"
    assert configs["global-only"].get("source") == "global"

    assert configs["user-only"].get("command") == "user-only-cmd"
    assert configs["user-only"].get("source") == "user"

    assert configs["project-only"].get("command") == "project-only-cmd"
    assert configs["project-only"].get("source") == "project"


def test_mcp_trust_store(tmp_path: Path) -> None:
    """Verify SHA-256 fingerprint gating in MCPTrustStore."""
    from talkops.mcp.trust import MCPTrustStore

    trust_file = tmp_path / "mcp_trust.json"
    trust_store = MCPTrustStore(trust_file=trust_file)

    config_file = tmp_path / ".mcp.json"
    config_file.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    # Not trusted initially
    assert not trust_store.is_trusted(config_file)

    # Trust it
    trust_store.trust(config_file)
    assert trust_store.is_trusted(config_file)

    # Modify content -> fingerprint invalidation
    config_file.write_text(json.dumps({"mcpServers": {"malicious": {}}}), encoding="utf-8")
    assert not trust_store.is_trusted(config_file)

    # Re-trust
    trust_store.trust(config_file)
    assert trust_store.is_trusted(config_file)

    # Revoke
    trust_store.revoke(config_file)
    assert not trust_store.is_trusted(config_file)


# ── Raw Config Import & Export ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_raw_config_export_and_import(tmp_path: Path) -> None:
    """Verify mcp raw config export and import."""
    adapter = SqliteConfigAdapter(tmp_path / "test_raw.db")
    store = ConfigStore(adapter)
    await store.initialize()

    raw_json = {
        "mcpServers": {
            "terraform-mcp": {
                "command": "uvx",
                "args": ["terraform-mcp-server"],
                "env": {"TF_LOG": "INFO"},
                "disabledTools": ["terraform_destroy"],
                "disabled": False,
            },
            "remote-aws-proxy": {
                "serverUrl": "http://127.0.0.1:8000/mcp",
                "headers": {"Authorization": "Bearer token123"},
            },
        }
    }

    imported = await import_raw_mcp_config(raw_json, store=store, source="user")
    assert "terraform-mcp" in imported
    assert "remote-aws-proxy" in imported

    db_servers = await store.list_mcp_servers()
    assert len(db_servers) == 2

    exported = export_raw_mcp_config(db_servers)
    assert "mcpServers" in exported
    assert "terraform-mcp" in exported["mcpServers"]
    assert exported["mcpServers"]["terraform-mcp"]["command"] == "uvx"
    assert "remote-aws-proxy" in exported["mcpServers"]
    assert exported["mcpServers"]["remote-aws-proxy"]["serverUrl"] == "http://127.0.0.1:8000/mcp"


# ── Probing and Preload Metadata ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_one_mcp_server_disabled_and_active() -> None:
    # 1. Disabled server
    """Verify probe one mcp server disabled and active."""
    disabled_conf = {"transport": "stdio", "command": "echo", "enabled": False}
    info_dis = await probe_one_mcp_server("disabled_srv", disabled_conf)
    assert info_dis.status == "disabled"
    assert info_dis.enabled is False
    assert info_dis.tool_count == 0

    # 2. Active server with mocked session
    mock_t1 = MagicMock()
    mock_t1.name = "describe_instances"
    mock_t1.description = "Describe EC2 instances"
    mock_t1.inputSchema = {"type": "object"}

    mock_session = AsyncMock()
    mock_session.initialize = AsyncMock()
    mock_session.list_tools = AsyncMock(return_value=MagicMock(tools=[mock_t1]))

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("langchain_mcp_adapters.sessions.create_session", return_value=mock_ctx):
        cfg = {"transport": "http", "url": "http://localhost:8080/mcp", "enabled": True}
        info_act = await probe_one_mcp_server("aws-ec2", cfg)
        assert info_act.status == "ok"
        assert info_act.tool_count == 1
        assert info_act.tools[0].name == "aws-ec2_describe_instances"
        assert info_act.tools[0].original_name == "describe_instances"


def test_mcp_tool_name_provider_safe_length_and_digest() -> None:
    """Verify tool name composition respects provider length limits and hashing."""
    # 1. Short name within limit
    short_name = _mcp_tool_name("aws-ec2", "describe_instances")
    assert short_name == "aws-ec2_describe_instances"
    assert len(short_name) <= 64

    # 2. Sanitization of invalid chars (appends digest to avoid collision)
    dirty_name = _mcp_tool_name("my@server!", "list:buckets?")
    assert dirty_name == "my_server_list_buckets_d02139680706"
    assert len(dirty_name) <= 64

    # 3. Long name with plugin prefix exceeding 64 characters (truncated with SHA256 digest)
    server_name = "plugin__aws-compute_talkops-devops-plugins_35da5a75__aws-mcp"
    tool_name = "aws___get_tasks"
    hashed_name = _mcp_tool_name(server_name, tool_name)
    assert len(hashed_name) <= 64
    assert hashed_name == "plugin__aws-compute_talkops-devops-_aws___get_tasks_9badd4f791c6"


@pytest.mark.asyncio
async def test_preload_mcp_server_info_integration(tmp_path: Path) -> None:
    """Verify preload mcp server info integration."""
    clear_cached_mcp_server_infos()

    # Create dummy mcp file
    mcp_file = tmp_path / "custom.mcp.json"
    mcp_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "dummy-server": {
                        "transport": "http",
                        "url": "http://127.0.0.1:9090",
                        "enabled": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    results = await preload_mcp_server_info(mcp_config_path=str(mcp_file))
    assert len(results) >= 1
    names = [r.name for r in results]
    assert "dummy-server" in names

    cached = get_cached_mcp_server_infos()
    assert any(c.name == "dummy-server" for c in cached)


@pytest.mark.asyncio
async def test_persistent_schema_cache_avoids_restarting_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warm process rebuilds tools from disk without probing the MCP server."""
    import talkops.mcp.preload as preload_module

    cache_path = tmp_path / "mcp-metadata.json"
    monkeypatch.setenv("TALKOPS_MCP_METADATA_CACHE", str(cache_path))
    config = {
        "transport": "stdio",
        "command": "example-mcp",
        "env": {"SECRET_TOKEN": "must-not-be-persisted"},
        "headers": {"Authorization": "must-not-be-persisted"},
    }
    info = MCPServerInfo(
        name="persistent",
        transport="stdio",
        status="ok",
        tools=(
            MCPToolInfo(
                name="persistent_list",
                original_name="list",
                input_schema={"type": "object"},
            ),
        ),
    )

    preload_module._PROBED_CACHE.clear()
    preload_module._PROBED_CONFIG_FINGERPRINTS.clear()
    preload_module._PERSISTED_ENTRIES.clear()
    preload_module._PERSISTENT_CACHE_LOADED = False
    with patch.object(preload_module, "probe_one_mcp_server", new=AsyncMock(return_value=info)) as probe:
        first = await preload_module.preload_mcp_metadata({"persistent": config})
    probe.assert_awaited_once()
    assert first[0].tool_count == 1
    persisted_text = cache_path.read_text(encoding="utf-8")
    assert "must-not-be-persisted" not in persisted_text

    # Simulate a fresh process and fail the test if a transport probe occurs.
    preload_module._PROBED_CACHE.clear()
    preload_module._PROBED_CONFIG_FINGERPRINTS.clear()
    preload_module._PERSISTED_ENTRIES.clear()
    preload_module._PERSISTENT_CACHE_LOADED = False
    with patch.object(
        preload_module,
        "probe_one_mcp_server",
        new=AsyncMock(side_effect=AssertionError("warm cache unexpectedly probed")),
    ) as probe:
        second = await preload_module.preload_mcp_metadata({"persistent": config})
    probe.assert_not_awaited()
    assert second[0].tools[0].original_name == "list"


@pytest.mark.asyncio
async def test_mcp_metadata_cache_is_bound_to_exact_config() -> None:
    """A reused server name must not return metadata for a changed command."""
    clear_cached_mcp_server_infos()
    first = {"shared": {"transport": "stdio", "command": "first", "enabled": False}}
    changed = {"shared": {"transport": "stdio", "command": "second", "enabled": False}}

    await preload_mcp_metadata(first)

    assert is_mcp_server_info_cached("shared", first["shared"]) is True
    assert is_mcp_server_info_cached("shared", changed["shared"]) is False

    await preload_mcp_metadata(changed)
    assert is_mcp_server_info_cached("shared", changed["shared"]) is True


# ── Semantic Profiler ─────────────────────────────────────────────────────────


def test_semantic_profiler_cloud_tiers() -> None:
    """Verify semantic profiler cloud tiers."""
    profiler = MCPSemanticProfiler.get_instance()

    # Tier 1 (Read-Only)
    t_read = MagicMock()
    t_read.name = "aws_describe_instances"
    t_read.annotations = None
    p_read = profiler.heuristic_profile(t_read, server_name="aws")
    assert p_read.inferred_tier == 1
    assert p_read.read_only_hint is True
    assert p_read.destructive_hint is False

    # Tier 4 (Destructive - terminate)
    t_dest = MagicMock()
    t_dest.name = "aws_terminate_instances"
    t_dest.annotations = None
    p_dest = profiler.heuristic_profile(t_dest, server_name="aws")
    assert p_dest.inferred_tier == 4
    assert p_dest.read_only_hint is False
    assert p_dest.destructive_hint is True
    assert "instance_id" in p_dest.sensitive_arguments

    # Annotation precedence over name
    t_annot = MagicMock()
    t_annot.name = "safe_delete_preview"
    t_annot.annotations = {"readOnlyHint": True, "destructiveHint": False}
    p_annot = profiler.heuristic_profile(t_annot, server_name="aws")
    # Annotations take precedence if readOnlyHint is true and destructiveHint is not set
    assert p_annot.read_only_hint is True


# ── Middleware & Tools ────────────────────────────────────────────────────────


def test_headless_mcp_guard_middleware() -> None:
    """Verify headless mcp guard middleware."""
    guard = HeadlessMCPGuardMiddleware(tool_names=["aws_terminate_instances"])

    req_blocked = MagicMock()
    req_blocked.tool_call = {"name": "aws_terminate_instances", "id": "call-1"}

    handler = MagicMock()
    result = guard.wrap_tool_call(req_blocked, handler)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "requires approval" in result.content
    handler.assert_not_called()

    req_allowed = MagicMock()
    req_allowed.tool_call = {"name": "aws_describe_instances", "id": "call-2"}
    guard.wrap_tool_call(req_allowed, handler)
    handler.assert_called_once_with(req_allowed)


def test_mcp_context_middleware() -> None:
    """Verify mcp context middleware."""
    server_info = [
        MCPServerInfo(
            name="aws-ec2",
            transport="stdio",
            status="ok",
            tools=(
                MCPToolInfo(name="aws-ec2:describe_instances", description="Describe instances"),
            ),
        )
    ]
    prompt_block = _build_mcp_context_from_infos(server_info)
    assert "**MCP Servers**" in prompt_block
    assert "aws-ec2" in prompt_block
    assert "aws-ec2:describe_instances" in prompt_block


@pytest.mark.asyncio
async def test_cached_mcp_tool_execution() -> None:
    """Verify cached mcp tool execution."""
    from mcp.types import CallToolResult, TextContent

    mock_mcp_tool = MagicMock()
    mock_mcp_tool.name = "list_buckets"
    mock_mcp_tool.description = "List S3 buckets"
    mock_mcp_tool.inputSchema = {"type": "object", "properties": {}}

    mock_session = AsyncMock()
    mock_session.call_tool = AsyncMock(
        return_value=CallToolResult(
            content=[TextContent(type="text", text="bucket-1\nbucket-2")],
            isError=False,
        )
    )

    manager = MCPSessionManager({"s3-server": {"transport": "http", "url": "http://localhost:9000"}})
    manager._sessions["s3-server"] = MagicMock(session=mock_session, exit_stack=AsyncMock())

    tool_obj = _build_cached_mcp_tool(
        mcp_tool=mock_mcp_tool,
        server_name="s3-server",
        session_manager=manager,
    )

    assert tool_obj.name == "s3-server:list_buckets"
    result = await tool_obj.ainvoke({})
    assert "bucket-1" in str(result)


@pytest.mark.asyncio
async def test_transient_error_retry_safety() -> None:
    """Verify that read-only tools retry on transient disconnect, but mutating tools fail-safe without replay."""
    from mcp.types import CallToolResult, TextContent

    # 1. Read-only tool (describe_instances) retries safely
    ro_tool = MagicMock()
    ro_tool.name = "describe_instances"
    ro_tool.description = "Describe instances"
    ro_tool.inputSchema = {"type": "object", "properties": {}}

    mock_ro_session_1 = AsyncMock()
    mock_ro_session_1.call_tool = AsyncMock(side_effect=BrokenPipeError("pipe dead"))
    mock_ro_session_2 = AsyncMock()
    mock_ro_session_2.call_tool = AsyncMock(
        return_value=CallToolResult(content=[TextContent(type="text", text="i-123")], isError=False)
    )

    manager = MCPSessionManager({"ec2-server": {"transport": "stdio", "command": "fake"}})
    manager.get_session = AsyncMock(side_effect=[mock_ro_session_1, mock_ro_session_2])
    manager.invalidate = AsyncMock()

    tool_ro = _build_cached_mcp_tool(
        mcp_tool=ro_tool,
        server_name="ec2-server",
        session_manager=manager,
    )
    res = await tool_ro.ainvoke({})
    assert "i-123" in str(res)
    assert manager.invalidate.called

    # 2. Mutating tool (terminate_instances) fails safe without replaying
    mut_tool = MagicMock()
    mut_tool.name = "terminate_instances"
    mut_tool.description = "Terminate instances"
    mut_tool.inputSchema = {"type": "object", "properties": {}}

    mock_mut_session = AsyncMock()
    mock_mut_session.call_tool = AsyncMock(side_effect=BrokenPipeError("socket dead mid-flight"))

    manager_mut = MCPSessionManager({"ec2-server": {"transport": "stdio", "command": "fake"}})
    manager_mut.get_session = AsyncMock(return_value=mock_mut_session)
    manager_mut.invalidate = AsyncMock()

    tool_mut = _build_cached_mcp_tool(
        mcp_tool=mut_tool,
        server_name="ec2-server",
        session_manager=manager_mut,
    )
    res_mut = await tool_mut.ainvoke({})

    assert "Connection lost while calling MCP tool" in str(res_mut)
    assert "not automatically retried" in str(res_mut)
    # Ensure it did not attempt a second call_tool invocation
    assert mock_mut_session.call_tool.call_count == 1


# ── CLI MCP Command ───────────────────────────────────────────────────────────


def test_run_mcp_command_cli() -> None:
    """Verify run mcp command cli."""
    args = argparse.Namespace(action="list", json_output=True, probe=False)
    assert run_mcp_command(args) == 0


def test_write_and_read_mcp_server_state_materializes_and_prunes(tmp_path: Path) -> None:
    """Verify write_mcp_server_state atomically writes <tool>.json files and prunes stale ones."""
    from talkops.mcp.preload import read_mcp_tool_schema, write_mcp_server_state

    server_dir = tmp_path / "aws-ec2"
    server_dir.mkdir(parents=True)
    stale_file = server_dir / "old_removed_tool.json"
    stale_file.write_text("{}", encoding="utf-8")

    info = MCPServerInfo(
        name="aws-ec2",
        transport="stdio",
        status="ok",
        tools=(
            MCPToolInfo(
                name="aws-ec2:describe_instances",
                original_name="describe_instances",
                description="Describe EC2 instances",
                input_schema={"type": "object", "properties": {"region": {"type": "string"}}},
            ),
            MCPToolInfo(
                name="aws-ec2:terminate_instances",
                original_name="terminate_instances",
                description="Terminate EC2 instances",
                input_schema={"type": "object", "required": ["instance_ids"]},
            ),
        ),
    )

    out_dir = write_mcp_server_state(info, state_dir=tmp_path)
    assert out_dir == server_dir
    assert not stale_file.exists()
    assert (server_dir / "describe_instances.json").is_file()
    assert (server_dir / "terminate_instances.json").is_file()

    schema = read_mcp_tool_schema("aws-ec2", "describe_instances", state_dir=tmp_path)
    assert schema is not None
    assert schema["name"] == "describe_instances"
    assert schema["qualified_name"] == "aws-ec2:describe_instances"
    assert schema["inputSchema"]["properties"]["region"]["type"] == "string"
    assert "parameters" not in schema


@pytest.mark.asyncio
async def test_lazy_mcp_tools_split_and_call_mcp_tool_dispatcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Eager/Lazy split binds eager tools directly and routes lazy tools via call_mcp_tool."""
    from mcp.types import CallToolResult, TextContent

    from talkops.mcp.session_manager import (
        build_mcp_tools_from_server_infos,
        split_enabled_tools_by_eager_lazy,
    )

    monkeypatch.setenv("TALKOPS_MCP_SCHEMA_DIR", str(tmp_path))
    monkeypatch.setenv("TALKOPS_LAZY_MCP_TOOLS", "1")

    info = MCPServerInfo(
        name="k8s-server",
        transport="stdio",
        status="ok",
        tools=(
            MCPToolInfo(
                name="k8s-server:get_pods",
                original_name="get_pods",
                description="List pods",
                input_schema={"type": "object", "properties": {"namespace": {"type": "string"}}},
            ),
            MCPToolInfo(
                name="k8s-server:describe_node",
                original_name="describe_node",
                description="Describe node",
                input_schema={
                    "type": "object",
                    "properties": {"node": {"type": "string"}, "optional_filter": {"type": "string"}},
                    "required": ["node"],
                },
            ),
        ),
    )

    srv_cfg = {"transport": "stdio", "command": "fake", "eager_tools": ["get_pods"]}
    eager, lazy = split_enabled_tools_by_eager_lazy(info, srv_cfg)
    assert [t.original_name for t in eager] == ["get_pods"]
    assert [t.original_name for t in lazy] == ["describe_node"]

    mock_session = AsyncMock()
    mock_session.call_tool = AsyncMock(
        return_value=CallToolResult(
            content=[TextContent(type="text", text="node-1 Ready")],
            isError=False,
        )
    )
    manager = MCPSessionManager({"k8s-server": srv_cfg})
    manager.get_session = AsyncMock(return_value=mock_session)

    tools = build_mcp_tools_from_server_infos([info], manager, configs={"k8s-server": srv_cfg})
    tool_names = [t.name for t in tools]
    assert "k8s-server:get_pods" in tool_names
    assert "call_mcp_tool" in tool_names
    assert "k8s-server:describe_node" not in tool_names

    dispatcher = next(t for t in tools if t.name == "call_mcp_tool")
    res = await dispatcher.ainvoke(
        {
            "server_name": "k8s-server",
            "tool_name": "describe_node",
            "arguments": {"node": "ip-10-0-1-1", "optional_filter": ""},
        }
    )
    assert "node-1 Ready" in str(res)
    # Empty string for optional_filter must be normalized away using the on-disk schema
    mock_session.call_tool.assert_awaited_once_with("describe_node", {"node": "ip-10-0-1-1"})


@pytest.mark.asyncio
async def test_stdio_connect_server_redirects_errlog_away_from_stderr() -> None:
    """Verify MCPSessionManager.connect_server passes non-stderr errlog to stdio_client."""
    captured_errlog: list[ object] = []

    mock_session = AsyncMock()
    mock_session.initialize = AsyncMock()
    mock_session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))

    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=None)

    def _fake_stdio_client(server_params: object, errlog: object = sys.stderr) -> AsyncMock:
        captured_errlog.append(errlog)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    cfg = {"transport": "stdio", "command": "uvx", "args": ["awslabs.billing-mcp"], "enabled": True}
    manager = MCPSessionManager({"billing": cfg})

    with (
        patch("mcp.client.stdio.stdio_client", side_effect=_fake_stdio_client),
        patch("mcp.ClientSession", return_value=mock_session_ctx),
    ):
        ok = await manager.connect_server("billing", cfg)
        assert ok is True
        assert len(captured_errlog) == 1
        assert captured_errlog[0] is not sys.stderr
        await manager.cleanup()


def test_truncated_subagent_tool_name_display_and_summary() -> None:
    """Verify truncated 64-char subagent MCP tool names parse cleanly in TUI headers and summaries."""
    from talkops.ui.widgets.messages import summarize_tool_group
    from talkops.ui.widgets.tool_display import (
        get_tool_display_name,
        parse_scoped_tool_name,
    )

    srv_name = "subagent__aws-finops-agent_talkops-devops-plugins_a5270392__billing"
    cost_explorer_name = _mcp_tool_name(srv_name, "cost-explorer")
    cost_opt_name = _mcp_tool_name(srv_name, "cost-optimization")

    assert parse_scoped_tool_name(cost_explorer_name) == ("cost-explorer", "aws-finops-agent")
    assert parse_scoped_tool_name(cost_opt_name) == ("cost-optimization", "aws-finops-agent")
    assert get_tool_display_name(cost_explorer_name) == "CostExplorer [aws-finops-agent]"

    summary = summarize_tool_group([cost_explorer_name, cost_explorer_name, cost_opt_name])
    assert "subagent__" not in summary
    assert "Ran 2 cost-explorer calls [aws-finops-agent]" in summary
    assert "ran 1 cost-optimization call [aws-finops-agent]" in summary


@pytest.mark.asyncio
async def test_connection_fingerprint_deduplicates_common_mcp_across_scopes() -> None:
    """Common MCP servers shared across plugins/subagents should be probed only once."""
    import talkops.mcp.preload as preload_module

    clear_cached_mcp_server_infos()
    common_cmd = "uvx"
    common_args = ["some-shared-mcp-server@latest"]
    configs = {
        "plugin__plugin-a__shared-mcp": {
            "transport": "stdio",
            "command": common_cmd,
            "args": common_args,
            "env": {"TALKOPS_PLUGIN_ROOT": "/tmp/plugin-a", "FASTMCP_LOG_LEVEL": "ERROR"},
        },
        "subagent__agent-1__shared-mcp": {
            "transport": "stdio",
            "command": common_cmd,
            "args": common_args,
            "env": {"TALKOPS_PLUGIN_ROOT": "/tmp/plugin-a/agents", "FASTMCP_LOG_LEVEL": "ERROR"},
        },
        "subagent__agent-2__shared-mcp": {
            "transport": "stdio",
            "command": common_cmd,
            "args": common_args,
            "env": {"TALKOPS_PLUGIN_ROOT": "/tmp/plugin-b/agents", "FASTMCP_LOG_LEVEL": "ERROR"},
        },
    }

    async def _fake_probe(name: str, cfg: dict) -> MCPServerInfo:
        return MCPServerInfo(
            name=name,
            transport="stdio",
            status="ok",
            tools=(
                MCPToolInfo(
                    name=_mcp_tool_name(name, "query_docs"),
                    original_name="query_docs",
                    description="Query docs",
                    input_schema={"type": "object"},
                ),
            ),
        )

    with patch.object(preload_module, "probe_one_mcp_server", side_effect=_fake_probe) as mock_probe:
        infos = await preload_mcp_metadata(configs)

    assert mock_probe.await_count == 1
    assert len(infos) == 3
    by_name = {info.name: info for info in infos}
    assert by_name["plugin__plugin-a__shared-mcp"].tools[0].name == _mcp_tool_name(
        "plugin__plugin-a__shared-mcp", "query_docs"
    )
    assert by_name["subagent__agent-1__shared-mcp"].tools[0].name == _mcp_tool_name(
        "subagent__agent-1__shared-mcp", "query_docs"
    )
    assert by_name["subagent__agent-2__shared-mcp"].tools[0].name == _mcp_tool_name(
        "subagent__agent-2__shared-mcp", "query_docs"
    )


@pytest.mark.asyncio
async def test_negative_cache_persists_failed_mcp_probes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed MCP probes are negative-cached so subsequent runs do not re-block on broken servers."""
    import talkops.mcp.preload as preload_module

    cache_path = tmp_path / "mcp-negative-cache.json"
    monkeypatch.setenv("TALKOPS_MCP_METADATA_CACHE", str(cache_path))
    monkeypatch.setenv("TALKOPS_MCP_ERROR_CACHE_TTL", "600")

    broken_cfg = {
        "transport": "stdio",
        "command": "uvx",
        "args": ["nonexistent-broken-mcp-pkg==99.99.99"],
    }
    failed_info = MCPServerInfo(
        name="broken-mcp",
        transport="stdio",
        status="error",
        error="Because nonexistent-broken-mcp-pkg was not found in the package registry, requirements are unsatisfiable.",
        tools=(),
    )

    preload_module._PROBED_CACHE.clear()
    preload_module._PROBED_CONFIG_FINGERPRINTS.clear()
    preload_module._PROBED_CONN_FINGERPRINTS.clear()
    preload_module._PERSISTED_ENTRIES.clear()
    preload_module._PERSISTENT_CACHE_LOADED = False

    with patch.object(preload_module, "probe_one_mcp_server", new=AsyncMock(return_value=failed_info)) as probe:
        first = await preload_module.preload_mcp_metadata({"broken-mcp": broken_cfg})
    probe.assert_awaited_once()
    assert first[0].status == "error"

    # Simulate fresh process startup: should hydrate the error status from disk without re-probing
    preload_module._PROBED_CACHE.clear()
    preload_module._PROBED_CONFIG_FINGERPRINTS.clear()
    preload_module._PROBED_CONN_FINGERPRINTS.clear()
    preload_module._PERSISTED_ENTRIES.clear()
    preload_module._PERSISTENT_CACHE_LOADED = False

    with patch.object(
        preload_module,
        "probe_one_mcp_server",
        new=AsyncMock(side_effect=AssertionError("broken server should be served from negative cache")),
    ) as probe2:
        second = await preload_module.preload_mcp_metadata(
            {"subagent__any-agent__broken-mcp": broken_cfg}
        )
    probe2.assert_not_awaited()
    assert second[0].status == "error"
    assert "unsatisfiable" in (second[0].error or "")


def test_clean_stderr_diagnostic_coalesces_uvx_resolution_block() -> None:
    """Multiline uvx '╰─▶' blocks are coalesced into a complete single-line root cause."""
    from talkops.mcp.preload import _clean_stderr_diagnostic

    stderr = (
        "  × No solution found when resolving tool dependencies:\n"
        "  ╰─▶ Because example-mcp-server was not found in the package registry\n"
        "      and you require example-mcp-server==0.2.15, we can conclude that\n"
        "      your requirements are unsatisfiable.\n"
    )
    diag = _clean_stderr_diagnostic(stderr)
    assert diag is not None
    assert "example-mcp-server was not found" in diag
    assert "unsatisfiable" in diag


@pytest.mark.asyncio
async def test_dynamic_mcp_hydration_in_context_and_tool_middleware() -> None:
    """MCPContextMiddleware and MCPToolMiddleware dynamically hydrate servers probed in background."""
    from talkops.mcp.preload import set_cached_mcp_server_info
    from talkops.middleware.mcp_middleware import MCPToolMiddleware

    clear_cached_mcp_server_infos()
    srv_cfg = {"transport": "stdio", "command": "uvx", "args": ["dyn-mcp"]}
    mcp_config = {"dyn-server": srv_cfg}

    ctx_mw = MCPContextMiddleware(mcp_server_info=[], mcp_config=mcp_config)
    tool_mw = MCPToolMiddleware(mcp_config=mcp_config)

    # Before background probe completes: falls back to config stub, no dynamic tools yet
    assert "configured" in ctx_mw._build_mcp_context()
    assert tool_mw._get_dynamic_mcp_tools() == {}

    # Background probe populates shared cache
    probed_info = MCPServerInfo(
        name="dyn-server",
        transport="stdio",
        status="ok",
        tools=(
            MCPToolInfo(
                name="dyn-server_list_items",
                original_name="list_items",
                description="List items dynamically",
                input_schema={"type": "object", "properties": {"filter": {"type": "string"}}},
            ),
        ),
    )
    set_cached_mcp_server_info(probed_info, srv_cfg)

    # Next turn automatically sees full tool inventory and binds dynamic tool
    updated_ctx = ctx_mw._build_mcp_context()
    assert "dyn-server_list_items" in updated_ctx
    dyn_tools = tool_mw._get_dynamic_mcp_tools()
    assert "dyn-server_list_items" in dyn_tools


def test_mcp_context_and_auto_lazy_for_large_subagent_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify scoped subagents with >30 MCP tools auto-enable lazy mode and compact MCPContextMiddleware."""
    from talkops.mcp.session_manager import build_mcp_tools_from_server_infos

    monkeypatch.setenv("TALKOPS_MCP_SCHEMA_DIR", str(tmp_path))
    monkeypatch.delenv("TALKOPS_MCP_LAZY_TOOLS", raising=False)
    monkeypatch.delenv("TALKOPS_LAZY_MCP_TOOLS", raising=False)

    tools = tuple(
        MCPToolInfo(
            name=f"subagent__aws-platform-engineer_talko_tool_{i}_1234567890ab",
            original_name=f"tool_{i}",
            description=f"Tool {i}",
            input_schema={"type": "object"},
        )
        for i in range(35)
    )
    info = MCPServerInfo(
        name="subagent__aws-platform-engineer_talkops-devops-plugins_771c9ed9__billing-cost",
        transport="stdio",
        status="ok",
        tools=tools,
    )

    ctx_block = _build_mcp_context_from_infos([info])
    assert "tool_0, tool_1" in ctx_block
    assert "subagent__aws-platform-engineer_talko_tool_0" not in ctx_block
    assert "(+25 more in schema dir)" in ctx_block

    manager = MCPSessionManager({info.name: {"transport": "stdio", "command": "fake"}})
    built = build_mcp_tools_from_server_infos([info], manager)
    assert [t.name for t in built] == ["call_mcp_tool"]


def test_mcp_context_eager_lazy_formatting() -> None:
    """Verify _build_mcp_context_from_infos renders Eager vs Lazy partitioning when configured."""
    info = MCPServerInfo(
        name="k8s-server",
        transport="stdio",
        status="ok",
        tools=(
            MCPToolInfo(
                name="k8s-server:get_pods",
                original_name="get_pods",
                description="List pods",
                input_schema={"type": "object"},
            ),
            MCPToolInfo(
                name="k8s-server:delete_pod",
                original_name="delete_pod",
                description="Delete pod",
                input_schema={"type": "object"},
            ),
        ),
    )
    mixed_cfg = {"k8s-server": {"transport": "stdio", "command": "fake", "eager_tools": ["get_pods"]}}
    ctx_mixed = _build_mcp_context_from_infos([info], configs=mixed_cfg)
    assert "Eager: k8s-server:get_pods" in ctx_mixed
    assert "Lazy (via `call_mcp_tool`): k8s-server:delete_pod" in ctx_mixed

    all_lazy_cfg = {"k8s-server": {"transport": "stdio", "command": "fake", "lazy_load": True}}
    ctx_lazy = _build_mcp_context_from_infos([info], configs=all_lazy_cfg)
    assert "[lazy]: k8s-server:get_pods, k8s-server:delete_pod" in ctx_lazy


def test_tool_filter_middleware_call_mcp_tool_validation() -> None:
    """Verify ToolFilterMiddleware validates call_mcp_tool across server_name/ServerName/server aliases."""
    from langgraph.prebuilt.tool_node import ToolCallRequest

    from talkops.middleware.tool_filter import ToolFilterMiddleware

    scoped_srv = "subagent__aws-finops-agent_talkops-devops-plugins_a5270392__billing"
    mw = ToolFilterMiddleware(
        allowed_patterns=["mcp__plugin_aws-finops-agent_billing__*", "read_file"],
        subagent_name="aws-finops-agent@talkops-devops-plugins",
        known_mcp_servers={scoped_srv},
    )

    none_runtime: object = None
    # 1. Allowed via base server name and ServerName/ToolName aliases
    req_ok = ToolCallRequest(
        tool=MagicMock(),
        tool_call={
            "name": "call_mcp_tool",
            "args": {"ServerName": "billing", "ToolName": "get_cost_and_usage", "Arguments": {}},
            "id": "tc_ok_1",
        },
        state={},
        runtime=none_runtime,  # type: ignore[arg-type]
    )
    assert mw._validate_tool_call(req_ok) is None

    # 2. Allowed via server/tool shorthand aliases
    req_ok_short = ToolCallRequest(
        tool=MagicMock(),
        tool_call={
            "name": "call_mcp_tool",
            "args": {"server": scoped_srv, "tool": "get_cost_forecast", "args": {}},
            "id": "tc_ok_2",
        },
        state={},
        runtime=none_runtime,  # type: ignore[arg-type]
    )
    assert mw._validate_tool_call(req_ok_short) is None

    # 3. Rejected for unauthorized server/tool
    req_blocked = ToolCallRequest(
        tool=MagicMock(),
        tool_call={
            "name": "call_mcp_tool",
            "args": {"server_name": "iam-admin", "tool_name": "delete_user", "arguments": {}},
            "id": "tc_block_1",
        },
        state={},
        runtime=none_runtime,  # type: ignore[arg-type]
    )
    err = mw._validate_tool_call(req_blocked)
    assert isinstance(err, ToolMessage)
    assert err.status == "error"
    assert "iam-admin:delete_user" in str(err.content)


def test_main_coordinator_call_mcp_tool_integration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify lazy MCP tools & call_mcp_tool across main coordinator HITL, HeadlessGuard, AutoMode, Jev, and ServerHooks."""
    from pathlib import Path as _Path
    from langgraph.prebuilt.tool_node import ToolCallRequest

    import talkops.mcp.preload as mcp_preload
    from talkops.agent.factory import _format_description, _should_interrupt_tool_call
    from talkops.mcp.session_manager import build_mcp_tools_from_server_infos
    from talkops.middleware.auto_mode_hitl import _deterministic_allow
    from talkops.middleware.headless_mcp_guard import (
        HeadlessMCPGuardMiddleware,
        gated_mcp_tool_names,
    )
    from talkops.middleware.server_hooks import _tool_call_data_from_call
    from talkops.security.jev_classifier import JevSecurityClassifier

    monkeypatch.setenv("TALKOPS_MCP_SCHEMA_DIR", str(tmp_path))

    info = MCPServerInfo(
        name="k8s",
        transport="stdio",
        status="ok",
        tools=(
            MCPToolInfo(
                name="k8s:list_pods",
                original_name="list_pods",
                description="List pods in namespace",
                input_schema={"type": "object"},
            ),
            MCPToolInfo(
                name="k8s:delete_pod",
                original_name="delete_pod",
                description="Delete pod in namespace",
                input_schema={"type": "object"},
            ),
        ),
    )
    monkeypatch.setattr(mcp_preload, "get_cached_mcp_server_infos", lambda: [info])

    cfg = {"k8s": {"transport": "stdio", "command": "fake", "lazy_load": True}}
    manager = MCPSessionManager(cfg)
    built_tools = build_mcp_tools_from_server_infos([info], manager, configs=cfg)
    assert [t.name for t in built_tools] == ["call_mcp_tool"]

    # 1. _format_description formats call_mcp_tool like eager MCP tools
    desc = _format_description(
        {
            "name": "call_mcp_tool",
            "args": {
                "server_name": "k8s",
                "tool_name": "delete_pod",
                "arguments": {"name": "nginx", "namespace": "default"},
            },
        }
    )
    assert desc == "Execute k8s action 'delete_pod' (name=nginx, namespace=default)"

    # 2. _should_interrupt_tool_call in manual mode allows read-only lazy MCP tool and interrupts mutating one
    ro_call = {
        "id": "tc_ro",
        "name": "call_mcp_tool",
        "args": {"server_name": "k8s", "tool_name": "list_pods", "arguments": {}},
    }
    mut_call = {
        "id": "tc_mut",
        "name": "call_mcp_tool",
        "args": {"server_name": "k8s", "tool_name": "delete_pod", "arguments": {"name": "nginx"}},
    }
    assert _should_interrupt_tool_call({"state": {"approval_mode": "manual"}, "tool_call": ro_call}) is False
    assert _should_interrupt_tool_call({"state": {"approval_mode": "manual"}, "tool_call": mut_call}) is True

    # 3. _deterministic_allow in Auto/Smart mode fast-allows read-only call_mcp_tool and reviews mutating one
    dispatcher_tool = built_tools[0]
    assert _deterministic_allow(_Path("."), ro_call, dispatcher_tool) is True  # type: ignore[arg-type]
    assert _deterministic_allow(_Path("."), mut_call, dispatcher_tool) is False  # type: ignore[arg-type]

    # 4. HeadlessMCPGuardMiddleware allows read-only call_mcp_tool even when "call_mcp_tool" is in gated_mcp
    gated = gated_mcp_tool_names(built_tools)
    assert "call_mcp_tool" in gated
    guard = HeadlessMCPGuardMiddleware(gated)
    none_runtime: object = None
    ro_req = ToolCallRequest(
        tool=dispatcher_tool,
        tool_call=ro_call,  # type: ignore[arg-type]
        state={},
        runtime=none_runtime,  # type: ignore[arg-type]
    )
    mut_req = ToolCallRequest(
        tool=dispatcher_tool,
        tool_call=mut_call,  # type: ignore[arg-type]
        state={},
        runtime=none_runtime,  # type: ignore[arg-type]
    )
    assert guard._rejection(ro_req) is None
    blocked_msg = guard._rejection(mut_req)
    assert isinstance(blocked_msg, ToolMessage)
    assert blocked_msg.status == "error"

    # 5. JevSecurityClassifier recognizes call_mcp_tool as mcp_tool and extracts server & action
    jev = JevSecurityClassifier(api_key="")
    assert jev._detect_action_type("call_mcp_tool", mut_call["args"], tool=dispatcher_tool) == "mcp_tool"  # type: ignore[arg-type]
    details = jev._extract_mcp_details("call_mcp_tool", tool=dispatcher_tool, tool_args=mut_call["args"])  # type: ignore[arg-type]
    assert details["server_name"] == "k8s"
    assert details["mcp_action"] == "delete_pod"

    # 6. ServerHooksMiddleware extracts mcp_server from call_mcp_tool args
    hook_call = _tool_call_data_from_call(mut_call, mcp_server=None)
    assert hook_call.mcp_server == "k8s"


@pytest.mark.asyncio
async def test_call_mcp_tool_json_string_arguments_and_schema_hints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify call_mcp_tool accepts JSON string arguments (Gemini/google_genai), Mapping, and emits schema hints."""
    from types import MappingProxyType

    import talkops.mcp.preload as mcp_preload
    from talkops.mcp.session_manager import build_call_mcp_tool
    from talkops.ui.widgets.tool_display import format_tool_parts

    monkeypatch.setenv("TALKOPS_MCP_SCHEMA_DIR", str(tmp_path))

    info = MCPServerInfo(
        name="subagent__reverse-engineer__rea",
        transport="stdio",
        status="ok",
        tools=(
            MCPToolInfo(
                name="subagent__reverse-engineer__rea_open_binary",
                original_name="open_binary",
                description="Open a local executable or archive",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            ),
            MCPToolInfo(
                name="subagent__reverse-engineer__rea_binary_session",
                original_name="binary_session",
                description="Inspect current binary session",
                input_schema={"type": "object", "properties": {}},
            ),
            MCPToolInfo(
                name="subagent__reverse-engineer__rea_search_strings",
                original_name="search_strings",
                description="Search strings in binary",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ),
        ),
    )
    monkeypatch.setattr(mcp_preload, "get_cached_mcp_server_infos", lambda: [info])

    from mcp.types import TextContent

    mock_session = AsyncMock()
    mock_res = MagicMock()
    mock_res.isError = False
    mock_res.content = [TextContent(type="text", text='{"status": "ok"}')]
    mock_session.call_tool = AsyncMock(return_value=mock_res)

    manager = MCPSessionManager({"subagent__reverse-engineer__rea": {"transport": "stdio", "command": "fake"}})
    monkeypatch.setattr(manager, "get_session", AsyncMock(return_value=mock_session))

    dispatcher = build_call_mcp_tool(manager, server_infos=[info])

    # 1. JSON string arguments (as produced by langchain_google_genai for Gemini)
    res1 = await dispatcher.ainvoke(
        {
            "server_name": "subagent__reverse-engineer__rea",
            "tool_name": "open_binary",
            "arguments": '{"path": "/Users/structbinary/.local/bin/agy"}',
        }
    )
    assert "ok" in str(res1)
    mock_session.call_tool.assert_awaited_with("open_binary", {"path": "/Users/structbinary/.local/bin/agy"})

    # 2. Empty JSON string "{}" for binary_session and prefixed tool_name ("rea_binary_session")
    res2 = await dispatcher.ainvoke(
        {
            "server_name": "rea",
            "tool_name": "rea_binary_session",
            "arguments": "{}",
        }
    )
    assert "ok" in str(res2)
    mock_session.call_tool.assert_awaited_with("binary_session", {})

    # 3. Non-dict Mapping (e.g., protobuf MapComposite)
    res3 = await dispatcher.ainvoke(
        {
            "server_name": "rea",
            "tool_name": "open_binary",
            "arguments": MappingProxyType({"path": "/tmp/bin"}),
        }
    )
    assert "ok" in str(res3)
    mock_session.call_tool.assert_awaited_with("open_binary", {"path": "/tmp/bin"})

    # 4. Missing required field returns actionable schema hint
    err_missing = await dispatcher.ainvoke(
        {
            "server_name": "rea",
            "tool_name": "open_binary",
            "arguments": "{}",
        }
    )
    assert "Missing required argument(s) ['path']" in str(err_missing)
    assert '"path": "string"' in str(err_missing)

    # 5. Unknown tool name suggests closest valid tool names on that server
    err_unknown = await dispatcher.ainvoke(
        {
            "server_name": "rea",
            "tool_name": "find_strings",
            "arguments": '{"query": "mcp"}',
        }
    )
    assert "does not exist on server" in str(err_unknown)
    assert "search_strings" in str(err_unknown)

    # 6. TUI format_tool_parts unwraps call_mcp_tool with JSON string arguments
    parts = format_tool_parts(
        "call_mcp_tool",
        {
            "server_name": "subagent__reverse-engineer__rea",
            "tool_name": "open_binary",
            "arguments": '{"path": "/Users/structbinary/.local/bin/agy"}',
        },
    )
    assert parts.name == "OpenBinary"
    assert parts.scope == "reverse-engineer"
    assert "agy" in parts.detail







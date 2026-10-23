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

"""Unit tests for MCPToolMiddleware argument normalization and authentication error handling."""
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from talkops.middleware.mcp_middleware import (
    MCPToolMiddleware,
    normalize_mcp_arguments,
)


def test_normalize_mcp_arguments_drops_optional_empty_strings():
    """Verify normalize mcp arguments drops optional empty strings."""
    schema = {
        "required": ["cluster_name"],
        "properties": {
            "cluster_name": {"type": "string"},
            "namespace": {"type": "string"},
            "region": {"type": "string"},
            "count": {"type": "integer"},
        },
    }
    raw_args = {
        "cluster_name": "prod-eks",
        "namespace": "",       # optional empty string -> should be dropped
        "region": "us-west-2",
        "count": 0,            # 0 should NOT be dropped
    }

    cleaned = normalize_mcp_arguments(raw_args, schema)
    assert cleaned == {
        "cluster_name": "prod-eks",
        "region": "us-west-2",
        "count": 0,
    }
    assert "namespace" not in cleaned


def test_normalize_mcp_arguments_preserves_required_empty_strings():
    """Verify normalize mcp arguments preserves required empty strings."""
    schema = {
        "required": ["mandatory_field"],
        "properties": {
            "mandatory_field": {"type": "string"},
            "optional_field": {"type": "string"},
        },
    }
    raw_args = {
        "mandatory_field": "",
        "optional_field": "",
    }

    cleaned = normalize_mcp_arguments(raw_args, schema)
    assert cleaned == {"mandatory_field": ""}


def test_normalize_mcp_arguments_non_dict_passthrough():
    """Verify normalize mcp arguments non dict passthrough."""
    non_dict_str: Any = "not-a-dict"
    non_dict_list: Any = [1, 2, 3]
    assert normalize_mcp_arguments(non_dict_str) == "not-a-dict"
    assert normalize_mcp_arguments(non_dict_list) == [1, 2, 3]


def test_is_mcp_call_detection():
    """Verify is mcp call detection."""
    mw = MCPToolMiddleware()

    req_colon = MagicMock(spec=ToolCallRequest)
    req_colon.tool_call = {"name": "aws:list_s3_buckets", "args": {}}
    assert mw._is_mcp_call(req_colon) is True

    req_mcp_prefix = MagicMock(spec=ToolCallRequest)
    req_mcp_prefix.tool_call = {"name": "mcp__github_list_prs", "args": {}}
    assert mw._is_mcp_call(req_mcp_prefix) is True

    req_native = MagicMock(spec=ToolCallRequest)
    req_native.tool_call = {"name": "execute_command", "args": {}}
    assert mw._is_mcp_call(req_native) is False


def test_wrap_tool_call_normalizes_mcp_arguments():
    """Verify wrap tool call normalizes mcp arguments."""
    mw = MCPToolMiddleware()

    tool_mock = MagicMock()
    args_schema_mock = MagicMock()
    args_schema_mock.schema.return_value = {
        "required": ["repo"],
        "properties": {
            "repo": {"type": "string"},
            "branch": {"type": "string"},
        },
    }
    tool_mock.args_schema = args_schema_mock

    none_runtime: Any = None
    request = ToolCallRequest(
        tool=tool_mock,
        tool_call={"name": "git:checkout", "args": {"repo": "talkops", "branch": ""}, "id": "call_123"},
        state={},
        runtime=none_runtime,
    )

    def dummy_handler(req: Any) -> Any:
        """Provide dummy handler test helper or fixture."""
        return req.tool_call["args"]

    result = mw.wrap_tool_call(request, dummy_handler)
    assert result == {"repo": "talkops"}


def test_wrap_tool_call_handles_auth_error():
    """Verify wrap tool call handles auth error."""
    mw = MCPToolMiddleware()

    tool_mock = MagicMock()
    none_runtime: Any = None
    request = ToolCallRequest(
        tool=tool_mock,
        tool_call={"name": "aws:describe_instances", "args": {}, "id": "call_auth_fail"},
        state={},
        runtime=none_runtime,
    )

    def failing_handler(req: Any) -> Any:
        """Provide failing handler test helper or fixture."""
        raise RuntimeError("AWS STS Error: The security token included in the request is expired")

    res = mw.wrap_tool_call(request, failing_handler)
    assert isinstance(res, ToolMessage)
    assert res.status == "error"
    assert res.tool_call_id == "call_auth_fail"
    assert "authentication" in res.content
    assert "expired" in res.content


def test_wrap_tool_call_reraises_non_auth_error():
    """Verify wrap tool call reraises non auth error."""
    mw = MCPToolMiddleware()

    tool_mock = MagicMock()
    none_runtime: Any = None
    request = ToolCallRequest(
        tool=tool_mock,
        tool_call={"name": "aws:describe_instances", "args": {}, "id": "call_fail"},
        state={},
        runtime=none_runtime,
    )

    def failing_handler(req: Any) -> Any:
        """Provide failing handler test helper or fixture."""
        raise KeyError("Invalid key lookup")

    with pytest.raises(KeyError):
        mw.wrap_tool_call(request, failing_handler)


@pytest.mark.asyncio
async def test_awrap_tool_call_handles_auth_error():
    """Verify awrap tool call handles auth error."""
    mw = MCPToolMiddleware()

    tool_mock = MagicMock()
    none_runtime: Any = None
    request = ToolCallRequest(
        tool=tool_mock,
        tool_call={"name": "k8s:get_pods", "args": {}, "id": "call_k8s_auth"},
        state={},
        runtime=none_runtime,
    )

    async def async_failing_handler(req: Any) -> Any:
        """Provide async failing handler test helper or fixture."""
        raise PermissionError("Access denied: unauthorized token for cluster api")

    res = await mw.awrap_tool_call(request, async_failing_handler)
    assert isinstance(res, ToolMessage)
    assert res.status == "error"
    assert res.tool_call_id == "call_k8s_auth"
    assert "authentication" in res.content


def test_resolve_lazy_mcp_tool_and_inject_dynamic_tools_never_duplicates_function_declarations(monkeypatch):
    """Verify resolving a lazy tool by short name and scoped name never injects duplicate tool declarations."""
    from talkops.mcp import preload as preload_mod
    from talkops.mcp.preload import MCPServerInfo, MCPToolInfo

    scoped_server = "subagent__aws-sre-agent_talkops-devops-plugins_e17162a6__aws-api"
    scoped_tool_name = "subagent__aws-sre-agent_talkops-devops-plu_call_aws_5584d6de0db6"
    fake_info = MCPServerInfo(
        name=scoped_server,
        transport="stdio",
        status="ok",
        enabled=True,
        tools=[
            MCPToolInfo(
                name=scoped_tool_name,
                original_name="call_aws",
                description="Call AWS CLI",
                input_schema={"type": "object", "properties": {"cli_command": {"type": "string"}}},
            )
        ],
    )
    monkeypatch.setattr(preload_mod, "get_cached_mcp_server_infos", lambda: [fake_info])
    monkeypatch.setattr(preload_mod, "is_mcp_server_info_cached", lambda name, cfg: True)

    mw = MCPToolMiddleware(mcp_config={scoped_server: {"command": "uvx", "args": [], "lazy_load": True}})

    # 1. Model calls tool by short name ("call_aws")
    t1 = mw._resolve_lazy_mcp_tool("call_aws")
    assert t1 is not None
    assert t1.name == scoped_tool_name

    # 2. Model calls tool on 2nd call by full scoped name
    t2 = mw._resolve_lazy_mcp_tool(scoped_tool_name)
    assert t2 is t1

    # 3. Inject dynamic tools into a ModelRequest-like object
    class _FakeModelRequest:
        def __init__(self, tools):
            self.tools = tools

        def override(self, **kwargs):
            return _FakeModelRequest(kwargs.get("tools", self.tools))

    req = _FakeModelRequest(tools=[])
    injected = mw._inject_dynamic_tools(req)
    tool_names = [t.name for t in injected.tools]
    assert tool_names.count(scoped_tool_name) == 1
    assert len(tool_names) == len(set(tool_names))


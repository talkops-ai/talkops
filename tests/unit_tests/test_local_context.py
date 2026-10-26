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

"""Unit tests for LocalContextMiddleware, detection script generation, and context injection."""
import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import SystemMessage

from talkops.middleware.local_context import (
    LocalContextMiddleware,
    _build_aws_context,
    _build_mcp_context,
    _build_tracing_context,
    _sanitize_error_detail,
    build_detect_script,
)


def test_build_detect_script_content():
    """Verify build detect script content."""
    script = build_detect_script()
    assert "## Local Context" in script
    # Git checks
    assert "git rev-parse" in script
    assert "Current branch" in script
    # Cloud IaC & projects
    assert "main.tf" in script
    assert "Chart.yaml" in script
    assert "cdk.json" in script
    assert "serverless.yml" in script
    assert "pyproject.toml" in script

    # Ensure hardcoded local devops tools and package managers are NOT probed
    assert "command -v kubectl" not in script
    assert "command -v docker" not in script
    assert "command -v podman" not in script
    assert "bun.lock" not in script
    assert "package-lock.json" not in script


def test_sanitize_error_detail():
    """Verify sanitize error detail."""
    assert _sanitize_error_detail(None) == "unknown error"
    assert _sanitize_error_detail("") == "unknown error"
    assert _sanitize_error_detail("Connection refused") == "Connection refused"
    # Strips control characters
    assert "\x00" not in _sanitize_error_detail("Bad\x00error\r\n")


def test_build_mcp_context():
    """Verify build mcp context."""
    assert _build_mcp_context([]) == ""

    server1 = MagicMock()
    server1.name = "k8s-mcp"
    server1.transport = "stdio"
    server1.status = "ok"
    t1, t2 = MagicMock(), MagicMock()
    t1.name, t2.name = "get_pods", "get_services"
    server1.tools = [t1, t2]

    server2 = MagicMock()
    server2.name = "failing-mcp"
    server2.transport = "sse"
    server2.status = "error"
    server2.error = "Connection timeout to daemon"
    server2.tools = []

    server3 = MagicMock()
    server3.name = "disabled-mcp"
    server3.transport = "stdio"
    server3.status = "disabled"
    server3.tools = []

    mcp_text = _build_mcp_context([server1, server2, server3])
    assert "**MCP Servers** (3 servers, 2 tools):" in mcp_text
    assert "**k8s-mcp** (stdio): get_pods, get_services" in mcp_text
    assert "FAILED TO LOAD — <error>Connection timeout to daemon</error>" in mcp_text
    assert "(disabled by user)" in mcp_text


def test_build_tracing_context():
    """Verify build tracing context."""
    assert _build_tracing_context(None, None) == ""

    tracing_text = _build_tracing_context("talkops-agent", "talkops-shell")
    assert "**LangSmith Tracing**:" in tracing_text
    assert '"talkops-agent"' in tracing_text
    assert '"talkops-shell"' in tracing_text


def test_build_aws_context(monkeypatch):
    """Verify build aws context."""
    mock_ctx = MagicMock()
    mock_ctx.account_id = "123456789012"
    mock_ctx.region = "us-east-1"
    mock_ctx.arn = "arn:aws:iam::123456789012:role/DevOpsRole"
    mock_ctx.profile = "prod"
    mock_ctx.authenticated = True

    monkeypatch.setattr("talkops.middleware.local_context.get_aws_context", lambda: mock_ctx)

    aws_text = _build_aws_context()
    assert "**AWS Operational Environment**:" in aws_text
    assert "`123456789012`" in aws_text
    assert "`us-east-1`" in aws_text
    assert "`arn:aws:iam::123456789012:role/DevOpsRole`" in aws_text
    assert "**Authenticated**" in aws_text


def test_local_context_middleware_before_agent_sync():
    """Verify local context middleware before agent sync."""
    backend_mock = MagicMock()
    exec_res = MagicMock()
    exec_res.output = "## Local Context\n**Current Directory**: `/workspace`"
    backend_mock.execute.return_value = exec_res

    mw = LocalContextMiddleware(backend=backend_mock)
    runtime_mock = MagicMock()

    # Initial run executes script and caches result
    empty_state: Any = {}
    update = mw.before_agent(empty_state, runtime_mock)
    assert update is not None
    assert update["_local_context"] == "## Local Context\n**Current Directory**: `/workspace`"
    assert backend_mock.execute.call_count == 1

    # Second run with cached state doesn't execute again
    state_cached: Any = {"_local_context": update["_local_context"]}
    assert mw.before_agent(state_cached, runtime_mock) is None
    assert backend_mock.execute.call_count == 1

    # Summarization event with new cutoff triggers refresh
    state_summarized: Any = {
        "_local_context": "old",
        "_summarization_event": {"cutoff_index": 10},
        "_local_context_refreshed_at_cutoff": 5,
    }
    refresh_update = mw.before_agent(state_summarized, runtime_mock)
    assert refresh_update is not None
    assert refresh_update["_local_context_refreshed_at_cutoff"] == 10
    assert backend_mock.execute.call_count == 2


@pytest.mark.asyncio
async def test_local_context_middleware_abefore_agent():
    """Verify local context middleware abefore agent."""
    async_backend_mock = MagicMock()
    exec_res = MagicMock()
    exec_res.output = "## Local Context\n**Git**: Current branch `main`"
    async_backend_mock.aexecute = AsyncMock(return_value=exec_res)

    mw = LocalContextMiddleware(backend=async_backend_mock)
    runtime_mock = MagicMock()

    empty_state: Any = {}
    update = await mw.abefore_agent(empty_state, runtime_mock)
    assert update is not None
    assert "Current branch `main`" in update["_local_context"]
    assert async_backend_mock.aexecute.call_count == 1


def test_local_context_middleware_wrap_model_call():
    """Verify local context middleware wrap model call."""
    mw = LocalContextMiddleware(tracing_project="test-proj")

    state_dict: Any = {"_local_context": "## Local Context\n**Git**: Current branch `main`"}
    request = ModelRequest(
        model=MagicMock(),
        system_message=SystemMessage(content="You are a Cloud DevOps assistant."),
        messages=[],
        state=state_dict,
        runtime=None,
    )

    passed_request = None

    def handler(req: Any) -> Any:
        """Provide handler test helper or fixture."""
        nonlocal passed_request
        passed_request = req
        return "ok"

    res = mw.wrap_model_call(request, handler)
    assert res == "ok"
    assert passed_request is not None
    content = passed_request.system_message.content
    assert "You are a Cloud DevOps assistant." in content
    assert "## Local Context" in content
    assert "Current branch `main`" in content
    assert '"test-proj"' in content

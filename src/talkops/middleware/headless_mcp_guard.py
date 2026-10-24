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

"""Headless MCP guard middleware — rejects mutating MCP calls when no approval UI exists."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from langchain.agents.middleware.human_in_the_loop import HumanInTheLoopMiddleware
from langchain.agents.middleware.types import AgentState, ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command

from talkops.middleware.registry import register_middleware
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


def mcp_tool_is_coherently_read_only(tool: object) -> bool:
    """Return whether an MCP tool has coherent read-only annotations."""
    metadata = getattr(tool, "metadata", None)
    if not isinstance(metadata, Mapping):
        return False
    hint_names = (
        "readOnlyHint",
        "destructiveHint",
        "idempotentHint",
        "openWorldHint",
    )
    if any(
        name in metadata and metadata[name] is not None and not isinstance(metadata[name], bool) for name in hint_names
    ):
        return False
    return metadata.get("readOnlyHint") is True and metadata.get("destructiveHint") is not True


def gated_mcp_tool_names(mcp_tools: Sequence[BaseTool]) -> set[str]:
    """Return MCP tool names that require manual approval (not coherently read-only)."""
    return {
        tool.name
        for tool in mcp_tools
        if tool.name == "call_mcp_tool" or not mcp_tool_is_coherently_read_only(tool)
    }


def extract_call_mcp_tool_target(args: Mapping[str, Any] | Any) -> tuple[str, str, dict[str, Any]]:
    """Extract (server_name, tool_name, inner_arguments) from call_mcp_tool args."""
    if not isinstance(args, Mapping):
        return "", "", {}
    srv = str(args.get("server_name") or args.get("ServerName") or args.get("server") or "").strip()
    target_tool = str(args.get("tool_name") or args.get("ToolName") or args.get("tool") or "").strip()
    if target_tool and ":" in target_tool and not srv:
        srv, target_tool = target_tool.split(":", 1)
    raw_inner = args.get("arguments")
    if raw_inner is None:
        raw_inner = args.get("Arguments")
    if raw_inner is None:
        raw_inner = args.get("args")
    if isinstance(raw_inner, str):
        stripped = raw_inner.strip()
        if stripped:
            try:
                import json

                loaded = json.loads(stripped)
                if isinstance(loaded, Mapping):
                    raw_inner = loaded
            except Exception:
                try:
                    import ast

                    loaded = ast.literal_eval(stripped)
                    if isinstance(loaded, Mapping):
                        raw_inner = loaded
                except Exception:
                    raw_inner = {}
        else:
            raw_inner = {}
    inner_args = dict(raw_inner) if isinstance(raw_inner, Mapping) else {}
    return srv, target_tool, inner_args


def is_call_mcp_tool_read_only(
    args: Mapping[str, Any] | Any,
    *,
    gated_tool_names: frozenset[str] | set[str] | None = None,
) -> bool:
    """Return whether a ``call_mcp_tool`` invocation targets a coherently read-only MCP tool."""
    srv, target_tool, _ = extract_call_mcp_tool_target(args)
    if not target_tool:
        return False

    qualified = f"{srv}:{target_tool}" if srv else target_tool
    if gated_tool_names:
        non_dispatcher_gated = {n for n in gated_tool_names if n != "call_mcp_tool"}
        if qualified in non_dispatcher_gated or target_tool in non_dispatcher_gated:
            return False

    try:
        from talkops.mcp.preload import get_cached_mcp_server_infos

        for info in get_cached_mcp_server_infos():
            info_name = str(getattr(info, "name", "") or "")
            if srv and info_name != srv and not info_name.endswith(f"__{srv}"):
                continue
            for t_info in getattr(info, "tools", ()) or ():
                t_name = str(getattr(t_info, "name", "") or "")
                orig_name = str(getattr(t_info, "original_name", "") or "")
                short_name = t_name.split(":", 1)[-1] if ":" in t_name else t_name
                if target_tool in {t_name, orig_name, short_name}:
                    annotations = getattr(t_info, "annotations", None)
                    if isinstance(annotations, Mapping) and "readOnlyHint" in annotations:
                        return (
                            annotations.get("readOnlyHint") is True
                            and annotations.get("destructiveHint") is not True
                        )
    except Exception:
        pass

    from talkops.mcp.semantic_profiler import MCPSemanticProfiler

    profiler = MCPSemanticProfiler.get_instance()
    profile = profiler.get_profile(srv, target_tool) or profiler.heuristic_profile(
        target_tool,
        server_name=srv,
    )
    return bool(profile.read_only_hint and not profile.destructive_hint)


@register_middleware(name="headless_mcp_guard")
class HeadlessMCPGuardMiddleware(HumanInTheLoopMiddleware[AgentState[Any], Any, Any]):
    """Reject dynamically gated MCP calls when running headlessly without an approval UI."""

    def __init__(self, tool_names: Sequence[str] | set[str] | None = None) -> None:
        """Initialize HeadlessMCPGuardMiddleware.

        Args:
            tool_names: Collection of mutating tool names that require human approval.
        """
        super().__init__({})
        self._tool_names = frozenset(tool_names or ())

    def _rejection(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_name = request.tool_call.get("name", "")
        if tool_name == "call_mcp_tool":
            args = request.tool_call.get("args") or {}
            blocked = not is_call_mcp_tool_read_only(
                args,
                gated_tool_names=self._tool_names,
            )
        else:
            blocked = tool_name in self._tool_names

        if not blocked:
            return None

        tool_id = request.tool_call.get("id", "")
        logger.warning("Headless MCP guard blocked mutating tool call: %r", tool_name)
        return ToolMessage(
            content=(
                "This MCP action requires approval, but the current headless runtime "
                "has no approval UI. Provide human approval or choose a read-only MCP action."
            ),
            name=tool_name,
            tool_call_id=tool_id,
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """Wrap synchronous tool call and block guarded tools if running headlessly."""
        return self._rejection(request) or handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Wrap asynchronous tool call and block guarded tools if running headlessly."""
        rejection = self._rejection(request)
        return rejection if rejection is not None else await handler(request)

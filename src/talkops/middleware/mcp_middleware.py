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

"""Agent middleware for MCP tools execution, argument normalization, and auth resilience.

Normalizes model-generated arguments before they hit MCP tools (e.g., dropping empty
strings for optional params) and provides clean error surfacing for expired tokens or
unauthenticated MCP sessions.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from talkops.middleware.registry import register_middleware
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

_MCP_AUTH_ERROR_SIGNALS = (
    "unauthorized",
    "not authorized",
    "unauthenticated",
    "authentication failed",
    "token expired",
    "token is expired",
    "is expired",
    "session expired",
    "invalid credentials",
    "access denied",
    "credentials have expired",
    "expiredtoken",
)


def normalize_mcp_arguments(
    arguments: dict[str, Any],
    input_schema: Any = None,
) -> dict[str, Any]:
    """Drop empty-string values for optional parameters to avoid MCP server schema rejections.

    Many MCP servers (AWS MCP, Slack, GitHub, K8s) validate optional string/ID parameters
    strictly and reject empty strings `""` when the parameter should have been omitted.
    """
    if not isinstance(arguments, dict):
        return arguments

    required_set: set[str] = set()
    properties: dict[str, Any] = {}
    if isinstance(input_schema, dict):
        required_set = set(input_schema.get("required") or ())
        properties = input_schema.get("properties") or {}

    cleaned: dict[str, Any] = {}
    for key, value in arguments.items():
        if value != "" or key in required_set:
            cleaned[key] = value
            continue
        # Drop empty strings for non-required fields
        logger.debug("Normalized MCP tool arg: dropped empty-string for key %r", key)

    return cleaned


@register_middleware(name="mcp_tool")
class MCPToolMiddleware(AgentMiddleware):
    """Middleware wrapping MCP tool calls and dynamic tool binding.

    Normalizes arguments, dynamically binds background-probed MCP tools,
    and handles authentication failures.
    """

    def __init__(
        self,
        mcp_config: Any = None,
        **kwargs: Any,
    ) -> None:
        """Initialize MCPToolMiddleware."""
        super().__init__()
        raw_servers = (
            (
                mcp_config.get("mcpServers")
                or mcp_config.get("mcp_servers")
                or mcp_config
            )
            if isinstance(mcp_config, dict)
            else {}
        )
        self._configs: dict[str, dict[str, Any]] = (
            {str(k): dict(v) for k, v in raw_servers.items() if isinstance(v, dict)}
            if isinstance(raw_servers, dict)
            else {}
        )
        self._dynamic_tools_by_name: dict[str, Any] = {}
        self._lazy_tool_aliases: dict[str, Any] = {}
        self._built_fingerprint: tuple[tuple[str, int], ...] = ()

    def _get_dynamic_mcp_tools(self) -> dict[str, Any]:
        """Build and cache StructuredTool wrappers for configured MCP servers.

        Uses server metadata populated in the shared preload cache.
        """
        if not self._configs:
            return self._dynamic_tools_by_name
        try:
            from talkops.mcp.preload import (
                get_cached_mcp_server_infos,
                is_mcp_server_info_cached,
            )
            from talkops.mcp.session_manager import (
                MCPSessionManager,
                build_mcp_tools_from_server_infos,
            )

            cached_by_name = {
                s.name: s for s in get_cached_mcp_server_infos() if s.name in self._configs
            }
            for name, cfg in self._configs.items():
                if name not in cached_by_name and is_mcp_server_info_cached(name, cfg):
                    for s in get_cached_mcp_server_infos():
                        if s.name == name:
                            cached_by_name[name] = s
                            break

            applicable = [
                s for s in cached_by_name.values()
                if s.enabled and s.status == "ok" and s.tools
            ]
            fp = tuple(sorted((s.name, len(s.tools)) for s in applicable))
            if fp and fp != self._built_fingerprint:
                mgr = MCPSessionManager.get_instance(self._configs)
                mgr.register_servers(self._configs)
                built = build_mcp_tools_from_server_infos(
                    applicable,
                    mgr,
                    configs=self._configs,
                )
                # Preserve any previously resolved lazy tools keyed by their canonical tool.name
                preserved = {
                    getattr(t, "name", k): t
                    for k, t in self._dynamic_tools_by_name.items()
                    if getattr(t, "name", k)
                }
                for t in built:
                    preserved[t.name] = t
                self._dynamic_tools_by_name = preserved
                self._built_fingerprint = fp
        except Exception as exc:
            logger.debug("Dynamic MCP tool hydration skipped: %s", exc)
        return self._dynamic_tools_by_name

    def _inject_dynamic_tools(self, request: Any) -> Any:
        dynamic_tools = self._get_dynamic_mcp_tools()
        if not dynamic_tools:
            return request
        existing_tools = list(getattr(request, "tools", None) or ())
        existing_names: set[str] = set()
        deduped_existing: list[Any] = []
        for t in existing_tools:
            t_name = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
            if t_name:
                if t_name in existing_names:
                    continue
                existing_names.add(str(t_name))
            deduped_existing.append(t)

        added: list[Any] = []
        for key_name, tool_obj in dynamic_tools.items():
            actual_name = (
                getattr(tool_obj, "name", None)
                or (tool_obj.get("name") if isinstance(tool_obj, dict) else None)
                or key_name
            )
            if actual_name not in existing_names:
                existing_names.add(str(actual_name))
                added.append(tool_obj)
        if not added and len(deduped_existing) == len(existing_tools):
            return request
        return request.override(tools=[*deduped_existing, *added])

    def wrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """Intercept and wrap synchronous model invocation."""
        return handler(self._inject_dynamic_tools(request))

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """Intercept and wrap asynchronous model invocation."""
        return await handler(self._inject_dynamic_tools(request))

    def _is_mcp_call(self, request: ToolCallRequest) -> bool:
        tool_name = (
            request.tool_call.get("name", "")
            if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict)
            else ""
        )
        if (
            tool_name == "call_mcp_tool"
            or ":" in tool_name
            or tool_name.startswith(("mcp__", "plugin__", "subagent__"))
        ):
            return True
        if getattr(request, "tool", None) is None and (
            tool_name in self._get_dynamic_mcp_tools() or tool_name in self._lazy_tool_aliases
        ):
            return True
        if isinstance(getattr(getattr(request, "tool", None), "metadata", None), dict) and getattr(
            request.tool, "metadata", {}
        ).get("_talkops_mcp"):
            return True
        return False

    def _resolve_lazy_mcp_tool(self, tool_name: str) -> Any | None:
        """Resolve a direct call to a lazy MCP tool (by original name, server:tool, or hashed name)."""
        if not tool_name or not self._configs:
            return None
        cached_alias = self._lazy_tool_aliases.get(tool_name)
        if cached_alias is not None:
            return cached_alias
        try:
            from talkops.mcp.preload import get_cached_mcp_server_infos
            from talkops.mcp.session_manager import (
                MCPSessionManager,
                _build_cached_mcp_tool,
            )

            req_srv, req_tool = (tool_name.split(":", 1) if ":" in tool_name else ("", tool_name))
            for s in get_cached_mcp_server_infos():
                if s.name not in self._configs or not s.enabled or s.status != "ok":
                    continue
                base_srv = s.name.rsplit("__", 1)[-1] if "__" in s.name else s.name
                if req_srv and req_srv not in (s.name, base_srv):
                    continue
                for t in s.tools:
                    orig = getattr(t, "original_name", "") or ""
                    full = getattr(t, "name", "") or ""
                    if req_tool in (orig, full) or tool_name in (orig, full):
                        mgr = MCPSessionManager.get_instance(self._configs)
                        mgr.register_servers(self._configs)
                        built = _build_cached_mcp_tool(
                            mcp_tool=t,
                            server_name=s.name,
                            session_manager=mgr,
                            name_prefix_sep=":",
                        )
                        canonical_name = str(getattr(built, "name", "") or full or tool_name)
                        existing = self._dynamic_tools_by_name.get(canonical_name)
                        if existing is not None:
                            built = existing
                        else:
                            self._dynamic_tools_by_name[canonical_name] = built
                        self._lazy_tool_aliases[tool_name] = built
                        if orig:
                            self._lazy_tool_aliases.setdefault(orig, built)
                        if full:
                            self._lazy_tool_aliases.setdefault(full, built)
                        return built
        except Exception as exc:
            logger.debug("Lazy MCP tool resolution skipped for %s: %s", tool_name, exc)
        return None

    def _preprocess_request(self, request: ToolCallRequest) -> ToolCallRequest:
        if getattr(request, "tool", None) is None:
            tool_name = (
                request.tool_call.get("name", "")
                if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict)
                else ""
            )
            dyn_tool = (
                self._get_dynamic_mcp_tools().get(tool_name)
                or self._lazy_tool_aliases.get(tool_name)
                or self._resolve_lazy_mcp_tool(tool_name)
            )
            if dyn_tool is not None:
                request = request.override(tool=dyn_tool)

        if not self._is_mcp_call(request):
            return request

        args = request.tool_call.get("args") or {}
        if isinstance(args, dict):
            input_schema = getattr(request.tool, "args_schema", None)
            schema_dict = input_schema.schema() if input_schema is not None and hasattr(input_schema, "schema") else None
            cleaned = normalize_mcp_arguments(args, schema_dict)
            request.tool_call["args"] = cleaned

        return request

    def _handle_error(self, request: ToolCallRequest, exc: Exception) -> ToolMessage | None:
        err_str = str(exc).lower()
        is_auth_error = any(sig in err_str for sig in _MCP_AUTH_ERROR_SIGNALS) or (
            "expired" in err_str and any(k in err_str for k in ("token", "credential", "auth", "session"))
        )
        if is_auth_error:
            tool_name = request.tool_call.get("name", "mcp_tool")
            tool_id = request.tool_call.get("id", "")
            logger.warning("MCP tool %r encountered authentication error: %s", tool_name, exc)
            return ToolMessage(
                content=(
                    f"MCP tool `{tool_name}` failed due to authentication: {exc}. "
                    "The cloud credentials or session token for this MCP integration may have expired. "
                    "Please verify your AWS / cloud authentication or refresh your credentials."
                ),
                name=tool_name,
                tool_call_id=tool_id,
                status="error",
            )
        return None

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """Intercept and wrap synchronous tool execution."""
        preprocessed = self._preprocess_request(request)
        try:
            return handler(preprocessed)
        except Exception as exc:
            auth_msg = self._handle_error(request, exc)
            if auth_msg is not None:
                return auth_msg
            raise

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """Intercept and wrap asynchronous tool execution."""
        preprocessed = self._preprocess_request(request)
        try:
            return await handler(preprocessed)
        except Exception as exc:
            auth_msg = self._handle_error(request, exc)
            if auth_msg is not None:
                return auth_msg
            raise


__all__ = [
    "MCPToolMiddleware",
    "normalize_mcp_arguments",
]

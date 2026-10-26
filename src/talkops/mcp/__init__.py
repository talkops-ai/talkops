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

"""Model Context Protocol (MCP) integration module for TalkOps."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from talkops.mcp.config import resolve_mcp_server_env, sync_mcp_env_aliases
    from talkops.mcp.discovery import (
        MCPDiscovery,
        MCPServerConfig,
        discover_mcp_configs,
    )
    from talkops.mcp.mcp_info import (
        MCPServerInfo,
        MCPServerStatus,
        MCPToolInfo,
    )
    from talkops.mcp.preload import (
        format_mcp_status_response,
        preload_mcp_metadata,
        preload_mcp_server_info,
        probe_one_mcp_server,
    )
    from talkops.mcp.raw_config import (
        export_raw_mcp_config,
        import_raw_mcp_config,
    )
    from talkops.mcp.session_manager import (
        MCPSessionManager,
        _is_transient_session_error,
        _normalize_mcp_arguments,
    )
    from talkops.middleware.headless_mcp_guard import (
        HeadlessMCPGuardMiddleware,
        gated_mcp_tool_names,
        mcp_tool_is_coherently_read_only,
    )

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "resolve_mcp_server_env": ("talkops.mcp.config", "resolve_mcp_server_env"),
    "sync_mcp_env_aliases": ("talkops.mcp.config", "sync_mcp_env_aliases"),
    "MCPDiscovery": ("talkops.mcp.discovery", "MCPDiscovery"),
    "MCPServerConfig": ("talkops.mcp.discovery", "MCPServerConfig"),
    "discover_mcp_configs": ("talkops.mcp.discovery", "discover_mcp_configs"),
    "MCPServerInfo": ("talkops.mcp.mcp_info", "MCPServerInfo"),
    "MCPServerStatus": ("talkops.mcp.mcp_info", "MCPServerStatus"),
    "MCPToolInfo": ("talkops.mcp.mcp_info", "MCPToolInfo"),
    "format_mcp_status_response": ("talkops.mcp.preload", "format_mcp_status_response"),
    "preload_mcp_metadata": ("talkops.mcp.preload", "preload_mcp_metadata"),
    "preload_mcp_server_info": ("talkops.mcp.preload", "preload_mcp_server_info"),
    "probe_one_mcp_server": ("talkops.mcp.preload", "probe_one_mcp_server"),
    "export_raw_mcp_config": ("talkops.mcp.raw_config", "export_raw_mcp_config"),
    "import_raw_mcp_config": ("talkops.mcp.raw_config", "import_raw_mcp_config"),
    "MCPSessionManager": ("talkops.mcp.session_manager", "MCPSessionManager"),
    "_is_transient_session_error": ("talkops.mcp.session_manager", "_is_transient_session_error"),
    "_normalize_mcp_arguments": ("talkops.mcp.session_manager", "_normalize_mcp_arguments"),
    "HeadlessMCPGuardMiddleware": ("talkops.middleware.headless_mcp_guard", "HeadlessMCPGuardMiddleware"),
    "gated_mcp_tool_names": ("talkops.middleware.headless_mcp_guard", "gated_mcp_tool_names"),
    "mcp_tool_is_coherently_read_only": ("talkops.middleware.headless_mcp_guard", "mcp_tool_is_coherently_read_only"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_IMPORTS:
        module_name, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_name)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "HeadlessMCPGuardMiddleware",
    "MCPDiscovery",
    "MCPServerConfig",
    "MCPServerInfo",
    "MCPServerStatus",
    "MCPSessionManager",
    "MCPToolInfo",
    "_is_transient_session_error",
    "_normalize_mcp_arguments",
    "discover_mcp_configs",
    "export_raw_mcp_config",
    "format_mcp_status_response",
    "gated_mcp_tool_names",
    "import_raw_mcp_config",
    "mcp_tool_is_coherently_read_only",
    "preload_mcp_metadata",
    "preload_mcp_server_info",
    "probe_one_mcp_server",
    "resolve_mcp_server_env",
    "sync_mcp_env_aliases",
]


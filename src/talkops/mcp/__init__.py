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


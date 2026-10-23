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

"""MCP server management command handler for TalkOps."""

from __future__ import annotations

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


class McpHandler(BaseCommandHandler):
    """Handler for /mcp — manage MCP server connections."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/mcp"

    @property
    def category(self) -> CommandCategory:
        """Return the command category classification."""
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        """Return the safety level required for this command."""
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        """Return the execution queue bypass tier for this command."""
        return BypassTier.SIDE_EFFECT_FREE

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute the command handler with the provided command context."""
        args = ctx.args.strip()

        # No args → mount MCPViewer widget
        if not args:
            return self._open_viewer(ctx)

        parts = args.split(maxsplit=1)
        sub = parts[0].lower()

        if sub == "status":
            return await self._status(ctx)
        if sub == "login" and len(parts) > 1:
            return await self._login(ctx, parts[1].strip())
        if sub == "reconnect":
            force = "--force" in args
            return await self._reconnect(ctx, force=force)
        return CommandResult(
            success=False,
            message="Usage: /mcp [status|login <server>|reconnect [--force]]",
        )

    def _open_viewer(self, ctx: CommandContext) -> CommandResult:
        """Push the MCP viewer modal screen."""
        if ctx.app is None:
            return CommandResult(success=False, message="App context not available.")
        try:
            from talkops.mcp.preload import get_cached_mcp_server_infos
            from talkops.ui.widgets.mcp_viewer import MCPViewerScreen

            servers = ctx.app.get_mcp_servers() if hasattr(ctx.app, "get_mcp_servers") else []
            if not servers:
                servers = get_cached_mcp_server_infos()
                if hasattr(ctx.app, "_mcp_server_info") and servers:
                    ctx.app._mcp_server_info = servers

            is_connecting = False
            if not servers and hasattr(ctx.app, "_mcp_preload_kwargs"):
                no_mcp = ctx.app._mcp_preload_kwargs.get("no_mcp", False)
                if not no_mcp and hasattr(ctx.app, "run_worker"):
                    is_connecting = True
                    if hasattr(ctx.app, "_preload_mcp_metadata_background"):
                        ctx.app.run_worker(ctx.app._preload_mcp_metadata_background(), group="mcp-preload")

            ctx.app.push_screen(MCPViewerScreen(server_info=servers, connecting=is_connecting))
            return CommandResult(success=True, message="", mount_as_app_message=False)
        except Exception as e:
            return CommandResult(success=False, message=f"Failed to open MCP viewer: {e}")

    async def _status(self, ctx: CommandContext) -> CommandResult:
        """Show inline MCP server status."""
        if ctx.app is None or not hasattr(ctx.app, "get_mcp_servers"):
            return CommandResult(success=True, message="MCP server status not available.")

        servers = ctx.app.get_mcp_servers()
        if not servers:
            return CommandResult(success=True, message="No MCP servers configured.")

        lines = ["### MCP Server Status\n"]
        for srv in servers:
            connected = getattr(srv, "connected", False)
            name = getattr(srv, "name", "unknown")
            tool_count = getattr(srv, "tool_count", 0)
            status = "Connected" if connected else "Disconnected"
            lines.append(f"- **`{name}`**: {status} · {tool_count} tools")
        return CommandResult(success=True, message="\n".join(lines))

    async def _login(self, ctx: CommandContext, server_name: str) -> CommandResult:
        """Trigger OAuth/auth flow for a named MCP server."""
        if ctx.app is None:
            return CommandResult(success=False, message="App context not available.")

        # Delegate to app's MCP login flow if available
        if hasattr(ctx.app, "_start_mcp_login"):
            ctx.app._start_mcp_login(server_name)
            return CommandResult(
                success=True,
                message=f"Starting auth flow for MCP server `{server_name}`...",
            )
        return CommandResult(
            success=False,
            message=f"MCP login not available for `{server_name}`.",
        )

    async def _reconnect(self, ctx: CommandContext, *, force: bool) -> CommandResult:
        """Reconnect failed/pending MCP servers."""
        if ctx.app is None:
            return CommandResult(success=False, message="App context not available.")

        if hasattr(ctx.app, "reconnect_mcp_servers"):
            count = await ctx.app.reconnect_mcp_servers(force=force)
            return CommandResult(
                success=True,
                message=f"Reconnected {count} MCP server(s).",
            )
        if hasattr(ctx.app, "_handle_mcp_reconnect_command"):
            await ctx.app._handle_mcp_reconnect_command(force=force)
            return CommandResult(success=True, message="MCP reconnect triggered.")

        return CommandResult(success=False, message="MCP reconnect not available.")


__all__ = ["McpHandler"]

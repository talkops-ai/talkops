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

"""Middleware for enforcing shell command allow-lists."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from talkops.middleware.registry import register_middleware
from talkops.security.shell_safety import is_shell_command_allowed
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


@register_middleware(name="shell_allow_list")
class ShellAllowListMiddleware(AgentMiddleware[Any, Any]):
    """Validate shell commands against an allow-list without HITL interrupts."""

    def __init__(self, allow_list: list[str] | None = None) -> None:
        """Initialize ShellAllowListMiddleware.

        Args:
            allow_list: Optional list of allowed shell executable binary names or command prefixes.
        """
        super().__init__()
        self._allow_list = allow_list or []

    def _validate_tool_call(self, request: ToolCallRequest) -> ToolMessage | None:
        if not getattr(request, "tool_call", None) or not isinstance(request.tool_call, dict):
            return None

        tool_name = request.tool_call.get("name")
        if tool_name not in {"execute", "run_command"}:
            return None

        args = request.tool_call.get("args") or {}
        command = args.get("command") or args.get("CommandLine") or ""
        if not self._allow_list:
            return ToolMessage(
                content="Shell command rejected: no commands are allowed in this mode.",
                name=str(tool_name),
                tool_call_id=str(request.tool_call.get("id") or ""),
                status="error",
            )

        if is_shell_command_allowed(command, self._allow_list):
            logger.debug("Shell command allowed: %r", command)
            return None

        logger.warning("Shell command rejected by allow-list: %r", command)
        allowed_str = ", ".join(self._allow_list)
        return ToolMessage(
            content=(
                f"Shell command rejected: `{command}` is not in the allow-list. "
                f"Allowed commands: {allowed_str}. "
                f"Please use an allowed command or try another approach."
            ),
            name=str(tool_name),
            tool_call_id=str(request.tool_call.get("id") or ""),
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """Wrap synchronous tool call and block disallowed shell commands."""
        err = self._validate_tool_call(request)
        if err is not None:
            return err
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """Wrap asynchronous tool call and block disallowed shell commands."""
        err = self._validate_tool_call(request)
        if err is not None:
            return err
        return await handler(request)


__all__ = ["ShellAllowListMiddleware"]

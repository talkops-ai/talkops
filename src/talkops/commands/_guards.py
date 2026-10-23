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

"""Cross-cutting safety guards for command execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from talkops.commands._types import SafetyLevel
from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from talkops.commands._base import BaseCommandHandler, CommandContext

logger = get_logger(__name__)


async def require_confirmation(handler: BaseCommandHandler, ctx: CommandContext) -> bool:
    """Prompt user for Human-In-The-Loop (HITL) confirmation on high-risk commands.

    Uses the existing ApprovalModalScreen from talkops.ui.approval.
    Returns True if approved, False if cancelled or denied.
    """
    if handler.safety_level not in (SafetyLevel.HIGH_RISK, SafetyLevel.DESTRUCTIVE):
        return True

    severity = "[HIGH RISK]" if handler.safety_level == SafetyLevel.HIGH_RISK else "[DESTRUCTIVE]"
    description = f"{severity}: {handler.name}\n{ctx.raw_command}"

    # If app supports screen pushing (Textual app in TUI mode)
    if ctx.app is not None and hasattr(ctx.app, "push_screen_wait"):
        try:
            from talkops.ui.widgets.approval import ApprovalModalScreen

            screen = ApprovalModalScreen(
                tool_name=handler.name,
                call_id=f"cmd-{handler.name}",
                args={"raw_command": ctx.raw_command, "description": description},
            )
            result = await ctx.app.push_screen_wait(screen)
            if hasattr(result, "approved") and isinstance(result.approved, bool):
                return result.approved
            if isinstance(result, bool):
                return result
            return False
        except Exception:
            logger.exception("Failed to prompt confirmation dialog for %s — denying execution", handler.name)
            return False

    logger.warning("No TUI runtime available to prompt HITL for %s — denying execution", handler.name)
    return False


__all__ = ["require_confirmation"]

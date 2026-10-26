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

"""Cost / token usage command handler for TalkOps (deprecated)."""

from __future__ import annotations

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel


class CostHandler(BaseCommandHandler):
    """Handler for deprecated /cost and /tokens commands."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/cost"

    @property
    def aliases(self) -> tuple[str, ...]:
        """Return alternative slash command aliases."""
        return ("/tokens",)

    @property
    def category(self) -> CommandCategory:
        """Return the command category classification."""
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        """Return the safety level required for this command."""
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        """Return the execution queue bypass tier for this command."""
        return BypassTier.QUEUED

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute the command handler with the provided command context."""
        return CommandResult(
            success=True,
            message=(
                "The /cost and /tokens commands have been deprecated.\n"
                "Active context window usage (percentage and token count) and estimated "
                "cumulative cost are now continuously displayed on the status bar "
                "(e.g. 'Context: 7% / Tokens: 13.9K • $0.04')."
            ),
        )


__all__ = ["CostHandler"]

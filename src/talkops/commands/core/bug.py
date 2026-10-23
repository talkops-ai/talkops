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

"""Bug / feedback command handler for TalkOps."""

from __future__ import annotations

import webbrowser

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel


class BugHandler(BaseCommandHandler):
    """Handler for /bug and /feedback commands."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/bug"

    @property
    def aliases(self) -> tuple[str, ...]:
        """Return alternative slash command aliases."""
        return ("/feedback",)

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
        url = "https://github.com/talkops-ai/talkops/issues/new"
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return CommandResult(
            success=True,
            message=f"**Bug Report / Feedback:** Submit an issue at:\n{url}",
        )


__all__ = ["BugHandler"]

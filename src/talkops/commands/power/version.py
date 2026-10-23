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

"""Version / about command handler for TalkOps."""

from __future__ import annotations

import sys

from talkops._version import __version__
from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel


class VersionHandler(BaseCommandHandler):
    """Handler for /version command."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/version"

    @property
    def aliases(self) -> tuple[str, ...]:
        """Return alternative slash command aliases."""
        return ()

    @property
    def category(self) -> CommandCategory:
        """Return the command category classification."""
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        """Return the safety level required for this command."""
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        """Return the execution queue bypass tier for this command."""
        return BypassTier.ALWAYS

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute the command handler with the provided command context."""
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        model_str = (
            ctx.model_spec
            or getattr(ctx.settings, "model_name", None)
            or (getattr(ctx.app, "_model", None) if ctx.app else None)
            or "default"
        )
        msg = (
            "### TalkOps Version & Runtime\n\n"
            f"- **TalkOps CLI:** `{__version__}`\n"
            f"- **TalkOps SDK:** `{__version__}`\n"
            f"- **Python:** `{py_ver}`\n"
            f"- **Active Model:** `{model_str}`"
        )
        return CommandResult(success=True, message=msg)


__all__ = ["VersionHandler"]

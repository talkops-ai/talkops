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

"""Resume / thread management command handler for TalkOps."""

from __future__ import annotations

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


class ResumeHandler(BaseCommandHandler):
    """Handler for /resume and /threads commands to browse or resume sessions."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/threads"

    @property
    def aliases(self) -> tuple[str, ...]:
        """Return alternative slash command aliases."""
        return ("/resume", "/continue")

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
        return BypassTier.IMMEDIATE_UI

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute the command handler with the provided command context."""
        args = ctx.args.strip()

        if not args:
            if ctx.app is not None and hasattr(ctx.app, "_show_thread_selector"):
                await ctx.app._show_thread_selector()
                return CommandResult(success=True, mount_as_app_message=False)
            installed = getattr(ctx.app, "_installed_screens", {}) if ctx.app else {}
            if "ThreadSelector" in installed:
                return CommandResult(
                    success=True,
                    push_screen="ThreadSelector",
                    mount_as_app_message=False,
                )
            return CommandResult(
                success=True,
                message="Opened Thread Selector.",
                mount_as_app_message=False,
            )

        if args == "-r":
            target_id: str | None = None
            if ctx.session is not None and hasattr(ctx.session, "get_recent_threads"):
                threads = await ctx.session.get_recent_threads(limit=1)
                if threads:
                    target_id = getattr(threads[0], "thread_id", str(threads[0]))
            elif ctx.app is not None and hasattr(ctx.app, "get_recent_threads"):
                threads = await ctx.app.get_recent_threads(limit=1)
                if threads:
                    target_id = getattr(threads[0], "thread_id", str(threads[0]))

            if not target_id:
                return CommandResult(success=False, message="No previous threads to resume.")
            return await self._resume_thread(ctx, target_id)

        if args.startswith("-r "):
            target_id = args[3:].strip()
            if not target_id:
                return CommandResult(success=False, message="Usage: /resume [-r [ID]]")
            return await self._resume_thread(ctx, target_id)

        if not args.startswith("-") and " " not in args:
            return await self._resume_thread(ctx, args)

        return CommandResult(success=False, message="Usage: /resume [-r [ID]]")

    async def _resume_thread(self, ctx: CommandContext, thread_id: str) -> CommandResult:
        if ctx.session is not None and hasattr(ctx.session, "thread_exists"):
            exists = await ctx.session.thread_exists(thread_id)
            if not exists:
                return CommandResult(success=False, message=f"Thread not found: {thread_id}")

        if ctx.app is not None:
            if hasattr(ctx.app, "resume_thread"):
                await ctx.app.resume_thread(thread_id)
            elif hasattr(ctx.app, "_switch_thread"):
                await ctx.app._switch_thread(thread_id)

        return CommandResult(
            success=True,
            message=f"**Resumed Thread:** `{thread_id}`",
        )


__all__ = ["ResumeHandler"]

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

"""Clear and force-clear command handlers for TalkOps."""

from __future__ import annotations

import uuid

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


class ClearHandler(BaseCommandHandler):
    """Handler for /clear command to reset session and TUI view."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/clear"

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
        return BypassTier.QUEUED

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute the command handler with the provided command context."""
        if ctx.app is not None:
            if hasattr(ctx.app, "clear_active_btw"):
                try:
                    ctx.app.clear_active_btw()
                except Exception:
                    pass
            if hasattr(ctx.app, "_pending_messages"):
                ctx.app._pending_messages.clear()
            if hasattr(ctx.app, "_queued_widgets"):
                ctx.app._queued_widgets.clear()
            if hasattr(ctx.app, "_sync_status_queued"):
                ctx.app._sync_status_queued()
            if hasattr(ctx.app, "_image_tracker") and ctx.app._image_tracker is not None:
                try:
                    ctx.app._image_tracker.clear()
                except Exception:
                    pass

            try:
                from talkops.ui.widgets.messages import MessageList
                messages = ctx.app.query_one("#messages", MessageList)
                messages.clear()
            except Exception:
                logger.debug("No MessageList widget found to clear")

            if hasattr(ctx.app, "_ensure_welcome_banner"):
                try:
                    ctx.app._ensure_welcome_banner()
                except Exception:
                    pass

            old_tid = getattr(ctx.app, "_agent_thread_id", None)
            new_thread_id = str(uuid.uuid4())
            if hasattr(ctx.app, "_session_state") and ctx.app._session_state and hasattr(ctx.app._session_state, "reset_thread"):
                try:
                    new_thread_id = ctx.app._session_state.reset_thread()
                except Exception:
                    pass
            ctx.app._agent_thread_id = new_thread_id
            try:
                from talkops.state.session import (
                    acquire_thread_presence,
                    release_thread_presence,
                )
                from talkops.utils.logger import bind_logging_to_thread

                if old_tid and old_tid != new_thread_id:
                    release_thread_presence(old_tid)
                if new_thread_id:
                    acquire_thread_presence(new_thread_id)
                bind_logging_to_thread(new_thread_id)
            except Exception:
                pass
            if hasattr(ctx.app, "_reset_ephemeral_mode_states"):
                try:
                    ctx.app._reset_ephemeral_mode_states()
                except Exception:
                    pass
            if hasattr(ctx.app, "_restore_goal_rubric_state"):
                ctx.app._restore_goal_rubric_state({})
            if hasattr(ctx.app, "_reset_thread_usage"):
                try:
                    ctx.app._reset_thread_usage(0.0, 0)
                except Exception:
                    pass

            try:
                from talkops.ui.widgets.status import StatusBar
                status_bar = ctx.app.query_one("#status-bar", StatusBar)
                status_bar.set_status("Ready")
            except Exception:
                pass

            msg = f"Started new thread: `{new_thread_id}`"
            previous_id = getattr(ctx.session, "previous_thread_id", None) if ctx.session else None
            if previous_id:
                msg += f"\nPrevious thread: `{previous_id}` (Resume with `/resume -r`)"

            return CommandResult(
                success=True,
                message=msg,
                mount_as_app_message=False,
                notify=f"Started new thread ({new_thread_id[:8]})",
            )

        return CommandResult(success=True, message="Conversation cleared.")


class ForceClearHandler(BaseCommandHandler):
    """Handler for /force-clear command to interrupt work and reset session."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/force-clear"

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
        return BypassTier.ALWAYS

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute the command handler with the provided command context."""
        if ctx.app is not None and hasattr(ctx.app, "_force_interrupt_active_work"):
            try:
                ctx.app._force_interrupt_active_work()
            except Exception as exc:
                logger.warning("Error during force_interrupt_active_work: %s", exc)

        clear_handler = ClearHandler()
        return await clear_handler.execute(ctx)


__all__ = ["ClearHandler", "ForceClearHandler"]

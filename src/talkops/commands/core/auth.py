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

"""Authentication command handlers for TalkOps."""

from __future__ import annotations

import asyncio
import os

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


class LoginHandler(BaseCommandHandler):
    """Handler for /login — open auth manager to manage API keys."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/login"

    @property
    def aliases(self) -> tuple[str, ...]:
        """Return alternative slash command aliases."""
        return ("/auth", "/connect")

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
        return BypassTier.IMMEDIATE_UI

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute the command handler with the provided command context."""
        if ctx.app is None:
            return CommandResult(success=False, message="App context not available.")

        from talkops.ui.widgets.auth_manager import AuthManagerScreen
        initial_provider = ctx.args.strip() or None
        screen = AuthManagerScreen(initial_provider=initial_provider)

        def _on_close(_result) -> None:
            # Refocus chat input after auth manager closes
            if hasattr(ctx.app, "_chat_input") and ctx.app._chat_input:
                ctx.app._chat_input.focus_input()
            try:
                from talkops.ui.widgets.welcome import WelcomeBanner
                ctx.app.query_one(WelcomeBanner).refresh_auth()
            except Exception:
                pass
            if hasattr(ctx.app, "maybe_start_deferred_server"):
                asyncio.create_task(ctx.app.maybe_start_deferred_server())

        ctx.app.push_screen(screen, _on_close)
        return CommandResult(success=True, message="", mount_as_app_message=False)


class LogoutHandler(BaseCommandHandler):
    """Handler for /logout — revoke stored credentials."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/logout"

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
        from talkops.model.config import revoke_provider_credentials

        provider_arg = ctx.args.strip() or None
        cleared = revoke_provider_credentials(provider_arg, settings=ctx.settings)

        if ctx.app is not None:
            try:
                from talkops.ui.widgets.welcome import WelcomeBanner
                ctx.app.query_one(WelcomeBanner).refresh_auth()
            except Exception:
                pass

        if cleared:
            names = ", ".join(cleared)
            return CommandResult(
                success=True,
                message=f"Logged out. Credentials revoked and removed from disk for: `{names}`",
            )
        return CommandResult(
            success=True,
            message="No active credentials found to revoke.",
        )


__all__ = ["LoginHandler", "LogoutHandler"]

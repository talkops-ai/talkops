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

"""``/skill-creator`` — Skill creation workflow (alias for ``/skill:skill-creator``)."""

from __future__ import annotations

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


class SkillCreatorHandler(BaseCommandHandler):
    """Launch the built-in skill-creator skill to scaffold a new skill.

    Convenience alias for ``/skill:skill-creator``.  The rewrite lets
    users discover this command before skill loading completes.
    """

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/skill-creator"

    @property
    def aliases(self) -> tuple[str, ...]:
        """Return alternative slash command aliases."""
        return ("/skill:skill-creator",)

    @property
    def category(self) -> CommandCategory:
        """Return the command category classification."""
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        """Return the safety level required for this command."""
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        """Return the execution queue bypass tier for this command."""
        return BypassTier.QUEUED

    @property
    def model_facing_description(self) -> str | None:
        """Return the description exposed to the LLM for command recommendations."""
        return (
            "Recommend this when the user has completed a repeatable multi-step "
            "workflow and wants to package it into a reusable skill."
        )

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute the command handler with the provided command context."""
        args = ctx.args.strip()

        # Rewrite to /skill:skill-creator <args> and delegate
        rewritten = f"/skill:skill-creator {args}" if args else "/skill:skill-creator"

        app = ctx.app
        if app is not None and hasattr(app, "_handle_skill_command"):
            try:
                await app._handle_skill_command(rewritten)
                return CommandResult(
                    success=True,
                    message=None,
                    mount_as_app_message=False,
                )
            except Exception as exc:
                logger.warning("Error delegating /skill-creator to app: %s", exc)

        # Fallback: try dispatching through the skill invoke handler directly
        from talkops.commands.power.skill_invoke import SkillInvokeHandler

        handler = SkillInvokeHandler()
        # Create a new context with the rewritten command
        from talkops.commands._base import CommandContext as Ctx

        new_ctx = Ctx(
            app=ctx.app,
            session=ctx.session,
            agent=ctx.agent,
            settings=ctx.settings,
            raw_command=rewritten,
            args=args,
            thread_id=ctx.thread_id,
            model_spec=ctx.model_spec,
        )
        return await handler.execute(new_ctx)

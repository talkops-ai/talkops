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

"""Model management command handler for TalkOps."""

from __future__ import annotations

import json

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


class ModelHandler(BaseCommandHandler):
    """Handler for /model command to switch models or set defaults."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/model"

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

    @property
    def model_facing_description(self) -> str | None:
        """Return the description exposed to the LLM for command recommendations."""
        return (
            "Recommend this when the user wants to switch the active LLM "
            "provider or model for the current session."
        )

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute the command handler with the provided command context."""
        args = ctx.args.strip()

        if not args:
            if ctx.app is not None and hasattr(ctx.app, "_show_model_selector"):
                await ctx.app._show_model_selector()
                return CommandResult(success=True, mount_as_app_message=False)
            return CommandResult(
                success=True,
                push_screen="ModelSelector",
                mount_as_app_message=False,
            )

        if args.startswith("--default"):
            return await self._handle_default(ctx, args)

        # Direct model switch
        params: dict = {}
        model_name = args

        if "--model-params" in args:
            parts = args.split("--model-params", 1)
            model_name = parts[0].strip()
            param_str = parts[1].strip()
            try:
                params = json.loads(param_str)
            except Exception as exc:
                return CommandResult(
                    success=False,
                    message=f"Invalid JSON in --model-params: {exc}",
                )

        if not model_name:
            return CommandResult(success=False, message="Please specify a model name.")

        if ctx.app is not None:
            if hasattr(ctx.app, "switch_model"):
                await ctx.app.switch_model(model_name, extra_kwargs=params)
            elif hasattr(ctx.app, "_switch_model"):
                await ctx.app._switch_model(model_name)

        if ctx.settings is not None:
            ctx.settings.model_name = model_name

        from talkops.config.toml_config import save_recent_model
        save_recent_model(model_name)

        return CommandResult(
            success=True,
            message=f"**Switched Model:** `{model_name}`",
        )

    async def _handle_default(self, ctx: CommandContext, args: str) -> CommandResult:
        from talkops.config.toml_config import clear_default_model, save_default_model

        rest = args[len("--default"):].strip()
        if rest == "--clear":
            clear_default_model()
            if ctx.app is not None and hasattr(ctx.app, "clear_default_model"):
                await ctx.app.clear_default_model()
            if ctx.settings is not None:
                ctx.settings.default_model = None
            return CommandResult(success=True, message="Default model cleared.")

        if rest:
            save_default_model(rest)
            if ctx.app is not None and hasattr(ctx.app, "set_default_model"):
                await ctx.app.set_default_model(rest)
            if ctx.settings is not None:
                ctx.settings.default_model = rest
            return CommandResult(success=True, message=f"Default model set to: `{rest}`")

        return CommandResult(success=False, message="Usage: /model --default provider:model | --clear")



__all__ = ["ModelHandler"]

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

"""Reasoning effort command handler for TalkOps."""

from __future__ import annotations

import os

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


class EffortHandler(BaseCommandHandler):
    """Handler for /effort command to control model reasoning effort level."""

    SUPPORTED = ("low", "medium", "high", "max")

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/effort"

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
        level = ctx.args.strip().lower()

        current = getattr(ctx.settings, "reasoning_effort", None)
        model = (
            getattr(ctx.settings, "model_name", None)
            or ctx.model_spec
            or (getattr(ctx.app, "_model", None) if ctx.app else None)
            or "default"
        )

        if not level:
            if ctx.app is not None and hasattr(ctx.app, "_show_effort_selector"):
                await ctx.app._show_effort_selector()
                return CommandResult(success=True, message="")
            eff_label = current or "default"
            return CommandResult(
                success=True,
                message=f"**Reasoning Effort:** `{eff_label}` · Model: `{model}`",
            )

        if level in ("clear", "off", "none", "0"):
            from talkops.config.toml_config import save_effort_for_model
            save_effort_for_model(model, None)
            if ctx.settings is not None:
                ctx.settings.reasoning_effort = None
            if ctx.app is not None:
                if hasattr(ctx.app, "_reasoning_effort"):
                    ctx.app._reasoning_effort = None
                try:
                    from talkops.ui.widgets.status import StatusBar
                    sb = ctx.app.query_one("#status-bar", StatusBar)
                    _spec = ctx.app._model or ""
                    _prov, _mod = (_spec.split(":", 1) if ":" in _spec else ("", _spec))
                    sb.set_model(provider=_prov, model=_mod, effort="")
                except Exception:
                    pass
            return CommandResult(success=True, message="Reasoning effort cleared (disabled).")


        from talkops.model.reasoning import supported_efforts_for_model
        supported_efforts = supported_efforts_for_model(model) or self.SUPPORTED

        if level not in supported_efforts and level not in self.SUPPORTED:
            return CommandResult(
                success=False,
                message=f"Unknown effort level `{level}` for model `{model}`. Supported: {', '.join(supported_efforts)}, clear",
            )

        if ctx.settings is not None:
            ctx.settings.reasoning_effort = level
        os.environ["TALKOPS_REASONING_EFFORT"] = level

        from talkops.config.toml_config import save_effort_for_model
        save_effort_for_model(model, level)

        if ctx.app is not None:
            ctx.app._reasoning_effort = level
            try:
                from talkops.ui.widgets.status import StatusBar
                sb = ctx.app.query_one("#status-bar", StatusBar)
                _spec = ctx.app._model or ""
                _prov, _mod = (_spec.split(":", 1) if ":" in _spec else ("", _spec))
                sb.set_model(provider=_prov, model=_mod, effort=level)
            except Exception:
                pass

        return CommandResult(
            success=True,
            message=f"**Reasoning Effort Set:** `{level}` · `{model}`",
        )


__all__ = ["EffortHandler"]

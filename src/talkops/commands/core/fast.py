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

"""Fast mode command handler for TalkOps."""

from __future__ import annotations

import inspect

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


def resolve_fast_model(curr_model: str | None, configured_fast_model: str | None) -> str:
    """Resolve fast model matching the active provider to avoid unnecessary auth prompts."""
    if configured_fast_model:
        return configured_fast_model

    curr = (curr_model or "").lower()
    if "google" in curr or "gemini" in curr:
        return "google_genai:gemini-3.5-flash-lite"
    if "anthropic" in curr or "claude" in curr:
        return "anthropic:claude-haiku-4-5"
    if "openai" in curr or "gpt" in curr or "o1" in curr or "o3" in curr:
        return "openai:gpt-5.4-mini"
    if "deepseek" in curr:
        return "deepseek:deepseek-v4-flash"
    if "groq" in curr:
        return "groq:llama-3.1-8b-instant"
    if "mistral" in curr:
        return "mistralai:mistral-small-latest"
    if "cohere" in curr:
        return "cohere:command-r"

    from talkops.model.config import has_provider_credentials
    if has_provider_credentials("anthropic") is True:
        return "anthropic:claude-haiku-4-5"
    if has_provider_credentials("openai") is True:
        return "openai:gpt-5.4-mini"
    if has_provider_credentials("google_genai") is True:
        return "google_genai:gemini-3.5-flash-lite"
    if has_provider_credentials("deepseek") is True:
        return "deepseek:deepseek-v4-flash"
    if has_provider_credentials("groq") is True:
        return "groq:llama-3.1-8b-instant"

    return curr_model or ""


class FastHandler(BaseCommandHandler):
    """Handler for /fast command to toggle cheapest model and low reasoning effort."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/fast"

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
        configured_fast = getattr(ctx.settings, "fast_model", None)
        curr_model = getattr(ctx.settings, "model_name", None) or (getattr(ctx.app, "_model", None) if ctx.app else None)
        curr_effort = getattr(ctx.settings, "reasoning_effort", None) or (getattr(ctx.app, "_reasoning_effort", None) if ctx.app else None)

        fast_model = resolve_fast_model(curr_model, configured_fast)
        if not fast_model:
            return CommandResult(
                success=False,
                message="Cannot enable fast mode: no active model or provider credentials configured.",
            )

        # Check if fast mode is currently active (toggle off condition)
        is_fast_active = (curr_model == fast_model and curr_effort == "low") or (getattr(ctx.app, "_fast_mode_active", False) is True)

        if is_fast_active:
            # Toggle OFF — restore previous model and effort
            prev_model: str | None = None
            prev_effort: str | None = None

            if ctx.app is not None:
                if hasattr(ctx.app, "get_previous_model"):
                    pm = ctx.app.get_previous_model()
                    if isinstance(pm, str):
                        prev_model = pm
                if hasattr(ctx.app, "get_previous_effort"):
                    pe = ctx.app.get_previous_effort()
                    if isinstance(pe, str):
                        prev_effort = pe

            restored_model = prev_model or curr_model or ""
            restored_effort = prev_effort or "high"

            if ctx.app is not None:
                if hasattr(ctx.app, "switch_model"):
                    res = ctx.app.switch_model(restored_model)
                    if inspect.isawaitable(res):
                        await res
                if hasattr(ctx.app, "_set_effort_override"):
                    res = ctx.app._set_effort_override(restored_effort)
                    if inspect.isawaitable(res):
                        await res
                setattr(ctx.app, "_fast_mode_active", False)

            if ctx.settings is not None:
                ctx.settings.model_name = restored_model
                ctx.settings.reasoning_effort = restored_effort

            return CommandResult(
                success=True,
                message=f"**Fast Mode OFF:** Restored model `{restored_model}`, effort: `{restored_effort}`",
            )

        # Toggle ON — save current state, switch to fast model with low effort
        if ctx.app is not None:
            if hasattr(ctx.app, "save_previous_model"):
                ctx.app.save_previous_model(curr_model, curr_effort)
            setattr(ctx.app, "_fast_mode_active", True)

        if ctx.app is not None:
            if hasattr(ctx.app, "switch_model"):
                res = ctx.app.switch_model(fast_model)
                if inspect.isawaitable(res):
                    await res
            if hasattr(ctx.app, "_set_effort_override"):
                res = ctx.app._set_effort_override("low")
                if inspect.isawaitable(res):
                    await res

        if ctx.settings is not None:
            ctx.settings.model_name = fast_model
            ctx.settings.reasoning_effort = "low"

        return CommandResult(
            success=True,
            message=f"**Fast Mode ON:** Model: `{fast_model}`, effort: `low`",
        )


__all__ = ["FastHandler", "resolve_fast_model"]

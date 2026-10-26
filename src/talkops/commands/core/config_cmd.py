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

"""Configuration management command handler for TalkOps."""

from __future__ import annotations

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

# Keys whose values should be masked in display output
_SECRET_INDICATORS = {"api_key", "token", "secret", "password", "credential"}


class ConfigHandler(BaseCommandHandler):
    """Handler for /config — view and manage TalkOps configuration."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/config"

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
        parts = ctx.args.strip().split(maxsplit=2)
        sub = parts[0].lower() if parts and parts[0] else "show"

        if sub == "path":
            return self._show_path(ctx)
        if sub == "show":
            return self._show_config(ctx)
        if sub == "set" and len(parts) >= 3:
            return self._set_config(ctx, parts[1], parts[2])
        if sub == "reset" and len(parts) >= 2:
            return self._reset_config(ctx, parts[1])
        return CommandResult(
            success=False,
            message="Usage: /config [show|set <key> <value>|reset <key>|path]",
        )

    def _get_settings(self, ctx: CommandContext):
        if ctx.settings is not None:
            return ctx.settings
        from talkops.config.settings import settings
        return settings

    def _show_path(self, ctx: CommandContext) -> CommandResult:
        settings = self._get_settings(ctx)
        if settings is None:
            return CommandResult(success=False, message="Settings not available.")
        path = settings.config_path
        return CommandResult(success=True, message=f"**Config file:** `{path}`")

    def _show_config(self, ctx: CommandContext) -> CommandResult:
        if ctx.app is not None:
            from talkops.ui.widgets.config_manager import ConfigManagerScreen
            settings = self._get_settings(ctx)
            screen = ConfigManagerScreen(settings=settings)

            def _on_config_closed(changed: bool | None) -> None:
                if changed and ctx.app is not None:
                    ctx.app._schedule_reload()

            ctx.app.push_screen(screen, _on_config_closed)
            return CommandResult(success=True, message="", mount_as_app_message=False)

        # Inline fallback: use the manifest for a grouped, source-aware display
        settings = self._get_settings(ctx)
        if settings is None:
            return CommandResult(success=False, message="Settings not available.")

        from talkops.config.manifest import get_config_options, resolve_scalar

        lines = ["**Current Configuration:**\n"]
        current_group = None
        for opt in get_config_options():
            if opt.group != current_group:
                current_group = opt.group
                lines.append(f"\n**{current_group}:**")
            value, source = resolve_scalar(opt, settings=settings)
            if opt.redacted:
                val_str = self._mask_secret(opt.key, str(value)) if value else "`not set`"
            else:
                val_str = f"`{value}`" if value is not None else "`None`"
            source_tag = f" [{source}]" if source != "default" else ""
            lines.append(f"  `{opt.key}`: {val_str}{source_tag}")
        return CommandResult(
            success=True,
            message="\n".join(lines),
        )

    def _set_config(self, ctx: CommandContext, key: str, value: str) -> CommandResult:
        settings = self._get_settings(ctx)
        if settings is None:
            return CommandResult(success=False, message="Settings not available.")
        ok, msg = settings.set_field(key, value)
        if ok and ctx.app is not None:
            ctx.app._schedule_reload()
        return CommandResult(success=ok, message=msg)

    def _reset_config(self, ctx: CommandContext, key: str) -> CommandResult:
        settings = self._get_settings(ctx)
        if settings is None:
            return CommandResult(success=False, message="Settings not available.")
        ok, msg = settings.reset_field(key)
        if ok and ctx.app is not None:
            ctx.app._schedule_reload()
        return CommandResult(success=ok, message=msg)

    @staticmethod
    def _mask_secret(key: str, value: str) -> str:
        """Mask values for keys containing secret indicators."""
        if any(s in key.lower() for s in _SECRET_INDICATORS) and len(value) > 8:
            return f"`{value[:4]}...{value[-4:]}`"
        return f"`{value}`"


__all__ = ["ConfigHandler"]

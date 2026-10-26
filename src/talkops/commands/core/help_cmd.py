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

"""Help command handler for TalkOps."""

from __future__ import annotations

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel


class HelpHandler(BaseCommandHandler):
    """Handler for /help command providing categorized listing and per-command details."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/help"

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
        return BypassTier.SIDE_EFFECT_FREE

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute the command handler with the provided command context."""
        target = ctx.args.strip()
        if target:
            return self._specific_help(ctx, target)
        return self._general_help(ctx)

    def _general_help(self, ctx: CommandContext) -> CommandResult:
        try:
            from talkops.ui.command_registry import get_slash_commands

            commands = get_slash_commands()
            formatted_cmds = [
                f"{entry.name} {entry.argument_hint}".rstrip()
                for entry in commands
            ]
            formatted_cmds.append("/skill:<name>")
            commands_str = ", ".join(formatted_cmds)
        except Exception:
            commands_str = (
                "/bug, /clear, /compact, /context, /effort [<level>|clear], "
                "/exit, /fast, /force-clear, /help, /model, /notifications, /plan [module_path], "
                "/resume, /scrollbar, /timestamps, /version, /skill:<name>"
            )

        from talkops.config.settings import newline_shortcut

        nl_shortcut = newline_shortcut()
        help_body = (
            "### TalkOps Commands & Shortcuts\n\n"
            f"**Slash Commands:**\n{commands_str}\n\n"
            "**Keyboard Shortcuts:**\n"
            "- **`Enter`**: Submit message\n"
            f"- **`{nl_shortcut}`**: Insert newline\n"
            "- **`Ctrl+X`**: Open prompt in external editor\n"
            "- **`Ctrl+N`**: Review notifications\n"
            "- **`Ctrl+\\`**: Toggle debug console\n"
            "- **`Shift+Tab`**: Toggle auto-approve mode\n"
            "- **`@filename`**: Complete files and inject content\n"
            "- **`/command`**: Slash commands (`/help`, `/doctor`, `/reload`, `/agent`)\n"
            "- **`!command`**: Run shell command directly\n"
            "- **`!!command`**: Run shell command without adding output to context\n\n"
            "_Documentation: https://talkops.dev/docs_"
        )

        return CommandResult(success=True, message=help_body)

    def _specific_help(self, ctx: CommandContext, target: str) -> CommandResult:
        clean_target = target if target.startswith("/") else f"/{target}"
        router = getattr(ctx.app, "_command_router", None)
        handler = router.get_handler(clean_target) if router else None

        if not handler:
            return CommandResult(success=False, message=f"Unknown command: `{clean_target}`")

        lines = [
            f"**Command:** `{handler.name}`",
            f"**Category:** {handler.category.value.title()}",
            f"**Safety Level:** `{handler.safety_level.value}`",
            f"**Bypass Tier:** `{handler.bypass_tier.value}`",
        ]
        if handler.aliases:
            alias_str = ", ".join(f"`{a}`" for a in handler.aliases)
            lines.append(f"**Aliases:** {alias_str}")

        return CommandResult(success=True, message="\n".join(lines))


__all__ = ["HelpHandler"]

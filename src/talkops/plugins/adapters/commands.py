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

"""Adapter from discovered plugins to TalkOps slash commands."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from talkops.plugins.models import PluginInstance

logger = get_logger(__name__)


class PluginCommandHandler(BaseCommandHandler):
    """Command handler for plugin-contributed slash command workflow files."""

    def __init__(
        self,
        *,
        command_name: str,
        description: str,
        instructions: str,
        plugin_id: str,
        aliases: tuple[str, ...] = (),
        plugin_root: Path | None = None,
        plugin_data_dir: Path | None = None,
    ) -> None:
        """Initialize PluginCommandHandler."""
        self._name = command_name if command_name.startswith("/") else f"/{command_name}"
        self._description = description
        self._instructions = instructions
        self._plugin_id = plugin_id
        self._aliases = aliases
        self._plugin_root = plugin_root
        self._plugin_data_dir = plugin_data_dir

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return self._name

    @property
    def aliases(self) -> tuple[str, ...]:
        """Return alternative slash command aliases."""
        return self._aliases

    @property
    def category(self) -> CommandCategory:
        """Return the command category classification."""
        return CommandCategory.AUTOMATION

    @property
    def safety_level(self) -> SafetyLevel:
        """Return the safety level required for this command."""
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        """Return the execution queue bypass tier for this command."""
        return BypassTier.QUEUED

    @property
    def description(self) -> str:
        """Return the human-readable command description."""
        return self._description

    async def execute(self, ctx: CommandContext) -> CommandResult:
        # Build prompt message combining plugin command instructions and user arguments
        """Execute the command handler with the provided command context."""
        user_input = ctx.args.strip()
        rendered_instructions = self._instructions
        has_arg_placeholder = "$ARGUMENTS" in rendered_instructions
        if has_arg_placeholder:
            rendered_instructions = rendered_instructions.replace("$ARGUMENTS", user_input)
        if self._plugin_root is not None and self._plugin_data_dir is not None:
            from talkops.plugins.substitution import substitute_string

            rendered_instructions = substitute_string(
                rendered_instructions,
                plugin_root=self._plugin_root,
                plugin_data=self._plugin_data_dir,
            )
        combined_prompt = (
            f"### Plugin Command Execution: {self._name} (from {self._plugin_id})\n\n"
            f"{rendered_instructions}"
        )
        if user_input and not has_arg_placeholder:
            combined_prompt += f"\n\n**User Input / Target Arguments**:\n{user_input}"

        return CommandResult(
            success=True,
            message=combined_prompt,
            mount_as_app_message=True,
            notify=f"Triggered plugin command {self._name}",
        )


def _parse_command_file(path: Path) -> tuple[str, str] | None:
    """Read a markdown command file and return (description, body)."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read plugin command file %s: %s", path, exc)
        return None

    # Check for optional YAML frontmatter
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.DOTALL)
    if match:
        body = match.group(2).strip()
        description = "Plugin slash command"
        try:
            import yaml

            fm = yaml.safe_load(match.group(1))
            if isinstance(fm, dict) and isinstance(fm.get("description"), str):
                description = fm["description"]
        except Exception:
            pass
        return description, body

    first_line = content.splitlines()[0] if content.splitlines() else "Plugin command"
    desc = first_line.lstrip("#").strip() or "Plugin command"
    return desc, content.strip()


def plugin_commands(
    plugins: tuple[PluginInstance, ...] | list[PluginInstance],
    *,
    include_subagents: bool = False,
) -> list[BaseCommandHandler]:
    """Discover and instantiate command handlers for enabled plugins."""
    handlers: list[BaseCommandHandler] = []

    for plugin in plugins:
        if not include_subagents and getattr(plugin, "is_agent_plugin", False):
            continue
        p_id = plugin.plugin_id
        p_name = plugin.name
        for cmd_path in plugin.inventory.commands:
            if not cmd_path.exists():
                continue
            cmd_files: list[Path] = []
            if cmd_path.is_dir():
                cmd_files.extend(sorted(cmd_path.glob("*.md")))
            elif cmd_path.is_file() and cmd_path.suffix == ".md":
                cmd_files.append(cmd_path)

            for file in cmd_files:
                parsed = _parse_command_file(file)
                if not parsed:
                    continue
                desc, instructions = parsed
                stem = file.stem.lower().strip()

                primary_name = f"/{stem}"
                namespaced_alias = f"/{p_name}:{stem}"
                full_scoped_alias = f"/{p_id}:{stem}"

                handler = PluginCommandHandler(
                    command_name=primary_name,
                    description=desc,
                    instructions=instructions,
                    plugin_id=p_id,
                    aliases=(namespaced_alias, full_scoped_alias),
                    plugin_root=plugin.root,
                    plugin_data_dir=plugin.data_dir,
                )
                handlers.append(handler)

    return handlers


def discover_plugin_commands(
    project_root: Path | None = None,
    *,
    include_subagents: bool = False,
) -> list[BaseCommandHandler]:
    """Discover command handlers across all enabled marketplace and project plugins."""
    try:
        from talkops.plugins.discovery import discover_plugins
        from talkops.plugins.project_plugins import load_project_plugins

        handlers: list[BaseCommandHandler] = []

        # 1. Enabled marketplace plugins
        plugins = discover_plugins(project_root=project_root).plugins
        handlers.extend(plugin_commands(plugins, include_subagents=include_subagents))

        # 2. Project-local marketplace plugins
        if project_root is not None:
            proj_res = load_project_plugins(project_root)
            for cmd_path in proj_res.main_commands:
                cmd_files: list[Path] = []
                if cmd_path.is_dir():
                    cmd_files.extend(sorted(cmd_path.glob("*.md")))
                elif cmd_path.is_file() and cmd_path.suffix == ".md":
                    cmd_files.append(cmd_path)

                for file in cmd_files:
                    parsed = _parse_command_file(file)
                    if not parsed:
                        continue
                    desc, instructions = parsed
                    stem = file.stem.lower().strip()
                    primary_name = f"/{stem}"
                    handler = PluginCommandHandler(
                        command_name=primary_name,
                        description=desc,
                        instructions=instructions,
                        plugin_id="project",
                    )
                    handlers.append(handler)

        return handlers
    except Exception as exc:
        logger.warning("Could not discover plugin commands: %s", exc)
        return []

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

"""``/browser`` — Invoke the browser subagent for web tasks or manage the Chrome session."""

from __future__ import annotations

from talkops.browser.session import BrowserSessionManager
from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

BROWSER_SUBAGENT_DIRECTIVE = (
    "<SUBAGENT>\n"
    "The user has explicitly added the browser subagent. "
    "You must use this subagent via the `task(subagent_type=\"browser\", ...)` tool to process their request.\n"
    "</SUBAGENT>"
)

_VALID_MODES = frozenset({"auto", "auto_connect", "persistent", "isolated", "browserbase"})


def format_browser_command_prompt(task_prompt: str) -> str:
    """Wrap *task_prompt* with the Antigravity-style ``<SUBAGENT>`` directive."""
    cleaned = task_prompt.strip()
    if cleaned.startswith("<SUBAGENT>"):
        return cleaned
    return f"{BROWSER_SUBAGENT_DIRECTIVE}\n\n{cleaned}"


def _render_status_markdown(manager: BrowserSessionManager) -> str:
    info = manager.status_summary()
    connected_str = (
        f"Connected (`{info['connection_tier']}` → `{info['endpoint']}`)"
        if info["connected"]
        else "Idle (connects lazily on first browser tool call)"
    )
    devtools_str = (
        f"Detected at `{info['devtools_active_port_path']}`"
        if info["devtools_active_port_detected"]
        else "Not detected (enable at `chrome://inspect/#remote-debugging` in Chrome to use your live persona)"
    )
    return (
        "**TalkOps Browser Agent (`/browser`)**\n\n"
        f"- **Session Status:** {connected_str}\n"
        f"- **Connection Mode:** `{info['mode']}`\n"
        f"- **Live Chrome `DevToolsActivePort`:** {devtools_str}\n"
        f"- **Persistent Profile Dir:** `{info['persistent_profile_dir']}`\n"
        f"- **Artifacts Dir:** `{info['artifacts_dir']}`\n\n"
        "**Usage:**\n"
        "- `/browser <task>` — Delegate a web task to the isolated `browser` subagent\n"
        "- `/browser status` — Show Chrome connection & persona status\n"
        "- `/browser mode <auto|auto_connect|persistent|isolated|browserbase>` — Switch persona/connection mode\n"
        "- `/browser disconnect` — Detach from the current browser session"
    )


class BrowserHandler(BaseCommandHandler):
    """Invoke a browser agent for web tasks or manage the Chrome connection lifecycle.

    Usage:
      ``/browser <task>``        — Delegate ``<task>`` to the ``browser`` subagent
      ``/browser status``        — Inspect Chrome connection tier and ``DevToolsActivePort`` status
      ``/browser mode <mode>``   — Set connection mode (``auto``, ``auto_connect``, ``persistent``, ``isolated``, ``browserbase``)
      ``/browser disconnect``    — Disconnect from the active Chrome session
    """

    @property
    def name(self) -> str:
        """Return primary slash command name."""
        return "/browser"

    @property
    def aliases(self) -> tuple[str, ...]:
        """Return alternative slash command aliases."""
        return ("/web",)

    @property
    def category(self) -> CommandCategory:
        """Return command category classification."""
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        """Return safety level required for this command."""
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        """Return execution queue bypass tier."""
        return BypassTier.QUEUED

    @property
    def model_facing_description(self) -> str | None:
        """Return description exposed to the LLM in ``<slash_commands>``."""
        return (
            "Recommend this when the user's task involves inspecting SRE/observability dashboards "
            "(Grafana, Datadog, Kibana, PagerDuty), navigating cloud/GitOps/CI-CD consoles, "
            "testing `localhost` web apps, or automating any SaaS or web UI."
        )

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute `/browser` command."""
        args = ctx.args.strip()
        manager = BrowserSessionManager.get_instance()

        if not args or args.lower() == "status":
            return CommandResult(
                success=True,
                message=_render_status_markdown(manager),
                data=manager.status_summary(),
            )

        parts = args.split(maxsplit=1)
        subcmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if subcmd in {"disconnect", "close"} and not rest:
            await manager.close()
            return CommandResult(
                success=True,
                message="Disconnected from the active browser session.",
            )

        if subcmd == "mode" and rest.lower() in _VALID_MODES:
            new_mode = rest.lower()
            await manager.close()
            manager.config.mode = new_mode  # type: ignore[assignment]
            return CommandResult(
                success=True,
                message=f"Browser connection mode set to **`{new_mode}`**.",
                data={"mode": new_mode},
            )

        prompt = format_browser_command_prompt(args)
        app = ctx.app
        if app is not None and hasattr(app, "send_agent_message"):
            try:
                await app.send_agent_message(prompt)
                return CommandResult(
                    success=True,
                    message=None,
                    mount_as_app_message=False,
                    data={"prompt": prompt, "subagent": "browser"},
                )
            except Exception as exc:
                logger.warning("Failed to dispatch /browser prompt to agent: %s", exc)

        return CommandResult(
            success=True,
            message=prompt,
            data={"prompt": prompt, "subagent": "browser"},
        )


__all__ = [
    "BROWSER_SUBAGENT_DIRECTIVE",
    "BrowserHandler",
    "format_browser_command_prompt",
]

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

"""Unified chat command registry shared by `ChannelHub` and channel webhook adapters.

Provides cross-platform ChatOps slash commands across Slack, Microsoft Teams, and Discord,
including operational controls (`/status`, `/mode` for `manual`/`smart`/`jev` tool approvals).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

MAX_SUMMARY_CHARS = 100


@dataclass(frozen=True, slots=True)
class ChatCommand:
    """One slash command a user can invoke in Slack, Microsoft Teams, or Discord."""

    name: str
    summary: str
    hidden: bool = False
    argument: str | None = None

    @property
    def text(self) -> str:
        """Command string with leading slash (e.g. `/new`)."""
        return f"/{self.name}"


CHAT_COMMANDS: tuple[ChatCommand, ...] = (
    ChatCommand("help", "Show available TalkOps chat commands and usage guide."),
    ChatCommand("new", "Stop current work and start a fresh conversation thread."),
    ChatCommand("stop", "Cancel the currently running agent turn in this thread."),
    ChatCommand("status", "Show active thread ID, model, approval mode, and RBAC role."),
    ChatCommand(
        "model",
        "Show this conversation's model or switch to another provider:model.",
        argument="provider:model or 'default'",
    ),
    ChatCommand(
        "smart-model",
        "Show or set the smart escalation model used across chats.",
        argument="provider:model, 'off', or 'default'",
    ),
    ChatCommand(
        "effort",
        "Show or set reasoning effort level (low, medium, high, max, or default).",
        argument="low | medium | high | max | default",
    ),
    ChatCommand(
        "mode",
        "Show or switch tool approval mode (manual, smart, or jev).",
        argument="manual | smart | jev",
    ),
    ChatCommand(
        "mcp-reload",
        "Reload MCP server configuration after edits.",
    ),
    ChatCommand(
        "context-doctor",
        "Inspect conversation thread state and message count.",
    ),
    ChatCommand(
        "pair",
        "Operator only: list, approve, or revoke paired DM senders.",
        hidden=True,
        argument="list | approve <code> | revoke <channel> <sender_id>",
    ),
    ChatCommand(
        "reset-all-history",
        "Delete this conversation's thread mapping and start fresh.",
        hidden=True,
    ),
)

COMMANDS_BY_NAME: Mapping[str, ChatCommand] = MappingProxyType(
    {cmd.name: cmd for cmd in CHAT_COMMANDS}
)


def visible_commands() -> tuple[ChatCommand, ...]:
    """Return all commands advertised in `/help` and platform command pickers."""
    return tuple(cmd for cmd in CHAT_COMMANDS if not cmd.hidden)


def parse_chat_command(text: str) -> tuple[ChatCommand, str] | None:
    """Parse a leading `/command [args]` out of inbound message text.

    Supports both `/command args` and `/talkops command args` (when invoked via a
    single umbrella slash command on Slack or Teams).
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None

    parts = stripped.split(maxsplit=1)
    first_token = parts[0][1:].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    # Strip Slack/Discord/Teams bot suffix like `/help@talkops_bot`
    if "@" in first_token:
        first_token = first_token.split("@", 1)[0]

    # Support umbrella `/talkops <subcommand> [args]` or `/ops <subcommand> [args]`
    if first_token in {"talkops", "ops"}:
        if not rest:
            return COMMANDS_BY_NAME["help"], ""
        sub_parts = rest.split(maxsplit=1)
        sub_name = sub_parts[0].lstrip("/").lower()
        sub_rest = sub_parts[1].strip() if len(sub_parts) > 1 else ""
        if sub_name in COMMANDS_BY_NAME:
            return COMMANDS_BY_NAME[sub_name], sub_rest
        return None

    cmd = COMMANDS_BY_NAME.get(first_token)
    if cmd is None:
        return None
    return cmd, rest


def build_help_message() -> str:
    """Build the `/help` response listing all visible commands."""
    header = (
        "**TalkOps Multi-Channel Cloud Assistant**\n"
        "Ask questions, investigate incidents, or run cloud/Kubernetes operations directly from chat. "
        "Each thread keeps its own isolated conversation context."
    )
    lines = [f"`{cmd.text}` — {cmd.summary}" for cmd in visible_commands()]
    footer = (
        "Tool Approvals: When a mutating tool requires approval, click **Approve** / **Reject** "
        "on the card, react with 👍 / 👎, or reply `approve` / `deny <reason>` in the thread."
    )
    return f"{header}\n\n" + "\n".join(lines) + f"\n\n{footer}"

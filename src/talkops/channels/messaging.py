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

"""Agent-callable `send_channel_message` tool and active conversation context.

Binds the active `(channel, conversation_id, thread_id)`
into a `ContextVar` during every agent turn and exposes a tool so the agent can send
intermediate progress messages or upload generated files/reports mid-turn.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.tools import tool

from talkops.channels.interfaces import ChannelMedia
from talkops.channels.media import detect_media_kind, validate_outbound_media

if TYPE_CHECKING:
    from talkops.channels.hub import ChannelHub


@dataclass(frozen=True, slots=True)
class ActiveConversationContext:
    """Context of the channel conversation currently executing a turn."""

    channel: str
    conversation_id: str
    thread_id: str | None = None
    lg_thread_id: str | None = None
    sender_id: str | None = None


ACTIVE_CHANNEL_CONVERSATION: ContextVar[ActiveConversationContext | tuple[Any, ...] | None] = ContextVar(
    "ACTIVE_CHANNEL_CONVERSATION",
    default=None,
)


def create_send_message_tool(
    hub: ChannelHub,
    *,
    outbound_media_root: Path | None = None,
) -> Any:
    """Create a LangChain `@tool` allowing the agent to send messages or files to chat mid-turn."""

    @tool("send_channel_message")
    async def send_channel_message(
        text: str = "",
        media_path: str | None = None,
        channel: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Send an immediate message or file attachment to the active chat conversation.

        Args:
            text: Message text to send.
            media_path: Optional local file path (e.g. a generated chart, log bundle, or report) to attach.
            channel: Optional target channel provider ('slack', 'teams', 'discord'); defaults to current chat.
            conversation_id: Optional target conversation ID; defaults to current chat thread.
        """
        active = ACTIVE_CHANNEL_CONVERSATION.get()
        if isinstance(active, tuple):
            act_ch = active[0] if len(active) > 0 else ""
            act_conv = active[1] if len(active) > 1 else ""
            act_thr = active[2] if len(active) > 2 else None
        elif active is not None:
            act_ch = active.channel
            act_conv = active.conversation_id
            act_thr = active.thread_id
        else:
            act_ch, act_conv, act_thr = "", "", None

        target_channel = (channel or act_ch).strip().lower()
        target_conv = (conversation_id or act_conv).strip()
        target_thread = act_thr if not conversation_id else None

        if not target_channel or not target_conv:
            return {
                "status": "error",
                "message": "No active channel conversation in context and none specified.",
            }

        adapter = hub.get_adapter(target_channel)
        if adapter is None:
            return {
                "status": "error",
                "message": f"Channel '{target_channel}' is not registered.",
            }

        media_list: list[ChannelMedia] = []
        if media_path:
            raw_path = Path(media_path)
            try:
                validated = validate_outbound_media(
                    ChannelMedia(
                        media_type=detect_media_kind(raw_path),
                        path=raw_path,
                    ),
                    root=outbound_media_root,
                )
                media_list.append(validated)
            except Exception as exc:
                return {"status": "error", "message": f"Invalid media_path: {exc}"}

        if not text.strip() and not media_list:
            return {"status": "error", "message": "Provide text or media_path."}

        sent = await adapter.send(
            target_conv,
            text or (media_list[0].filename or "Attached file"),
            thread_id=target_thread,
            media=tuple(media_list),
        )
        return {
            "status": "sent",
            "channel": sent.channel,
            "conversation_id": sent.conversation_id,
            "message_id": sent.message_id,
        }

    return send_channel_message

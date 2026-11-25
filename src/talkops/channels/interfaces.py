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

"""Core interfaces, envelopes, and protocols for `talkops.channels`.

Defines provider-neutral message, interaction, reaction, media, and approval card
models along with the `WebhookChannelAdapter` protocol implemented by Slack,
Microsoft Teams, and Discord adapters in central HTTP webhook mode.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

ChannelProviderName = Literal["slack", "teams", "discord"]
TransportMode = Literal["auto", "webhook", "websocket", "polling", "hybrid"]
MediaKind = Literal["image", "audio", "video", "document"]
ApprovalDecisionType = Literal["approve", "reject"]


@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    """Declarative capability & rate-limit contract advertised by a channel adapter.

    Allows `ChannelHub` and `ChannelStreamSink` to adapt chunk sizes, streaming edit
    cadence, typing indicator refresh intervals, thread auto-spawning, and deferred
    interaction token windows without hardcoding per-platform conditionals.
    """

    max_text_chars: int = 4000
    edit_debounce_seconds: float = 1.0
    typing_refresh_seconds: float = 4.0
    supports_threads: bool = True
    auto_spawn_thread_on_mention: bool = False
    supports_streaming_edits: bool = True
    supports_reactions: bool = True
    supports_attachments: bool = True
    supports_ephemeral_interactions: bool = False
    interaction_token_ttl_seconds: float = 0.0
    supported_transports: tuple[str, ...] = ("webhook",)


@dataclass(frozen=True, slots=True)
class ChannelTransportHealth:
    """Live diagnostic health snapshot for an adapter's inbound transport."""

    mode: str = "webhook"
    state: Literal["idle", "connecting", "connected", "reconnecting", "standby", "error", "stopped"] = "idle"
    connected: bool = False
    detail: str = ""
    reconnect_count: int = 0
    last_heartbeat_at: float | None = None
    shard: tuple[int, int] | None = None
    lease_owner: str | None = None
    fatal_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize transport health into a JSON-compatible dictionary."""
        return {
            "mode": self.mode,
            "state": self.state,
            "connected": self.connected,
            "detail": self.detail,
            "reconnect_count": self.reconnect_count,
            "last_heartbeat_at": self.last_heartbeat_at,
            "shard": list(self.shard) if self.shard is not None else None,
            "lease_owner": self.lease_owner,
            "fatal_code": self.fatal_code,
        }


@dataclass(frozen=True, slots=True)
class ChannelMedia:
    """Provider-neutral media attachment for inbound or outbound messages.

    Args:
        media_type: Media category (`image`, `audio`, `video`, `document`).
        path: Local filesystem path when staged/downloaded on disk.
        url: Remote platform URL when available.
        mime_type: MIME type of the attachment.
        filename: Original or target filename.
        size_bytes: Size in bytes if known.
        content_bytes: Optional in-memory bytes for generated attachments (e.g. `.md` RCA reports).
        metadata: Provider-specific attachment metadata.
    """

    media_type: MediaKind
    path: Path | None = None
    url: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    size_bytes: int | None = None
    content_bytes: bytes | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChannelMessage:
    """Normalized inbound message delivered to a central channel webhook.

    Args:
        channel: Provider identifier (`"slack"`, `"teams"`, `"discord"`).
        conversation_id: Canonical conversation key (e.g. `<channel_id>:<thread_ts>`
            on Slack, `conversation.id` on Teams, `channel_id` on Discord).
        sender_id: Platform-specific user identifier of the author.
        text: Message text with bot mention stripped.
        message_id: Platform message identifier.
        thread_id: Optional thread root identifier when distinct from `conversation_id`.
        parent_channel_id: Base channel ID when `conversation_id` includes a thread suffix.
        sender_name: Human-readable display name of the sender.
        is_dm: Whether the message arrived in a 1-on-1 direct message chat.
        mentioned: Whether the bot was explicitly `@mentioned` in the message.
        from_bot: Whether the author is a bot account.
        media: Tuple of normalized media attachments.
        metadata: Provider-specific metadata (e.g. `service_url` on Teams, `guild_id` on Discord).
    """

    channel: str
    conversation_id: str
    sender_id: str
    text: str
    message_id: str | None = None
    thread_id: str | None = None
    parent_channel_id: str | None = None
    sender_name: str = ""
    is_dm: bool = False
    mentioned: bool = False
    from_bot: bool = False
    media: tuple[ChannelMedia, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChannelReaction:
    """Normalized emoji reaction event delivered to a central channel webhook.

    Used to resolve pending HITL tool approvals with 👍 (`approve`) or 👎 (`reject`),
    matching `deepagents-talon` semantics.
    """

    channel: str
    conversation_id: str
    sender_id: str
    message_id: str
    emoji: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChannelInteraction:
    """Normalized interactive callback from a button click, Adaptive Card, or slash command.

    Args:
        channel: Provider identifier (`"slack"`, `"teams"`, `"discord"`).
        conversation_id: Canonical conversation/thread identifier.
        sender_id: Platform user ID of the user who clicked or invoked the command.
        interaction_kind: `"approval"` (HITL button), `"command"` (slash command), or `"action"`.
        sender_name: Human-readable display name.
        message_id: Platform message ID of the interactive card/message if applicable.
        thread_id: Optional thread root identifier.
        action_id: Identifier of the clicked button/action.
        decision: Normalized HITL decision (`"approve"` or `"reject"`) when `interaction_kind == "approval"`.
        approval_id: Unique ID of the pending approval prompt.
        run_id: Target LangGraph run ID if embedded in the button payload.
        reason: Optional rejection reason or user comment submitted from an Adaptive Card/modal.
        command_name: Bare slash command name (without leading `/`) when `interaction_kind == "command"`.
        command_args: Arguments string passed to the slash command.
        response_url: Ephemeral callback URL (Slack `response_url` or Discord follow-up webhook URL).
        is_dm: Whether the interaction occurred in a direct message.
        metadata: Provider-specific fields.
    """

    channel: str
    conversation_id: str
    sender_id: str
    interaction_kind: Literal["approval", "command", "action", "feedback"]
    sender_name: str = ""
    message_id: str | None = None
    thread_id: str | None = None
    action_id: str = ""
    decision: ApprovalDecisionType | None = None
    approval_id: str | None = None
    run_id: str | None = None
    reason: str | None = None
    command_name: str | None = None
    command_args: str = ""
    response_url: str | None = None
    is_dm: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolCallSummary:
    """One tool invocation requiring human-in-the-loop approval."""

    name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    tool_call_id: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalCardSpec:
    """Specification for rendering a rich interactive HITL tool approval card."""

    approval_id: str
    channel: str
    conversation_id: str
    thread_id: str | None
    lg_thread_id: str
    run_id: str | None
    requester_id: str
    tool_calls: tuple[ToolCallSummary, ...]
    title: str = "Tool Execution Approval Required"
    prompt_text: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SendResult:
    """Receipt returned after sending or updating an outbound channel message."""

    channel: str
    conversation_id: str
    message_id: str | None = None
    thread_id: str | None = None
    raw_response: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChannelStatus:
    """Diagnostic health status for a registered channel adapter."""

    channel: str
    configured: bool
    webhook_events_path: str
    webhook_interactions_path: str
    exposure_mode: str = "allowlist"
    detail: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WebhookHTTPResponse:
    """HTTP response envelope returned by a channel adapter's webhook handler."""

    status_code: int = 200
    body: dict[str, Any] | str | bytes = field(default_factory=lambda: {"ok": True})
    headers: Mapping[str, str] = field(default_factory=dict)
    content_type: str = "application/json"


MessageHandler = Callable[[ChannelMessage], Awaitable[None]]
ReactionHandler = Callable[[ChannelReaction], Awaitable[None]]
InteractionHandler = Callable[[ChannelInteraction], Awaitable[str | None]]


@runtime_checkable
class WebhookChannelAdapter(Protocol):
    """Protocol implemented by every HTTP webhook channel adapter (`slack`, `teams`, `discord`)."""

    name: str

    def set_message_handler(self, handler: MessageHandler) -> None:
        """Register the callback invoked when an inbound user message is parsed."""
        ...

    def set_reaction_handler(self, handler: ReactionHandler) -> None:
        """Register the callback invoked when an inbound emoji reaction is parsed."""
        ...

    def set_interaction_handler(self, handler: InteractionHandler) -> None:
        """Register the callback invoked when an interactive button or slash command arrives."""
        ...

    async def start(self) -> None:
        """Initialize HTTP clients or warm provider state on server startup."""
        ...

    async def stop(self) -> None:
        """Close HTTP clients and release resources on server shutdown."""
        ...

    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> bool:
        """Verify cryptographic authenticity of an inbound HTTP webhook request."""
        ...

    async def handle_webhook_event(
        self,
        headers: Mapping[str, str],
        body: bytes,
        query_params: Mapping[str, str] | None = None,
    ) -> WebhookHTTPResponse:
        """Process an inbound `/channels/{provider}/events` HTTP webhook call."""
        ...

    async def handle_webhook_interaction(
        self,
        headers: Mapping[str, str],
        body: bytes,
        query_params: Mapping[str, str] | None = None,
    ) -> WebhookHTTPResponse:
        """Process an inbound `/channels/{provider}/interactions` or `/commands` HTTP call."""
        ...

    async def send(
        self,
        conversation_id: str,
        text: str,
        *,
        thread_id: str | None = None,
        media: Sequence[ChannelMedia] = (),
        blocks_or_cards: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Send a message to `conversation_id` (and optional `thread_id`)."""
        ...

    async def send_in_thread(
        self,
        conversation_id: str,
        thread_id: str,
        text: str,
        *,
        media: Sequence[ChannelMedia] = (),
        blocks_or_cards: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Send a reply anchored to a specific `thread_id`."""
        ...

    async def update_message(
        self,
        conversation_id: str,
        message_id: str,
        text: str,
        *,
        blocks_or_cards: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Edit an existing message in place (used for live streaming & resolved approval cards)."""
        ...

    async def send_approval_prompt(
        self,
        spec: ApprovalCardSpec,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Render and send a rich interactive HITL tool approval card."""
        ...

    async def send_typing(
        self,
        conversation_id: str,
        *,
        thread_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Emit a typing indicator to the conversation if supported by the platform."""
        ...

    async def fetch_thread_context(
        self,
        conversation_id: str,
        thread_id: str | None = None,
        *,
        limit: int = 40,
        metadata: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Fetch recent thread messages so mid-thread mentions include prior context."""
        ...

    def status(self) -> ChannelStatus:
        """Return current configuration and endpoint status."""
        ...

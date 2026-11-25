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

"""`talkops.channels` — Central Multi-Channel HTTP Webhook Integration Hub for TalkOps.

Provides unified webhook-driven channel adapters for Slack, Microsoft Teams, and
Discord along with `ChannelHub` orchestration and built-in `ChannelsAddon` (`ChatOpsAddon`)
enabled by default on `talkops serve`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from talkops.channels.approvals import (
    PendingApprovalRegistry,
    PendingToolApproval,
    extract_tool_calls_from_interrupt,
    parse_text_approval_decision,
)
from talkops.channels.commands import (
    CHAT_COMMANDS,
    COMMANDS_BY_NAME,
    ChatCommand,
    build_help_message,
    parse_chat_command,
    visible_commands,
)
from talkops.channels.formatting import (
    ChannelExposure,
    ExposureMode,
    MarkdownBlock,
    MarkdownTable,
    ParsedChannelOutput,
    build_discord_incident_embed,
    build_discord_rca_embed,
    build_slack_incident_blocks,
    build_slack_rca_blocks,
    build_slack_response_blocks,
    build_teams_incident_card,
    build_teams_rca_card,
    build_teams_response_card,
    chunk_text,
    decode_slack_mrkdwn,
    extract_rca_takeaway,
    format_for_channel,
    format_markdown_for_discord,
    format_markdown_for_slack,
    format_markdown_for_teams,
    parse_channel_output,
    parse_incident_output,
    parse_markdown_blocks,
    parse_rca_output,
)
from talkops.channels.hub import ChannelHub
from talkops.channels.interfaces import (
    ApprovalCardSpec,
    ChannelCapabilities,
    ChannelInteraction,
    ChannelMedia,
    ChannelMessage,
    ChannelReaction,
    ChannelStatus,
    ChannelTransportHealth,
    SendResult,
    ToolCallSummary,
    TransportMode,
    WebhookChannelAdapter,
    WebhookHTTPResponse,
)
from talkops.channels.media import (
    DISCORD_MEDIA_HOSTS,
    ChannelMediaError,
    build_inbound_media_prompt,
    build_multimodal_content,
    contains_oauth_callback_url,
    download_channel_media_safely,
    download_discord_attachment_safely,
    download_slack_file_safely,
    extract_loopback_oauth_callback_url,
    extract_markdown_media,
    resolve_bounded_media_path,
)
from talkops.channels.messaging import (
    ACTIVE_CHANNEL_CONVERSATION,
    create_send_message_tool,
)
from talkops.channels.pairing import (
    PairedSender,
    PairingStore,
    PendingPairingRequest,
    format_code,
    generate_code,
    normalize_code,
)
from talkops.channels.providers import (
    DEFAULT_DISCORD_INTENTS,
    DiscordChannelConfig,
    DiscordGatewayConfig,
    DiscordGatewayTransport,
    DiscordWebhookChannel,
    SlackChannelConfig,
    SlackWebhookChannel,
    TeamsChannelConfig,
    TeamsWebhookChannel,
    compute_discord_intents,
)
from talkops.channels.registry import (
    CHANNEL_ADAPTER_ENTRYPOINT_GROUP,
    ChannelAdapterFactory,
    ChannelAdapterRegistry,
    clear_registered_channel_adapter_factories,
    register_channel_adapter_factory,
    unregister_channel_adapter_factory,
)
from talkops.channels.state_store import (
    ChannelStateStore,
    InMemoryChannelStateStore,
    PersistentChannelStateStore,
)
from talkops.channels.stream_sink import ChannelStreamSink
from talkops.channels.transports import (
    ChannelTransport,
    FatalTransportError,
    ReconnectingWebSocketTransport,
)

if TYPE_CHECKING:
    from talkops.addons.builtin.channels import (
        ChannelsAddon,
        ChannelsAddonSettings,
        ChatOpsAddon,
    )


def __getattr__(name: str) -> Any:
    """Lazily expose `ChannelsAddon`, `ChannelsAddonSettings`, and `ChatOpsAddon` without circular imports."""
    if name in {"ChannelsAddon", "ChannelsAddonSettings", "ChatOpsAddon"}:
        from talkops.addons.builtin import channels as _channels_addon_mod

        return getattr(_channels_addon_mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ACTIVE_CHANNEL_CONVERSATION",
    "CHANNEL_ADAPTER_ENTRYPOINT_GROUP",
    "CHAT_COMMANDS",
    "COMMANDS_BY_NAME",
    "DEFAULT_DISCORD_INTENTS",
    "DISCORD_MEDIA_HOSTS",
    "ApprovalCardSpec",
    "ChannelAdapterFactory",
    "ChannelAdapterRegistry",
    "ChannelCapabilities",
    "ChannelExposure",
    "ChannelHub",
    "ChannelInteraction",
    "ChannelMedia",
    "ChannelMediaError",
    "ChannelMessage",
    "ChannelReaction",
    "ChannelStateStore",
    "ChannelStatus",
    "ChannelStreamSink",
    "ChannelTransport",
    "ChannelTransportHealth",
    "ChannelsAddon",
    "ChannelsAddonSettings",
    "ChatCommand",
    "ChatOpsAddon",
    "DiscordChannelConfig",
    "DiscordGatewayConfig",
    "DiscordGatewayTransport",
    "DiscordWebhookChannel",
    "ExposureMode",
    "FatalTransportError",
    "InMemoryChannelStateStore",
    "MarkdownBlock",
    "MarkdownTable",
    "PairedSender",
    "PairingStore",
    "ParsedChannelOutput",
    "PendingApprovalRegistry",
    "PendingPairingRequest",
    "PendingToolApproval",
    "PersistentChannelStateStore",
    "ReconnectingWebSocketTransport",
    "SendResult",
    "SlackChannelConfig",
    "SlackWebhookChannel",
    "TeamsChannelConfig",
    "TeamsWebhookChannel",
    "ToolCallSummary",
    "TransportMode",
    "WebhookChannelAdapter",
    "WebhookHTTPResponse",
    "build_discord_incident_embed",
    "build_discord_rca_embed",
    "build_help_message",
    "build_inbound_media_prompt",
    "build_multimodal_content",
    "build_slack_incident_blocks",
    "build_slack_rca_blocks",
    "build_slack_response_blocks",
    "build_teams_incident_card",
    "build_teams_rca_card",
    "build_teams_response_card",
    "chunk_text",
    "clear_registered_channel_adapter_factories",
    "compute_discord_intents",
    "contains_oauth_callback_url",
    "create_send_message_tool",
    "decode_slack_mrkdwn",
    "download_channel_media_safely",
    "download_discord_attachment_safely",
    "download_slack_file_safely",
    "extract_loopback_oauth_callback_url",
    "extract_markdown_media",
    "extract_rca_takeaway",
    "extract_tool_calls_from_interrupt",
    "format_code",
    "format_for_channel",
    "format_markdown_for_discord",
    "format_markdown_for_slack",
    "format_markdown_for_teams",
    "generate_code",
    "normalize_code",
    "parse_channel_output",
    "parse_chat_command",
    "parse_incident_output",
    "parse_markdown_blocks",
    "parse_rca_output",
    "parse_text_approval_decision",
    "register_channel_adapter_factory",
    "resolve_bounded_media_path",
    "unregister_channel_adapter_factory",
    "visible_commands",
]

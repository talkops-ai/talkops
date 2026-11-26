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

"""Central HTTP Webhook Channel Providers for TalkOps Server Mode."""

from __future__ import annotations

from talkops.channels.providers.discord import (
    DiscordChannelConfig,
    DiscordWebhookChannel,
)
from talkops.channels.providers.discord_gateway import (
    DEFAULT_DISCORD_INTENTS,
    DiscordGatewayConfig,
    DiscordGatewayTransport,
    compute_discord_intents,
)
from talkops.channels.providers.slack import SlackChannelConfig, SlackWebhookChannel
from talkops.channels.providers.teams import TeamsChannelConfig, TeamsWebhookChannel

__all__ = [
    "DEFAULT_DISCORD_INTENTS",
    "DiscordChannelConfig",
    "DiscordGatewayConfig",
    "DiscordGatewayTransport",
    "DiscordWebhookChannel",
    "SlackChannelConfig",
    "SlackWebhookChannel",
    "TeamsChannelConfig",
    "TeamsWebhookChannel",
    "compute_discord_intents",
]

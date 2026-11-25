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

"""Discord Multi-Transport Channel Adapter (`DiscordWebhookChannel`).

Supports all three Discord inbound transport modes with recommended defaults:
1. **`hybrid`** *(default when `bot_token` + `public_key` are set)*:
   - Official HTTP Interactions Endpoint (`POST /channels/discord/interactions`) with Ed25519
     signature verification for slash commands & buttons, PLUS
   - Persistent Discord API v10 Gateway WebSocket (`DiscordGatewayTransport`) for live
     `MESSAGE_CREATE` and `MESSAGE_REACTION_ADD` events in channels, threads, and DMs.
2. **`websocket`** *(default when `bot_token` is set without `public_key` — zero tunnel required!)*:
   - Receives `MESSAGE_CREATE`, `MESSAGE_REACTION_ADD`, and `INTERACTION_CREATE` over the
     outbound Gateway WebSocket and ACKs interactions via `POST /interactions/{id}/{token}/callback`.
3. **`webhook`** *(when `gateway_enabled=False` or `transport_role="http_only"`)*:
   - Pure HTTP Interactions (`POST /channels/discord/interactions`) and HMAC-signed forwarded
     event webhooks (`POST /channels/discord/events`).

Also implements:
- Automatic bot identity discovery (`GET /users/@me` / Gateway `READY`) and strict echo-loop
  prevention (`author.bot`, `webhook_id`, self-user filtering)
- Automatic Discord thread creation (`POST /channels/{id}/messages/{msg_id}/threads`) for
  authorized `@mention` conversations and webhook incidents + thread channel detection
- Deferred `/ask` (`type: 5`) `@original` webhook edits with automatic 13-minute Bot REST fallback
- HTTP `429` rate-limit backoff, button cleanup on approval resolution, emoji status reactions,
  inbound/outbound file attachments, and slash command syncing (`sync_commands`).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html
import json
import os
import re
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from talkops.channels.commands import COMMANDS_BY_NAME, ChatCommand
from talkops.channels.formatting import (
    DISCORD_MAX_TEXT_CHARS,
    ChannelExposure,
    build_discord_incident_embed,
    chunk_text,
    exposure_from_mapping,
    format_markdown_for_discord,
)
from talkops.channels.interfaces import (
    ApprovalCardSpec,
    ChannelCapabilities,
    ChannelInteraction,
    ChannelMedia,
    ChannelMessage,
    ChannelReaction,
    ChannelStatus,
    ChannelTransportHealth,
    InteractionHandler,
    MessageHandler,
    ReactionHandler,
    SendResult,
    TransportMode,
    WebhookHTTPResponse,
)
from talkops.channels.media import (
    contains_oauth_callback_url,
    resolve_bounded_media_path,
)
from talkops.channels.providers.discord_gateway import (
    DISCORD_THREAD_CHANNEL_TYPES,
    DiscordGatewayConfig,
    DiscordGatewayTransport,
    compute_discord_intents,
)
from talkops.channels.state_store import ChannelStateStore
from talkops.channels.transports import ChannelTransport, WebSocketConnector
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
_DISCORD_MENTION_RE = re.compile(r"<@!?\d+>|<@&\d+>")
_SEEN_EVENT_CAP = 1024
_DEFERRED_INTERACTION_CAP = 256
_THREAD_LOOKUP_CAP = 2048
_INTERACTION_SAFE_WINDOW_SECONDS = 780.0  # 13 minutes (< Discord's 15m token TTL)
_MAX_RATE_LIMIT_RETRIES = 3

INTERACTION_PING = 1
INTERACTION_APPLICATION_COMMAND = 2
INTERACTION_MESSAGE_COMPONENT = 3

RESPONSE_PONG = 1
RESPONSE_CHANNEL_MESSAGE_WITH_SOURCE = 4
RESPONSE_DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE = 5
RESPONSE_DEFERRED_UPDATE_MESSAGE = 6
RESPONSE_UPDATE_MESSAGE = 7

_SLACK_TO_DISCORD_EMOJI: dict[str, str] = {
    "eyes": "👀",
    "white_check_mark": "✅",
    "thumbsup": "👍",
    "+1": "👍",
    "thumbsdown": "👎",
    "-1": "👎",
    "warning": "⚠️",
    "x": "❌",
}


def _parse_optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    norm = str(value).strip().lower()
    if norm in {"1", "true", "yes", "on"}:
        return True
    if norm in {"0", "false", "no", "off"}:
        return False
    return None


@dataclass(slots=True)
class _DeferredInteractionState:
    """Tracks an active deferred Discord interaction (`type: 5`) for `@original` follow-ups."""

    interaction_id: str
    interaction_token: str
    application_id: str
    channel_id: str
    created_at: float = field(default_factory=time.monotonic)
    original_edited: bool = False
    message_id: str | None = None
    handed_off: bool = False

    def is_within_safe_window(self) -> bool:
        """Return `True` if the interaction token is still safely within Discord's 15m TTL."""
        return (time.monotonic() - self.created_at) < _INTERACTION_SAFE_WINDOW_SECONDS


def build_discord_approval_components(
    spec: ApprovalCardSpec,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build Discord Embeds and ActionRow Button Components for HITL tool approval."""
    fields: list[dict[str, Any]] = []
    for idx, tc in enumerate(spec.tool_calls, start=1):
        args_str = ", ".join(f"{k}={v!r}" for k, v in list(tc.args.items())[:5])
        val = f"`{args_str or '(no args)'}`"
        if tc.description:
            val += f"\n_{tc.description}_"
        fields.append(
            {
                "name": f"{idx}. {tc.name}"[:256],
                "value": val[:1024],
                "inline": False,
            }
        )

    embeds = [
        {
            "title": f"🛡️ {spec.title}"[:256],
            "description": (
                f"**{len(spec.tool_calls)} tool call(s) require approval.**\n"
                "Click **Approve** / **Reject**, react with 👍 / 👎, or reply `approve` / `deny <reason>`."
            ),
            "color": 0xF59E0B,
            "fields": fields[:25],
        }
    ]
    components = [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 3,
                    "label": "Approve",
                    "emoji": {"name": "✅"},
                    "custom_id": f"talkops_approve:{spec.approval_id}",
                },
                {
                    "type": 2,
                    "style": 4,
                    "label": "Reject",
                    "emoji": {"name": "🛑"},
                    "custom_id": f"talkops_reject:{spec.approval_id}",
                },
            ],
        }
    ]
    return embeds, components


def build_discord_slash_commands_payload(
    commands: Sequence[ChatCommand],
    *,
    include_ask: bool = True,
) -> list[dict[str, Any]]:
    """Build Discord Application Command JSON definitions for `PUT /applications/{id}/commands`."""
    payloads: list[dict[str, Any]] = []
    if include_ask:
        payloads.append(
            {
                "name": "ask",
                "type": 1,
                "description": "Ask TalkOps a question or start an SRE investigation.",
                "options": [
                    {
                        "name": "prompt",
                        "description": "Question or operational task for TalkOps.",
                        "type": 3,
                        "required": True,
                    }
                ],
            }
        )
    for cmd in commands:
        if cmd.hidden and cmd.name != "pair":
            continue
        cmd_def: dict[str, Any] = {
            "name": cmd.name[:32],
            "type": 1,
            "description": (cmd.summary or f"Run /{cmd.name}")[:100],
        }
        if cmd.argument:
            cmd_def["options"] = [
                {
                    "name": "args",
                    "description": cmd.argument[:100],
                    "type": 3,
                    "required": False,
                }
            ]
        payloads.append(cmd_def)
    return payloads


@dataclass(frozen=True, slots=True)
class DiscordChannelConfig:
    """Configuration for the Discord multi-transport channel adapter."""

    bot_token: str = field(default="", repr=False)
    application_id: str = ""
    public_key: str = ""
    webhook_secret: str = field(default="", repr=False)
    bot_user_id: str | None = None
    api_base: str = DISCORD_API_BASE
    exposure: ChannelExposure = field(default_factory=ChannelExposure)
    verify_signatures: bool = True
    request_timeout_seconds: float = 30.0
    transport_mode: TransportMode = "auto"
    gateway_enabled: bool | None = None
    transport_role: Literal["all", "http_only", "gateway_worker"] = "all"
    auto_create_threads: bool = True
    enable_members_intent: bool = False
    shard_id: int = 0
    num_shards: int = 1
    gateway_url: str = ""
    persona_webhook_url: str = field(default="", repr=False)

    def effective_transport_mode(self) -> Literal["webhook", "websocket", "hybrid"]:
        """Resolve effective Discord inbound transport mode following recommended defaults.

        - `"auto"` (default):
          - `"hybrid"` when `bot_token` and `public_key` are configured (HTTP Interactions + Gateway)
          - `"websocket"` when `bot_token` is configured without `public_key` (zero-tunnel Gateway-only)
          - `"webhook"` when `gateway_enabled` is explicitly `False` or `transport_role == "http_only"`
        """
        if self.transport_role == "http_only" or self.gateway_enabled is False:
            return "webhook"
        if self.transport_mode == "webhook":
            return "webhook"
        if self.transport_mode == "websocket":
            return "websocket"
        if self.transport_mode == "hybrid":
            return "hybrid"
        if self.bot_token:
            return "hybrid" if self.public_key else "websocket"
        return "webhook"

    @classmethod
    def from_mapping(
        cls,
        cfg: Mapping[str, Any],
        env: Mapping[str, str],
    ) -> DiscordChannelConfig:
        """Construct `DiscordChannelConfig` from addon config mapping and environment variables."""
        bot_token = str(
            cfg.get("bot_token")
            or env.get("TALKOPS_CHANNELS_DISCORD_BOT_TOKEN")
            or env.get("TALKOPS_DISCORD_BOT_TOKEN")
            or env.get("DISCORD_BOT_TOKEN")
            or ""
        ).strip()
        application_id = str(
            cfg.get("application_id")
            or env.get("TALKOPS_CHANNELS_DISCORD_APPLICATION_ID")
            or env.get("TALKOPS_DISCORD_APPLICATION_ID")
            or env.get("DISCORD_APPLICATION_ID")
            or ""
        ).strip()
        public_key = str(
            cfg.get("public_key")
            or env.get("TALKOPS_CHANNELS_DISCORD_PUBLIC_KEY")
            or env.get("TALKOPS_DISCORD_PUBLIC_KEY")
            or env.get("DISCORD_PUBLIC_KEY")
            or ""
        ).strip()
        webhook_secret = str(
            cfg.get("webhook_secret")
            or env.get("TALKOPS_CHANNELS_DISCORD_WEBHOOK_SECRET")
            or env.get("TALKOPS_DISCORD_WEBHOOK_SECRET")
            or env.get("DISCORD_WEBHOOK_SECRET")
            or ""
        ).strip()
        bot_user_id = (
            str(
                cfg.get("bot_user_id")
                or env.get("TALKOPS_CHANNELS_DISCORD_BOT_USER_ID")
                or env.get("TALKOPS_DISCORD_BOT_USER_ID")
                or ""
            ).strip()
            or None
        )
        api_base = str(
            cfg.get("api_base")
            or env.get("TALKOPS_CHANNELS_DISCORD_API_BASE")
            or DISCORD_API_BASE
        ).rstrip("/")
        verify_sig = cfg.get("verify_signatures")
        if verify_sig is None:
            verify_sig = env.get("TALKOPS_CHANNELS_DISCORD_VERIFY_SIGNATURES", "1")
        verify_signatures = str(verify_sig).strip().lower() not in {"0", "false", "no", "off"}

        raw_mode = str(
            cfg.get("transport_mode")
            or env.get("TALKOPS_CHANNELS_DISCORD_TRANSPORT_MODE")
            or "auto"
        ).strip().lower()
        transport_mode: TransportMode = (
            raw_mode if raw_mode in {"auto", "webhook", "websocket", "hybrid"} else "auto"
        )  # type: ignore[assignment]

        raw_gw = cfg.get("gateway_enabled")
        if raw_gw is None:
            raw_gw = env.get("TALKOPS_CHANNELS_DISCORD_GATEWAY_ENABLED")
        gateway_enabled = _parse_optional_bool(raw_gw)

        raw_role = str(
            cfg.get("transport_role")
            or env.get("TALKOPS_CHANNELS_DISCORD_TRANSPORT_ROLE")
            or "all"
        ).strip().lower()
        transport_role: Literal["all", "http_only", "gateway_worker"] = (
            raw_role if raw_role in {"all", "http_only", "gateway_worker"} else "all"
        )  # type: ignore[assignment]

        raw_auto_threads = cfg.get("auto_create_threads")
        if raw_auto_threads is None:
            raw_auto_threads = env.get("TALKOPS_CHANNELS_DISCORD_AUTO_CREATE_THREADS", "1")
        auto_create_threads = _parse_optional_bool(raw_auto_threads)
        if auto_create_threads is None:
            auto_create_threads = True

        raw_members = cfg.get("enable_members_intent")
        if raw_members is None:
            raw_members = env.get("TALKOPS_CHANNELS_DISCORD_MEMBERS_INTENT", "0")
        enable_members_intent = bool(_parse_optional_bool(raw_members))

        try:
            shard_id = max(
                0,
                int(cfg.get("shard_id") or env.get("TALKOPS_CHANNELS_DISCORD_SHARD_ID") or 0),
            )
        except ValueError:
            shard_id = 0
        try:
            num_shards = max(
                1,
                int(cfg.get("num_shards") or env.get("TALKOPS_CHANNELS_DISCORD_NUM_SHARDS") or 1),
            )
        except ValueError:
            num_shards = 1

        gateway_url = str(
            cfg.get("gateway_url") or env.get("TALKOPS_CHANNELS_DISCORD_GATEWAY_URL") or ""
        ).strip()
        persona_webhook_url = str(
            cfg.get("persona_webhook_url")
            or env.get("TALKOPS_CHANNELS_DISCORD_PERSONA_WEBHOOK_URL")
            or ""
        ).strip()

        exposure = exposure_from_mapping("discord", cfg, env)
        return cls(
            bot_token=bot_token,
            application_id=application_id,
            public_key=public_key,
            webhook_secret=webhook_secret,
            bot_user_id=bot_user_id,
            api_base=api_base,
            exposure=exposure,
            verify_signatures=verify_signatures,
            transport_mode=transport_mode,
            gateway_enabled=gateway_enabled,
            transport_role=transport_role,
            auto_create_threads=auto_create_threads,
            enable_members_intent=enable_members_intent,
            shard_id=shard_id,
            num_shards=num_shards,
            gateway_url=gateway_url,
            persona_webhook_url=persona_webhook_url,
        )


class DiscordWebhookChannel:
    """Discord channel adapter supporting HTTP Interactions, Event Webhooks, and Gateway WebSocket."""

    name = "discord"

    def __init__(
        self,
        config: DiscordChannelConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        state_store: ChannelStateStore | None = None,
        gateway_transport: ChannelTransport | None = None,
        ws_connector: WebSocketConnector | None = None,
    ) -> None:
        """Initialize `DiscordWebhookChannel` with `config` and optional transport/store overrides."""
        self.config = config
        self.capabilities = ChannelCapabilities(
            max_text_chars=DISCORD_MAX_TEXT_CHARS,
            edit_debounce_seconds=1.4,
            typing_refresh_seconds=8.0,
            supports_threads=True,
            auto_spawn_thread_on_mention=config.auto_create_threads,
            supports_streaming_edits=True,
            supports_reactions=True,
            supports_attachments=True,
            supports_ephemeral_interactions=True,
            interaction_token_ttl_seconds=900.0,
            supported_transports=("webhook", "websocket", "hybrid"),
        )
        self._client = http_client
        self._owns_client = http_client is None
        self._state_store = state_store
        self._ws_connector = ws_connector
        self._gateway: ChannelTransport | None = gateway_transport
        self._owns_gateway = gateway_transport is None
        self._bot_user_id: str | None = config.bot_user_id
        self._application_id: str = config.application_id
        self._message_handler: MessageHandler | None = None
        self._reaction_handler: ReactionHandler | None = None
        self._interaction_handler: InteractionHandler | None = None
        self._seen_events: OrderedDict[str, None] = OrderedDict()
        self._deferred_interactions: OrderedDict[str, _DeferredInteractionState] = OrderedDict()
        self._thread_meta_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def set_message_handler(self, handler: MessageHandler) -> None:
        """Register inbound message callback."""
        self._message_handler = handler

    def set_reaction_handler(self, handler: ReactionHandler) -> None:
        """Register inbound reaction callback."""
        self._reaction_handler = handler

    def set_interaction_handler(self, handler: InteractionHandler) -> None:
        """Register inbound interaction/command callback."""
        self._interaction_handler = handler

    def _should_start_gateway(self) -> bool:
        if not self._owns_gateway and self._gateway is not None:
            return True
        if not self.config.bot_token:
            return False
        mode = self.config.effective_transport_mode()
        if mode not in {"websocket", "hybrid"}:
            return False
        if self.config.gateway_enabled is True:
            return True
        if self.config.gateway_enabled is False:
            return False
        # Auto mode: enabled in live server runs, quiet during unit tests unless explicitly opted in
        return self._owns_client and "PYTEST_CURRENT_TEST" not in os.environ

    async def start(self) -> None:
        """Initialize HTTP client, discover bot identity, and start Gateway transport if active."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.request_timeout_seconds)

        if (
            not self._bot_user_id
            and self.config.bot_token
            and self._owns_client
            and "PYTEST_CURRENT_TEST" not in os.environ
        ):
            with contextlib.suppress(Exception):
                me = await self._rest_call("GET", "users/@me")
                if isinstance(me, Mapping) and me.get("id"):
                    self._bot_user_id = str(me["id"])
                    if not self._application_id:
                        self._application_id = str(me["id"])

        if self._should_start_gateway():
            if self._gateway is None:
                mode = self.config.effective_transport_mode()
                intents = compute_discord_intents(
                    message_content=True,
                    guild_members=self.config.enable_members_intent,
                )
                gw_cfg = DiscordGatewayConfig(
                    bot_token=self.config.bot_token,
                    api_base=self.config.api_base,
                    gateway_url=self.config.gateway_url,
                    intents=intents,
                    shard_id=self.config.shard_id,
                    num_shards=self.config.num_shards,
                    mode=mode,
                )
                self._gateway = DiscordGatewayTransport(
                    gw_cfg,
                    on_dispatch=self._on_gateway_dispatch,
                    http_client=self._client if not self._owns_client else None,
                    ws_connector=self._ws_connector,
                    state_store=self._state_store,
                )
            await self._gateway.start()

    async def stop(self) -> None:
        """Stop Gateway transport, cancel background tasks, and close owned HTTP client."""
        if self._gateway is not None:
            with contextlib.suppress(Exception):
                await self._gateway.stop()
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _dedup_event(self, event_id: str) -> bool:
        if not event_id:
            return True
        if event_id in self._seen_events:
            return False
        self._seen_events[event_id] = None
        while len(self._seen_events) > _SEEN_EVENT_CAP:
            self._seen_events.popitem(last=False)
        return True

    def _record_thread_meta(
        self,
        thread_id: str,
        *,
        parent_id: str | None = None,
        owner_id: str | None = None,
        channel_type: int = 11,
    ) -> None:
        tid = thread_id.strip()
        if not tid:
            return
        self._thread_meta_cache[tid] = {
            "id": tid,
            "parent_id": (parent_id or "").strip() or None,
            "owner_id": (owner_id or "").strip() or None,
            "type": channel_type,
        }
        self._thread_meta_cache.move_to_end(tid)
        while len(self._thread_meta_cache) > _THREAD_LOOKUP_CAP:
            self._thread_meta_cache.popitem(last=False)
        if isinstance(self._gateway, DiscordGatewayTransport):
            self._gateway.cache_thread(
                tid,
                parent_id=parent_id,
                owner_id=owner_id,
                channel_type=channel_type,
            )

    def _get_cached_thread_meta(self, channel_id: str) -> dict[str, Any] | None:
        if isinstance(self._gateway, DiscordGatewayTransport):
            cached = self._gateway.get_cached_thread(channel_id)
            if cached is not None:
                return cached
        info = self._thread_meta_cache.get(channel_id)
        if info is not None:
            self._thread_meta_cache.move_to_end(channel_id)
            return dict(info)
        return None

    def _is_known_thread(self, channel_id: str) -> bool:
        cached = self._get_cached_thread_meta(channel_id)
        return bool(cached and int(cached.get("type") or 0) in DISCORD_THREAD_CHANNEL_TYPES)

    async def _resolve_channel_thread_meta(self, channel_id: str) -> dict[str, Any] | None:
        """Return thread metadata `{id, parent_id, owner_id, type}` if `channel_id` is a Discord thread."""
        cached = self._get_cached_thread_meta(channel_id)
        if cached is not None:
            return cached if cached.get("type") in DISCORD_THREAD_CHANNEL_TYPES else None
        if not self.config.bot_token or not channel_id:
            return None
        try:
            info = await self._rest_call("GET", f"channels/{channel_id}")
            if isinstance(info, Mapping):
                ch_type = int(info.get("type") or 0)
                parent_id = str(info.get("parent_id") or "").strip() or None
                owner_id = str(info.get("owner_id") or "").strip() or None
                self._thread_meta_cache[channel_id] = {
                    "id": channel_id,
                    "parent_id": parent_id,
                    "owner_id": owner_id,
                    "type": ch_type,
                }
                while len(self._thread_meta_cache) > _THREAD_LOOKUP_CAP:
                    self._thread_meta_cache.popitem(last=False)
                if ch_type in DISCORD_THREAD_CHANNEL_TYPES:
                    return dict(self._thread_meta_cache[channel_id])
        except Exception:
            pass
        return None

    def _register_deferred_interaction(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        channel_id: str,
        application_id: str = "",
    ) -> _DeferredInteractionState:
        eff_app = application_id or self._application_id or self.config.application_id
        state = _DeferredInteractionState(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            application_id=eff_app,
            channel_id=channel_id,
        )
        self._deferred_interactions[interaction_id] = state
        self._deferred_interactions.move_to_end(interaction_id)
        while len(self._deferred_interactions) > _DEFERRED_INTERACTION_CAP:
            self._deferred_interactions.popitem(last=False)
        return state

    def _get_deferred_state(self, metadata: Mapping[str, Any] | None) -> _DeferredInteractionState | None:
        if not isinstance(metadata, Mapping):
            return None
        inter_id = str(metadata.get("interaction_id") or "").strip()
        if inter_id and inter_id in self._deferred_interactions:
            return self._deferred_interactions[inter_id]
        token = str(metadata.get("interaction_token") or "").strip()
        if inter_id and token and metadata.get("deferred_interaction"):
            return self._register_deferred_interaction(
                interaction_id=inter_id,
                interaction_token=token,
                channel_id=str(metadata.get("channel_id") or ""),
                application_id=str(metadata.get("application_id") or ""),
            )
        return None

    def transport_health(self) -> ChannelTransportHealth:
        """Return live inbound transport health."""
        if self._gateway is not None:
            return self._gateway.health()
        mode = self.config.effective_transport_mode()
        return ChannelTransportHealth(
            mode=mode,
            state="connected" if self.config.public_key or self.config.webhook_secret else "idle",
            connected=bool(self.config.public_key or self.config.webhook_secret),
            detail="HTTP Interactions / Event Webhook",
        )

    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> bool:
        """Verify Discord `X-Signature-Ed25519` or forwarded `X-Discord-Signature` HMAC."""
        if not self.config.verify_signatures:
            return True

        lowered = {k.lower(): v for k, v in headers.items()}
        sig_ed25519 = lowered.get("x-signature-ed25519", "").strip()
        timestamp = lowered.get("x-signature-timestamp", "").strip()

        if sig_ed25519 and timestamp and self.config.public_key:
            try:
                pub_bytes = bytes.fromhex(self.config.public_key)
                sig_bytes = bytes.fromhex(sig_ed25519)
                pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
                pub_key.verify(sig_bytes, timestamp.encode("utf-8") + body)
                return True
            except (ValueError, InvalidSignature, Exception):
                return False

        hmac_sig = lowered.get("x-discord-signature", "").strip()
        if hmac_sig and self.config.webhook_secret:
            if hmac_sig.startswith("sha256="):
                hmac_sig = hmac_sig[7:]
            expected = hmac.new(
                self.config.webhook_secret.encode("utf-8"),
                body,
                hashlib.sha256,
            ).hexdigest()
            return hmac.compare_digest(expected, hmac_sig)

        return False

    # ── Gateway Dispatch Ingress ──────────────────────────────────────

    async def _on_gateway_dispatch(self, event_name: str, data: dict[str, Any]) -> None:
        """Handle normalized Gateway dispatch events from `DiscordGatewayTransport`."""
        if event_name == "READY":
            raw_user = data.get("user")
            if isinstance(raw_user, Mapping) and raw_user.get("id"):
                self._bot_user_id = str(raw_user["id"])
            raw_app = data.get("application")
            if isinstance(raw_app, Mapping) and raw_app.get("id") and not self._application_id:
                self._application_id = str(raw_app["id"])
            return

        if event_name == "MESSAGE_CREATE":
            msg = await self._parse_message_event_async(data)
            if msg is not None and self._message_handler is not None:
                self._spawn(self._message_handler(msg))
            return

        if event_name == "MESSAGE_REACTION_ADD":
            react = self._parse_reaction_event(data)
            if react is not None and self._reaction_handler is not None:
                self._spawn(self._reaction_handler(react))
            return

        if event_name == "INTERACTION_CREATE":
            # In hybrid mode, Discord delivers interactions via HTTP Interactions Endpoint
            # (leaving Interactions Endpoint URL blank in Discord Portal delivers them here).
            await self.handle_gateway_interaction(data)

    async def handle_gateway_interaction(self, payload: Mapping[str, Any]) -> None:
        """Process an `INTERACTION_CREATE` received over Gateway WebSocket and ACK via REST callback."""
        interaction_id = str(payload.get("id") or "").strip()
        interaction_token = str(payload.get("token") or "").strip()
        if not interaction_id or not interaction_token:
            return
        response_body = await self._process_interaction_payload(payload)
        if response_body is None:
            return
        try:
            await self._rest_call(
                "POST",
                f"interactions/{interaction_id}/{interaction_token}/callback",
                payload=response_body,
                auth=False,
            )
        except Exception as exc:
            logger.warning("Failed sending Discord Gateway interaction callback: %s", exc)

    # ── Shared Interaction Processor (HTTP + Gateway) ─────────────────

    async def _process_interaction_payload(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        interaction_type = payload.get("type")

        if interaction_type == INTERACTION_PING:
            return {"type": RESPONSE_PONG}

        user_info = self._extract_interaction_user(payload)
        sender_id = user_info["id"]
        sender_name = user_info["username"]
        channel_id = str(payload.get("channel_id") or "").strip()
        guild_id = payload.get("guild_id")
        is_dm = guild_id is None
        interaction_id = str(payload.get("id") or "").strip()
        interaction_token = str(payload.get("token") or "").strip()
        app_id = str(payload.get("application_id") or self._application_id or self.config.application_id).strip()

        # Detect thread context if interaction occurred inside a thread
        thread_id: str | None = None
        parent_channel_id = channel_id
        raw_channel = payload.get("channel")
        if isinstance(raw_channel, Mapping) and int(raw_channel.get("type") or 0) in DISCORD_THREAD_CHANNEL_TYPES:
            thread_id = channel_id
            parent_channel_id = str(raw_channel.get("parent_id") or channel_id).strip() or channel_id
            self._record_thread_meta(
                thread_id,
                parent_id=parent_channel_id,
                owner_id=str(raw_channel.get("owner_id") or "") or None,
                channel_type=int(raw_channel.get("type") or 11),
            )
        else:
            cached_th = self._get_cached_thread_meta(channel_id)
            if cached_th and cached_th.get("type") in DISCORD_THREAD_CHANNEL_TYPES:
                thread_id = channel_id
                parent_channel_id = str(cached_th.get("parent_id") or channel_id)

        if interaction_type == INTERACTION_APPLICATION_COMMAND:
            raw_data = payload.get("data")
            data: Mapping[str, Any] = raw_data if isinstance(raw_data, Mapping) else {}
            cmd_name = str(data.get("name") or "").strip().lower()
            raw_options = data.get("options")
            options = raw_options if isinstance(raw_options, Sequence) else []
            arg_str = ""
            if options and isinstance(options[0], Mapping):
                arg_str = str(options[0].get("value") or "").strip()

            if cmd_name in COMMANDS_BY_NAME:
                interaction = ChannelInteraction(
                    channel="discord",
                    conversation_id=channel_id,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    interaction_kind="command",
                    thread_id=thread_id,
                    command_name=cmd_name,
                    command_args=arg_str,
                    is_dm=is_dm,
                    metadata={
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                        "parent_channel_id": parent_channel_id,
                        "interaction_id": interaction_id,
                        "interaction_token": interaction_token,
                        "application_id": app_id,
                    },
                )
                reply_text = "Done."
                if self._interaction_handler is not None:
                    res = await self._interaction_handler(interaction)
                    if res:
                        reply_text = res
                return {
                    "type": RESPONSE_CHANNEL_MESSAGE_WITH_SOURCE,
                    "data": {
                        "content": format_markdown_for_discord(reply_text)[:DISCORD_MAX_TEXT_CHARS],
                        "flags": 64,
                    },
                }

            prompt_text = arg_str or cmd_name
            if interaction_id and interaction_token:
                self._register_deferred_interaction(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    channel_id=channel_id,
                    application_id=app_id,
                )
            msg = ChannelMessage(
                channel="discord",
                conversation_id=channel_id,
                sender_id=sender_id,
                sender_name=sender_name,
                text=prompt_text,
                message_id=interaction_id,
                thread_id=thread_id,
                parent_channel_id=parent_channel_id,
                is_dm=is_dm,
                mentioned=True,
                metadata={
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "parent_channel_id": parent_channel_id,
                    "interaction_id": interaction_id,
                    "interaction_token": interaction_token,
                    "application_id": app_id,
                    "deferred_interaction": True,
                },
            )
            if self._message_handler is not None:
                self._spawn(self._message_handler(msg))
            return {"type": RESPONSE_DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE}

        if interaction_type == INTERACTION_MESSAGE_COMPONENT:
            raw_data = payload.get("data")
            data = raw_data if isinstance(raw_data, Mapping) else {}
            custom_id = str(data.get("custom_id") or "")
            raw_msg_obj = payload.get("message")
            msg_obj: Mapping[str, Any] = raw_msg_obj if isinstance(raw_msg_obj, Mapping) else {}
            message_id = str(msg_obj.get("id") or "").strip() or None

            decision: str | None = None
            approval_id: str | None = None
            if custom_id.startswith("talkops_approve:"):
                decision = "approve"
                approval_id = custom_id.split(":", 1)[1]
            elif custom_id.startswith("talkops_reject:"):
                decision = "reject"
                approval_id = custom_id.split(":", 1)[1]

            if decision in {"approve", "reject"}:
                interaction = ChannelInteraction(
                    channel="discord",
                    conversation_id=channel_id,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    interaction_kind="approval",
                    message_id=message_id,
                    thread_id=thread_id,
                    action_id=custom_id,
                    decision=decision,  # type: ignore[arg-type]
                    approval_id=approval_id,
                    is_dm=is_dm,
                    metadata={
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                        "interaction_id": interaction_id,
                        "interaction_token": interaction_token,
                        "application_id": app_id,
                    },
                )
                if self._interaction_handler is not None:
                    self._spawn(self._interaction_handler(interaction))
                return {"type": RESPONSE_DEFERRED_UPDATE_MESSAGE}

            if custom_id.startswith(("talkops_dispute:", "talkops_confirm:")):
                prefix, lg_tid = custom_id.split(":", 1)
                verdict = "rejected" if prefix == "talkops_dispute" else "helped"
                interaction = ChannelInteraction(
                    channel="discord",
                    conversation_id=channel_id,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    interaction_kind="feedback",
                    message_id=message_id,
                    thread_id=thread_id,
                    action_id=custom_id,
                    is_dm=is_dm,
                    metadata={
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                        "parent_channel_id": parent_channel_id,
                        "lg_thread_id": lg_tid.strip(),
                        "verdict": verdict,
                        "interaction_id": interaction_id,
                        "interaction_token": interaction_token,
                        "application_id": app_id,
                        "original_embeds": msg_obj.get("embeds")
                        if isinstance(msg_obj.get("embeds"), list)
                        else None,
                        "original_components": msg_obj.get("components")
                        if isinstance(msg_obj.get("components"), list)
                        else None,
                        "original_content": str(msg_obj.get("content") or ""),
                    },
                )
                ack = f"Feedback (`{verdict}`) recorded."
                if self._interaction_handler is not None:
                    res = await self._interaction_handler(interaction)
                    if res:
                        ack = res
                return {
                    "type": RESPONSE_CHANNEL_MESSAGE_WITH_SOURCE,
                    "data": {"content": ack[:DISCORD_MAX_TEXT_CHARS], "flags": 64},
                }

        return None

    async def handle_webhook_interaction(
        self,
        headers: Mapping[str, str],
        body: bytes,
        query_params: Mapping[str, str] | None = None,
    ) -> WebhookHTTPResponse:
        """Handle `POST /channels/discord/interactions` (Discord HTTP Interactions Endpoint)."""
        del query_params
        lowered = {k.lower(): v for k, v in headers.items()}
        if not self.verify_signature(lowered, body):
            return WebhookHTTPResponse(
                status_code=401,
                body={"ok": False, "error": "invalid_discord_signature"},
            )

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return WebhookHTTPResponse(status_code=400, body={"ok": False, "error": "invalid_json"})

        if not isinstance(payload, Mapping):
            return WebhookHTTPResponse(
                status_code=400, body={"ok": False, "error": "invalid_payload"}
            )

        response_body = await self._process_interaction_payload(payload)
        if response_body is not None:
            return WebhookHTTPResponse(status_code=200, body=response_body)
        return WebhookHTTPResponse(status_code=200, body={"ok": True})

    async def handle_webhook_event(
        self,
        headers: Mapping[str, str],
        body: bytes,
        query_params: Mapping[str, str] | None = None,
    ) -> WebhookHTTPResponse:
        """Handle `POST /channels/discord/events` (Forwarded Gateway events or Interactions)."""
        lowered = {k.lower(): v for k, v in headers.items()}
        if not self.verify_signature(lowered, body):
            return WebhookHTTPResponse(
                status_code=401,
                body={"ok": False, "error": "invalid_discord_signature"},
            )

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return WebhookHTTPResponse(status_code=400, body={"ok": False, "error": "invalid_json"})

        if not isinstance(payload, Mapping):
            return WebhookHTTPResponse(
                status_code=400, body={"ok": False, "error": "invalid_payload"}
            )

        if isinstance(payload.get("type"), int):
            return await self.handle_webhook_interaction(headers, body, query_params)

        event_type = str(payload.get("t") or payload.get("type") or "").upper()
        raw_d = payload.get("d")
        event_data: Mapping[str, Any] = raw_d if isinstance(raw_d, Mapping) else payload

        if event_type in {"MESSAGE_CREATE", "MESSAGE"}:
            msg = await self._parse_message_event_async(event_data)
            if msg is not None and self._message_handler is not None:
                self._spawn(self._message_handler(msg))
        elif event_type in {"MESSAGE_REACTION_ADD", "REACTION_ADD"}:
            react = self._parse_reaction_event(event_data)
            if react is not None and self._reaction_handler is not None:
                self._spawn(self._reaction_handler(react))

        return WebhookHTTPResponse(status_code=200, body={"ok": True})

    def _extract_interaction_user(self, payload: Mapping[str, Any]) -> dict[str, str]:
        member = payload.get("member")
        if isinstance(member, Mapping) and isinstance(member.get("user"), Mapping):
            u = member["user"]
            return {
                "id": str(u.get("id") or "").strip(),
                "username": str(u.get("username") or u.get("global_name") or "").strip(),
            }
        user = payload.get("user")
        if isinstance(user, Mapping):
            return {
                "id": str(user.get("id") or "").strip(),
                "username": str(user.get("username") or u_global(user) or "").strip(),
            }
        return {"id": "", "username": ""}

    def _extract_attachments(self, data: Mapping[str, Any]) -> tuple[ChannelMedia, ...]:
        raw_atts = data.get("attachments")
        if not isinstance(raw_atts, Sequence):
            return ()
        media_items: list[ChannelMedia] = []
        for att in raw_atts:
            if not isinstance(att, Mapping):
                continue
            url = str(att.get("url") or att.get("proxy_url") or "").strip()
            if not url:
                continue
            mime = str(att.get("content_type") or "application/octet-stream")
            kind: Literal["image", "audio", "video", "document"] = "document"
            if mime.startswith("image/"):
                kind = "image"
            elif mime.startswith("audio/"):
                kind = "audio"
            elif mime.startswith("video/"):
                kind = "video"
            media_items.append(
                ChannelMedia(
                    media_type=kind,
                    url=url,
                    mime_type=mime,
                    filename=str(att.get("filename") or att.get("id") or "attachment"),
                    size_bytes=int(att["size"]) if isinstance(att.get("size"), int) else None,
                    metadata={"attachment_id": att.get("id")},
                )
            )
        return tuple(media_items)

    def _parse_message_event(
        self,
        data: Mapping[str, Any],
        *,
        resolved_thread_meta: Mapping[str, Any] | None = None,
    ) -> ChannelMessage | None:
        # Strict echo-loop prevention: drop bots, outbound webhooks, and our own user ID
        if data.get("webhook_id") is not None:
            return None
        raw_author = data.get("author")
        author: Mapping[str, Any] = raw_author if isinstance(raw_author, Mapping) else {}
        if author.get("bot"):
            return None
        sender_id = str(author.get("id") or "").strip()
        if not sender_id or (self._bot_user_id and sender_id == self._bot_user_id):
            return None
        sender_name = str(author.get("username") or author.get("global_name") or "").strip()
        channel_id = str(data.get("channel_id") or "").strip()
        message_id = str(data.get("id") or "").strip()
        if not channel_id or not message_id:
            return None
        if not self._dedup_event(f"msg:{channel_id}:{message_id}"):
            return None

        guild_id = data.get("guild_id")
        is_dm = guild_id is None
        raw_content = str(data.get("content") or "")

        mentioned = bool(data.get("mentioned"))
        mentions = data.get("mentions")
        if isinstance(mentions, Sequence):
            for m in mentions:
                if isinstance(m, Mapping):
                    if not self._bot_user_id or str(m.get("id") or "") == self._bot_user_id:
                        mentioned = True
        if self._bot_user_id and (
            f"<@{self._bot_user_id}>" in raw_content or f"<@!{self._bot_user_id}>" in raw_content
        ):
            mentioned = True

        # Also treat direct replies to the bot's own messages as implicit mentions
        raw_ref = data.get("referenced_message")
        if isinstance(raw_ref, Mapping):
            ref_author = raw_ref.get("author")
            if isinstance(ref_author, Mapping) and self._bot_user_id:
                if str(ref_author.get("id") or "") == self._bot_user_id:
                    mentioned = True

        cleaned = _DISCORD_MENTION_RE.sub("", raw_content).strip()
        if not mentioned and self.config.exposure.matches_mention_pattern(cleaned):
            mentioned = True

        # Thread detection: explicit field (forwarded bridge) OR Gateway/REST thread metadata (type 10/11/12)
        explicit_thread_id = str(data.get("thread_id") or "").strip() or None
        explicit_parent_id = str(data.get("parent_channel_id") or "").strip() or None
        thread_meta = resolved_thread_meta or self._get_cached_thread_meta(channel_id)

        if explicit_thread_id:
            thread_id: str | None = explicit_thread_id
            parent_channel_id = explicit_parent_id or channel_id
            conversation_id = explicit_thread_id
        elif thread_meta and int(thread_meta.get("type") or 0) in DISCORD_THREAD_CHANNEL_TYPES:
            thread_id = channel_id
            parent_channel_id = str(thread_meta.get("parent_id") or channel_id).strip() or channel_id
            conversation_id = channel_id
        else:
            thread_id = None
            parent_channel_id = channel_id
            conversation_id = channel_id

        media_items = self._extract_attachments(data)

        return ChannelMessage(
            channel="discord",
            conversation_id=conversation_id,
            sender_id=sender_id,
            sender_name=sender_name,
            text=cleaned,
            message_id=message_id,
            thread_id=thread_id,
            parent_channel_id=parent_channel_id,
            is_dm=is_dm,
            mentioned=mentioned,
            media=media_items,
            metadata={
                "guild_id": guild_id,
                "channel_id": channel_id,
                "parent_channel_id": parent_channel_id,
            },
        )

    async def _parse_message_event_async(self, data: Mapping[str, Any]) -> ChannelMessage | None:
        """Parse `MESSAGE_CREATE`, resolving cold thread parent metadata via REST when needed."""
        channel_id = str(data.get("channel_id") or "").strip()
        resolved_meta: Mapping[str, Any] | None = None
        if (
            channel_id
            and data.get("guild_id") is not None
            and not data.get("thread_id")
            and self._get_cached_thread_meta(channel_id) is None
        ):
            resolved_meta = await self._resolve_channel_thread_meta(channel_id)
        return self._parse_message_event(data, resolved_thread_meta=resolved_meta)

    def _parse_reaction_event(self, data: Mapping[str, Any]) -> ChannelReaction | None:
        sender_id = str(data.get("user_id") or "").strip()
        if not sender_id or (self._bot_user_id and sender_id == self._bot_user_id):
            return None
        sender_name = ""
        member = data.get("member")
        if isinstance(member, Mapping) and isinstance(member.get("user"), Mapping):
            if member["user"].get("bot"):
                return None
            sender_name = str(
                member["user"].get("username") or member["user"].get("global_name") or ""
            ).strip()
        channel_id = str(data.get("channel_id") or "").strip()
        message_id = str(data.get("message_id") or "").strip()
        raw_emoji = data.get("emoji")
        emoji_obj: Mapping[str, Any] = raw_emoji if isinstance(raw_emoji, Mapping) else {}
        emoji_name = str(emoji_obj.get("name") or "").strip()
        if not channel_id or not message_id or not emoji_name:
            return None
        cached_th = self._get_cached_thread_meta(channel_id)
        parent_channel_id = (
            str(cached_th.get("parent_id") or channel_id)
            if isinstance(cached_th, Mapping)
            else channel_id
        )
        return ChannelReaction(
            channel="discord",
            conversation_id=channel_id,
            sender_id=sender_id,
            message_id=message_id,
            emoji=emoji_name,
            metadata={
                "guild_id": data.get("guild_id"),
                "sender_name": sender_name,
                "parent_channel_id": parent_channel_id,
            },
        )

    # ── REST API v10 Client with HTTP 429 Backoff ─────────────────────

    async def _rest_call(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | Sequence[Any] | None = None,
        params: Mapping[str, str] | None = None,
        auth: bool = True,
        files: Any = None,
        data: Mapping[str, Any] | None = None,
        full_url: str | None = None,
    ) -> Any:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.request_timeout_seconds)
        url = full_url or f"{self.config.api_base}/{path.lstrip('/')}"
        headers: dict[str, str] = {}
        if auth and self.config.bot_token:
            headers["Authorization"] = f"Bot {self.config.bot_token}"
        if files is None:
            headers["Content-Type"] = "application/json"

        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            req_kwargs: dict[str, Any] = {"headers": headers, "params": params}
            if files is not None:
                req_kwargs["files"] = files
                if data is not None:
                    req_kwargs["data"] = dict(data)
            elif payload is not None:
                req_kwargs["json"] = (
                    dict(payload) if isinstance(payload, Mapping) else list(payload)
                )

            resp = await self._client.request(method, url, **req_kwargs)
            if resp.status_code == 429 and attempt < _MAX_RATE_LIMIT_RETRIES:
                retry_after = 1.0
                with contextlib.suppress(Exception):
                    body_429 = resp.json()
                    if isinstance(body_429, Mapping) and body_429.get("retry_after") is not None:
                        retry_after = float(body_429["retry_after"])
                    elif resp.headers.get("retry-after"):
                        retry_after = float(resp.headers["retry-after"])
                await asyncio.sleep(max(0.05, min(30.0, retry_after)))
                continue

            resp.raise_for_status()
            if not resp.content:
                return {}
            return resp.json()
        return {}

    async def open_dm(self, user_id: str) -> str | None:
        """Open (or retrieve) a Discord DM channel with `user_id` via `POST /users/@me/channels`."""
        uid = user_id.strip()
        if not uid or not self.config.bot_token:
            return None
        try:
            res = await self._rest_call("POST", "users/@me/channels", payload={"recipient_id": uid})
            if isinstance(res, Mapping) and res.get("id"):
                return str(res["id"])
        except Exception:
            pass
        return None

    async def ensure_conversation_thread(self, message: ChannelMessage) -> ChannelMessage:
        """Auto-spawn a Discord thread for an authorized top-level guild `@mention` message."""
        if (
            not self.config.auto_create_threads
            or message.is_dm
            or message.thread_id
            or not message.message_id
            or not self.config.bot_token
        ):
            return message
        # Do not call /messages/{id}/threads on a slash command interaction ID
        if (message.metadata or {}).get("interaction_token"):
            return message

        parent_id = message.parent_channel_id or message.conversation_id
        snippet = " ".join(message.text.split())[:72] or "TalkOps Session"
        thread_name = f"TalkOps: {snippet}"[:95]
        try:
            res = await self._rest_call(
                "POST",
                f"channels/{parent_id}/messages/{message.message_id}/threads",
                payload={"name": thread_name},
            )
            if isinstance(res, Mapping) and res.get("id"):
                new_tid = str(res["id"])
                self._record_thread_meta(
                    new_tid,
                    parent_id=parent_id,
                    owner_id=self._bot_user_id,
                    channel_type=int(res.get("type") or 11),
                )
                merged_meta = dict(message.metadata or {})
                merged_meta["parent_channel_id"] = parent_id
                merged_meta["newly_created_thread"] = True
                return replace(
                    message,
                    conversation_id=new_tid,
                    thread_id=new_tid,
                    parent_channel_id=parent_id,
                    metadata=merged_meta,
                )
        except Exception as exc:
            logger.debug("Discord auto-thread creation skipped for %s/%s: %s", parent_id, message.message_id, exc)
        return message

    async def has_bot_participated_in_thread(
        self,
        conversation_id: str,
        thread_id: str | None = None,
    ) -> bool:
        """Return `True` if this bot created or replied inside the Discord thread."""
        target = thread_id or conversation_id
        if not target or not self.config.bot_token:
            return False
        cached = self._get_cached_thread_meta(target)
        if cached and self._bot_user_id and cached.get("owner_id") == self._bot_user_id:
            return True
        try:
            messages = await self._rest_call(
                "GET",
                f"channels/{target}/messages",
                params={"limit": "20"},
            )
            if not isinstance(messages, Sequence):
                return False
            for m in messages:
                if not isinstance(m, Mapping):
                    continue
                author = m.get("author")
                if isinstance(author, Mapping):
                    aid = str(author.get("id") or "")
                    if self._bot_user_id and aid == self._bot_user_id:
                        return True
                    if not self._bot_user_id and author.get("bot"):
                        return True
                if self._bot_user_id and f"<@{self._bot_user_id}>" in str(m.get("content") or ""):
                    return True
            return False
        except Exception:
            return False

    def _build_multipart_files(
        self,
        body: Mapping[str, Any],
        media: Sequence[ChannelMedia],
    ) -> tuple[dict[str, str], list[tuple[str, tuple[str, bytes, str]]]] | None:
        attached: list[tuple[str, tuple[str, bytes, str]]] = []
        attachment_meta: list[dict[str, Any]] = []
        for idx, item in enumerate(media):
            try:
                if item.content_bytes is not None:
                    file_bytes = item.content_bytes
                    filename = item.filename or f"attachment-{idx}.md"
                elif item.path is not None:
                    resolved = resolve_bounded_media_path(item.path)
                    file_bytes = resolved.read_bytes()
                    filename = item.filename or resolved.name
                else:
                    continue
                mime = item.mime_type or "application/octet-stream"
                file_idx = len(attached)
                attached.append((f"files[{file_idx}]", (filename, file_bytes, mime)))
                att_desc = str(item.metadata.get("description") or "").strip()
                meta_entry: dict[str, Any] = {"id": file_idx, "filename": filename}
                if att_desc:
                    meta_entry["description"] = att_desc[:1024]
                attachment_meta.append(meta_entry)
            except Exception as exc:
                logger.warning("Skipping invalid Discord media attachment %s: %s", item.path or item.filename, exc)
        if not attached:
            return None
        payload = dict(body)
        if attachment_meta:
            payload["attachments"] = attachment_meta
        return {"payload_json": json.dumps(payload)}, attached

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
        """Send 2000-char chunked Markdown, embeds, buttons, and file attachments to Discord.

        Automatically routes deferred slash command responses (`type: 5`) to
        `PATCH /webhooks/{app_id}/{interaction_token}/messages/@original` within Discord's
        safe window, falling back cleanly to the Bot REST API for jobs > 13 minutes.
        """
        target_channel = thread_id or conversation_id
        allowlist = self.config.exposure.mention_allowlist_user_ids or None
        suppress_content = (
            isinstance(blocks_or_cards, Mapping)
            and bool(blocks_or_cards.get("suppress_content"))
            and bool(blocks_or_cards.get("embeds"))
        )
        if suppress_content:
            chunks = [""]
        else:
            formatted = format_markdown_for_discord(text, mention_allowlist=allowlist)
            chunks = chunk_text(formatted, max_chars=DISCORD_MAX_TEXT_CHARS) or [formatted]
        last_msg_id: str | None = None
        last_resp: dict[str, Any] = {}

        deferred = self._get_deferred_state(metadata)
        persona_username = str((metadata or {}).get("persona_username") or "").strip()
        persona_avatar = str((metadata or {}).get("persona_avatar_url") or "").strip()

        for idx, chunk in enumerate(chunks):
            is_last = idx == len(chunks) - 1
            body: dict[str, Any] = {"content": "" if suppress_content else chunk}
            if is_last and isinstance(blocks_or_cards, Mapping):
                if "embeds" in blocks_or_cards:
                    body["embeds"] = blocks_or_cards["embeds"]
                if "components" in blocks_or_cards:
                    body["components"] = blocks_or_cards["components"]

            multipart = self._build_multipart_files(body, media) if (is_last and media) else None

            # Route 1: Active deferred slash-command interaction (`@original` + follow-ups)
            if deferred is not None and deferred.application_id and not deferred.handed_off:
                if deferred.is_within_safe_window():
                    if not deferred.original_edited:
                        method = "PATCH"
                        path = f"webhooks/{deferred.application_id}/{deferred.interaction_token}/messages/@original"
                    else:
                        method = "POST"
                        path = f"webhooks/{deferred.application_id}/{deferred.interaction_token}"
                    if multipart is not None:
                        form_data, file_list = multipart
                        res = await self._rest_call(
                            method, path, data=form_data, files=file_list, auth=False
                        )
                    else:
                        res = await self._rest_call(method, path, payload=body, auth=False)
                    if isinstance(res, dict):
                        last_resp = res
                        msg_id = str(res.get("id") or "")
                        if msg_id:
                            last_msg_id = msg_id
                            if not deferred.original_edited:
                                deferred.message_id = msg_id
                    deferred.original_edited = True
                    continue
                else:
                    # Exceeded 13m safe window: notify `@original` once and hand off to Bot REST API
                    deferred.handed_off = True
                    with contextlib.suppress(Exception):
                        await self._rest_call(
                            "PATCH",
                            f"webhooks/{deferred.application_id}/{deferred.interaction_token}/messages/@original",
                            payload={
                                "content": "⏳ Long-running task exceeded 13 minutes — posting final report in thread."
                            },
                            auth=False,
                        )

            # Route 2: Optional multi-agent persona webhook (non-interactive messages only)
            if (
                self.config.persona_webhook_url
                and persona_username
                and "components" not in body
                and multipart is None
            ):
                wh_body = dict(body, username=persona_username[:80])
                if persona_avatar:
                    wh_body["avatar_url"] = persona_avatar
                wh_params: dict[str, str] = {"wait": "true"}
                if thread_id:
                    wh_params["thread_id"] = thread_id
                res = await self._rest_call(
                    "POST",
                    "",
                    full_url=self.config.persona_webhook_url,
                    params=wh_params,
                    payload=wh_body,
                    auth=False,
                )
            elif multipart is not None:
                form_data, file_list = multipart
                res = await self._rest_call(
                    "POST",
                    f"channels/{target_channel}/messages",
                    data=form_data,
                    files=file_list,
                )
            else:
                res = await self._rest_call(
                    "POST",
                    f"channels/{target_channel}/messages",
                    payload=body,
                )

            if isinstance(res, dict):
                last_resp = res
                last_msg_id = str(res.get("id") or "") or last_msg_id

        return SendResult(
            channel="discord",
            conversation_id=conversation_id,
            message_id=last_msg_id,
            thread_id=target_channel,
            raw_response=last_resp,
        )

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
        """Send a reply in a Discord thread channel."""
        return await self.send(
            conversation_id,
            text,
            thread_id=thread_id,
            media=media,
            blocks_or_cards=blocks_or_cards,
            metadata=metadata,
        )

    async def update_message(
        self,
        conversation_id: str,
        message_id: str,
        text: str,
        *,
        blocks_or_cards: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Edit an existing Discord message (or deferred `@original` interaction response)."""
        suppress_content = (
            isinstance(blocks_or_cards, Mapping)
            and bool(blocks_or_cards.get("suppress_content"))
            and bool(blocks_or_cards.get("embeds"))
        )
        formatted = "" if suppress_content else format_markdown_for_discord(text)[:DISCORD_MAX_TEXT_CHARS]
        body: dict[str, Any] = {"content": formatted}
        if isinstance(blocks_or_cards, Mapping):
            if "embeds" in blocks_or_cards:
                body["embeds"] = blocks_or_cards["embeds"]
            if "components" in blocks_or_cards:
                body["components"] = blocks_or_cards["components"]
        elif isinstance(blocks_or_cards, list) and len(blocks_or_cards) == 0:
            # Explicit empty list (e.g. resolved HITL approval card) clears buttons and embeds
            body["embeds"] = []
            body["components"] = []

        deferred = self._get_deferred_state(metadata)
        if (
            deferred is not None
            and deferred.application_id
            and not deferred.handed_off
            and deferred.is_within_safe_window()
            and (deferred.message_id is None or deferred.message_id == message_id)
        ):
            res = await self._rest_call(
                "PATCH",
                f"webhooks/{deferred.application_id}/{deferred.interaction_token}/messages/@original",
                payload=body,
                auth=False,
            )
            deferred.original_edited = True
            if isinstance(res, Mapping) and res.get("id"):
                deferred.message_id = str(res["id"])
        else:
            res = await self._rest_call(
                "PATCH",
                f"channels/{conversation_id}/messages/{message_id}",
                payload=body,
            )

        return SendResult(
            channel="discord",
            conversation_id=conversation_id,
            message_id=message_id,
            raw_response=res if isinstance(res, dict) else {},
        )

    def resolve_thread_conversation_id(
        self,
        conversation_id: str,
        sent: SendResult,
    ) -> str:
        """Return the canonical Discord thread/channel conversation key for a sent message."""
        if sent.thread_id and sent.thread_id != conversation_id:
            return sent.thread_id
        return conversation_id

    async def create_incident_thread(
        self,
        conversation_id: str,
        *,
        title: str,
        initial_text: str,
        blocks_or_cards: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Create an incident root message with Rich Embed and spawn a dedicated Discord thread."""
        meta = dict(metadata or {})
        allowlist = self.config.exposure.mention_allowlist_user_ids or None
        embed_payload = blocks_or_cards
        if embed_payload is None:
            embed_payload = build_discord_incident_embed(
                title=title,
                summary=str(meta.get("summary") or initial_text),
                provider=str(meta.get("provider") or ""),
                severity=str(meta.get("severity") or ""),
                service_name=str(meta.get("service_name") or ""),
                source_url=str(meta.get("source_url") or ""),
                lg_thread_id=str(meta.get("lg_thread_id") or ""),
                investigation_state=str(meta.get("investigation_state") or "investigating"),  # type: ignore[arg-type]
                rca_preview=str(meta.get("rca_preview") or ""),
                mention_allowlist=allowlist,
            )
        fallback_text = (
            ""
            if isinstance(embed_payload, Mapping)
            and embed_payload.get("suppress_content")
            and embed_payload.get("embeds")
            else f"**{title}**\n\n{initial_text}".strip()
        )
        sent = await self.send(
            conversation_id,
            fallback_text,
            blocks_or_cards=embed_payload,
            metadata=metadata,
        )

        # Spawn a dedicated Discord thread anchored to the root incident message
        if (
            sent.message_id
            and self.config.auto_create_threads
            and self.config.bot_token
            and not self._is_known_thread(conversation_id)
        ):
            try:
                th_res = await self._rest_call(
                    "POST",
                    f"channels/{conversation_id}/messages/{sent.message_id}/threads",
                    payload={"name": (title or "Incident")[:95]},
                )
                if isinstance(th_res, Mapping) and th_res.get("id"):
                    new_tid = str(th_res["id"])
                    self._record_thread_meta(
                        new_tid,
                        parent_id=conversation_id,
                        owner_id=self._bot_user_id,
                        channel_type=int(th_res.get("type") or 11),
                    )
                    return SendResult(
                        channel="discord",
                        conversation_id=conversation_id,
                        message_id=sent.message_id,
                        thread_id=new_tid,
                        raw_response=sent.raw_response,
                    )
            except Exception as exc:
                logger.debug(
                    "Discord incident thread spawn failed for %s/%s: %s",
                    conversation_id,
                    sent.message_id,
                    exc,
                )
        return sent

    async def send_approval_prompt(
        self,
        spec: ApprovalCardSpec,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Render and send a Discord Rich Embed + ActionRow Buttons HITL approval card."""
        # If triggered from a deferred slash command that hasn't edited `@original` yet,
        # clear the "Bot is thinking..." spinner before posting the durable Bot REST card.
        deferred = self._get_deferred_state(metadata)
        if (
            deferred is not None
            and deferred.application_id
            and not deferred.original_edited
            and deferred.is_within_safe_window()
        ):
            deferred.original_edited = True
            with contextlib.suppress(Exception):
                await self._rest_call(
                    "PATCH",
                    f"webhooks/{deferred.application_id}/{deferred.interaction_token}/messages/@original",
                    payload={"content": "🛡️ Tool execution paused — awaiting operator approval below."},
                    auth=False,
                )

        embeds, components = build_discord_approval_components(spec)
        # Always send approval cards via Bot REST API so buttons stay active beyond interaction token TTL
        safe_meta = {
            k: v
            for k, v in dict(metadata or {}).items()
            if k not in {"interaction_id", "interaction_token", "deferred_interaction"}
        }
        return await self.send(
            spec.conversation_id,
            spec.prompt_text or spec.title,
            thread_id=spec.thread_id,
            blocks_or_cards={"embeds": embeds, "components": components},
            metadata=safe_meta,
        )

    async def add_reaction(
        self,
        conversation_id: str,
        message_id: str,
        emoji: str,
    ) -> None:
        """Add an emoji reaction (e.g. `eyes`, `white_check_mark`) to a Discord message."""
        if not self.config.bot_token or not message_id or not conversation_id:
            return
        # Skip interaction IDs that are not channel messages
        if message_id in self._deferred_interactions:
            return
        norm_emoji = _SLACK_TO_DISCORD_EMOJI.get(emoji.strip(":").lower(), emoji.strip(":"))
        encoded = urllib.parse.quote(norm_emoji, safe="")
        with contextlib.suppress(Exception):
            await self._rest_call(
                "PUT",
                f"channels/{conversation_id}/messages/{message_id}/reactions/{encoded}/@me",
            )

    async def remove_reaction(
        self,
        conversation_id: str,
        message_id: str,
        emoji: str,
    ) -> None:
        """Remove an emoji reaction from a Discord message."""
        if not self.config.bot_token or not message_id or not conversation_id:
            return
        if message_id in self._deferred_interactions:
            return
        norm_emoji = _SLACK_TO_DISCORD_EMOJI.get(emoji.strip(":").lower(), emoji.strip(":"))
        encoded = urllib.parse.quote(norm_emoji, safe="")
        with contextlib.suppress(Exception):
            await self._rest_call(
                "DELETE",
                f"channels/{conversation_id}/messages/{message_id}/reactions/{encoded}/@me",
            )

    async def send_typing(
        self,
        conversation_id: str,
        *,
        thread_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Emit a Discord typing indicator via `POST /channels/{id}/typing`."""
        del metadata
        target = thread_id or conversation_id
        try:
            await self._rest_call("POST", f"channels/{target}/typing")
        except Exception:
            pass

    async def sync_commands(
        self,
        commands: Sequence[ChatCommand],
        *,
        guild_id: str | None = None,
    ) -> dict[str, Any]:
        """Bulk-overwrite Discord application slash commands globally or for a specific guild."""
        app_id = self._application_id or self.config.application_id
        if not app_id or not self.config.bot_token:
            raise ValueError("Discord application_id and bot_token are required to sync commands.")
        payload = build_discord_slash_commands_payload(commands, include_ask=True)
        path = (
            f"applications/{app_id}/guilds/{guild_id}/commands"
            if guild_id
            else f"applications/{app_id}/commands"
        )
        res = await self._rest_call("PUT", path, payload=payload)
        registered = len(res) if isinstance(res, Sequence) else len(payload)
        return {
            "scope": f"guild:{guild_id}" if guild_id else "global",
            "application_id": app_id,
            "registered_count": registered,
            "commands": [c["name"] for c in payload],
        }

    async def fetch_thread_context(
        self,
        conversation_id: str,
        thread_id: str | None = None,
        *,
        limit: int = 40,
        metadata: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Fetch recent chronological messages in a Discord channel/thread."""
        del metadata
        target = thread_id or conversation_id
        if not target or not self.config.bot_token:
            return None
        try:
            messages = await self._rest_call(
                "GET",
                f"channels/{target}/messages",
                params={"limit": str(limit)},
            )
            if not isinstance(messages, Sequence) or len(messages) <= 1:
                return None
            ordered = list(reversed(messages))[:-1]
            lines: list[str] = []
            for m in ordered:
                if not isinstance(m, Mapping):
                    continue
                content = str(m.get("content") or "").strip()
                if not content or contains_oauth_callback_url(content):
                    continue
                raw_author_obj = m.get("author")
                author_obj: Mapping[str, Any] = (
                    raw_author_obj if isinstance(raw_author_obj, Mapping) else {}
                )
                author = str(author_obj.get("username") or author_obj.get("id") or "user")
                is_bot = bool(author_obj.get("bot"))
                author_id = str(author_obj.get("id") or "")
                if self._bot_user_id and author_id == self._bot_user_id:
                    if "_Working on your request..._" in content:
                        continue
                    lines.append(f"[Assistant (TalkOps)]: {html.escape(content[:1000])}")
                elif is_bot:
                    lines.append(
                        f'<input-message sender="{html.escape(author, quote=True)}" sender_type="bot">{html.escape(content[:1000])}</input-message>'
                    )
                else:
                    lines.append(f"[{author}]: {html.escape(content[:1000])}")
            return "\n".join(lines) if lines else None
        except Exception:
            return None

    def status(self) -> ChannelStatus:
        """Return Discord adapter configuration, transport mode, and live health status."""
        mode = self.config.effective_transport_mode()
        t_health = self.transport_health()
        configured = bool(
            self.config.bot_token
            and (
                mode in {"websocket", "hybrid"}
                or self.config.public_key
                or self.config.webhook_secret
            )
        )
        return ChannelStatus(
            channel="discord",
            configured=configured,
            webhook_events_path="/channels/discord/events",
            webhook_interactions_path="/channels/discord/interactions",
            exposure_mode=str(self.config.exposure.mode),
            detail=f"Discord ({mode}: Gateway v10 + HTTP Interactions)",
            metadata={
                "has_bot_token": bool(self.config.bot_token),
                "has_public_key": bool(self.config.public_key),
                "application_id": self._application_id or self.config.application_id,
                "bot_user_id": self._bot_user_id,
                "transport_mode": mode,
                "auto_create_threads": self.config.auto_create_threads,
                "transport": t_health.to_dict(),
            },
        )


def u_global(user: Mapping[str, Any]) -> Any:
    """Extract `global_name` from a Discord user mapping."""
    return user.get("global_name")

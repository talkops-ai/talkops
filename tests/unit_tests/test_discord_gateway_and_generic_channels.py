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

"""Unit tests for generic multi-channel extensibility and Discord Gateway v10 integration.

Covers:
1. Generic `ChannelAdapterRegistry` & custom third-party provider registration without core edits
2. Declarative `ChannelCapabilities` driving chunking, debounce, and typing refresh intervals
3. Durable `PersistentChannelStateStore` (SQLite WAL): restart-safe HITL approvals, persisted
   incident root refs, and multi-replica distributed leader leases
4. `ChannelHub` `multitask_strategy` (`enqueue` vs `interrupt`) and `feedback` -> `HookEventBus`
   -> MACE `ReflectionFeedbackObserver` integration
5. `DiscordGatewayTransport`: OP 10 Hello, OP 2 Identify (38401 intents), OP 1/11 Heartbeat,
   OP 7 Reconnect -> OP 6 Resume on `resume_gateway_url`, fatal close code `4014` circuit breaker,
   and `GUILD_CREATE` warm thread cache
6. `DiscordWebhookChannel`: recommended default transport resolution (`auto` -> `hybrid`/`websocket`),
   strict echo-loop prevention (`bot`, `webhook_id`, self user), post-auth auto-thread spawning,
   incident thread creation, unmentioned thread follow-ups, Gateway `INTERACTION_CREATE` callbacks,
   deferred `/ask` `@original` edits + 13m Bot REST fallback, button clearing on approval resolve,
   HTTP 429 retry backoff, inbound/outbound media safety, and `/channels/discord/sync-commands`.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from talkops.addons.builtin.channels import ChannelsAddon, ChannelsAddonSettings
from talkops.addons.gateway import HookEventBus
from talkops.addons.manager import AddonManager
from talkops.channels.approvals import PendingApprovalRegistry
from talkops.channels.commands import CHAT_COMMANDS
from talkops.channels.formatting import ChannelExposure, ExposureMode
from talkops.channels.hub import ChannelHub
from talkops.channels.interfaces import (
    ApprovalCardSpec,
    ChannelCapabilities,
    ChannelInteraction,
    ChannelMedia,
    ChannelMessage,
    ChannelStatus,
    SendResult,
    WebhookHTTPResponse,
)
from talkops.channels.media import ChannelMediaError, download_discord_attachment_safely
from talkops.channels.pairing import PairingStore
from talkops.channels.providers.discord import (
    DiscordChannelConfig,
    DiscordWebhookChannel,
)
from talkops.channels.providers.discord_gateway import (
    DEFAULT_DISCORD_INTENTS,
    OP_DISPATCH,
    OP_HEARTBEAT_ACK,
    OP_HELLO,
    OP_IDENTIFY,
    OP_RECONNECT,
    OP_RESUME,
    DiscordGatewayConfig,
    DiscordGatewayTransport,
)
from talkops.channels.registry import (
    clear_registered_channel_adapter_factories,
    register_channel_adapter_factory,
)
from talkops.channels.state_store import (
    InMemoryChannelStateStore,
    PersistentChannelStateStore,
)
from talkops.channels.stream_sink import ChannelStreamSink
from talkops.observers.mace_observers import ReflectionFeedbackObserver
from talkops.storage.sqlite_store import SqliteOperationalStore
from talkops.webhooks.formatting import build_discord_rca_embed


class _FakeGateway:
    def __init__(self, *, reply_text: str = "Discord SRE analysis complete.", delay: float = 0.0) -> None:
        self.reply_text = reply_text
        self.delay = delay
        self.calls: list[dict[str, Any]] = []
        self.event_bus = HookEventBus()

    async def stream_run(
        self,
        input_payload: Any,
        *,
        thread_id: str,
        source_addon: str = "channels",
        context: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[tuple[tuple[str, ...], str, Any]]:
        self.calls.append({"input": input_payload, "thread_id": thread_id, "context": dict(context or {})})
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        yield ((), "messages", ({"content": self.reply_text}, {}))


class _FakeCloseError(Exception):
    def __init__(self, code: int, reason: str = "") -> None:
        super().__init__(f"WebSocket closed with code {code}: {reason}")
        self.code = code


class _FakeWebSocket:
    def __init__(
        self,
        incoming_frames: list[dict[str, Any]],
        *,
        close_exc_at_end: Exception | None = None,
    ) -> None:
        self._incoming = list(incoming_frames)
        self._close_exc = close_exc_at_end
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.close_code: int | None = None

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del reason
        self.closed = True
        self.close_code = code

    def __aiter__(self) -> _FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        if self.closed:
            raise StopAsyncIteration
        if self._incoming:
            await asyncio.sleep(0)
            return json.dumps(self._incoming.pop(0))
        if self._close_exc is not None:
            exc = self._close_exc
            self._close_exc = None
            raise exc
        raise StopAsyncIteration


class _CustomTelegramLikeAdapter:
    """Minimal third-party adapter proving `ChannelAdapterRegistry` extensibility without core edits."""

    name = "telegram"
    capabilities = ChannelCapabilities(
        max_text_chars=4096,
        edit_debounce_seconds=2.0,
        typing_refresh_seconds=0.0,
        supported_transports=("webhook", "polling"),
    )

    def __init__(self, cfg: Mapping[str, Any], env: Mapping[str, str]) -> None:
        self.cfg = dict(cfg)
        self.env = dict(env)
        self.sent: list[dict[str, Any]] = []
        self._msg_handler: Any = None
        self._react_handler: Any = None
        self._inter_handler: Any = None

    def set_message_handler(self, handler: Any) -> None:
        self._msg_handler = handler

    def set_reaction_handler(self, handler: Any) -> None:
        self._react_handler = handler

    def set_interaction_handler(self, handler: Any) -> None:
        self._inter_handler = handler

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> bool:
        del headers, body
        return True

    async def handle_webhook_event(
        self, headers: Mapping[str, str], body: bytes, query_params: Mapping[str, str] | None = None
    ) -> WebhookHTTPResponse:
        del headers, body, query_params
        return WebhookHTTPResponse(status_code=200, body={"ok": True, "provider": "telegram"})

    async def handle_webhook_interaction(
        self, headers: Mapping[str, str], body: bytes, query_params: Mapping[str, str] | None = None
    ) -> WebhookHTTPResponse:
        del headers, body, query_params
        return WebhookHTTPResponse(status_code=200, body={"ok": True})

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
        del media, blocks_or_cards, metadata
        self.sent.append({"conversation_id": conversation_id, "text": text, "thread_id": thread_id})
        return SendResult(channel="telegram", conversation_id=conversation_id, message_id="tg_1")

    async def send_in_thread(
        self, conversation_id: str, thread_id: str, text: str, **kwargs: Any
    ) -> SendResult:
        return await self.send(conversation_id, text, thread_id=thread_id, **kwargs)

    async def update_message(
        self, conversation_id: str, message_id: str, text: str, **kwargs: Any
    ) -> SendResult:
        del kwargs
        return SendResult(channel="telegram", conversation_id=conversation_id, message_id=message_id)

    async def send_approval_prompt(self, spec: ApprovalCardSpec, **kwargs: Any) -> SendResult:
        del kwargs
        return await self.send(spec.conversation_id, spec.title)

    async def send_typing(self, conversation_id: str, **kwargs: Any) -> None:
        del conversation_id, kwargs

    async def fetch_thread_context(self, conversation_id: str, **kwargs: Any) -> str | None:
        del conversation_id, kwargs
        return None

    def status(self) -> ChannelStatus:
        return ChannelStatus(
            channel="telegram",
            configured=True,
            webhook_events_path="/channels/telegram/events",
            webhook_interactions_path="/channels/telegram/interactions",
            detail="Custom Telegram Adapter",
        )


def test_generic_adapter_registry_and_capabilities_without_core_changes(tmp_path: Path) -> None:
    """Verify third-party adapters plug into `ChannelsAddon` and `ChannelStreamSink` respects capabilities."""
    try:
        register_channel_adapter_factory(
            "telegram",
            lambda cfg, env, _store=None: _CustomTelegramLikeAdapter(cfg, env),
        )
        addon = ChannelsAddon(environ={})
        addon.bind_config(
            ChannelsAddonSettings(
                providers={"telegram": {"token": "tg-token"}},
            )
        )
        hub = addon.hub
        assert set(hub.adapters.keys()) >= {"slack", "teams", "discord", "telegram"}

        tg_adapter = hub.get_adapter("telegram")
        assert tg_adapter is not None
        sink = ChannelStreamSink(
            tg_adapter,
            conversation_id="chat-1",
            lg_thread_id="lg-1",
            edit_debounce_seconds=1.0,
        )
        # Capability `edit_debounce_seconds=2.0` overrides the lower 1.0s global default
        assert sink.edit_debounce_seconds == 2.0

        # Built-in Slack/Teams capabilities remain intact
        slack_adapter = hub.get_adapter("slack")
        teams_adapter = hub.get_adapter("teams")
        discord_adapter = hub.get_adapter("discord")
        assert getattr(slack_adapter, "capabilities").typing_refresh_seconds == 0.0
        assert getattr(teams_adapter, "capabilities").typing_refresh_seconds == 4.0
        assert getattr(discord_adapter, "capabilities").typing_refresh_seconds == 8.0
        assert getattr(discord_adapter, "capabilities").edit_debounce_seconds == 1.4
    finally:
        clear_registered_channel_adapter_factories()


def test_persistent_channel_state_store_approvals_incidents_and_leases(tmp_path: Path) -> None:
    """Verify SQLite WAL `PersistentChannelStateStore` survives restarts and enforces leader leases."""
    db_file = tmp_path / "channel_state.sqlite"
    store_a = PersistentChannelStateStore(backend="sqlite", sqlite_path=db_file)
    reg_a = PendingApprovalRegistry(state_store=store_a)
    pending = reg_a.register(
        channel="discord",
        conversation_id="TH_999",
        thread_id="TH_999",
        lg_thread_id="lg-restart-test",
        run_id="run-1",
        requester_id="U_SRE",
        interrupt_value={"action_requests": [{"name": "kubectl_rollout_restart", "args": {"deploy": "api"}}]},
    )
    reg_a.bind_message_id(pending.approval_id, "MSG_APPR_1")

    # Simulate container restart / second replica reading from shared store
    store_b = PersistentChannelStateStore(backend="sqlite", sqlite_path=db_file)
    reg_b = PendingApprovalRegistry(state_store=store_b)
    recovered = reg_b.get_by_approval_id(pending.approval_id)
    assert recovered is not None
    assert recovered.lg_thread_id == "lg-restart-test"
    assert recovered.prompt_message_id == "MSG_APPR_1"
    assert recovered.tool_calls[0].name == "kubectl_rollout_restart"

    # Match reaction on Replica B and confirm pop removes from durable DB
    matched = reg_b.match_reaction(channel="discord", message_id="MSG_APPR_1", emoji="👍")
    assert matched is not None and matched[1] == "approve"
    reg_b.pop(pending.approval_id)
    assert PendingApprovalRegistry(state_store=store_b).get_by_approval_id(pending.approval_id) is None

    # Incident ref persistence
    store_a.put_incident_ref("lg-inc-1", "discord", "CH_WAR", {"thread_conv": "TH_WAR", "message_id": "M_ROOT"})
    assert store_b.get_incident_ref("lg-inc-1", "discord", "CH_WAR")["thread_conv"] == "TH_WAR"

    # Distributed transport lease mutual exclusion & expiry handover
    now = 1_000_000.0
    assert store_a.try_acquire_lease("discord:gateway:shard:0:1", "pod-1", ttl_seconds=45.0, now=now) is True
    assert store_b.try_acquire_lease("discord:gateway:shard:0:1", "pod-2", ttl_seconds=45.0, now=now + 10.0) is False
    assert store_a.renew_lease("discord:gateway:shard:0:1", "pod-1", ttl_seconds=45.0, now=now + 20.0) is True
    assert store_b.try_acquire_lease("discord:gateway:shard:0:1", "pod-2", ttl_seconds=45.0, now=now + 70.0) is True
    assert store_a.get_lease_owner("discord:gateway:shard:0:1", now=now + 80.0) == "pod-2"
    store_b.release_lease("discord:gateway:shard:0:1", "pod-2")
    assert store_a.get_lease_owner("discord:gateway:shard:0:1", now=now + 80.0) is None


def test_discord_transport_mode_recommended_defaults() -> None:
    """Verify `auto` picks `hybrid` with public_key, `websocket` without public_key, `webhook` when disabled."""
    hybrid_cfg = DiscordChannelConfig.from_mapping(
        {}, {"TALKOPS_DISCORD_BOT_TOKEN": "tok", "TALKOPS_DISCORD_PUBLIC_KEY": "ab" * 32}
    )
    assert hybrid_cfg.effective_transport_mode() == "hybrid"

    ws_cfg = DiscordChannelConfig.from_mapping({}, {"TALKOPS_DISCORD_BOT_TOKEN": "tok"})
    assert ws_cfg.effective_transport_mode() == "websocket"

    wh_cfg = DiscordChannelConfig.from_mapping(
        {},
        {
            "TALKOPS_DISCORD_BOT_TOKEN": "tok",
            "TALKOPS_DISCORD_PUBLIC_KEY": "ab" * 32,
            "TALKOPS_CHANNELS_DISCORD_GATEWAY_ENABLED": "0",
        },
    )
    assert wh_cfg.effective_transport_mode() == "webhook"

    api_only_cfg = DiscordChannelConfig.from_mapping(
        {},
        {
            "TALKOPS_DISCORD_BOT_TOKEN": "tok",
            "TALKOPS_CHANNELS_DISCORD_TRANSPORT_ROLE": "http_only",
        },
    )
    assert api_only_cfg.effective_transport_mode() == "webhook"


@pytest.mark.asyncio
async def test_discord_gateway_handshake_resume_thread_cache_and_fatal_4014() -> None:
    """Verify Discord Gateway v10 Identify, OP 7 Resume on `resume_gateway_url`, thread cache, and fatal 4014."""
    dispatched: list[tuple[str, dict[str, Any]]] = []
    connect_urls: list[str] = []
    sockets: list[_FakeWebSocket] = [
        _FakeWebSocket(
            [
                {"op": OP_HELLO, "d": {"heartbeat_interval": 60_000}},
                {
                    "op": OP_DISPATCH,
                    "s": 1,
                    "t": "READY",
                    "d": {
                        "session_id": "sess_abc123",
                        "resume_gateway_url": "wss://resume-edge.discord.gg",
                        "user": {"id": "BOT_777"},
                    },
                },
                {
                    "op": OP_DISPATCH,
                    "s": 2,
                    "t": "GUILD_CREATE",
                    "d": {
                        "id": "G_1",
                        "threads": [
                            {"id": "TH_55", "parent_id": "CH_OPS", "owner_id": "BOT_777", "type": 11, "name": "Inc"}
                        ],
                    },
                },
                {"op": OP_HEARTBEAT_ACK, "d": None},
                {"op": OP_RECONNECT, "d": None},
            ]
        ),
        _FakeWebSocket(
            [
                {"op": OP_HELLO, "d": {"heartbeat_interval": 60_000}},
                {"op": OP_DISPATCH, "s": 3, "t": "RESUMED", "d": {}},
                {
                    "op": OP_DISPATCH,
                    "s": 4,
                    "t": "MESSAGE_CREATE",
                    "d": {
                        "id": "M_2",
                        "channel_id": "TH_55",
                        "guild_id": "G_1",
                        "author": {"id": "U_1", "username": "alice"},
                        "content": "Any pod restarts?",
                    },
                },
            ],
            close_exc_at_end=_FakeCloseError(4014, "Disallowed intents"),
        ),
    ]
    socket_iter = iter(sockets)

    @asynccontextmanager
    async def _fake_connector(url: str, **_kwargs: Any) -> AsyncIterator[_FakeWebSocket]:
        connect_urls.append(url)
        yield next(socket_iter)

    async def _on_dispatch(event_name: str, data: dict[str, Any]) -> None:
        dispatched.append((event_name, data))

    gw = DiscordGatewayTransport(
        DiscordGatewayConfig(
            bot_token="bot-token-123",
            gateway_url="wss://gateway.discord.gg",
            initial_backoff_seconds=0.05,
            jitter_initial_heartbeat=False,
        ),
        on_dispatch=_on_dispatch,
        ws_connector=_fake_connector,
        state_store=InMemoryChannelStateStore(),
    )
    await gw.start()
    for _ in range(80):
        if gw.health().state == "error":
            break
        await asyncio.sleep(0.02)
    await gw.stop()

    # Session 1 connected to primary gateway and sent OP 2 Identify with exact intents
    assert connect_urls[0] == "wss://gateway.discord.gg?v=10&encoding=json"
    identify_frames = [f for f in sockets[0].sent if f.get("op") == OP_IDENTIFY]
    assert len(identify_frames) == 1
    assert identify_frames[0]["d"]["intents"] == DEFAULT_DISCORD_INTENTS
    assert identify_frames[0]["d"]["intents"] == 46593

    # Session 2 reconnected to READY.resume_gateway_url and sent OP 6 Resume with session_id + seq=2
    assert connect_urls[1] == "wss://resume-edge.discord.gg?v=10&encoding=json"
    resume_frames = [f for f in sockets[1].sent if f.get("op") == OP_RESUME]
    assert len(resume_frames) == 1
    assert resume_frames[0]["d"]["session_id"] == "sess_abc123"
    assert resume_frames[0]["d"]["seq"] == 2

    # Warm thread cache populated from GUILD_CREATE
    th = gw.get_cached_thread("TH_55")
    assert th is not None and th["parent_id"] == "CH_OPS" and th["owner_id"] == "BOT_777"

    # Fatal 4014 halted without retrying a 3rd time and surfaced clear diagnostic
    assert len(connect_urls) == 2
    assert gw.health().fatal_code == 4014
    assert any(ev == "MESSAGE_CREATE" for ev, _ in dispatched)


@pytest.mark.asyncio
async def test_discord_two_way_threads_loop_guard_deferred_original_429_and_sync(tmp_path: Path) -> None:
    """Verify full Discord two-way flow, auto-threads, deferred `@original`, 429 retry, and command sync."""
    api_calls: list[dict[str, Any]] = []
    rate_limit_once = {"triggered": False}

    def _mock_discord_api(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: Any = {}
        if request.content and "application/json" in request.headers.get("content-type", ""):
            body = json.loads(request.content.decode("utf-8"))
        api_calls.append(
            {
                "method": request.method,
                "path": path,
                "auth": request.headers.get("authorization"),
                "content_type": request.headers.get("content-type", ""),
                "body": body,
            }
        )
        # Simulate one HTTP 429 rate-limit on first channel message post
        if path.endswith("/channels/TH_AUTO_1/messages") and request.method == "POST" and not rate_limit_once["triggered"]:
            rate_limit_once["triggered"] = True
            return httpx.Response(429, json={"retry_after": 0.05, "message": "You are being rate limited."})
        if path.endswith("/threads") and request.method == "POST":
            return httpx.Response(201, json={"id": "TH_AUTO_1", "type": 11, "parent_id": "CH_OPS"})
        if path.endswith("/typing") or "/reactions/" in path:
            return httpx.Response(204)
        if path.endswith("/commands") and request.method == "PUT":
            return httpx.Response(200, json=body if isinstance(body, list) else [])
        return httpx.Response(200, json={"id": f"msg_{len(api_calls)}"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_discord_api))
    gateway = _FakeGateway(reply_text="Checkout pods look healthy.")
    store = InMemoryChannelStateStore()
    hub = ChannelHub(
        gateway=gateway,
        pairing_store=PairingStore(tmp_path / "pairing.json"),
        state_store=store,
        event_bus=gateway.event_bus,
        verbosity=1,
    )
    cfg = DiscordChannelConfig(
        bot_token="disc-tok",
        application_id="APP_42",
        bot_user_id="BOT_42",
        transport_mode="websocket",
        gateway_enabled=False,  # driven manually via `_on_gateway_dispatch`
        auto_create_threads=True,
        exposure=ChannelExposure(
            mode=ExposureMode.ALLOWLIST,
            operator_id="U_ADMIN",
            allowed_chats=frozenset({"CH_OPS"}),
        ),
    )
    adapter = DiscordWebhookChannel(cfg, http_client=http_client, state_store=store)
    hub.register_adapter(adapter)
    await hub.start()

    # 1. Loop prevention: bot messages and webhook messages are silently dropped
    assert adapter._parse_message_event(
        {"id": "M_BOT", "channel_id": "CH_OPS", "guild_id": "G1", "author": {"id": "BOT_42", "bot": True}, "content": "hi"}
    ) is None
    assert adapter._parse_message_event(
        {"id": "M_WH", "channel_id": "CH_OPS", "guild_id": "G1", "webhook_id": "WH_9", "author": {"id": "U_WH"}, "content": "hi"}
    ) is None

    # 2. Unauthorized user mentioning bot in non-allowlisted channel MUST NOT create a thread
    await adapter._on_gateway_dispatch(
        "MESSAGE_CREATE",
        {
            "id": "M_UNAUTH",
            "channel_id": "CH_RANDOM",
            "guild_id": "G1",
            "author": {"id": "U_STRANGER", "username": "stranger"},
            "content": "<@BOT_42> hack prod",
            "mentions": [{"id": "BOT_42"}],
        },
    )
    await asyncio.sleep(0.05)
    assert not any(c["path"].endswith("/threads") for c in api_calls)

    # 3. Authorized `@mention` in allowlisted channel auto-spawns thread `TH_AUTO_1` and survives 429 retry
    await adapter._on_gateway_dispatch(
        "MESSAGE_CREATE",
        {
            "id": "M_AUTH_1",
            "channel_id": "CH_OPS",
            "guild_id": "G1",
            "author": {"id": "U_ADMIN", "username": "sre_lead"},
            "content": "<@BOT_42> check checkout-api",
            "mentions": [{"id": "BOT_42"}],
        },
    )
    await asyncio.sleep(0.15)
    assert any(c["path"] == "/api/v10/channels/CH_OPS/messages/M_AUTH_1/threads" for c in api_calls)
    assert rate_limit_once["triggered"] is True
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["context"]["conversation_id"] == "TH_AUTO_1"

    # 4. Unmentioned follow-up reply inside `TH_AUTO_1` routes straight into the same LangGraph thread!
    await adapter._on_gateway_dispatch(
        "MESSAGE_CREATE",
        {
            "id": "M_AUTH_2",
            "channel_id": "TH_AUTO_1",
            "guild_id": "G1",
            "author": {"id": "U_ADMIN", "username": "sre_lead"},
            "content": "What about orders-rds?",
            "mentions": [],
        },
    )
    await asyncio.sleep(0.05)
    assert len(gateway.calls) == 2
    assert gateway.calls[1]["thread_id"] == gateway.calls[0]["thread_id"]

    # 5. Gateway `INTERACTION_CREATE` for `/ask` -> ACKs with type 5 AND edits `@original` with NO Bot Auth header!
    api_calls.clear()
    await adapter._on_gateway_dispatch(
        "INTERACTION_CREATE",
        {
            "type": 2,
            "id": "INTER_ASK_1",
            "token": "TOK_ASK_1",
            "application_id": "APP_42",
            "channel_id": "CH_OPS",
            "guild_id": "G1",
            "member": {"user": {"id": "U_ADMIN", "username": "sre_lead"}},
            "data": {"name": "ask", "options": [{"name": "prompt", "value": "Check latency"}]},
        },
    )
    await asyncio.sleep(0.08)
    cb_calls = [c for c in api_calls if "/interactions/INTER_ASK_1/TOK_ASK_1/callback" in c["path"]]
    assert len(cb_calls) == 1
    assert cb_calls[0]["body"] == {"type": 5}
    assert cb_calls[0]["auth"] is None

    orig_edits = [c for c in api_calls if "/webhooks/APP_42/TOK_ASK_1/messages/@original" in c["path"]]
    assert len(orig_edits) == 1
    assert orig_edits[0]["method"] == "PATCH"
    assert orig_edits[0]["auth"] is None
    assert "Checkout pods look healthy" in orig_edits[0]["body"]["content"]

    # 6. Long-running deferred interaction (> 13m) hands off cleanly to Bot REST API
    api_calls.clear()
    state = adapter._register_deferred_interaction(
        interaction_id="INTER_OLD",
        interaction_token="TOK_OLD",
        channel_id="CH_OPS",
        application_id="APP_42",
    )
    state.created_at = time.monotonic() - 800.0
    await adapter.send(
        "CH_OPS",
        "Final 15-minute RCA report",
        metadata={"interaction_id": "INTER_OLD", "interaction_token": "TOK_OLD", "deferred_interaction": True},
    )
    assert any(c["path"].endswith("/webhooks/APP_42/TOK_OLD/messages/@original") for c in api_calls)
    bot_fallback = [c for c in api_calls if c["path"].endswith("/channels/CH_OPS/messages")]
    assert len(bot_fallback) == 1 and bot_fallback[0]["auth"] == "Bot disc-tok"

    # 7. Resolving an HITL card with `blocks_or_cards=[]` explicitly clears components & embeds
    api_calls.clear()
    await adapter.update_message("TH_AUTO_1", "MSG_CARD", "✅ Approved", blocks_or_cards=[])
    assert api_calls[0]["body"]["components"] == []
    assert api_calls[0]["body"]["embeds"] == []

    # 8. Outbound multipart attachment upload + inbound CDN host SSRF validation
    sample_file = tmp_path / "postmortem.md"
    sample_file.write_text("# Postmortem\nAll clear.", encoding="utf-8")
    api_calls.clear()
    await adapter.send(
        "TH_AUTO_1",
        "Attached postmortem",
        media=(ChannelMedia(media_type="document", path=sample_file, filename="postmortem.md"),),
    )
    assert "multipart/form-data" in api_calls[0]["content_type"]

    with pytest.raises(ChannelMediaError, match="Refusing to download Discord file"):
        await download_discord_attachment_safely("https://evil.example.com/leak.png", dest_dir=tmp_path)

    # 9. Incident thread creation spawns a real Discord thread
    api_calls.clear()
    inc_res = await adapter.create_incident_thread(
        "CH_OPS",
        title="[ALERTMANAGER] Checkout 5xx",
        initial_text="Investigating...",
    )
    assert inc_res.thread_id == "TH_AUTO_1"
    assert any(c["path"].endswith("/threads") for c in api_calls)

    # 10. Slash command sync via adapter & HTTP endpoint
    sync_res = await adapter.sync_commands(CHAT_COMMANDS, guild_id="G1")
    assert sync_res["scope"] == "guild:G1"
    assert "ask" in sync_res["commands"] and "status" in sync_res["commands"]

    # 11. Stage 2 RCA card includes feedback buttons (`talkops_dispute:` / `talkops_confirm:`)
    rca_embed = build_discord_rca_embed(
        "### Root Cause\nDB pool exhausted.",
        provider="alertmanager",
        lg_thread_id="lg-rca-1",
    )
    btn_ids = [b.get("custom_id") for b in rca_embed["components"][0]["components"]]
    assert "talkops_confirm:lg-rca-1" in btn_ids
    assert "talkops_dispute:lg-rca-1" in btn_ids

    # 12. Clicking `talkops_dispute` publishes `feedback.rejected` onto HookEventBus (MACE observer)
    received_feedback: list[Any] = []
    gateway.event_bus.subscribe("feedback.*", lambda ev: received_feedback.append(ev))
    api_calls.clear()
    await adapter._on_gateway_dispatch(
        "INTERACTION_CREATE",
        {
            "type": 3,
            "id": "INTER_DISP_1",
            "token": "TOK_DISP_1",
            "channel_id": "TH_AUTO_1",
            "guild_id": "G1",
            "member": {"user": {"id": "U_ADMIN", "username": "sre_lead"}},
            "data": {"custom_id": "talkops_dispute:lg-rca-1"},
        },
    )
    assert len(received_feedback) == 1
    assert received_feedback[0].event_type == "feedback.rejected"
    assert received_feedback[0].thread_id == "lg-rca-1"

    await hub.stop()


@pytest.mark.asyncio
async def test_channels_addon_sync_commands_route_and_hub_enqueue_strategy(tmp_path: Path) -> None:
    """Verify `POST /channels/discord/sync-commands` route and `multitask_strategy='enqueue'`."""
    op_store = SqliteOperationalStore(tmp_path / "mace.sqlite")
    await op_store.setup()
    bus = HookEventBus()
    reflection = ReflectionFeedbackObserver(op_store)
    bus.subscribe("*", reflection.on_event)

    gateway = _FakeGateway(reply_text="Done", delay=0.05)
    hub = ChannelHub(
        gateway=gateway,
        pairing_store=PairingStore(tmp_path / "pairing.json"),
        event_bus=bus,
        verbosity=1,
        multitask_strategy="enqueue",
    )

    def _mock_sync(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else []
        return httpx.Response(200, json=body if isinstance(body, list) else {"id": "1"})

    adapter = DiscordWebhookChannel(
        DiscordChannelConfig(
            bot_token="disc-tok",
            application_id="APP_SYNC",
            gateway_enabled=False,
            exposure=ChannelExposure(mode=ExposureMode.OPEN, open_ack=True),
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_mock_sync)),
    )
    hub.register_adapter(adapter)

    # Verify `enqueue` strategy runs concurrent messages sequentially without cancelling the first
    t1 = asyncio.create_task(
        hub.handle_inbound_message(
            ChannelMessage(channel="discord", conversation_id="TH_Q", thread_id="TH_Q", sender_id="U_1", text="turn 1", is_dm=True)
        )
    )
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(
        hub.handle_inbound_message(
            ChannelMessage(channel="discord", conversation_id="TH_Q", thread_id="TH_Q", sender_id="U_1", text="turn 2", is_dm=True)
        )
    )
    await asyncio.gather(t1, t2)
    assert len(gateway.calls) == 2

    # Verify feedback interaction flows through ChannelHub -> HookEventBus -> ReflectionFeedbackObserver
    ack = await hub.handle_inbound_interaction(
        ChannelInteraction(
            channel="discord",
            conversation_id="TH_1",
            sender_id="U_1",
            interaction_kind="feedback",
            action_id="talkops_dispute:lg-99",
            reason="Nightly ETL cron causes expected CPU burst",
            metadata={"lg_thread_id": "lg-99", "service_name": "batch-worker", "verdict": "corrected"},
        )
    )
    assert ack is not None and "Feedback" in ack
    assert len(reflection._staged_counts) == 1

    mgr = AddonManager(
        profile="default",
        project_root=tmp_path,
        event_bus=bus,
        extra_addons=[ChannelsAddon(hub=hub)],
    )
    app = mgr.create_asgi_app()
    with TestClient(app) as client:
        res = client.post("/channels/discord/sync-commands", json={"guild_id": "GUILD_1"})
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["result"]["scope"] == "guild:GUILD_1"
        assert "ask" in data["result"]["commands"]

    await op_store.aclose()

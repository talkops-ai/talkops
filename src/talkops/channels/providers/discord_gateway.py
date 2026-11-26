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

"""Discord API v10 Gateway WebSocket transport (`DiscordGatewayTransport`).

Built on the generic `ReconnectingWebSocketTransport` base in `talkops.channels.transports`,
adding Discord-specific protocol handling:
- `GET /gateway/bot` discovery & `session_start_limit` awareness
- `OP 10 Hello` -> jittered `OP 1 Heartbeat` + `OP 11 Heartbeat ACK` zombie detection
- `OP 2 Identify` (with intents bitmask & optional sharding `[shard_id, num_shards]`)
- `OP 6 Resume` targeting Discord v10's `READY.resume_gateway_url`
- `OP 7 Reconnect` & `OP 9 Invalid Session` handling
- Fatal non-retryable close codes (`4004`, `4010`–`4014`) with actionable diagnostics
- Warm thread channel cache (`GUILD_CREATE.threads`, `THREAD_CREATE`, `THREAD_UPDATE`, `THREAD_DELETE`)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from talkops.channels.state_store import ChannelStateStore
from talkops.channels.transports import (
    FatalTransportError,
    ReconnectingWebSocketTransport,
    WebSocketConnector,
)
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

# Discord Gateway v10 Intents
INTENT_GUILDS = 1 << 0
INTENT_GUILD_MEMBERS = 1 << 1
INTENT_GUILD_MESSAGES = 1 << 9
INTENT_GUILD_MESSAGE_REACTIONS = 1 << 10
INTENT_DIRECT_MESSAGES = 1 << 12
INTENT_DIRECT_MESSAGE_REACTIONS = 1 << 13
INTENT_MESSAGE_CONTENT = 1 << 15

DEFAULT_DISCORD_INTENTS = (
    INTENT_GUILDS
    | INTENT_GUILD_MESSAGES
    | INTENT_GUILD_MESSAGE_REACTIONS
    | INTENT_DIRECT_MESSAGES
    | INTENT_DIRECT_MESSAGE_REACTIONS
    | INTENT_MESSAGE_CONTENT
)

# Discord Thread Channel Types (API v10)
DISCORD_THREAD_CHANNEL_TYPES = frozenset({10, 11, 12})

# Discord Gateway Opcodes
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# Non-recoverable Discord Gateway close codes (per Discord API v10 docs)
FATAL_CLOSE_MESSAGES: dict[int, str] = {
    4004: "Discord Gateway authentication failed (4004): verify DISCORD_BOT_TOKEN is valid.",
    4010: "Discord Gateway invalid shard configuration (4010): verify shard_id and num_shards.",
    4011: "Discord Gateway sharding required (4011): increase num_shards for this bot.",
    4012: "Discord Gateway invalid API version (4012).",
    4013: "Discord Gateway invalid intents bitmask (4013).",
    4014: (
        "Discord Gateway disallowed privileged intent (4014): toggle ON 'MESSAGE CONTENT INTENT' "
        "(and 'SERVER MEMBERS INTENT' if enabled) in Discord Developer Portal -> Bot -> Privileged Gateway Intents."
    ),
}

_THREAD_CACHE_CAP = 4096

GatewayDispatchCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


def compute_discord_intents(
    *,
    message_content: bool = True,
    guild_members: bool = False,
) -> int:
    """Compute the Discord Gateway v10 intents bitmask."""
    mask = (
        INTENT_GUILDS
        | INTENT_GUILD_MESSAGES
        | INTENT_GUILD_MESSAGE_REACTIONS
        | INTENT_DIRECT_MESSAGES
        | INTENT_DIRECT_MESSAGE_REACTIONS
    )
    if message_content:
        mask |= INTENT_MESSAGE_CONTENT
    if guild_members:
        mask |= INTENT_GUILD_MEMBERS
    return mask


@dataclass(frozen=True, slots=True)
class DiscordGatewayConfig:
    """Configuration for `DiscordGatewayTransport`."""

    bot_token: str
    api_base: str = "https://discord.com/api/v10"
    gateway_url: str = ""
    intents: int = DEFAULT_DISCORD_INTENTS
    shard_id: int = 0
    num_shards: int = 1
    mode: str = "hybrid"
    lease_ttl_seconds: float = 45.0
    lease_renew_interval_seconds: float = 15.0
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 60.0
    jitter_initial_heartbeat: bool = True


class DiscordGatewayTransport(ReconnectingWebSocketTransport):
    """Persistent Discord API v10 Gateway WebSocket client."""

    def __init__(
        self,
        config: DiscordGatewayConfig,
        *,
        on_dispatch: GatewayDispatchCallback,
        http_client: httpx.AsyncClient | None = None,
        ws_connector: WebSocketConnector | None = None,
        state_store: ChannelStateStore | None = None,
        lease_owner_id: str | None = None,
    ) -> None:
        """Initialize `DiscordGatewayTransport`."""
        shard_tuple = (config.shard_id, config.num_shards) if config.num_shards > 1 else (0, 1)
        lease_key = f"discord:gateway:shard:{shard_tuple[0]}:{shard_tuple[1]}"
        super().__init__(
            transport_name=f"discord-gateway-s{shard_tuple[0]}",
            mode=config.mode,
            ws_connector=ws_connector,
            state_store=state_store,
            lease_key=lease_key if state_store is not None else None,
            lease_owner_id=lease_owner_id,
            lease_ttl_seconds=config.lease_ttl_seconds,
            lease_renew_interval_seconds=config.lease_renew_interval_seconds,
            initial_backoff_seconds=config.initial_backoff_seconds,
            max_backoff_seconds=config.max_backoff_seconds,
            shard=shard_tuple,
        )
        self.config = config
        self._on_dispatch = on_dispatch
        self._http_client = http_client
        self._owns_http = http_client is None

        # Session & resume state (Discord v10)
        self._session_id: str | None = None
        self._resume_gateway_url: str | None = None
        self._seq: int | None = None
        self._bot_user_id: str | None = None
        self._awaiting_ack: bool = False

        # Thread metadata cache: thread_id -> {parent_id, owner_id, type, name}
        self._thread_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()

    @property
    def bot_user_id(self) -> str | None:
        """Return bot user ID discovered from Gateway `READY` event, if available."""
        return self._bot_user_id

    @property
    def session_id(self) -> str | None:
        """Return active Discord Gateway session ID."""
        return self._session_id

    @property
    def resume_gateway_url(self) -> str | None:
        """Return Discord v10 `resume_gateway_url` received in `READY`."""
        return self._resume_gateway_url

    @property
    def sequence(self) -> int | None:
        """Return last processed Gateway sequence number (`s`)."""
        return self._seq

    def cache_thread(
        self,
        thread_id: str,
        *,
        parent_id: str | None = None,
        owner_id: str | None = None,
        channel_type: int = 11,
        name: str = "",
    ) -> None:
        """Record thread channel metadata in the bounded LRU cache."""
        tid = thread_id.strip()
        if not tid:
            return
        self._thread_cache[tid] = {
            "id": tid,
            "parent_id": (parent_id or "").strip() or None,
            "owner_id": (owner_id or "").strip() or None,
            "type": channel_type,
            "name": name,
        }
        self._thread_cache.move_to_end(tid)
        while len(self._thread_cache) > _THREAD_CACHE_CAP:
            self._thread_cache.popitem(last=False)

    def get_cached_thread(self, channel_id: str) -> dict[str, Any] | None:
        """Return cached thread metadata if `channel_id` is a known Discord thread."""
        info = self._thread_cache.get(channel_id)
        if info is not None:
            self._thread_cache.move_to_end(channel_id)
            return dict(info)
        return None

    def _evict_thread(self, thread_id: str) -> None:
        self._thread_cache.pop(thread_id.strip(), None)

    def _clear_resume_state(self) -> None:
        self._session_id = None
        self._resume_gateway_url = None
        self._seq = None

    def _format_gateway_url(self, base_url: str) -> str:
        clean = base_url.rstrip("/")
        if "v=10" in clean:
            return clean
        sep = "&" if "?" in clean else "?"
        return f"{clean}{sep}v=10&encoding=json"

    async def _resolve_connect_url(self) -> str:
        if self._session_id and self._resume_gateway_url:
            return self._format_gateway_url(self._resume_gateway_url)
        if self.config.gateway_url:
            return self._format_gateway_url(self.config.gateway_url)

        client = self._http_client
        if client is None:
            client = httpx.AsyncClient(timeout=20.0)
            self._http_client = client
            self._owns_http = True

        resp = await client.get(
            f"{self.config.api_base.rstrip('/')}/gateway/bot",
            headers={"Authorization": f"Bot {self.config.bot_token}"},
        )
        if resp.status_code == 401:
            raise FatalTransportError(FATAL_CLOSE_MESSAGES[4004], close_code=4004)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, Mapping):
            limit = data.get("session_start_limit")
            if isinstance(limit, Mapping):
                remaining = limit.get("remaining")
                if isinstance(remaining, int) and remaining <= 0:
                    reset_ms = int(limit.get("reset_after") or 60_000)
                    raise RuntimeError(
                        f"Discord session_start_limit exhausted; resets in {reset_ms / 1000:.1f}s"
                    )
            url = str(data.get("url") or "wss://gateway.discord.gg")
            return self._format_gateway_url(url)
        return self._format_gateway_url("wss://gateway.discord.gg")

    def _classify_close_code(self, close_code: int) -> str | None:
        return FATAL_CLOSE_MESSAGES.get(close_code)

    async def stop(self) -> None:
        """Stop Gateway transport and close any owned HTTP client."""
        await super().stop()
        if self._owns_http and self._http_client is not None:
            with contextlib.suppress(Exception):
                await self._http_client.aclose()
            self._http_client = None

    async def _send_json(self, ws: Any, payload: Mapping[str, Any]) -> None:
        await ws.send(json.dumps(dict(payload)))

    async def _heartbeat_loop(self, ws: Any, interval_seconds: float, stop_hb: asyncio.Event) -> None:
        first_wait = (
            interval_seconds * random.uniform(0.1, 0.9)  # noqa: S311
            if self.config.jitter_initial_heartbeat
            else min(0.05, interval_seconds)
        )
        try:
            await asyncio.wait_for(stop_hb.wait(), timeout=first_wait)
            return
        except TimeoutError:
            pass

        while not stop_hb.is_set() and not self._stop_event.is_set():
            if self._awaiting_ack:
                logger.warning("Discord Gateway heartbeat ACK missed (zombie connection); reconnecting")
                with contextlib.suppress(Exception):
                    await ws.close(code=4000, reason="Heartbeat ACK timeout")
                return
            self._awaiting_ack = True
            await self._send_json(ws, {"op": OP_HEARTBEAT, "d": self._seq})
            try:
                await asyncio.wait_for(stop_hb.wait(), timeout=interval_seconds)
                return
            except TimeoutError:
                continue

    async def _send_identify_or_resume(self, ws: Any) -> None:
        if self._session_id and self._seq is not None:
            await self._send_json(
                ws,
                {
                    "op": OP_RESUME,
                    "d": {
                        "token": self.config.bot_token,
                        "session_id": self._session_id,
                        "seq": self._seq,
                    },
                },
            )
            return

        identify_data: dict[str, Any] = {
            "token": self.config.bot_token,
            "intents": self.config.intents,
            "properties": {
                "os": "linux",
                "browser": "talkops",
                "device": "talkops",
            },
        }
        if self.config.num_shards > 1:
            identify_data["shard"] = [self.config.shard_id, self.config.num_shards]
        await self._send_json(ws, {"op": OP_IDENTIFY, "d": identify_data})

    def _ingest_thread_cache_event(self, event_name: str, data: Mapping[str, Any]) -> None:
        if event_name == "GUILD_CREATE":
            raw_threads = data.get("threads")
            if isinstance(raw_threads, Sequence):
                for th in raw_threads:
                    if isinstance(th, Mapping) and th.get("id"):
                        ch_type = int(th.get("type") or 11)
                        if ch_type in DISCORD_THREAD_CHANNEL_TYPES:
                            self.cache_thread(
                                str(th["id"]),
                                parent_id=str(th.get("parent_id") or "") or None,
                                owner_id=str(th.get("owner_id") or "") or None,
                                channel_type=ch_type,
                                name=str(th.get("name") or ""),
                            )
        elif event_name in {"THREAD_CREATE", "THREAD_UPDATE"}:
            if data.get("id"):
                ch_type = int(data.get("type") or 11)
                self.cache_thread(
                    str(data["id"]),
                    parent_id=str(data.get("parent_id") or "") or None,
                    owner_id=str(data.get("owner_id") or "") or None,
                    channel_type=ch_type,
                    name=str(data.get("name") or ""),
                )
        elif event_name == "THREAD_DELETE":
            if data.get("id"):
                self._evict_thread(str(data["id"]))

    async def _run_session(self, ws: Any) -> None:
        self._awaiting_ack = False
        stop_hb = asyncio.Event()
        hb_task: asyncio.Task[None] | None = None

        try:
            async for raw_frame in ws:
                if self._stop_event.is_set():
                    break
                if isinstance(raw_frame, bytes):
                    raw_frame = raw_frame.decode("utf-8", errors="replace")
                try:
                    frame = json.loads(raw_frame)
                except Exception:
                    continue
                if not isinstance(frame, Mapping):
                    continue

                op = frame.get("op")
                seq = frame.get("s")
                if isinstance(seq, int):
                    self._seq = seq
                event_name = str(frame.get("t") or "")
                raw_d = frame.get("d")

                if op == OP_HELLO:
                    interval_ms = 41250
                    if isinstance(raw_d, Mapping) and raw_d.get("heartbeat_interval"):
                        interval_ms = int(raw_d["heartbeat_interval"])
                    interval_sec = max(0.05, interval_ms / 1000.0)
                    if hb_task is not None and not hb_task.done():
                        stop_hb.set()
                        hb_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await hb_task
                        stop_hb = asyncio.Event()
                    hb_task = asyncio.create_task(
                        self._heartbeat_loop(ws, interval_sec, stop_hb),
                        name=f"talkops-discord-hb-s{self.config.shard_id}",
                    )
                    await self._send_identify_or_resume(ws)

                elif op == OP_HEARTBEAT:
                    await self._send_json(ws, {"op": OP_HEARTBEAT, "d": self._seq})

                elif op == OP_HEARTBEAT_ACK:
                    self._awaiting_ack = False
                    self._mark_heartbeat()

                elif op == OP_RECONNECT:
                    logger.info("Discord Gateway requested OP 7 Reconnect; resuming session")
                    with contextlib.suppress(Exception):
                        await ws.close(code=4000, reason="Reconnect requested")
                    return

                elif op == OP_INVALID_SESSION:
                    resumable = bool(raw_d)
                    if not resumable:
                        self._clear_resume_state()
                    logger.info(
                        "Discord Gateway received OP 9 Invalid Session (resumable=%s)",
                        resumable,
                    )
                    with contextlib.suppress(Exception):
                        await ws.close(code=4000, reason="Invalid session")
                    return

                elif op == OP_DISPATCH:
                    event_data: dict[str, Any] = dict(raw_d) if isinstance(raw_d, Mapping) else {}
                    if event_name == "READY":
                        self._session_id = str(event_data.get("session_id") or "") or self._session_id
                        resume_url = str(event_data.get("resume_gateway_url") or "").strip()
                        if resume_url:
                            self._resume_gateway_url = resume_url
                        raw_user = event_data.get("user")
                        if isinstance(raw_user, Mapping) and raw_user.get("id"):
                            self._bot_user_id = str(raw_user["id"])
                        self._mark_heartbeat()
                        self._set_health(
                            "connected",
                            connected=True,
                            detail=f"Connected (session={str(self._session_id or '')[:8]})",
                        )
                    elif event_name == "RESUMED":
                        self._mark_heartbeat()
                        self._set_health(
                            "connected",
                            connected=True,
                            detail=f"Resumed (session={str(self._session_id or '')[:8]})",
                        )
                    elif event_name in {"GUILD_CREATE", "THREAD_CREATE", "THREAD_UPDATE", "THREAD_DELETE"}:
                        self._ingest_thread_cache_event(event_name, event_data)

                    try:
                        await self._on_dispatch(event_name, event_data)
                    except Exception as exc:
                        logger.warning(
                            "Discord Gateway dispatch handler error for %s: %s",
                            event_name,
                            exc,
                            exc_info=True,
                        )
        finally:
            stop_hb.set()
            if hb_task is not None and not hb_task.done():
                hb_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await hb_task

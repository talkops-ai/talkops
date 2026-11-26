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

"""Pluggable inbound transport primitives for `talkops.channels`.

Decouples persistent outbound-initiated connections (Discord Gateway WebSocket,
future Slack Socket Mode, Mattermost WebSocket, Telegram long-polling) from
channel-specific payload parsing, while adding:
- Jittered exponential backoff reconnection
- Fatal close-code circuit breaking (never spinning on auth/intent errors)
- Optional distributed leader-lease coordination via `ChannelStateStore` so
  multi-replica Agent Server / Kubernetes deployments never duplicate gateway streams.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import socket
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from talkops.channels.interfaces import ChannelTransportHealth
from talkops.channels.state_store import ChannelStateStore
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


class FatalTransportError(RuntimeError):
    """Raised when a transport encounters a non-recoverable configuration or auth error."""

    def __init__(self, message: str, *, close_code: int | None = None) -> None:
        """Initialize fatal transport error with optional WebSocket `close_code`."""
        super().__init__(message)
        self.close_code = close_code


@runtime_checkable
class ChannelTransport(Protocol):
    """Lifecycle protocol for any channel inbound transport (WebSocket, polling, etc.)."""

    async def start(self) -> None:
        """Start background transport loop."""
        ...

    async def stop(self) -> None:
        """Gracefully stop transport and release any acquired leases."""
        ...

    def health(self) -> ChannelTransportHealth:
        """Return live transport health diagnostics."""
        ...


WebSocketConnector = Callable[..., Any]


def _default_ws_connector(url: str, **kwargs: Any) -> Any:
    """Resolve `websockets` async client context manager lazily."""
    try:
        from websockets.asyncio.client import (
            connect as ws_connect,  # type: ignore[import-not-found]
        )

        return ws_connect(url, **kwargs)
    except ImportError:
        import websockets  # type: ignore[import-not-found]

        return websockets.connect(url, **kwargs)


def _extract_ws_close_code(exc: BaseException) -> int | None:
    """Extract numeric WebSocket close code across `websockets` versions."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    rcvd = getattr(exc, "rcvd", None)
    rcvd_code = getattr(rcvd, "code", None)
    if isinstance(rcvd_code, int):
        return rcvd_code
    return None


class ReconnectingWebSocketTransport(ABC):
    """Reusable persistent WebSocket transport base for multi-channel adapters.

    Subclasses implement `_resolve_connect_url()`, `_run_session(ws)`, and
    `_classify_close_code(code)` while inheriting:
    - Distributed leader lease acquisition & periodic renewal (for HA multi-replica servers)
    - Jittered exponential backoff reconnects
    - Fatal close-code circuit breaking
    - Clean cancellation & structured `ChannelTransportHealth` reporting
    """

    def __init__(
        self,
        *,
        transport_name: str,
        mode: str = "websocket",
        ws_connector: WebSocketConnector | None = None,
        state_store: ChannelStateStore | None = None,
        lease_key: str | None = None,
        lease_owner_id: str | None = None,
        lease_ttl_seconds: float = 45.0,
        lease_renew_interval_seconds: float = 15.0,
        standby_poll_seconds: float = 10.0,
        initial_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 60.0,
        shard: tuple[int, int] | None = None,
    ) -> None:
        """Initialize reusable reconnecting WebSocket transport."""
        self.transport_name = transport_name
        self.mode = mode
        self._ws_connector = ws_connector or _default_ws_connector
        self._state_store = state_store
        self._lease_key = lease_key
        self._owner_id = lease_owner_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self._lease_ttl_seconds = max(10.0, lease_ttl_seconds)
        self._lease_renew_interval = max(2.0, min(lease_renew_interval_seconds, lease_ttl_seconds / 2.0))
        self._standby_poll_seconds = max(1.0, standby_poll_seconds)
        self._initial_backoff = max(0.05, initial_backoff_seconds)
        self._max_backoff = max(self._initial_backoff, max_backoff_seconds)
        self._shard = shard

        self._stop_event = asyncio.Event()
        self._runner_task: asyncio.Task[None] | None = None
        self._active_ws: Any = None
        self._owns_lease = False

        self._state: Any = "idle"
        self._connected = False
        self._detail = ""
        self._reconnect_count = 0
        self._last_heartbeat_at: float | None = None
        self._fatal_code: int | None = None

    @property
    def is_running(self) -> bool:
        """Return `True` if the background transport task is currently alive."""
        return self._runner_task is not None and not self._runner_task.done()

    def _set_health(
        self,
        state: Any,
        *,
        connected: bool | None = None,
        detail: str | None = None,
        fatal_code: int | None = None,
    ) -> None:
        self._state = state
        if connected is not None:
            self._connected = connected
        if detail is not None:
            self._detail = detail
        if fatal_code is not None:
            self._fatal_code = fatal_code

    def _mark_heartbeat(self) -> None:
        self._last_heartbeat_at = time.time()

    def health(self) -> ChannelTransportHealth:
        """Return live transport health diagnostics."""
        lease_owner: str | None = None
        if self._state_store is not None and self._lease_key:
            lease_owner = self._state_store.get_lease_owner(self._lease_key)
        elif self._owns_lease:
            lease_owner = self._owner_id
        return ChannelTransportHealth(
            mode=self.mode,
            state=self._state,
            connected=self._connected,
            detail=self._detail,
            reconnect_count=self._reconnect_count,
            last_heartbeat_at=self._last_heartbeat_at,
            shard=self._shard,
            lease_owner=lease_owner,
            fatal_code=self._fatal_code,
        )

    async def start(self) -> None:
        """Launch the supervised background WebSocket runner task."""
        if self.is_running:
            return
        self._stop_event.clear()
        self._fatal_code = None
        self._runner_task = asyncio.create_task(
            self._run_loop(),
            name=f"talkops-transport-{self.transport_name}",
        )

    async def stop(self) -> None:
        """Stop the transport cleanly, close the socket, and release any held lease."""
        self._stop_event.set()
        ws = self._active_ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        task = self._runner_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._runner_task = None
        self._active_ws = None
        self._release_lease_if_held()
        self._set_health("stopped", connected=False, detail="Stopped")

    def _try_acquire_lease(self) -> bool:
        if self._state_store is None or not self._lease_key:
            self._owns_lease = True
            return True
        acquired = self._state_store.try_acquire_lease(
            self._lease_key,
            self._owner_id,
            ttl_seconds=self._lease_ttl_seconds,
        )
        self._owns_lease = acquired
        return acquired

    def _release_lease_if_held(self) -> None:
        if self._owns_lease and self._state_store is not None and self._lease_key:
            with contextlib.suppress(Exception):
                self._state_store.release_lease(self._lease_key, self._owner_id)
        self._owns_lease = False

    async def _lease_renew_loop(self, lease_lost: asyncio.Event) -> None:
        if self._state_store is None or not self._lease_key:
            return
        while not self._stop_event.is_set() and not lease_lost.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._lease_renew_interval)
                return
            except TimeoutError:
                pass
            ok = self._state_store.renew_lease(
                self._lease_key,
                self._owner_id,
                ttl_seconds=self._lease_ttl_seconds,
            )
            if not ok:
                logger.warning(
                    "Transport %s lost leader lease %s; closing active socket to enter standby",
                    self.transport_name,
                    self._lease_key,
                )
                self._owns_lease = False
                lease_lost.set()
                ws = self._active_ws
                if ws is not None:
                    with contextlib.suppress(Exception):
                        await ws.close()
                return

    async def _sleep_or_stop(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False

    async def _run_loop(self) -> None:
        backoff = self._initial_backoff
        while not self._stop_event.is_set():
            if not self._try_acquire_lease():
                self._set_health(
                    "standby",
                    connected=False,
                    detail="Waiting for leader lease held by another replica",
                )
                if await self._sleep_or_stop(self._standby_poll_seconds):
                    break
                continue

            lease_lost = asyncio.Event()
            lease_task = asyncio.create_task(
                self._lease_renew_loop(lease_lost),
                name=f"talkops-lease-{self.transport_name}",
            )
            try:
                self._set_health("connecting", connected=False, detail="Resolving WebSocket endpoint")
                ws_url = await self._resolve_connect_url()
                ws_kwargs = self._connect_kwargs()
                async with self._ws_connector(ws_url, **ws_kwargs) as ws:
                    self._active_ws = ws
                    backoff = self._initial_backoff
                    await self._run_session(ws)
            except asyncio.CancelledError:
                raise
            except FatalTransportError as exc:
                logger.error(
                    "Transport %s stopped permanently (fatal close_code=%s): %s",
                    self.transport_name,
                    exc.close_code,
                    exc,
                )
                self._set_health(
                    "error",
                    connected=False,
                    detail=str(exc),
                    fatal_code=exc.close_code,
                )
                break
            except Exception as exc:
                close_code = _extract_ws_close_code(exc)
                fatal_msg = self._classify_close_code(close_code) if close_code is not None else None
                if fatal_msg is not None:
                    logger.error(
                        "Transport %s closed with fatal code %s: %s",
                        self.transport_name,
                        close_code,
                        fatal_msg,
                    )
                    self._set_health(
                        "error",
                        connected=False,
                        detail=fatal_msg,
                        fatal_code=close_code,
                    )
                    break

                if self._stop_event.is_set():
                    break
                if lease_lost.is_set():
                    continue

                self._reconnect_count += 1
                self._set_health(
                    "reconnecting",
                    connected=False,
                    detail=f"Disconnected ({close_code or type(exc).__name__}); retrying in {backoff:.1f}s",
                )
                logger.warning(
                    "Transport %s disconnected (%s); reconnecting in %.1fs",
                    self.transport_name,
                    exc,
                    backoff,
                )
                jitter = random.uniform(0.0, min(1.0, backoff * 0.25))  # noqa: S311
                if await self._sleep_or_stop(backoff + jitter):
                    break
                backoff = min(self._max_backoff, backoff * 2.0)
            finally:
                self._active_ws = None
                lease_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await lease_task

        self._release_lease_if_held()

    def _connect_kwargs(self) -> dict[str, Any]:
        """Return optional kwargs forwarded to `ws_connector`."""
        return {"max_size": 16 * 1024 * 1024}

    @abstractmethod
    async def _resolve_connect_url(self) -> str:
        """Return the WebSocket URL to connect or resume against."""

    @abstractmethod
    async def _run_session(self, ws: Any) -> None:
        """Drive the protocol handshake, heartbeats, and dispatch loop for an open WebSocket."""

    def _classify_close_code(self, close_code: int) -> str | None:
        """Return a fatal error message if `close_code` is non-recoverable, else `None`."""
        del close_code
        return None

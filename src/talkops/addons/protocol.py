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

"""Core protocol, capability declarations, and base class for TalkOps Server Addons.

This module defines the public extension contract (`ServerAddon` and `BaseServerAddon`)
used by:
1. Built-in Open-Source addons (`talkops.addons.builtin.*`)
2. External Enterprise addons installed from separate repositories via
   `importlib.metadata.entry_points(group="talkops.addons")`
3. User-defined custom addons loaded via module paths (`pkg.mod:AddonClass`) or
   `.talkops/addons/*.py` files.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel
from starlette.middleware import Middleware
from starlette.routing import BaseRoute

from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from talkops.addons.gateway import AgentRunGateway, HookEventBus
    from talkops.config.settings import Settings
    from talkops.mcp.session_manager import MCPSessionManager

logger = get_logger(__name__)

ADDON_API_VERSION: int = 1
ADDON_ENTRYPOINT_GROUP: str = "talkops.addons"

AddonTier = Literal["oss", "enterprise", "custom"]


@dataclass(frozen=True)
class AddonCapabilities:
    """Declares which extension surfaces a ServerAddon uses."""

    routes: bool = False
    middleware: bool = False
    background_workers: bool = False
    companion_processes: bool = False
    auth_provider: bool = False
    event_sinks: bool = False
    mcp_provider: bool = False
    gateway_runner: bool = False
    event_subscriber: bool = False


@dataclass(frozen=True)
class CompanionProcessSpec:
    """Opt-in OS subprocess managed alongside the main server process."""

    name: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    healthcheck_url: str | None = None
    restart_policy: Literal["never", "on-failure", "always"] = "on-failure"


@dataclass(frozen=True)
class AddonHealthStatus:
    """Structured health report returned by an addon."""

    name: str
    status: Literal["ok", "degraded", "error", "disabled"] = "ok"
    tier: AddonTier = "oss"
    version: str = "0.1.0"
    port: int | None = None
    source: str = "builtin"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert instance to dict."""
        payload: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "tier": self.tier,
            "version": self.version,
            "source": self.source,
            "details": dict(self.details),
        }
        if self.port is not None:
            payload["port"] = self.port
        return payload


@runtime_checkable
class IdentityMapper(Protocol):
    """Pluggable authentication & RBAC authorization provider for Server Mode."""

    async def authenticate_request(self, headers: dict[str, str]) -> dict[str, Any]:
        """Validate request credentials and return an identity dict (with at least `identity`)."""
        ...

    def authorize_action(
        self,
        identity: dict[str, Any],
        resource: Literal["thread", "run", "store", "mcp", "webhook", "admin"],
        action: Literal["create", "read", "update", "delete", "execute"],
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Check whether `identity` is permitted to perform `action` on `resource`."""
        ...


@dataclass
class AddonContext:
    """Shared runtime context injected into every enabled ServerAddon."""

    settings: Settings
    project_root: Path
    server_url: str
    gateway: AgentRunGateway
    event_bus: HookEventBus
    mcp_manager: MCPSessionManager | None = None
    checkpointer: Any = None
    store: Any = None
    identity_mapper: IdentityMapper | None = None
    addon_configs: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    _background_tasks: set[asyncio.Task[Any]] = field(default_factory=set, init=False, repr=False)

    def get_addon_config(self, addon_name: str, default: Any = None) -> Any:
        """Return the resolved configuration object/dict for `addon_name`."""
        return self.addon_configs.get(addon_name, default)

    def spawn_background_task(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        """Spawn a managed asyncio background task tied to the server lifespan."""
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)

        def _on_done(t: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(t)
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    logger.error(
                        "Background task %s failed with error: %s",
                        t.get_name(),
                        exc,
                        exc_info=exc,
                    )

        task.add_done_callback(_on_done)
        return task

    async def cancel_background_tasks(self, timeout: float = 5.0) -> None:
        """Cancel and await all active background tasks spawned through this context."""
        if not self._background_tasks:
            return
        tasks = [t for t in self._background_tasks if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        self._background_tasks.clear()


@runtime_checkable
class ServerAddon(Protocol):
    """Unified protocol implemented by all TalkOps Server Addons."""

    name: str
    api_version: int
    tier: Any
    version: str
    description: str
    dependencies: Any
    capabilities: AddonCapabilities
    settings_schema: Any
    source: str
    enabled_by_default: bool

    def bind_config(self, config: Any) -> None:
        """Store validated addon configuration prior to `on_startup`."""
        ...

    def is_enabled(self, addon_config: Any) -> bool:
        """Return True if this addon should be activated given its resolved config."""
        ...

    async def on_startup(self, ctx: AddonContext) -> None:
        """Initialize background workers, connections, or event subscriptions."""
        ...

    async def on_shutdown(self, ctx: AddonContext) -> None:
        """Drain tasks and release resources cleanly before server exit."""
        ...

    def get_routes(self) -> list[BaseRoute]:
        """Return Starlette `Route` / `Mount` / `WebSocketRoute` objects."""
        ...

    def get_middleware(self) -> list[Middleware]:
        """Return Starlette `Middleware` definitions to wrap the server app."""
        ...

    def get_companion_specs(self, ctx: AddonContext) -> list[CompanionProcessSpec]:
        """Return optional OS companion processes to supervise alongside the server."""
        ...

    async def health_check(self) -> AddonHealthStatus:
        """Return structured health information for `/api/v1/addons` and `/api/v1/server/info`."""
        ...


class BaseServerAddon:
    """Ergonomic base class for built-in, enterprise, and custom user addons.

    External package authors or users writing custom addons can subclass
    `BaseServerAddon` and override only the hooks they need.
    """

    name: str = "unnamed-addon"
    api_version: int = ADDON_API_VERSION
    tier: AddonTier = "custom"
    version: str = "0.1.0"
    description: str = ""
    dependencies: tuple[str, ...] = ()
    capabilities: AddonCapabilities = AddonCapabilities()
    settings_schema: type[BaseModel] | None = None
    enabled_by_default: bool = False
    source: str = "custom"

    def __init__(self) -> None:
        """Initialize BaseServerAddon."""
        self._ctx: AddonContext | None = None
        self._config: Any = None
        self.source = "custom"

    def bind_config(self, config: Any) -> None:
        """Store validated addon configuration prior to `on_startup`."""
        self._config = config

    def is_enabled(self, addon_config: Any) -> bool:
        """Determine whether the addon is enabled from its resolved config."""
        if addon_config is None:
            return self.enabled_by_default
        if isinstance(addon_config, BaseModel):
            return bool(getattr(addon_config, "enabled", self.enabled_by_default))
        if isinstance(addon_config, dict):
            return bool(addon_config.get("enabled", self.enabled_by_default))
        if isinstance(addon_config, bool):
            return addon_config
        return self.enabled_by_default

    async def on_startup(self, ctx: AddonContext) -> None:
        """Lifecycle hook called when the server starts."""
        self._ctx = ctx

    async def on_shutdown(self, ctx: AddonContext) -> None:
        """Lifecycle hook called when the server shuts down."""
        self._ctx = None

    def get_routes(self) -> list[BaseRoute]:
        """Return Starlette routes contributed by this addon."""
        return []

    def get_middleware(self) -> list[Middleware]:
        """Return Starlette middleware contributed by this addon."""
        return []

    def get_companion_specs(self, ctx: AddonContext) -> list[CompanionProcessSpec]:
        """Return companion process specifications contributed by this addon."""
        return []

    async def health_check(self) -> AddonHealthStatus:
        """Return default health status for this addon."""
        port: int | None = None
        if isinstance(self._config, BaseModel):
            port = getattr(self._config, "port", None)
        elif isinstance(self._config, dict):
            port = self._config.get("port")
        return AddonHealthStatus(
            name=self.name,
            status="ok",
            tier=self.tier,
            version=self.version,
            port=port,
            source=self.source,
            details={"description": self.description},
        )

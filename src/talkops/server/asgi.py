"""Unified ASGI application factory powered by TalkOps `AddonManager`.

Assembles the Starlette ASGI application from:
- Built-in Default Addons (`client-gateway`, `core-auth`, `webhooks`)
- External Repository Addons loaded via `addons.json` (`--addons-config`) or `entry_points(group="talkops.addons")`
- Custom User Addons loaded from `.talkops/addons/*.py`, `settings.json`, or `--custom-addon`
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.applications import Starlette

from talkops.addons.builtin.webhooks import set_webhook_dispatcher
from talkops.addons.gateway import AgentRunGateway, HookEventBus
from talkops.addons.manager import AddonManager
from talkops.addons.protocol import ServerAddon
from talkops.webhooks.dispatcher import WebhookDispatcher


def create_server_app(
    *,
    dispatcher: WebhookDispatcher | None = None,
    profile: str | None = None,
    addons: str | list[str] | tuple[str, ...] | None = None,
    enabled_addons: str | list[str] | tuple[str, ...] | None = None,
    disable_addons: str | list[str] | tuple[str, ...] | None = None,
    disabled_addons: str | list[str] | tuple[str, ...] | None = None,
    custom_addons: list[str] | tuple[str, ...] | None = None,
    custom_addon_specs: list[str] | tuple[str, ...] | None = None,
    addons_config: Path | str | None = None,
    addons_config_path: Path | str | None = None,
    webhook_port: int | None = None,
    main_port: int | None = None,
    host: str = "127.0.0.1",
    project_root: Path | None = None,
    gateway: AgentRunGateway | None = None,
    event_bus: HookEventBus | None = None,
    extra_addons: list[ServerAddon] | None = None,
) -> Starlette:
    """Create the unified TalkOps Server Starlette ASGI application via `AddonManager`."""
    if dispatcher is not None:
        set_webhook_dispatcher(dispatcher)

    manager = AddonManager(
        profile=profile,
        enabled_addons=enabled_addons if enabled_addons is not None else addons,
        disabled_addons=disabled_addons if disabled_addons is not None else disable_addons,
        custom_addon_specs=custom_addon_specs if custom_addon_specs is not None else custom_addons,
        addons_config_path=addons_config_path if addons_config_path is not None else addons_config,
        webhook_port=webhook_port,
        main_port=main_port,
        host=host,
        project_root=project_root,
        gateway=gateway,
        event_bus=event_bus,
        extra_addons=extra_addons,
    )
    return manager.create_asgi_app()


def create_app(**kwargs: Any) -> Starlette:
    """Standard ASGI factory entrypoint (`uvicorn talkops.server.asgi:create_app --factory`)."""
    return create_server_app(**kwargs)

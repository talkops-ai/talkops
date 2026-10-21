"""Built-in Open-Source Server Addons shipped with TalkOps core (`client-gateway`, `core-auth`, `webhooks`)."""

from __future__ import annotations

from talkops.addons.builtin.client_gateway import ClientGatewayAddon, ClientGatewaySettings
from talkops.addons.builtin.core_auth import CoreAuthAddon, CoreAuthSettings
from talkops.addons.builtin.webhooks import (
    WebhookAddon,
    WebhookAddonSettings,
    get_webhook_dispatcher,
    set_webhook_dispatcher,
)
from talkops.addons.protocol import ServerAddon


def get_builtin_addons() -> list[ServerAddon]:
    """Return fresh instances of all built-in Open-Source Server Addons."""
    return [
        CoreAuthAddon(),
        ClientGatewayAddon(),
        WebhookAddon(),
    ]


__all__ = [
    "ClientGatewayAddon",
    "ClientGatewaySettings",
    "CoreAuthAddon",
    "CoreAuthSettings",
    "WebhookAddon",
    "WebhookAddonSettings",
    "get_builtin_addons",
    "get_webhook_dispatcher",
    "set_webhook_dispatcher",
]

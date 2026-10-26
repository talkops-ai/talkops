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

"""Built-in Open-Source Server Addons shipped with TalkOps core (`client-gateway`, `core-auth`, `webhooks`)."""

from __future__ import annotations

from talkops.addons.builtin.client_gateway import (
    ClientGatewayAddon,
    ClientGatewaySettings,
)
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

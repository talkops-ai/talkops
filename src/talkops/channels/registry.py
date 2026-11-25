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

"""Pluggable channel adapter registry for `talkops.channels`.

Allows built-in adapters (`slack`, `teams`, `discord`) and external/custom
messaging adapters (e.g. Telegram, Mattermost, Google Chat, Matrix) to plug into
`ChannelsAddon` cleanly via:
1. Built-in default registrations
2. Python package entry points (`importlib.metadata.entry_points(group="talkops.channel_adapters")`)
3. Programmatic registration (`register_channel_adapter_factory`)
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Mapping
from typing import Any

from talkops.channels.interfaces import WebhookChannelAdapter
from talkops.channels.state_store import ChannelStateStore
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

CHANNEL_ADAPTER_ENTRYPOINT_GROUP = "talkops.channel_adapters"

ChannelAdapterFactory = Callable[
    [Mapping[str, Any], Mapping[str, str], ChannelStateStore | None],
    WebhookChannelAdapter,
]

_GLOBAL_ADAPTER_FACTORIES: dict[str, ChannelAdapterFactory] = {}


def register_channel_adapter_factory(
    provider: str,
    factory: ChannelAdapterFactory,
) -> ChannelAdapterFactory:
    """Programmatically register a channel adapter factory by provider name."""
    norm = provider.strip().lower()
    if not norm:
        raise ValueError("Channel adapter provider name must be non-empty")
    _GLOBAL_ADAPTER_FACTORIES[norm] = factory
    return factory


def unregister_channel_adapter_factory(provider: str) -> None:
    """Remove a programmatically registered channel adapter factory."""
    _GLOBAL_ADAPTER_FACTORIES.pop(provider.strip().lower(), None)


def clear_registered_channel_adapter_factories() -> None:
    """Clear programmatically registered channel adapter factories (primarily for tests)."""
    _GLOBAL_ADAPTER_FACTORIES.clear()


def _slack_factory(
    cfg: Mapping[str, Any],
    env: Mapping[str, str],
    state_store: ChannelStateStore | None,
) -> WebhookChannelAdapter:
    del state_store
    from talkops.channels.providers.slack import SlackChannelConfig, SlackWebhookChannel

    return SlackWebhookChannel(SlackChannelConfig.from_mapping(cfg, env))


def _teams_factory(
    cfg: Mapping[str, Any],
    env: Mapping[str, str],
    state_store: ChannelStateStore | None,
) -> WebhookChannelAdapter:
    from talkops.channels.providers.teams import TeamsChannelConfig, TeamsWebhookChannel

    return TeamsWebhookChannel(
        TeamsChannelConfig.from_mapping(cfg, env),
        state_store=state_store,
    )


def _discord_factory(
    cfg: Mapping[str, Any],
    env: Mapping[str, str],
    state_store: ChannelStateStore | None,
) -> WebhookChannelAdapter:
    from talkops.channels.providers.discord import (
        DiscordChannelConfig,
        DiscordWebhookChannel,
    )

    return DiscordWebhookChannel(
        DiscordChannelConfig.from_mapping(cfg, env),
        state_store=state_store,
    )


class ChannelAdapterRegistry:
    """Registry resolving built-in, entry-point, and custom channel adapter factories."""

    def __init__(self, *, discover_entrypoints: bool = True) -> None:
        """Initialize registry with built-in Slack, Teams, and Discord adapters."""
        self._discover_entrypoints = discover_entrypoints
        self._entrypoints_loaded = False
        self._factories: dict[str, ChannelAdapterFactory] = {
            "slack": _slack_factory,
            "teams": _teams_factory,
            "discord": _discord_factory,
        }

    def _ensure_discovered(self) -> None:
        if self._discover_entrypoints and not self._entrypoints_loaded:
            self._entrypoints_loaded = True
            self._load_entrypoints()
        for name, factory in _GLOBAL_ADAPTER_FACTORIES.items():
            self._factories.setdefault(name, factory)

    def _load_entrypoints(self) -> None:
        try:
            eps = importlib.metadata.entry_points(group=CHANNEL_ADAPTER_ENTRYPOINT_GROUP)
            for ep in eps:
                try:
                    loaded = ep.load()
                    if callable(loaded):
                        self._factories[ep.name.strip().lower()] = loaded
                except Exception as exc:
                    logger.warning(
                        "Failed loading channel adapter entry point '%s' (%s): %s",
                        ep.name,
                        ep.value,
                        exc,
                    )
        except Exception as exc:
            logger.debug(
                "Skipping entry_points(group='%s') discovery: %s",
                CHANNEL_ADAPTER_ENTRYPOINT_GROUP,
                exc,
            )

    def register(self, provider: str, factory: ChannelAdapterFactory) -> None:
        """Register or override a channel adapter factory on this registry instance."""
        norm = provider.strip().lower()
        if not norm:
            raise ValueError("Channel adapter provider name must be non-empty")
        self._factories[norm] = factory

    def list_providers(self) -> tuple[str, ...]:
        """Return registered provider names in registration order."""
        self._ensure_discovered()
        return tuple(self._factories.keys())

    def build_adapter(
        self,
        provider: str,
        cfg: Mapping[str, Any],
        env: Mapping[str, str],
        *,
        state_store: ChannelStateStore | None = None,
    ) -> WebhookChannelAdapter:
        """Instantiate an adapter for `provider` using its registered factory."""
        self._ensure_discovered()
        norm = provider.strip().lower()
        factory = self._factories.get(norm)
        if factory is None:
            raise KeyError(f"Unknown channel adapter provider: {provider!r}")
        try:
            return factory(cfg, env, state_store)
        except TypeError:
            # Support simplified 2-arg custom factories `(cfg, env) -> adapter`
            return factory(cfg, env)  # type: ignore[call-arg]

    def build_all(
        self,
        provider_configs: Mapping[str, Mapping[str, Any]],
        env: Mapping[str, str],
        *,
        state_store: ChannelStateStore | None = None,
    ) -> list[WebhookChannelAdapter]:
        """Instantiate all registered adapters using `provider_configs` and `env`."""
        self._ensure_discovered()
        adapters: list[WebhookChannelAdapter] = []
        for provider in self._factories:
            raw_cfg = provider_configs.get(provider)
            cfg = raw_cfg if isinstance(raw_cfg, Mapping) else {}
            try:
                adapters.append(
                    self.build_adapter(provider, cfg, env, state_store=state_store)
                )
            except Exception as exc:
                logger.warning("Failed initializing channel adapter '%s': %s", provider, exc)
        return adapters

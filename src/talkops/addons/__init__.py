"""TalkOps Pluggable Server Addon Framework.

Provides the unified extension architecture for TalkOps Server Mode:
- Built-in Default Addons (`client-gateway`, `core-auth`, `webhooks`) — enabled by default
- External Repository Addons (TalkOps official or third-party) hooked in via:
  1. `addons.json` / `.talkops/addons.json` / `--addons-config <path>` (`TALKOPS_ADDONS_CONFIG`)
  2. `importlib.metadata.entry_points(group="talkops.addons")`
  3. `--custom-addon <package_or_path>` (`TALKOPS_CUSTOM_ADDONS`) or `.talkops/addons/*.py`
  4. Programmatic `@register_addon`
- Secrets are supplied by the user via standard environment variables or `.talkops/.env` / `~/.talkops/.env`.
"""

from __future__ import annotations

from talkops.addons.config import (
    SERVER_PROFILES,
    AddonSettings,
    ServerProfileName,
    load_merged_addons_settings_json,
    register_server_profile,
    resolve_addon_configs,
    resolve_env_reference,
    resolve_env_references_in_mapping,
    resolve_secret_value,
    resolve_secrets_in_mapping,
)
from talkops.addons.discovery import (
    add_addon_marketplace_source,
    discover_marketplace_addons,
    install_addon,
    list_available_addons,
    refresh_addon_marketplace,
    refresh_all_addon_marketplaces,
    remove_addon_marketplace,
    set_installed_addon_enabled,
    uninstall_addon,
)
from talkops.addons.gateway import (
    ADDON_THREAD_NAMESPACE,
    AddonEvent,
    AgentRunGateway,
    HookEventBus,
    RunHandle,
)
from talkops.addons.manager import (
    AddonManager,
    clear_registered_addons,
    load_addon_from_spec,
    register_addon,
    unregister_addon,
)
from talkops.addons.manifest import (
    AddonManifestError,
    ensure_addon_python_dependencies,
    find_addon_manifest_path,
    find_missing_python_dependencies,
    is_requirement_satisfied,
    load_addon_manifest,
)
from talkops.addons.marketplace_models import (
    AddonDiscoveryResult,
    AddonInstance,
    AddonManifest,
    AddonMarketplace,
    AddonMarketplaceRecord,
    InstalledAddonEntry,
    MarketplaceAddonEntry,
    split_addon_id,
)
from talkops.addons.protocol import (
    ADDON_API_VERSION,
    ADDON_ENTRYPOINT_GROUP,
    AddonCapabilities,
    AddonContext,
    AddonHealthStatus,
    AddonTier,
    BaseServerAddon,
    CompanionProcessSpec,
    IdentityMapper,
    ServerAddon,
)

HookEvent = AddonEvent

__all__ = [
    "ADDON_API_VERSION",
    "ADDON_ENTRYPOINT_GROUP",
    "ADDON_THREAD_NAMESPACE",
    "SERVER_PROFILES",
    "AddonCapabilities",
    "AddonContext",
    "AddonEvent",
    "AddonHealthStatus",
    "AddonManager",
    "AddonSettings",
    "AddonTier",
    "AgentRunGateway",
    "BaseServerAddon",
    "CompanionProcessSpec",
    "HookEvent",
    "HookEventBus",
    "IdentityMapper",
    "RunHandle",
    "ServerAddon",
    "ServerProfileName",
    "clear_registered_addons",
    "load_addon_from_spec",
    "load_merged_addons_settings_json",
    "register_addon",
    "register_server_profile",
    "resolve_addon_configs",
    "resolve_env_reference",
    "resolve_env_references_in_mapping",
    "resolve_secret_value",
    "resolve_secrets_in_mapping",
    "unregister_addon",
]

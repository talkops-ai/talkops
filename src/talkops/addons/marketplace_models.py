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

"""Data models for TalkOps Server Addon manifests and marketplaces (`.talkops/`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

JsonObject = dict[str, Any]
JsonValue = Any

AddonMarketplaceSourceType = Literal["directory", "file", "github", "git", "url"]
AddonInstallScope = Literal["global", "user", "team", "project", "local"]
ExternalAddonRepositorySourceType = Literal["github", "git-subdir", "url"]


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalAddonMarketplaceSource:
    """Local directory or `.talkops/marketplace.json` file used as an addon marketplace source."""

    source_type: Literal["directory", "file"]
    value: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RepositoryAddonMarketplaceSource:
    """GitHub or Git repository used as an addon marketplace source."""

    source_type: Literal["github", "git"]
    value: str
    ref: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class UrlAddonMarketplaceSource:
    """Addon marketplace manifest downloaded from an HTTP(S) URL."""

    source_type: Literal["url"]
    value: str


AddonMarketplaceSource = (
    LocalAddonMarketplaceSource | RepositoryAddonMarketplaceSource | UrlAddonMarketplaceSource
)


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalAddonSource:
    """Addon resolved relative to the marketplace root directory."""

    source_type: Literal["local"]
    path: str


@dataclass(frozen=True, slots=True, kw_only=True)
class GithubAddonSource:
    """Addon sourced from a GitHub repository (`owner/repo`)."""

    source_type: Literal["github"]
    repo: str
    ref: str | None = None
    path: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class GitSubdirectoryAddonSource:
    """Addon sourced from a Git repository URL and optional subdirectory."""

    source_type: Literal["git-subdir"]
    url: str
    ref: str | None = None
    path: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class UrlAddonSource:
    """Addon sourced from an HTTP(S) URL."""

    source_type: Literal["url"]
    url: str
    ref: str | None = None
    path: str | None = None


AddonSource = LocalAddonSource | GithubAddonSource | GitSubdirectoryAddonSource | UrlAddonSource


@dataclass(frozen=True, slots=True, kw_only=True)
class AddonManifest:
    """Parsed `.talkops/addon.json` manifest for a TalkOps Server Addon."""

    name: str
    display_name: str | None = None
    version: str = "0.1.0"
    description: str | None = None
    author: str | JsonObject | None = None
    tier: Literal["oss", "enterprise", "custom"] = "oss"
    api_version: int = 1
    entry_point: str
    python_path: tuple[str, ...] = (".",)
    python_dependencies: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    enabled_by_default: bool = False
    capabilities: JsonObject = field(default_factory=dict)
    webhook_sink: JsonObject = field(default_factory=dict)
    routes: tuple[JsonObject, ...] = ()
    agent_tools: tuple[JsonObject, ...] = ()
    env_vars: tuple[str, ...] = ()
    settings_schema: JsonObject = field(default_factory=dict)
    raw: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketplaceAddonEntry:
    """Single addon entry inside `.talkops/marketplace.json`."""

    name: str
    source: AddonSource
    display_name: str | None = None
    description: str | None = None
    version: str = "0.1.0"
    tier: Literal["oss", "enterprise", "custom"] = "oss"
    category: str | None = None
    entry_point: str | None = None
    python_path: tuple[str, ...] = (".",)
    python_dependencies: tuple[str, ...] = ()
    author: str | JsonObject | None = None
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class AddonMarketplace:
    """Parsed `.talkops/marketplace.json` catalog."""

    name: str
    display_name: str | None = None
    description: str | None = None
    owner: JsonObject | None = None
    addons: tuple[MarketplaceAddonEntry, ...] = ()
    root_dir: Path | None = None
    manifest_path: Path | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class AddonMarketplaceRecord:
    """Persisted addon marketplace source record in `~/.talkops/.state/addon_marketplaces.json`."""

    name: str
    source_type: AddonMarketplaceSourceType
    source: str
    install_location: str
    ref: str | None = None
    addon_count: int = 0
    is_team: bool = False
    is_project: bool = False
    is_default: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class InstalledAddonEntry:
    """Install record for an addon in `~/.talkops/.state/installed_addons.json`."""

    install_path: str
    version: str | None = None
    scope: AddonInstallScope = "user"
    project_path: str | None = None
    installed_at: str | None = None
    last_updated: str | None = None
    git_commit_sha: str | None = None
    entry_point: str | None = None
    python_dependencies: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class AddonInstance:
    """Materialized addon instance discovered from an addon marketplace."""

    addon_id: str
    name: str
    marketplace: str
    root_dir: Path
    manifest: AddonManifest
    version: str = "0.1.0"
    description: str | None = None
    enabled: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class AddonDiscoveryResult:
    """Result from marketplace addon discovery."""

    addons: tuple[AddonInstance, ...]
    warnings: tuple[str, ...] = ()


def split_addon_id(addon_id: str, *, default_marketplace: str | None = None) -> tuple[str, str]:
    """Split an addon identifier in `{addon}@{marketplace}` or `{addon}` form."""
    cleaned = addon_id.strip()
    if "@" in cleaned:
        addon_name, marketplace = cleaned.rsplit("@", 1)
        if not addon_name or not marketplace:
            msg = f"Invalid addon id {addon_id!r}; expected name@marketplace"
            raise ValueError(msg)
        return addon_name, marketplace
    if default_marketplace:
        return cleaned, default_marketplace
    from talkops.config.addons import get_primary_default_addon_marketplace_name

    return cleaned, get_primary_default_addon_marketplace_name()

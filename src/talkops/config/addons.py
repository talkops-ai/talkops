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

"""Addon marketplace configuration for TalkOps Server Mode."""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from typing import Any, Final

from talkops.utils.logger import get_logger

logger = get_logger(__name__)

_GITHUB_URL_PATTERN = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:)?([^/]+)/([^/]+?)(?:\.git)?/?$"
)


@dataclass(frozen=True, slots=True)
class DefaultAddonMarketplaceConfig:
    """Specification for an out-of-the-box or organization-default addon marketplace.

    Uses `.talkops/marketplace.json` by convention across all repositories.
    """

    name: str
    repository_url: str
    manifest_url: str
    ref: str = "main"
    description: str = ""

    @classmethod
    def from_github(
        cls,
        repo: str,
        *,
        name: str | None = None,
        ref: str = "main",
        manifest_path: str = ".talkops/marketplace.json",
        description: str = "",
    ) -> DefaultAddonMarketplaceConfig:
        """Create a default addon marketplace configuration from a GitHub repository slug or URL."""
        match = _GITHUB_URL_PATTERN.match(repo.strip())
        if not match:
            msg = f"Invalid GitHub repository specifier: {repo!r}. Expected 'owner/repo' or GitHub URL."
            raise ValueError(msg)

        owner, repo_name = match.group(1), match.group(2)
        resolved_name = name or repo_name
        clean_manifest_path = manifest_path.lstrip("/")

        return cls(
            name=resolved_name,
            repository_url=f"https://github.com/{owner}/{repo_name}.git",
            manifest_url=(
                f"https://raw.githubusercontent.com/{owner}/{repo_name}/{ref}/{clean_manifest_path}"
            ),
            ref=ref,
            description=description,
        )


DEFAULT_ADDON_MARKETPLACE_CONFIGS: Final[tuple[DefaultAddonMarketplaceConfig, ...]] = (
    DefaultAddonMarketplaceConfig.from_github(
        "talkops-ai/talkops-addons",
        name="talkops-addons",
        ref="main",
        manifest_path=".talkops/marketplace.json",
        description="Official TalkOps Server Addons Marketplace (Channels, Integrations, Enterprise)",
    ),
)


def get_effective_default_addon_marketplace_configs() -> tuple[DefaultAddonMarketplaceConfig, ...]:
    """Return configured default addon marketplaces, honoring organization env overrides.

    Organizations can set `TALKOPS_ADDON_MARKETPLACE_REPO` (e.g. `my-org/talkops-addons`)
    and optional `TALKOPS_ADDON_MARKETPLACE_REF` / `TALKOPS_ADDON_MARKETPLACE_NAME` to point
    TalkOps at an internal or custom central addon repository across all environments.
    """
    env_repo = os.environ.get("TALKOPS_ADDON_MARKETPLACE_REPO", "").strip()
    if env_repo:
        env_ref = os.environ.get("TALKOPS_ADDON_MARKETPLACE_REF", "main").strip() or "main"
        env_name = os.environ.get("TALKOPS_ADDON_MARKETPLACE_NAME", "").strip() or None
        try:
            custom_default = DefaultAddonMarketplaceConfig.from_github(
                env_repo,
                name=env_name,
                ref=env_ref,
                manifest_path=".talkops/marketplace.json",
                description=f"Organization default addon marketplace ({env_repo})",
            )
            return (custom_default, *DEFAULT_ADDON_MARKETPLACE_CONFIGS)
        except ValueError as exc:
            logger.warning("Ignoring invalid TALKOPS_ADDON_MARKETPLACE_REPO=%r: %s", env_repo, exc)
    return DEFAULT_ADDON_MARKETPLACE_CONFIGS


def get_default_addon_marketplace_config(name: str) -> DefaultAddonMarketplaceConfig | None:
    """Look up a default addon marketplace configuration by name."""
    for config in get_effective_default_addon_marketplace_configs():
        if config.name == name:
            return config
    return None


def is_default_addon_marketplace(name: str) -> bool:
    """Check if a marketplace name matches any configured default addon marketplace."""
    return get_default_addon_marketplace_config(name) is not None


def get_primary_default_addon_marketplace_name() -> str:
    """Return the name of the primary default addon marketplace."""
    configs = get_effective_default_addon_marketplace_configs()
    return configs[0].name if configs else "talkops-addons"


def get_default_addon_marketplace_names() -> frozenset[str]:
    """Return the set of all configured default addon marketplace names."""
    return frozenset(c.name for c in get_effective_default_addon_marketplace_configs())


def fetch_default_addon_marketplace_manifest(
    config: DefaultAddonMarketplaceConfig,
    *,
    timeout: float = 10.0,
) -> dict[str, Any] | None:
    """Fetch the remote `.talkops/marketplace.json` manifest for a default addon marketplace."""
    try:
        req = urllib.request.Request(
            config.manifest_url,
            headers={"User-Agent": "TalkOps-Addon-Client/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
    except Exception as exc:
        logger.debug(
            "Failed to fetch remote addon marketplace manifest from %s: %s",
            config.manifest_url,
            exc,
        )
    return None


__all__ = [
    "DEFAULT_ADDON_MARKETPLACE_CONFIGS",
    "DefaultAddonMarketplaceConfig",
    "fetch_default_addon_marketplace_manifest",
    "get_default_addon_marketplace_config",
    "get_default_addon_marketplace_names",
    "get_effective_default_addon_marketplace_configs",
    "get_primary_default_addon_marketplace_name",
    "is_default_addon_marketplace",
]

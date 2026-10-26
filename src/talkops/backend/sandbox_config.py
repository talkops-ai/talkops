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

"""Configuration model for sandboxed backend execution environments in TalkOps.

Supports both environment variable configuration and `~/.talkops/config.toml`
(including `[sandboxes]` and `[sandboxes.providers.<name>]`).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

logger = logging.getLogger(__name__)


class ProviderConfigEntry(TypedDict, total=False):
    """Configuration for a single config-declared sandbox provider.

    Declared under `[sandboxes.providers.<name>]` in `config.toml`.
    """

    class_path: str
    """Fully-qualified provider class in `module.path:ClassName` format."""

    working_dir: str
    """Default working directory inside the sandbox."""

    package: str
    """Package name suggested in install error messages."""

    supports_sandbox_id: bool
    """Whether the provider can reattach to an existing sandbox by id."""

    supports_snapshot_name: bool
    """Whether the provider honors `--sandbox-snapshot-name`."""

    params: dict[str, Any]
    """Extra keyword arguments forwarded to `get_or_create`."""


def _validate_provider_entry(
    name: str,
    raw: dict[str, Any],
) -> ProviderConfigEntry | None:
    """Validate a single `[sandboxes.providers.<name>]` table."""
    class_path = raw.get("class_path")
    if not isinstance(class_path, str) or ":" not in class_path:
        logger.warning(
            "[sandboxes.providers.%s]: 'class_path' must be a string in "
            "'module.path:ClassName' format; skipping provider",
            name,
        )
        return None
    mod_part, _, attr_part = class_path.partition(":")
    if not mod_part or not attr_part:
        logger.warning(
            "[sandboxes.providers.%s]: 'class_path' %r is missing module or "
            "class name; skipping provider",
            name,
            class_path,
        )
        return None

    entry: ProviderConfigEntry = {"class_path": class_path}

    for key in ("working_dir", "package"):
        if key in raw:
            val = raw[key]
            if isinstance(val, str) and val:
                entry[key] = val  # type: ignore[literal-required]
            else:
                logger.warning(
                    "[sandboxes.providers.%s]: %r must be a non-empty string; ignoring",
                    name,
                    key,
                )

    for key in ("supports_sandbox_id", "supports_snapshot_name"):
        if key in raw:
            val = raw[key]
            if isinstance(val, bool):
                entry[key] = val  # type: ignore[literal-required]
            else:
                logger.warning(
                    "[sandboxes.providers.%s]: %r must be a boolean; ignoring",
                    name,
                    key,
                )

    if "params" in raw:
        params = raw["params"]
        if isinstance(params, dict):
            entry["params"] = dict(params)
        else:
            logger.warning(
                "[sandboxes.providers.%s]: 'params' must be a table; ignoring",
                name,
            )

    return entry


@dataclass
class SandboxConfig:
    """Settings required to provision, configure, and connect to a sandbox environment."""

    provider: str = "local"
    """Target provider identifier ('local', 'langsmith', 'daytona', 'modal', 'runloop', 'vercel', 'agentcore')."""

    sandbox_id: str | None = None
    """Optional existing remote sandbox ID to reconnect to."""

    image: str | None = None
    """Optional container base image name or Dockerfile ref."""

    setup_script_path: str | None = None
    """Optional path to a shell script to run inside the sandbox on boot."""

    snapshot_name: str | None = None
    """Optional snapshot or blueprint name to boot from (supported by LangSmith, Daytona, Runloop)."""

    sync_workspace: bool = True
    """Whether to automatically synchronize local project files into the sandbox (default True)."""

    providers: dict[str, Any] = field(default_factory=dict)
    """Provider-specific configurations parsed from [sandboxes.<provider>] and [sandboxes.providers.<name>]."""

    parse_error: str | None = None
    """Error message if `config.toml` existed but could not be parsed."""

    @property
    def default(self) -> str | None:
        """The configured default sandbox provider, or `None` when local/unset."""
        if self.provider and self.provider not in ("local", "none", ""):
            return self.provider
        return None

    @classmethod
    def load(cls, config_path: Path | None = None) -> SandboxConfig:
        """Load sandbox configuration with layered precedence: Env > TOML > Defaults."""
        from talkops.config.toml_config import read_config_toml

        parse_error: str | None = None
        if config_path is not None and config_path.is_file():
            import tomllib

            try:
                with config_path.open("rb") as f:
                    toml_data = tomllib.load(f)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                logger.warning("Failed to parse sandbox config from %s: %s", config_path, exc)
                parse_error = f"{config_path}: {exc}"
                toml_data = {}
        else:
            toml_data = read_config_toml(config_path)

        sandboxes_table = toml_data.get("sandboxes", {})
        if not isinstance(sandboxes_table, dict):
            parse_error = parse_error or "[sandboxes] is not a table"
            sandboxes_table = {}

        # Provider / default
        env_provider = (
            os.getenv("TALKOPS_SERVER_SANDBOX_TYPE")
            or os.getenv("TALKOPS_SANDBOX_PROVIDER")
            or os.getenv("SANDBOX_PROVIDER")
            or os.getenv("SANDBOX_TYPE")
        )
        raw_toml_default = sandboxes_table.get("default")
        if raw_toml_default is not None and not isinstance(raw_toml_default, str):
            logger.warning("[sandboxes].default is not a string; ignoring the default sandbox")
            raw_toml_default = None
        raw_toml_provider = sandboxes_table.get("provider")
        if raw_toml_provider is not None and not isinstance(raw_toml_provider, str):
            raw_toml_provider = None
        toml_provider = (raw_toml_default or raw_toml_provider or "").strip() or None
        provider = env_provider or toml_provider or "local"

        # Sandbox ID
        env_sandbox_id = (
            os.getenv("TALKOPS_SERVER_SANDBOX_ID")
            or os.getenv("TALKOPS_SANDBOX_ID")
            or os.getenv("SANDBOX_ID")
        )
        toml_sandbox_id = sandboxes_table.get("sandbox_id")
        sandbox_id = env_sandbox_id or toml_sandbox_id

        # Image
        env_image = os.getenv("TALKOPS_SANDBOX_IMAGE") or os.getenv("SANDBOX_IMAGE")
        toml_image = sandboxes_table.get("image")
        image = env_image or toml_image

        # Setup script path
        env_setup_script = (
            os.getenv("TALKOPS_SERVER_SANDBOX_SETUP")
            or os.getenv("TALKOPS_SANDBOX_SETUP_SCRIPT")
            or os.getenv("SANDBOX_SETUP_SCRIPT")
            or os.getenv("SANDBOX_SETUP")
        )
        toml_setup_script = sandboxes_table.get("setup_script_path") or sandboxes_table.get("setup")
        setup_script_path = env_setup_script or toml_setup_script

        # Snapshot name
        env_snapshot = (
            os.getenv("TALKOPS_SERVER_SANDBOX_SNAPSHOT_NAME")
            or os.getenv("TALKOPS_SANDBOX_SNAPSHOT")
            or os.getenv("SANDBOX_SNAPSHOT")
            or os.getenv("SANDBOX_SNAPSHOT_NAME")
            or os.getenv("LANGSMITH_SANDBOX_SNAPSHOT_NAME")
        )
        toml_snapshot = sandboxes_table.get("snapshot_name") or sandboxes_table.get("snapshot")
        snapshot_name = env_snapshot or toml_snapshot

        # Sync workspace
        sync_raw = os.getenv("TALKOPS_SANDBOX_SYNC_WORKSPACE") or os.getenv("SANDBOX_SYNC_WORKSPACE")
        if sync_raw is not None:
            sync_workspace = sync_raw.lower() not in ("false", "0", "no")
        elif "sync_workspace" in sandboxes_table:
            val = sandboxes_table["sync_workspace"]
            sync_workspace = bool(val) if not isinstance(val, str) else val.lower() not in ("false", "0", "no")
        else:
            sync_workspace = True

        # Extract nested provider tables (both `[sandboxes.<provider>]` and `[sandboxes.providers.<name>]`)
        nested_providers: dict[str, Any] = {}
        for k, v in sandboxes_table.items():
            if k == "providers":
                if isinstance(v, dict):
                    for prov_name, prov_entry in v.items():
                        if not isinstance(prov_entry, dict):
                            logger.warning(
                                "[sandboxes.providers.%s] is not a table; skipping provider",
                                prov_name,
                            )
                            continue
                        validated = _validate_provider_entry(prov_name, prov_entry)
                        if validated is not None:
                            nested_providers[prov_name] = validated
                else:
                    logger.warning("[sandboxes.providers] is not a table; ignoring sandbox providers")
            elif isinstance(v, dict):
                # Support both direct provider parameter tables (e.g. `[sandboxes.daytona]`)
                # and custom provider declarations with `class_path`
                if "class_path" in v:
                    validated = _validate_provider_entry(k, v)
                    if validated is not None:
                        nested_providers[k] = validated
                else:
                    nested_providers[k] = dict(v)

        provider_clean = str(provider).lower().strip()
        if provider_clean in ("none", ""):
            provider_clean = "local"

        return cls(
            provider=provider_clean,
            sandbox_id=str(sandbox_id) if sandbox_id else None,
            image=str(image) if image else None,
            setup_script_path=str(setup_script_path) if setup_script_path else None,
            snapshot_name=str(snapshot_name) if snapshot_name else None,
            sync_workspace=sync_workspace,
            providers=nested_providers,
            parse_error=parse_error,
        )

    @classmethod
    def from_env(cls) -> SandboxConfig:
        """Load sandbox configuration (delegates to load())."""
        return cls.load()


__all__ = ["ProviderConfigEntry", "SandboxConfig"]

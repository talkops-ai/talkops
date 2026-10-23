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

"""Configuration schemas, `addons.json` discovery, Server Profiles, and 4-Tier Precedence.

Design Principles:
1. **No Hardcoded Secret Providers**: Secrets are never hardcoded in addon configs. Users
   supply secrets via standard environment variables or `.talkops/.env` / `~/.talkops/.env`
   (loaded automatically by TalkOps's bootstrap). Config files may optionally reference
   environment variable names via `${ENV_VAR}` or `<field>_env`.
2. **Industry-Standard Declarative Addon Configuration (`addons.json` / `--addons-config`)**:
   Mirrors TalkOps's `.mcp.json` discovery pattern so addons living in completely separate
   repositories (TalkOps official enterprise repos or custom user repos) can be hooked in
   at runtime without modifying TalkOps core.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from talkops.config.paths import (
    DATA_DIR,
    USER_SETTINGS_PATH,
    find_project_root,
    project_local_settings_path,
    project_settings_path,
    project_talkops_dir,
)
from talkops.config.settings import _ensure_bootstrap
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

ServerProfileName = Literal["default", "api-only", "webhook-worker", "enterprise-full"]

# Built-in profiles. External packages or `addons.json` can also register/extend profiles.
SERVER_PROFILES: dict[str, tuple[str, ...]] = {
    "default": ("client-gateway", "core-auth", "webhooks"),
    "api-only": ("client-gateway", "core-auth"),
    "webhook-worker": ("webhooks",),
    "enterprise-full": ("client-gateway", "core-auth", "webhooks"),
}


def register_server_profile(name: str, addon_names: tuple[str, ...] | list[str]) -> None:
    """Register or override a named server profile (usable by external addon packages)."""
    SERVER_PROFILES[name.strip().lower()] = tuple(addon_names)


class AddonSettings(BaseModel):
    """Base configuration model inherited by all ServerAddon settings schemas."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    port: int | None = Field(
        default=None,
        description="Optional dedicated port. When set, AddonManager binds a secondary uvicorn.Server on the shared asyncio loop.",
    )
    mount_prefix: str = Field(
        default="",
        description="Optional URL prefix prepended to all routes registered by this addon.",
    )
    package: str | None = Field(
        default=None,
        description="Optional Python package import spec ('talkops_addons.slack:SlackAddon') from an external repository.",
    )
    module: str | None = Field(
        default=None,
        description="Alias for 'package': module:Class or file.py:Class path for loading external/custom addons.",
    )
    path: str | None = Field(
        default=None,
        description="Optional filesystem path to an external repository file or directory ('/path/to/repo/addon.py:MyAddon').",
    )
    python_path: list[str] = Field(
        default_factory=list,
        description="Optional list of directories to add to sys.path before importing 'package' / 'module'.",
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional addon-specific runtime settings dictionary.",
    )


def resolve_env_reference(raw_value: str | None, environ: Mapping[str, str] | None = None) -> str | None:
    """Resolve `${ENV_VAR}` or `env://ENV_VAR` from the environment (populated from shell & `.talkops/.env`)."""
    if not raw_value or not isinstance(raw_value, str):
        return raw_value

    _ensure_bootstrap()
    env_map: Mapping[str, str] = environ if environ is not None else os.environ
    value = raw_value.strip()

    if value.startswith("env://"):
        env_var = value[len("env://") :].strip()
        return env_map.get(env_var)

    if value.startswith("${") and value.endswith("}"):
        env_var = value[2:-1].strip()
        return env_map.get(env_var)

    return raw_value


# Backward-compatible alias
resolve_secret_value = resolve_env_reference


def resolve_env_references_in_mapping(
    raw_dict: dict[str, Any],
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Recursively resolve `${ENV_VAR}`, `env://VAR`, and `<field>_env` indirection from `.env` / `os.environ`."""
    _ensure_bootstrap()
    env_map: Mapping[str, str] = environ if environ is not None else os.environ
    resolved: dict[str, Any] = {}
    for key, val in raw_dict.items():
        if isinstance(val, dict):
            resolved[key] = resolve_env_references_in_mapping(val, environ=env_map)
        elif isinstance(val, str):
            resolved[key] = resolve_env_reference(val, environ=env_map)
        else:
            resolved[key] = val

    # Support `<field>_env: "MY_ENV_VAR"` indirection (reads value from os.environ / .talkops/.env)
    for key, val in list(resolved.items()):
        if key.endswith("_env") and isinstance(val, str) and val.strip():
            target_key = key[: -len("_env")]
            env_val = env_map.get(val.strip())
            if env_val is not None and not resolved.get(target_key):
                resolved[target_key] = env_val
    return resolved


# Backward-compatible alias
resolve_secrets_in_mapping = resolve_env_references_in_mapping


def _extract_addons_from_json_data(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract normalized `{addon_name: config_dict}` from an `addons.json` or `settings.json` payload."""
    extracted: dict[str, dict[str, Any]] = {}

    # 1. Backward compatibility: top-level `"webhooks"` block in `settings.json`
    webhooks_sec = raw.get("webhooks")
    if isinstance(webhooks_sec, dict):
        wh_target = extracted.setdefault("webhooks", {})
        _deep_merge_dict(wh_target, webhooks_sec)

    # 2. Top-level `"profiles"` registration in `addons.json` (optional)
    profiles_sec = raw.get("profiles")
    if isinstance(profiles_sec, dict):
        for prof_name, prof_addons in profiles_sec.items():
            if isinstance(prof_addons, list):
                register_server_profile(prof_name, [str(x) for x in prof_addons])

    # 3. Canonical `"addons"` block (dict or list format)
    addons_sec = raw.get("addons")
    if isinstance(addons_sec, dict):
        for addon_name, addon_cfg in addons_sec.items():
            norm_name = addon_name.strip().lower().replace("_", "-")
            if isinstance(addon_cfg, dict):
                target = extracted.setdefault(norm_name, {})
                # Flatten nested `"config"` keys onto top-level while keeping `"config"` accessible
                _deep_merge_dict(target, addon_cfg)
                if isinstance(addon_cfg.get("config"), dict):
                    for ck, cv in addon_cfg["config"].items():
                        target.setdefault(ck, cv)
            elif isinstance(addon_cfg, bool):
                target = extracted.setdefault(norm_name, {})
                target["enabled"] = addon_cfg
            elif isinstance(addon_cfg, str):
                # Shorthand: `"my-addon": "my_repo_pkg.addon:MyAddon"`
                target = extracted.setdefault(norm_name, {})
                target["enabled"] = True
                target["package"] = addon_cfg
    elif isinstance(addons_sec, list):
        # Also support array of addon objects: `{"addons": [{"name": "chatops", "package": "...", "enabled": true}]}`
        for entry in addons_sec:
            if isinstance(entry, dict) and entry.get("name"):
                norm_name = str(entry["name"]).strip().lower().replace("_", "-")
                target = extracted.setdefault(norm_name, {})
                _deep_merge_dict(target, entry)
                if isinstance(entry.get("config"), dict):
                    for ck, cv in entry["config"].items():
                        target.setdefault(ck, cv)
            elif isinstance(entry, str) and entry.strip():
                norm_name = entry.strip().lower().replace("_", "-")
                extracted.setdefault(norm_name, {})["enabled"] = True

    return extracted


def _load_project_dotenv(project_root: Path | None = None) -> Path:
    """Ensure `.talkops/.env` and `.env` are loaded into `os.environ` for the active project."""
    _ensure_bootstrap()
    effective_root = project_root or find_project_root() or Path.cwd()
    try:
        import dotenv

        for candidate in (
            project_talkops_dir(effective_root) / ".env",
            effective_root / ".env",
        ):
            if candidate.is_file():
                dotenv.load_dotenv(dotenv_path=candidate, override=False)
    except Exception:
        pass
    return effective_root


def load_merged_addons_settings_json(
    project_root: Path | None = None,
    addons_config_path: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    """Discover and merge addon configurations across `addons.json`, `settings.json`, and `--addons-config`.

    Discovery order (lowest to highest precedence, matching `.mcp.json`):
    1. User global: `~/.talkops/addons.json`, `~/.talkops/.addons.json`, `~/.talkops/settings.json`
    2. Project scope: `{project_root}/.talkops/addons.json`, `{project_root}/.addons.json`,
       `{project_root}/addons.json`, `{project_root}/.talkops/settings.json`,
       `{project_root}/.talkops/settings.local.json`
    3. Explicit runtime config: `--addons-config <path>` or `TALKOPS_ADDONS_CONFIG=<path>`
    """
    effective_root = _load_project_dotenv(project_root)
    proj_talkops = project_talkops_dir(effective_root)

    candidate_paths: list[Path] = [
        DATA_DIR / "addons.json",
        DATA_DIR / ".addons.json",
        USER_SETTINGS_PATH,
        proj_talkops / "addons.json",
        proj_talkops / ".addons.json",
        effective_root / ".addons.json",
        effective_root / "addons.json",
        project_settings_path(effective_root),
        project_local_settings_path(effective_root),
    ]

    explicit_cfg = addons_config_path or os.environ.get("TALKOPS_ADDONS_CONFIG")
    if explicit_cfg:
        exp_path = Path(explicit_cfg).expanduser()
        if not exp_path.is_absolute():
            exp_path = (effective_root / exp_path).resolve()
        candidate_paths.append(exp_path)

    merged_addons: dict[str, dict[str, Any]] = {}
    seen_paths: set[Path] = set()

    for path in candidate_paths:
        try:
            resolved_path = path.resolve()
        except Exception:
            resolved_path = path
        if resolved_path in seen_paths or not path.is_file():
            continue
        seen_paths.add(resolved_path)

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse addon config file %s: %s", path, exc)
            continue
        if not isinstance(raw, dict):
            continue

        extracted = _extract_addons_from_json_data(raw)
        for addon_name, addon_cfg in extracted.items():
            target = merged_addons.setdefault(addon_name, {})
            _deep_merge_dict(target, addon_cfg)

    return {k: resolve_env_references_in_mapping(v) for k, v in merged_addons.items()}


def _deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> None:
    """Recursively merge `override` into `base` in-place."""
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge_dict(base[k], v)
        else:
            base[k] = v


def _parse_csv_names(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        items: list[str] = []
        for entry in raw:
            items.extend(part.strip().lower().replace("_", "-") for part in str(entry).split(",") if part.strip())
        return items
    return [part.strip().lower().replace("_", "-") for part in str(raw).split(",") if part.strip()]


def _coerce_env_scalar(raw: str, environ: dict[str, str] | None = None) -> Any:
    lowered = raw.strip().lower()
    if lowered in ("true", "1", "yes", "on"):
        return True
    if lowered in ("false", "0", "no", "off"):
        return False
    if lowered.isdigit():
        return int(lowered)
    try:
        return float(raw)
    except ValueError:
        pass
    if (raw.startswith("{") and raw.endswith("}")) or (raw.startswith("[") and raw.endswith("]")):
        try:
            return json.loads(raw)
        except Exception:
            pass
    return resolve_env_reference(raw, environ=environ)


def resolve_addon_configs(
    discovered_addons: dict[str, Any],
    *,
    profile: str | None = None,
    enabled_addons: str | list[str] | tuple[str, ...] | None = None,
    disabled_addons: str | list[str] | tuple[str, ...] | None = None,
    webhook_port: int | None = None,
    project_root: Path | None = None,
    addons_config_path: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Resolve configuration dictionaries for all addons using the 4-tier precedence hierarchy.

    Returns:
        Tuple of `(resolved_profile_name, {addon_name: resolved_config_dict})`.
    """
    _load_project_dotenv(project_root)
    environ = env if env is not None else dict(os.environ)

    # Load file configs early so any custom `"profiles"` in `addons.json` are registered
    file_configs = load_merged_addons_settings_json(
        project_root=project_root,
        addons_config_path=addons_config_path,
    )

    # ── Tier 1: Addon Default Schema ──────────────────────────────────────
    resolved: dict[str, dict[str, Any]] = {}
    for name, addon in discovered_addons.items():
        default_enabled = bool(getattr(addon, "enabled_by_default", False))
        resolved[name] = {"enabled": default_enabled}

    # ── Tier 2: Server Profile ────────────────────────────────────────────
    active_profile = (
        profile
        or environ.get("TALKOPS_SERVER_PROFILE")
        or environ.get("TALKOPS_PROFILE")
        or "default"
    ).strip().lower()

    if active_profile not in SERVER_PROFILES:
        logger.warning(
            "Unknown server profile '%s'; falling back to 'default'. Known profiles: %s",
            active_profile,
            ", ".join(sorted(SERVER_PROFILES)),
        )
        active_profile = "default"

    profile_addons = set(SERVER_PROFILES[active_profile])
    for name in set(resolved.keys()) | profile_addons:
        resolved.setdefault(name, {})["enabled"] = name in profile_addons

    # ── Tier 3: Merged `addons.json` / `settings.json` / Marketplace State ─
    for name, addon in discovered_addons.items():
        src = str(getattr(addon, "source", "") or "")
        if src.startswith("marketplace:") and bool(getattr(addon, "enabled_by_default", False)):
            resolved.setdefault(name, {})["enabled"] = True

    for name, cfg_dict in file_configs.items():
        target = resolved.setdefault(name, {"enabled": True})
        _deep_merge_dict(target, cfg_dict)

    # ── Tier 4: Environment Variables & Explicit CLI Flags ────────────────
    # 4a. Per-addon field overrides: TALKOPS_ADDON_<NAME>_<FIELD>=value
    prefix = "TALKOPS_ADDON_"
    known_names_sorted = sorted(set(resolved.keys()) | set(discovered_addons.keys()), key=len, reverse=True)
    for env_key, env_val in environ.items():
        if not env_key.startswith(prefix) or not env_val:
            continue
        remainder = env_key[len(prefix) :]
        matched_addon: str | None = None
        field_part: str | None = None
        for candidate in known_names_sorted:
            cand_env = candidate.upper().replace("-", "_")
            if remainder.startswith(cand_env + "_"):
                matched_addon = candidate
                field_part = remainder[len(cand_env) + 1 :].lower()
                break
        if matched_addon is None and "_" in remainder:
            raw_addon, raw_field = remainder.split("_", 1)
            matched_addon = raw_addon.lower().replace("_", "-")
            field_part = raw_field.lower()
        if matched_addon and field_part:
            target = resolved.setdefault(matched_addon, {"enabled": True})
            target[field_part] = _coerce_env_scalar(env_val, environ=environ)

    # 4b. Dedicated webhook port env / CLI flag
    effective_webhook_port = webhook_port
    if effective_webhook_port is None:
        raw_wh_port = environ.get("TALKOPS_WEBHOOK_PORT")
        if raw_wh_port and raw_wh_port.strip().isdigit():
            effective_webhook_port = int(raw_wh_port.strip())
    if effective_webhook_port is not None:
        resolved.setdefault("webhooks", {"enabled": True})["port"] = effective_webhook_port

    # 4c. Explicit enable list (`TALKOPS_ADDONS` / `--addons`)
    env_enable = _parse_csv_names(environ.get("TALKOPS_ADDONS"))
    cli_enable = _parse_csv_names(enabled_addons)
    for name in env_enable + cli_enable:
        if name == "all":
            for known in resolved:
                resolved[known]["enabled"] = True
        else:
            resolved.setdefault(name, {})["enabled"] = True

    # 4d. Explicit disable list (`TALKOPS_DISABLE_ADDONS` / `--disable-addons`)
    env_disable = _parse_csv_names(environ.get("TALKOPS_DISABLE_ADDONS"))
    cli_disable = _parse_csv_names(disabled_addons)
    for name in env_disable + cli_disable:
        if name in resolved:
            resolved[name]["enabled"] = False
        else:
            resolved[name] = {"enabled": False}

    # 4e. If an external auth provider addon (e.g. `enterprise-auth`) is enabled and discovered,
    # or any addon with `capabilities.auth_provider` other than `core-auth` is enabled, it can supersede `core-auth`.
    if (
        resolved.get("enterprise-auth", {}).get("enabled")
        and "enterprise-auth" in discovered_addons
        and "core-auth" in resolved
    ):
        resolved["core-auth"]["enabled"] = False

    return active_profile, resolved

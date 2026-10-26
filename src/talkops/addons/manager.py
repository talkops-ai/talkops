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

"""AddonManager — discovery, validation, lifecycle orchestration, and ASGI assembly.

Supports hooking in addons from completely separate repositories (TalkOps official
addon packages or user-authored custom repositories) at runtime without modifying
TalkOps core code:
1. **Built-in Default Addons** (`talkops.addons.builtin`: `core-auth`, `client-gateway`, `webhooks`)
2. **Installed External Repository Packages** via `importlib.metadata.entry_points(group="talkops.addons")`
3. **Declarative `addons.json` / `--addons-config` / `settings.json`**:
   - `"package"` / `"module"`: `"talkops_addons.slack:SlackAddon"`
   - `"path"`: `"/path/to/other-repo/my_addon.py:MyAddon"` (or directory containing `addon.py`)
   - `"python_path"`: `["/path/to/other-repo/src"]` (injected into `sys.path` before import)
4. **Drop-in Directory & CLI Specs**: `~/.talkops/addons/*.py`, `.talkops/addons/*.py`,
   `TALKOPS_CUSTOM_ADDONS`, `--custom-addon`, and programmatic `@register_addon`.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import importlib.util
import inspect
import os
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Mount, Route

from talkops.addons.config import (
    AddonSettings,
    load_merged_addons_settings_json,
    resolve_addon_configs,
)
from talkops.addons.gateway import AgentRunGateway, HookEventBus
from talkops.addons.protocol import (
    ADDON_API_VERSION,
    ADDON_ENTRYPOINT_GROUP,
    AddonContext,
    AddonHealthStatus,
    BaseServerAddon,
    CompanionProcessSpec,
    IdentityMapper,
    ServerAddon,
)
from talkops.config.paths import DATA_DIR, find_project_root, project_talkops_dir
from talkops.config.settings import get_settings
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

# Global programmatic registry for custom addons registered via `register_addon()`
_PROGRAMMATIC_ADDONS: dict[str, Any] = {}


def register_addon(
    addon_or_factory: ServerAddon | BaseServerAddon | type[Any] | Callable[[], Any],
    *,
    name: str | None = None,
) -> Any:
    """Programmatically register a custom or external `ServerAddon` class, instance, or factory."""
    addon_name = name or getattr(addon_or_factory, "name", None)
    if not addon_name and callable(addon_or_factory) and not inspect.isclass(addon_or_factory):
        instance = addon_or_factory()
        addon_name = getattr(instance, "name", None)
        addon_or_factory = instance
    if not addon_name or not isinstance(addon_name, str):
        raise ValueError("Addon must define a non-empty string 'name' attribute")
    norm_name = addon_name.strip().lower().replace("_", "-")
    _PROGRAMMATIC_ADDONS[norm_name] = addon_or_factory
    return addon_or_factory


def unregister_addon(name: str) -> None:
    """Remove a programmatically registered addon by name."""
    norm_name = name.strip().lower().replace("_", "-")
    _PROGRAMMATIC_ADDONS.pop(norm_name, None)


def clear_registered_addons() -> None:
    """Clear all programmatically registered addons (primarily for unit tests)."""
    _PROGRAMMATIC_ADDONS.clear()


def _instantiate_addon(
    target: Any,
    *,
    source: str,
    override_name: str | None = None,
) -> ServerAddon | None:
    """Instantiate a `ServerAddon` from a class, factory callable, or instance."""
    try:
        instance: Any
        if inspect.isclass(target):
            instance = target()
        elif isinstance(target, BaseServerAddon) or (
            hasattr(target, "name")
            and hasattr(target, "on_startup")
            and hasattr(target, "get_routes")
        ):
            instance = target
        elif callable(target):
            instance = target()
        else:
            logger.warning("Object %r from %s is not a valid ServerAddon", target, source)
            return None

        if override_name and getattr(instance, "name", "unnamed-addon") == "unnamed-addon":
            instance.name = override_name
        if hasattr(instance, "source"):
            instance.source = source
        return instance
    except Exception as exc:
        logger.warning("Failed to instantiate ServerAddon from %s: %s", source, exc, exc_info=True)
        return None


def _ensure_python_paths(paths: list[str] | tuple[str, ...], *, base_dir: Path) -> None:
    """Prepend external repository directories to `sys.path` if not already present."""
    for raw_p in paths:
        if not raw_p or not str(raw_p).strip():
            continue
        p = Path(str(raw_p).strip()).expanduser()
        if not p.is_absolute():
            p = (base_dir / p).resolve()
        p_str = str(p)
        if p.is_dir() and p_str not in sys.path:
            sys.path.insert(0, p_str)


def load_addon_from_spec(
    spec: str,
    *,
    project_root: Path | None = None,
    python_paths: list[str] | tuple[str, ...] | None = None,
    override_name: str | None = None,
) -> ServerAddon | None:
    """Dynamically load a `ServerAddon` from an external package/module or repository file/directory path.

    Supported `spec` formats:
    - Python package/module spec: `"talkops_addons.chatops:ChatOpsAddon"` or `"talkops_addons.chatops"`
    - File path spec: `"/path/to/repo/my_addon.py:MyAddon"` or `"./addons/my_addon.py"`
    - Directory path spec: `"/path/to/repo/my_addon_dir"` (loads `addon.py` or `__init__.py` inside it)
    """
    raw = spec.strip()
    if not raw:
        return None

    base = project_root or find_project_root() or Path.cwd()
    if python_paths:
        _ensure_python_paths(python_paths, base_dir=base)

    target_part, _, attr_name = raw.partition(":")
    target_part = target_part.strip()
    attr_name = attr_name.strip()

    # Check if `target_part` resolves to a filesystem file or directory (external repo path)
    candidate_path = Path(target_part).expanduser()
    if not candidate_path.is_absolute():
        candidate_path = (base / candidate_path).resolve()

    is_filesystem_spec = (
        target_part.endswith(".py")
        or "/" in target_part
        or "\\" in target_part
        or candidate_path.exists()
    )

    if is_filesystem_spec:
        file_path = candidate_path
        if file_path.is_dir():
            # Check for `.talkops/addon.json` (or `addon.json`) manifest inside the directory
            try:
                from talkops.addons.manifest import (
                    ensure_addon_python_dependencies,
                    find_addon_manifest_path,
                    load_addon_manifest,
                )

                manifest_file = find_addon_manifest_path(file_path)
                if manifest_file is not None:
                    manifest = load_addon_manifest(
                        manifest_file,
                        fallback_name=override_name or file_path.name,
                    )
                    if manifest.python_dependencies:
                        ensure_addon_python_dependencies(
                            manifest.python_dependencies,
                            addon_name=manifest.name,
                        )
                    _ensure_python_paths(manifest.python_path, base_dir=file_path)
                    ep_spec = f"{manifest.entry_point}:{attr_name}" if attr_name and ":" not in manifest.entry_point else manifest.entry_point
                    ep_target, _, ep_attr = ep_spec.partition(":")
                    ep_candidate = (file_path / ep_target).resolve()
                    if ep_candidate.is_file():
                        file_path = ep_candidate
                        attr_name = ep_attr.strip() or attr_name
                    else:
                        try:
                            ep_module = importlib.import_module(ep_target.strip())
                            return _extract_addon_from_module(
                                ep_module,
                                attr_name=ep_attr.strip() or attr_name,
                                source=f"manifest:{manifest_file}",
                                override_name=override_name or manifest.name,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Failed to import entryPoint '%s' from %s: %s",
                                manifest.entry_point,
                                manifest_file,
                                exc,
                            )
            except Exception as exc:
                logger.debug("Could not load .talkops/addon.json from %s: %s", file_path, exc)

            # Add parent/repo directory to sys.path so relative/sibling imports work
            if str(file_path) not in sys.path:
                sys.path.insert(0, str(file_path))
            if file_path.is_dir():
                if (file_path / "addon.py").is_file():
                    file_path = file_path / "addon.py"
                elif (file_path / "__init__.py").is_file():
                    file_path = file_path / "__init__.py"
                else:
                    py_files = sorted(p for p in file_path.glob("*.py") if not p.name.startswith("_"))
                    if py_files:
                        file_path = py_files[0]

        if not file_path.is_file():
            logger.warning("External/custom addon file not found: %s (from spec '%s')", file_path, spec)
            return None

        if str(file_path.parent) not in sys.path:
            sys.path.insert(0, str(file_path.parent))

        mod_name = f"talkops_ext_addon_{file_path.stem}_{abs(hash(str(file_path))) % 100000}"
        mod_spec = importlib.util.spec_from_file_location(mod_name, file_path)
        if mod_spec is None or mod_spec.loader is None:
            logger.warning("Could not load module spec from file %s", file_path)
            return None
        module = importlib.util.module_from_spec(mod_spec)
        try:
            mod_spec.loader.exec_module(module)
        except Exception as exc:
            logger.warning("Failed executing addon file %s: %s", file_path, exc, exc_info=True)
            return None
        return _extract_addon_from_module(
            module,
            attr_name=attr_name,
            source=f"file:{file_path}",
            override_name=override_name,
        )

    # Case 2: Python dotted package/module path (e.g. installed from separate repo via pip/uv)
    try:
        module = importlib.import_module(target_part)
    except Exception as exc:
        logger.warning("Failed to import addon module '%s': %s", target_part, exc)
        return None

    return _extract_addon_from_module(
        module,
        attr_name=attr_name,
        source=f"package:{raw}",
        override_name=override_name,
    )


def _extract_addon_from_module(
    module: Any,
    *,
    attr_name: str = "",
    source: str,
    override_name: str | None = None,
) -> ServerAddon | None:
    if attr_name:
        if not hasattr(module, attr_name):
            logger.warning("Addon attribute '%s' not found in %s", attr_name, source)
            return None
        return _instantiate_addon(
            getattr(module, attr_name),
            source=source,
            override_name=override_name,
        )

    # Auto-detect `addon`, `create_addon`, `Addon`, or the first `BaseServerAddon` subclass
    for candidate_attr in ("addon", "create_addon", "Addon"):
        if hasattr(module, candidate_attr):
            inst = _instantiate_addon(
                getattr(module, candidate_attr),
                source=source,
                override_name=override_name,
            )
            if inst is not None:
                return inst

    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, BaseServerAddon) and obj is not BaseServerAddon:
            inst = _instantiate_addon(obj, source=source, override_name=override_name)
            if inst is not None:
                return inst
    logger.warning("No ServerAddon class or factory found in %s", source)
    return None


class AddonManager:
    """Discovers, validates, and manages the lifecycle of all TalkOps Server Addons."""

    def __init__(
        self,
        *,
        profile: str | None = None,
        enabled_addons: str | list[str] | tuple[str, ...] | None = None,
        disabled_addons: str | list[str] | tuple[str, ...] | None = None,
        custom_addon_specs: list[str] | tuple[str, ...] | None = None,
        addons_config_path: Path | str | None = None,
        webhook_port: int | None = None,
        main_port: int | None = None,
        host: str = "127.0.0.1",
        project_root: Path | None = None,
        gateway: AgentRunGateway | None = None,
        event_bus: HookEventBus | None = None,
        extra_addons: Sequence[Any] | None = None,
        strict_dependencies: bool = False,
    ) -> None:
        """Initialize AddonManager."""
        self._requested_profile = profile
        self._enabled_addons_override = enabled_addons
        self._disabled_addons_override = disabled_addons
        self._custom_addon_specs = list(custom_addon_specs or [])
        self._addons_config_path = addons_config_path
        self._webhook_port = webhook_port
        self._main_port = main_port
        self._host = host
        self._project_root = project_root or find_project_root() or Path.cwd()
        self._event_bus = event_bus or (gateway.event_bus if gateway is not None else HookEventBus())
        self._gateway = gateway or AgentRunGateway(
            event_bus=self._event_bus,
            cwd=str(self._project_root),
        )
        self._extra_addons = list(extra_addons or [])
        self._strict_dependencies = strict_dependencies

        self._discovered: dict[str, ServerAddon] = {}
        self._active_addons: list[ServerAddon] = []
        self._validated_configs: dict[str, Any] = {}
        self._resolved_profile: str = "default"
        self._context: AddonContext | None = None
        self._secondary_servers: dict[int, tuple[Any, asyncio.Task[Any]]] = {}
        self._started: bool = False

    @property
    def profile(self) -> str:
        """Return the profile property."""
        return self._resolved_profile

    @property
    def gateway(self) -> AgentRunGateway:
        """Return the gateway property."""
        return self._gateway

    @property
    def event_bus(self) -> HookEventBus:
        """Return the event bus property."""
        return self._event_bus

    @property
    def discovered_addons(self) -> dict[str, ServerAddon]:
        """Return the discovered addons property."""
        return dict(self._discovered)

    @property
    def active_addons(self) -> list[ServerAddon]:
        """Return the active addons property."""
        return list(self._active_addons)

    @property
    def context(self) -> AddonContext | None:
        """Return the context property."""
        return self._context

    def discover_addons(self) -> dict[str, ServerAddon]:
        """Discover all available addons across built-ins, external entry points, `addons.json`, and CLI."""
        discovered: dict[str, ServerAddon] = {}

        def _register_instance(addon: ServerAddon | None) -> None:
            if addon is None:
                return
            norm_name = addon.name.strip().lower().replace("_", "-")
            api_ver = getattr(addon, "api_version", ADDON_API_VERSION)
            if api_ver != ADDON_API_VERSION:
                logger.warning(
                    "Skipping addon '%s' (api_version=%s != required %s)",
                    norm_name,
                    api_ver,
                    ADDON_API_VERSION,
                )
                return
            discovered[norm_name] = addon

        # 1. Built-in Default Addons (`talkops.addons.builtin`: `core-auth`, `client-gateway`, `webhooks`)
        try:
            from talkops.addons.builtin import get_builtin_addons

            for addon in get_builtin_addons():
                if hasattr(addon, "source"):
                    addon.source = "builtin"
                _register_instance(addon)
        except Exception as exc:
            logger.error("Failed loading built-in addons: %s", exc, exc_info=True)

        # 2. External Python Package Entry Points (`importlib.metadata.entry_points(group="talkops.addons")`)
        # External repositories (e.g., TalkOps official addon packages or third-party wheels)
        # register here via `[project.entry-points."talkops.addons"]`.
        try:
            eps = importlib.metadata.entry_points(group=ADDON_ENTRYPOINT_GROUP)
            for ep in eps:
                try:
                    loaded = ep.load()
                    inst = _instantiate_addon(
                        loaded,
                        source=f"entrypoint:{ep.value}",
                        override_name=ep.name,
                    )
                    _register_instance(inst)
                except Exception as exc:
                    logger.warning(
                        "Failed to load addon entry point '%s' (%s): %s",
                        ep.name,
                        ep.value,
                        exc,
                    )
        except Exception as exc:
            logger.warning("Error querying entry_points(group='%s'): %s", ADDON_ENTRYPOINT_GROUP, exc)

        # 3. Drop-in addon directories (`~/.talkops/addons/*.py` and `.talkops/addons/*.py`)
        for addon_dir in (DATA_DIR / "addons", project_talkops_dir(self._project_root) / "addons"):
            if addon_dir.is_dir():
                for py_file in sorted(addon_dir.glob("*.py")):
                    if py_file.name.startswith("_"):
                        continue
                    inst = load_addon_from_spec(str(py_file), project_root=self._project_root)
                    _register_instance(inst)

        # 4. Declarative `addons.json` / `settings.json` / `--addons-config` (`package`, `module`, `path`, `python_path`)
        file_settings = load_merged_addons_settings_json(
            project_root=self._project_root,
            addons_config_path=self._addons_config_path,
        )

        # 4a. Installed & Marketplace Addons (`~/.talkops/addons/cache/...` & `.talkops/marketplace.json`)
        try:
            from talkops.addons.discovery import discover_marketplace_addons

            requested_names: set[str] = set()
            for cfg_name, cfg_dict in file_settings.items():
                if isinstance(cfg_dict, dict) and cfg_dict.get("enabled") is True:
                    requested_names.add(cfg_name)
                    if isinstance(cfg_dict.get("marketplace"), str) and cfg_dict["marketplace"].strip():
                        requested_names.add(f"{cfg_name}@{cfg_dict['marketplace'].strip()}")
            env_enabled = os.environ.get("TALKOPS_ADDONS", "")
            for tok in env_enabled.split(","):
                if tok.strip() and tok.strip() != "all":
                    requested_names.add(tok.strip())
            if self._enabled_addons_override:
                raw_en = (
                    self._enabled_addons_override
                    if isinstance(self._enabled_addons_override, (list, tuple))
                    else [str(self._enabled_addons_override)]
                )
                for item in raw_en:
                    for tok in str(item).split(","):
                        if tok.strip() and tok.strip() != "all":
                            requested_names.add(tok.strip())

            mp_result = discover_marketplace_addons(
                project_root=self._project_root,
                requested_addon_names=requested_names,
            )
            for mp_addon in mp_result.addons:
                inst = load_addon_from_spec(
                    str(mp_addon.root_dir),
                    project_root=mp_addon.root_dir,
                    override_name=mp_addon.name,
                )
                if inst is not None:
                    inst.source = f"marketplace:{mp_addon.addon_id}"  # type: ignore[attr-defined]
                    inst.enabled_by_default = True  # type: ignore[attr-defined]
                    _register_instance(inst)
        except Exception as exc:
            logger.debug("Marketplace addon discovery skipped or failed: %s", exc)

        for cfg_name, cfg_dict in file_settings.items():
            py_paths = cfg_dict.get("python_path")
            py_paths_list = list(py_paths) if isinstance(py_paths, (list, tuple)) else None
            spec = (
                cfg_dict.get("package")
                or cfg_dict.get("module")
                or cfg_dict.get("entrypoint")
                or cfg_dict.get("path")
            )
            if isinstance(spec, str) and spec.strip():
                inst = load_addon_from_spec(
                    spec,
                    project_root=self._project_root,
                    python_paths=py_paths_list,
                    override_name=cfg_name,
                )
                if inst is not None:
                    _register_instance(inst)

        # 5. Runtime CLI `--custom-addon` and `TALKOPS_CUSTOM_ADDONS` specs
        env_custom = os.environ.get("TALKOPS_CUSTOM_ADDONS", "")
        all_specs = [
            *(s.strip() for s in env_custom.split(",") if s.strip()),
            *self._custom_addon_specs,
        ]
        for spec in all_specs:
            inst = load_addon_from_spec(spec, project_root=self._project_root)
            if inst is not None:
                # Explicitly passed `--custom-addon` / `TALKOPS_CUSTOM_ADDONS` specs are enabled by default
                inst.enabled_by_default = True  # type: ignore[attr-defined]
                _register_instance(inst)

        # 6. Programmatically registered addons (`register_addon(...)`) and `extra_addons`
        for reg_name, reg_target in _PROGRAMMATIC_ADDONS.items():
            inst = _instantiate_addon(
                reg_target,
                source=f"programmatic:{reg_name}",
                override_name=reg_name,
            )
            _register_instance(inst)

        for extra in self._extra_addons:
            if hasattr(extra, "source") and getattr(extra, "source", "") == "custom":
                extra.source = "explicit"
            _register_instance(extra)

        self._discovered = discovered
        return dict(discovered)

    def resolve_and_validate(self) -> list[ServerAddon]:
        """Resolve 4-tier configurations, validate schemas, and topologically sort enabled addons."""
        if not self._discovered:
            self.discover_addons()

        profile_name, raw_configs = resolve_addon_configs(
            self._discovered,
            profile=self._requested_profile,
            enabled_addons=self._enabled_addons_override,
            disabled_addons=self._disabled_addons_override,
            webhook_port=self._webhook_port,
            project_root=self._project_root,
            addons_config_path=self._addons_config_path,
        )
        self._resolved_profile = profile_name

        # Also ensure any addon explicitly passed via `--custom-addon` or `TALKOPS_CUSTOM_ADDONS`
        # is enabled unless explicitly disabled via `--disable-addons`
        disabled_set = set()
        if self._disabled_addons_override:
            raw_dis = (
                self._disabled_addons_override
                if isinstance(self._disabled_addons_override, (list, tuple))
                else [str(self._disabled_addons_override)]
            )
            for item in raw_dis:
                disabled_set.update(s.strip().lower().replace("_", "-") for s in str(item).split(",") if s.strip())

        for name, addon in self._discovered.items():
            src = str(getattr(addon, "source", ""))
            if (src.startswith("file:") or src.startswith("package:")) and getattr(addon, "enabled_by_default", False):
                if name not in disabled_set:
                    raw_configs.setdefault(name, {})["enabled"] = True

        enabled_map: dict[str, ServerAddon] = {}
        validated_configs: dict[str, Any] = {}

        for name, addon in self._discovered.items():
            raw_cfg = raw_configs.get(name, {"enabled": getattr(addon, "enabled_by_default", False)})
            schema_cls = getattr(addon, "settings_schema", None) or AddonSettings
            try:
                validated = schema_cls.model_validate(raw_cfg)
            except ValidationError as exc:
                logger.error("Configuration validation failed for addon '%s': %s", name, exc)
                raise ValueError(f"Invalid configuration for addon '{name}': {exc}") from exc

            validated_configs[name] = validated
            if hasattr(addon, "bind_config"):
                addon.bind_config(validated)

            if addon.is_enabled(validated):
                enabled_map[name] = addon

        # Log any requested addons that were not found in discovery
        for req_name, req_cfg in raw_configs.items():
            if req_cfg.get("enabled") and req_name not in self._discovered:
                logger.info(
                    "Requested addon '%s' is not installed in the current environment "
                    "(install its package with entry point '%s' or declare its 'package'/'path' in addons.json).",
                    req_name,
                    ADDON_ENTRYPOINT_GROUP,
                )

        ordered = self._topological_sort(enabled_map)
        self._active_addons = ordered
        self._validated_configs = validated_configs
        return list(ordered)

    def _topological_sort(self, enabled_map: dict[str, ServerAddon]) -> list[ServerAddon]:
        ordered: list[ServerAddon] = []
        visited: set[str] = set()
        visiting: set[str] = set()

        def _visit(name: str) -> bool:
            if name in visited:
                return True
            if name in visiting:
                raise ValueError(f"Circular addon dependency detected involving '{name}'")
            addon = enabled_map.get(name)
            if addon is None:
                return False

            visiting.add(name)
            for dep in getattr(addon, "dependencies", ()):
                norm_dep = dep.strip().lower().replace("_", "-")
                if norm_dep not in enabled_map:
                    if norm_dep == "core-auth" and "enterprise-auth" in enabled_map:
                        norm_dep = "enterprise-auth"
                    elif self._strict_dependencies:
                        raise ValueError(
                            f"Addon '{name}' requires dependency '{norm_dep}', which is not enabled or installed"
                        )
                    else:
                        logger.debug(
                            "Optional/external dependency '%s' for addon '%s' is not active",
                            norm_dep,
                            name,
                        )
                        continue
                _visit(norm_dep)

            visiting.remove(name)
            visited.add(name)
            ordered.append(addon)
            return True

        for addon_name in enabled_map:
            _visit(addon_name)

        return ordered

    def build_context(self) -> AddonContext:
        """Construct the shared `AddonContext` passed to all enabled addons."""
        if self._context is not None:
            return self._context

        settings = get_settings()
        port = self._main_port or int(os.environ.get("TALKOPS_SERVER_PORT", "2024"))
        server_url = (
            os.environ.get("TALKOPS_SERVER_URL")
            or os.environ.get("LANGGRAPH_API_URL")
            or f"http://{self._host}:{port}"
        ).rstrip("/")

        self._context = AddonContext(
            settings=settings,
            project_root=self._project_root,
            server_url=server_url,
            gateway=self._gateway,
            event_bus=self._event_bus,
            addon_configs=dict(self._validated_configs),
        )
        return self._context

    async def startup(self) -> AddonContext:
        """Start all enabled addons in dependency order and bind any dedicated secondary ports."""
        if not self._active_addons:
            self.resolve_and_validate()

        ctx = self.build_context()
        if self._started:
            return ctx

        for addon in self._active_addons:
            try:
                await addon.on_startup(ctx)
                if isinstance(addon, IdentityMapper) and (
                    ctx.identity_mapper is None or addon.name != "core-auth"
                ):
                    ctx.identity_mapper = addon
                logger.info(
                    "Started server addon '%s' (tier=%s, source=%s)",
                    addon.name,
                    addon.tier,
                    getattr(addon, "source", "builtin"),
                )
            except Exception as exc:
                logger.error("Addon '%s' failed during on_startup: %s", addon.name, exc, exc_info=True)
                raise

        await self._start_secondary_port_servers()
        self._started = True
        return ctx

    async def shutdown(self) -> None:
        """Gracefully stop secondary port listeners, addon background tasks, and addons in reverse order."""
        if not self._started and not self._secondary_servers:
            return

        for port, (uv_server, uv_task) in list(self._secondary_servers.items()):
            try:
                uv_server.should_exit = True
                await asyncio.wait_for(uv_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError, Exception):
                if not uv_task.done():
                    uv_task.cancel()
            logger.info("Stopped secondary addon listener on port %d", port)
        self._secondary_servers.clear()

        ctx = self._context or self.build_context()
        for addon in reversed(self._active_addons):
            try:
                await addon.on_shutdown(ctx)
            except Exception as exc:
                logger.warning("Addon '%s' raised error during on_shutdown: %s", addon.name, exc)

        await ctx.cancel_background_tasks()
        self._started = False

    async def _start_secondary_port_servers(self) -> None:
        """Bind secondary `uvicorn.Server` instances on the same asyncio loop for addons with a custom `port`."""
        port_routes: dict[int, list[BaseRoute]] = {}
        for addon in self._active_addons:
            cfg = self._validated_configs.get(addon.name)
            addon_port = getattr(cfg, "port", None) if cfg is not None else None
            if addon_port is not None and addon_port != self._main_port and addon_port > 0:
                routes = self._collect_addon_routes(addon)
                if routes:
                    port_routes.setdefault(int(addon_port), []).extend(routes)

        if not port_routes:
            return

        try:
            import uvicorn
        except ImportError:
            logger.warning("uvicorn is not installed; cannot bind dedicated secondary addon ports")
            return

        for port, routes in port_routes.items():
            if port in self._secondary_servers:
                continue
            sub_app = Starlette(routes=routes)
            uv_cfg = uvicorn.Config(
                app=sub_app,
                host=self._host,
                port=port,
                log_level="warning",
                access_log=False,
            )
            uv_server = uvicorn.Server(uv_cfg)
            uv_server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
            task = asyncio.create_task(
                uv_server.serve(),
                name=f"talkops-addon-port-{port}",
            )
            self._secondary_servers[port] = (uv_server, task)
            logger.info("Bound dedicated addon listener on http://%s:%d", self._host, port)

    def _collect_addon_routes(self, addon: ServerAddon) -> list[BaseRoute]:
        routes = list(addon.get_routes() or [])
        cfg = self._validated_configs.get(addon.name)
        prefix = (getattr(cfg, "mount_prefix", "") if cfg is not None else "").strip()
        if prefix and routes:
            norm_prefix = "/" + prefix.strip("/")
            return [Mount(norm_prefix, routes=routes, name=f"addon-{addon.name}")]
        return routes

    def get_companion_specs(self) -> list[CompanionProcessSpec]:
        """Collect all opt-in OS companion processes requested by active addons."""
        if not self._active_addons:
            self.resolve_and_validate()
        ctx = self.build_context()
        specs: list[CompanionProcessSpec] = []
        for addon in self._active_addons:
            specs.extend(addon.get_companion_specs(ctx))
        return specs

    async def get_addons_status(self) -> list[dict[str, Any]]:
        """Return health & metadata dicts for all discovered addons."""
        if not self._discovered:
            self.discover_addons()
        active_names = {a.name for a in self._active_addons}
        results: list[dict[str, Any]] = []
        for name, addon in sorted(self._discovered.items()):
            if name in active_names:
                try:
                    health = await addon.health_check()
                    results.append(health.to_dict())
                except Exception as exc:
                    results.append(
                        AddonHealthStatus(
                            name=name,
                            status="error",
                            tier=addon.tier,
                            version=getattr(addon, "version", "0.1.0"),
                            source=getattr(addon, "source", "builtin"),
                            details={"error": str(exc)},
                        ).to_dict()
                    )
            else:
                results.append(
                    AddonHealthStatus(
                        name=name,
                        status="disabled",
                        tier=addon.tier,
                        version=getattr(addon, "version", "0.1.0"),
                        source=getattr(addon, "source", "builtin"),
                        details={"description": addon.description},
                    ).to_dict()
                )
        return results

    def create_asgi_app(self) -> Starlette:
        """Assemble the unified Starlette ASGI application from all enabled addons."""
        if not self._active_addons:
            self.resolve_and_validate()

        routes: list[BaseRoute] = []
        middlewares: list[Middleware] = []

        for addon in self._active_addons:
            routes.extend(self._collect_addon_routes(addon))
            middlewares.extend(addon.get_middleware() or [])

        async def _list_addons_endpoint(_request: Request) -> JSONResponse:
            statuses = await self.get_addons_status()
            return JSONResponse(
                {
                    "status": "ok",
                    "profile": self._resolved_profile,
                    "active_addons": [a.name for a in self._active_addons],
                    "addons": statuses,
                }
            )

        routes.append(Route("/api/v1/addons", _list_addons_endpoint, methods=["GET"]))

        @asynccontextmanager
        async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
            await self.startup()
            try:
                yield
            finally:
                await self.shutdown()

        app = Starlette(
            routes=routes,
            middleware=middlewares,
            lifespan=_lifespan,
        )
        app.state.addon_manager = self
        app.state.gateway = self._gateway
        app.state.event_bus = self._event_bus
        return app

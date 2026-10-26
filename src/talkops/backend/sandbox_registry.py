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

"""Discovery and instantiation of sandbox providers for TalkOps.

Providers come from three sources, merged in order of increasing precedence:

1. Built-in providers (`langsmith`, `agentcore`, `daytona`, `modal`, `runloop`, `vercel`).
2. Entry-point providers registered by installed packages under the
   `talkops.sandbox_providers` or `deepagents_code.sandbox_providers` groups.
3. Config-declared providers from `[sandboxes.providers]` in
   `~/.talkops/config.toml`.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
import logging
from typing import Any

from talkops.backend.sandbox_config import SandboxConfig
from talkops.backend.sandbox_provider import (
    SandboxInstallHint,
    SandboxProvider,
    SandboxProviderMetadata,
)

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "talkops.sandbox_providers"
LEGACY_ENTRY_POINT_GROUPS = (
    "talkops.sandbox_providers",
    "deepagents_code.sandbox_providers",
)

_BUILTIN_METADATA: dict[str, SandboxProviderMetadata] = {
    "agentcore": SandboxProviderMetadata(
        name="agentcore",
        working_dir="/workspace",
        install=SandboxInstallHint(kind="extra", name="agentcore"),
        supports_sandbox_id=False,
        backend_module="langchain_agentcore_codeinterpreter",
    ),
    "daytona": SandboxProviderMetadata(
        name="daytona",
        working_dir="/home/daytona",
        install=SandboxInstallHint(kind="extra", name="daytona"),
        supports_sandbox_id=False,
        supports_snapshot_name=True,
        backend_module="langchain_daytona",
    ),
    "langsmith": SandboxProviderMetadata(
        name="langsmith",
        working_dir="/root",
        install=None,
        supports_sandbox_id=True,
        supports_snapshot_name=True,
        backend_module="deepagents.backends.langsmith",
    ),
    "modal": SandboxProviderMetadata(
        name="modal",
        working_dir="/workspace",
        install=SandboxInstallHint(kind="extra", name="modal"),
        supports_sandbox_id=True,
        backend_module="langchain_modal",
    ),
    "runloop": SandboxProviderMetadata(
        name="runloop",
        working_dir="/home/user",
        install=SandboxInstallHint(kind="extra", name="runloop"),
        supports_sandbox_id=True,
        supports_snapshot_name=True,
        backend_module="langchain_runloop",
    ),
    "vercel": SandboxProviderMetadata(
        name="vercel",
        working_dir="/vercel/sandbox",
        install=SandboxInstallHint(kind="extra", name="vercel"),
        supports_sandbox_id=True,
        backend_module="langchain_vercel_sandbox",
    ),
}


def _import_class_path(class_path: str, *, context: str) -> type[SandboxProvider]:
    """Import a `module.path:ClassName` string and validate it is a `SandboxProvider`."""
    module_name, _, attr_name = class_path.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        msg = (
            f"Could not import module {module_name!r} for {context} "
            f"(from class_path {class_path!r}): {exc}"
        )
        raise ImportError(msg) from exc

    try:
        cls = getattr(module, attr_name)
    except AttributeError as exc:
        msg = (
            f"Module {module_name!r} has no attribute {attr_name!r} "
            f"for {context} (from class_path {class_path!r})"
        )
        raise ImportError(msg) from exc

    if not (isinstance(cls, type) and issubclass(cls, SandboxProvider)):
        msg = f"{class_path!r} ({context}) is not a SandboxProvider subclass (got {cls!r})"
        raise TypeError(msg)
    return cls


class SandboxRegistry:
    """Merged view of built-in, entry-point, and config sandbox providers."""

    def __init__(self, config: SandboxConfig | None = None) -> None:
        """Discover providers from all sources."""
        self._config = config if config is not None else SandboxConfig.load()
        self._metadata: dict[str, SandboxProviderMetadata] = dict(_BUILTIN_METADATA)
        self._entry_point_classes: dict[str, type[SandboxProvider]] = {}
        self._config_classes: dict[str, str] = {}
        self._config_params: dict[str, dict[str, Any]] = {}

        self._discover_entry_points()
        self._apply_config()

    @classmethod
    def load(cls, config: SandboxConfig | None = None) -> SandboxRegistry:
        """Load a `SandboxRegistry` from the given or default `SandboxConfig`."""
        return cls(config=config)

    @property
    def config(self) -> SandboxConfig:
        """The `SandboxConfig` this registry was built from."""
        return self._config

    @property
    def default(self) -> str | None:
        """The configured `[sandboxes].default` provider, or `None`."""
        return self._config.default

    @property
    def config_error(self) -> str | None:
        """Parse error from `config.toml`, if any."""
        return self._config.parse_error

    def _discover_entry_points(self) -> None:
        """Load providers registered under entry-point groups."""
        for group in LEGACY_ENTRY_POINT_GROUPS:
            try:
                eps = importlib.metadata.entry_points(group=group)
            except Exception:
                logger.warning("Failed to discover sandbox entry points for %s", group, exc_info=True)
                continue

            for ep in eps:
                name = ep.name
                try:
                    loaded = ep.load()
                except Exception:
                    logger.warning(
                        "Failed to load sandbox provider entry point '%s' (%s); skipping",
                        name,
                        ep.value,
                        exc_info=True,
                    )
                    continue

                if not (isinstance(loaded, type) and issubclass(loaded, SandboxProvider)):
                    logger.warning(
                        "Sandbox entry point '%s' (%s) did not resolve to a "
                        "SandboxProvider subclass (got %r); skipping",
                        name,
                        ep.value,
                        loaded,
                    )
                    continue

                self._entry_point_classes[name] = loaded
                meta = self._read_class_metadata(loaded, fallback_name=name, ep=ep)
                self._metadata[name] = meta

    @staticmethod
    def _read_class_metadata(
        cls: type[SandboxProvider],
        *,
        fallback_name: str,
        ep: importlib.metadata.EntryPoint,
    ) -> SandboxProviderMetadata:
        """Extract `SandboxProviderMetadata` from a provider class without calling `__init__`."""
        try:
            prop = inspect.getattr_static(cls, "metadata")
            if isinstance(prop, property) and prop.fget is not None:
                bare = object.__new__(cls)
                declared = prop.fget(bare)
                if isinstance(declared, SandboxProviderMetadata):
                    return declared
            elif isinstance(prop, SandboxProviderMetadata):
                return prop
        except Exception:
            logger.debug(
                "Could not read static metadata from %s; using defaults",
                cls,
                exc_info=True,
            )

        dist_name = ep.dist.name if ep.dist is not None else None
        install = (
            SandboxInstallHint(kind="package", name=dist_name)
            if dist_name
            else None
        )
        return SandboxProviderMetadata(name=fallback_name, install=install)

    def _apply_config(self) -> None:
        """Overlay `[sandboxes.providers]` entries onto the discovered set."""
        for name, entry in self._config.providers.items():
            if not isinstance(entry, dict):
                continue
            if "class_path" in entry:
                self._config_classes[name] = entry["class_path"]
                base = self._metadata.get(name)
                pkg = entry.get("package")
                install = (
                    SandboxInstallHint(kind="package", name=pkg)
                    if pkg
                    else (base.install if base else None)
                )
                self._metadata[name] = SandboxProviderMetadata(
                    name=name,
                    working_dir=entry.get(
                        "working_dir",
                        base.working_dir if base else "/workspace",
                    ),
                    install=install,
                    supports_sandbox_id=entry.get(
                        "supports_sandbox_id",
                        base.supports_sandbox_id if base else True,
                    ),
                    supports_snapshot_name=entry.get(
                        "supports_snapshot_name",
                        base.supports_snapshot_name if base else False,
                    ),
                    backend_module=base.backend_module if base else None,
                )
                if "params" in entry and isinstance(entry["params"], dict):
                    self._config_params[name] = dict(entry["params"])
            else:
                # Direct provider params table (e.g. `[sandboxes.daytona]`)
                self._config_params[name] = dict(entry)

    def list_providers(self) -> list[str]:
        """Return all known provider names in sorted order."""
        return sorted(self._metadata)

    def available_providers(self) -> list[str]:
        """Alias for `list_providers()` matching `dcode`."""
        return self.list_providers()

    def is_available(self, name: str) -> bool:
        """Return whether `name` is a known provider."""
        return name in self._metadata

    def get_metadata(self, name: str) -> SandboxProviderMetadata | None:
        """Return metadata for `name`, or `None` if unknown."""
        return self._metadata.get(name)

    def get_working_dir(self, name: str) -> str:
        """Return the default working directory for `name`."""
        meta = self._metadata.get(name)
        if meta is None:
            msg = (
                f"Unknown sandbox provider: {name}. "
                f"Available providers: {', '.join(self.list_providers())}"
            )
            raise ValueError(msg)
        return meta.working_dir

    def get_config_params(self, name: str) -> dict[str, Any]:
        """Return extra `get_or_create` kwargs configured for `name`."""
        return dict(self._config_params.get(name, {}))

    def create_provider(self, name: str) -> SandboxProvider:
        """Instantiate the `SandboxProvider` for `name`.

        Precedence: config `class_path` > entry point > built-in.
        """
        if name in self._config_classes:
            cls = _import_class_path(
                self._config_classes[name],
                context=f"[sandboxes.providers.{name}]",
            )
            return cls()

        if name in self._entry_point_classes:
            return self._entry_point_classes[name]()

        builtin = _get_builtin_provider_classes().get(name)
        if builtin is not None:
            return builtin()

        msg = (
            f"Unknown sandbox provider: {name}. "
            f"Available providers: {', '.join(self.list_providers())}"
        )
        raise ValueError(msg)


def _get_builtin_provider_classes() -> dict[str, type[SandboxProvider]]:
    """Return built-in provider name -> class mapping."""
    from talkops.backend import sandbox_factory

    return {
        "agentcore": sandbox_factory._AgentCoreProvider,
        "daytona": sandbox_factory._DaytonaProvider,
        "langsmith": sandbox_factory._LangSmithProvider,
        "modal": sandbox_factory._ModalProvider,
        "runloop": sandbox_factory._RunloopProvider,
        "vercel": sandbox_factory._VercelProvider,
    }


__all__ = [
    "ENTRY_POINT_GROUP",
    "SandboxRegistry",
]

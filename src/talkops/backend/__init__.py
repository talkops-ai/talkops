"""Backend module for TalkOps — shell execution, sandbox environments, and filesystem access."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from talkops.backend.registry import (
    BackendRegistry,
    get_backend_class,
    get_backend_registry,
    register_backend,
)

if TYPE_CHECKING:
    from talkops.backend.composite import CompositeBackend, TalkOpsCompositeBackend
    from talkops.backend.local import LocalShellBackend
    from talkops.backend.sandbox_config import SandboxConfig
    from talkops.backend.sandbox_factory import (
        cleanup_all_sandboxes,
        cleanup_sandbox,
        clear_sandbox_cache,
        create_sandbox_backend,
        get_sandbox_backend,
    )

__all__ = [
    "BackendRegistry",
    "CompositeBackend",
    "LocalShellBackend",
    "TalkOpsCompositeBackend",
    "SandboxConfig",
    "cleanup_all_sandboxes",
    "cleanup_sandbox",
    "clear_sandbox_cache",
    "create_sandbox_backend",
    "get_backend_class",
    "get_backend_registry",
    "get_sandbox_backend",
    "register_backend",
]


def __getattr__(name: str) -> Any:
    """Lazy-load backend classes to avoid circular imports and unnecessary dependencies."""
    _lazy_map = {
        "LocalShellBackend": "talkops.backend.local",
        "TalkOpsCompositeBackend": "talkops.backend.composite",
        "CompositeBackend": "talkops.backend.composite",
        "SandboxConfig": "talkops.backend.sandbox_config",
        "get_sandbox_backend": "talkops.backend.sandbox_factory",
        "create_sandbox_backend": "talkops.backend.sandbox_factory",
        "cleanup_sandbox": "talkops.backend.sandbox_factory",
        "cleanup_all_sandboxes": "talkops.backend.sandbox_factory",
        "clear_sandbox_cache": "talkops.backend.sandbox_factory",
    }
    if name in _lazy_map:
        import importlib

        mod = importlib.import_module(_lazy_map[name])
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

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

"""Singleton registry for backend providers in TalkOps."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T", bound=type)


class BackendRegistry:
    """Singleton registry for backend providers."""

    _instance: BackendRegistry | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        """Initialize BackendRegistry with an empty provider mapping."""
        self._providers: dict[str, type] = {}

    def register(self, name: str, cls: type) -> None:
        """Register a backend provider class under *name*."""
        self._providers[name] = cls

    def build(self, name: str, **kwargs: Any) -> Any:
        """Instantiate a registered backend by *name*."""
        cls = self._providers.get(name)
        if cls is None:
            raise ValueError(f"Backend provider '{name}' is not registered.")
        return cls(**kwargs)

    def list_registered(self) -> list[str]:
        """Return sorted list of registered backend names."""
        return sorted(self._providers.keys())

    @classmethod
    def get_instance(cls) -> BackendRegistry:
        """Return the singleton instance, creating it if needed."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance


def get_backend_registry() -> BackendRegistry:
    """Module-level accessor for the singleton ``BackendRegistry``."""
    return BackendRegistry.get_instance()


def register_backend(name: str) -> Callable[[T], T]:
    """Class decorator to register a backend provider.

    Usage::

        @register_backend("local")
        class LocalShellBackend(SDKLocalShellBackend):
            ...
    """

    def decorator(cls: T) -> T:
        """Execute decorator."""
        get_backend_registry().register(name, cls)
        return cls

    return decorator


def get_backend_class(name: str) -> type | None:
    """Return the registered backend class for *name*, or None."""
    return get_backend_registry()._providers.get(name)


__all__ = [
    "BackendRegistry",
    "get_backend_class",
    "get_backend_registry",
    "register_backend",
]

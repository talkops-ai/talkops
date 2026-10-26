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

"""Sandbox provider interface and metadata for TalkOps."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from deepagents.backends.protocol import SandboxBackendProtocol


@dataclass(frozen=True)
class SandboxInstallHint:
    """How to install the package that provides a sandbox backend."""

    kind: Literal["extra", "package"]
    """`'extra'` for a TalkOps extra; `'package'` for a PyPI distribution."""

    name: str
    """Extra name (e.g. `'daytona'`) or distribution name (e.g. `'langchain-e2b'`)."""

    def command(self, *, in_app: bool = False) -> str:
        """Render the install command for CLI or in-app help."""
        del in_app
        if self.kind == "extra":
            return f"uv pip install 'talkops-ai[{self.name}]'"
        return f"uv pip install {self.name}"


@dataclass(frozen=True)
class SandboxProviderMetadata:
    """Static description of a sandbox provider used by the registry.

    Carries everything callers need without instantiating the provider (which
    validates credentials and may make network calls).
    """

    name: str
    """Provider identifier as passed to `--sandbox` (e.g. `'daytona'`)."""

    working_dir: str = "/workspace"
    """Default working directory inside the sandbox."""

    install: SandboxInstallHint | None = None
    """How to install the provider's dependencies, or `None` if bundled."""

    supports_sandbox_id: bool = True
    """Whether `--sandbox-id` reattach is supported."""

    supports_snapshot_name: bool = False
    """Whether `--sandbox-snapshot-name` is supported."""

    backend_module: str | None = None
    """Python module probed by `verify_sandbox_deps`, or `None` to skip."""


class SandboxError(Exception):
    """Base error for sandbox provider operations."""


class SandboxNotFoundError(SandboxError):
    """Raised when the requested sandbox cannot be found."""

    def __init__(self, sandbox_id: str) -> None:
        """Initialize with the ID of the missing sandbox."""
        self.sandbox_id = sandbox_id
        super().__init__(f"Sandbox '{sandbox_id}' not found")


class SandboxProvider(ABC):
    """Interface for creating and deleting sandbox backends."""

    @property
    def metadata(self) -> SandboxProviderMetadata | None:
        """Static metadata describing this provider, or `None` if unspecified.

        Entry-point providers registered under the `talkops.sandbox_providers`
        or `deepagents_code.sandbox_providers` entry-point group can override
        this so the registry can read their working directory and capability
        flags (snapshot/sandbox-id support).
        """
        return None

    @abstractmethod
    def get_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        **kwargs: Any,
    ) -> SandboxBackendProtocol:
        """Get an existing sandbox, or create one if needed."""
        ...

    @abstractmethod
    def delete(
        self,
        *,
        sandbox_id: str,
        **kwargs: Any,
    ) -> None:
        """Delete a sandbox by id."""
        ...

    async def aget_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        **kwargs: Any,
    ) -> SandboxBackendProtocol:
        """Async wrapper around `get_or_create`."""
        return await asyncio.to_thread(
            self.get_or_create, sandbox_id=sandbox_id, **kwargs
        )

    async def adelete(
        self,
        *,
        sandbox_id: str,
        **kwargs: Any,
    ) -> None:
        """Async wrapper around `delete`."""
        await asyncio.to_thread(self.delete, sandbox_id=sandbox_id, **kwargs)


__all__ = [
    "SandboxError",
    "SandboxInstallHint",
    "SandboxNotFoundError",
    "SandboxProvider",
    "SandboxProviderMetadata",
]

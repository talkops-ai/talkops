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

"""Composite backend routing for TalkOps.

Routes special virtual paths (``/large_tool_results/`` and ``/conversation_history/``)
to specialized backends while delegating all standard filesystem and command
execution to the default backend (e.g. ``LocalShellBackend`` or remote sandbox).
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path
from typing import Any

from deepagents.backends import (
    BackendProtocol,
    FilesystemBackend,
)
from deepagents.backends import (
    CompositeBackend as SDKCompositeBackend,
)

from talkops.config.paths import ensure_conversation_history_dir


class TalkOpsCompositeBackend(SDKCompositeBackend):
    """Composite backend routing virtual paths (/large_tool_results/, /conversation_history/)."""

    def __init__(
        self,
        default: Any,
        routes: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize TalkOpsCompositeBackend with default and routed backends.

        Args:
            default: Default backend instance to fall back to.
            routes: Optional mapping of virtual path prefixes to backend instances.
            **kwargs: Additional keyword arguments for SDKCompositeBackend.
        """
        self._large_results_dir = tempfile.mkdtemp(prefix="talkops_large_results_")
        self._conversation_history_dir = ensure_conversation_history_dir()

        atexit.register(self.cleanup)

        large_results_backend = FilesystemBackend(
            root_dir=self._large_results_dir,
            virtual_mode=True,
        )
        conversation_history_backend = FilesystemBackend(
            root_dir=self._conversation_history_dir,
            virtual_mode=True,
        )

        effective_routes: dict[str, BackendProtocol] = {
            "/large_tool_results/": large_results_backend,
            "/conversation_history/": conversation_history_backend,
        }
        if routes:
            effective_routes.update(routes)

        super().__init__(
            default=default,
            routes=effective_routes,
            **kwargs,
        )

    def cleanup(self) -> None:
        """Clean up temporary large tool result directories on shutdown.

        Note: Conversation history is persistent and is intentionally preserved.
        """
        if hasattr(self, "_large_results_dir") and self._large_results_dir:
            shutil.rmtree(self._large_results_dir, ignore_errors=True)

    def __enter__(self) -> TalkOpsCompositeBackend:
        """Enter the runtime context related to this object."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the runtime context and handle any exception."""
        self.cleanup()


# Alias for convenience
CompositeBackend = TalkOpsCompositeBackend

__all__ = [
    "CompositeBackend",
    "TalkOpsCompositeBackend",
]

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

"""Stderr marker emission used by server and graph startup entry points in TalkOps."""

from __future__ import annotations

import sys
import traceback

from talkops.utils.logger import get_logger

logger = get_logger(__name__)

STARTUP_ERROR_MARKER = "TALKOPS_STARTUP_ERROR:"


def emit_startup_failure(exc: BaseException) -> None:
    """Report a server graph startup failure to the parent process.

    Emits full traceback for logs, then a single-line
    ``{STARTUP_ERROR_MARKER}{type}: {summary}`` line.
    """
    logger.critical("Failed to initialize server graph", exc_info=exc)
    print(
        f"Failed to initialize server graph: {exc}\n{traceback.format_exc()}",
        file=sys.stderr,
    )
    exc_lines = str(exc).splitlines()
    summary = exc_lines[0] if exc_lines else "<no message>"
    print(
        f"{STARTUP_ERROR_MARKER}{type(exc).__name__}: {summary}",
        file=sys.stderr,
    )


__all__ = [
    "STARTUP_ERROR_MARKER",
    "emit_startup_failure",
]

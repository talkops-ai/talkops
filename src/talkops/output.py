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

"""Machine-readable JSON output helpers for CLI subcommands.

This module deliberately stays stdlib-only so it can be imported from CLI
startup paths without pulling in unnecessary dependency trees.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Literal

OutputFormat = Literal["text", "json"]
"""Accepted internal output modes for CLI subcommands."""


def add_json_output_arg(
    parser: argparse.ArgumentParser, *, default: OutputFormat | None = None
) -> None:
    """Add a `--json` flag to an argparse parser.

    Args:
        parser: Parser to update.
        default: Default output format for this parser.
            Pass `None` for subparsers so parent parser values are preserved.
    """
    if default is None:
        parser.add_argument(
            "--json",
            dest="output_format",
            action="store_const",
            const="json",
            default=argparse.SUPPRESS,
            help="Emit machine-readable JSON for this command",
        )
    else:
        parser.add_argument(
            "--json",
            dest="output_format",
            action="store_const",
            const="json",
            default=default,
            help="Emit machine-readable JSON for this command",
        )


def write_json(command: str, data: list[Any] | dict[str, Any]) -> None:
    """Write a JSON envelope to stdout and flush.

    The envelope is a single-line JSON object with a stable schema:

    ```json
    {"schema_version": 1, "command": "...", "data": ...}
    ```

    Args:
        command: Self-documenting command name (e.g. `'list'`, `'threads list'`).
        data: Payload — typically a list for listing commands or a dict
            for action/info commands.
    """
    envelope = {"schema_version": 1, "command": command, "data": data}
    sys.stdout.write(json.dumps(envelope, default=str) + "\n")
    sys.stdout.flush()

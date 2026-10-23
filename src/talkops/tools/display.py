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

"""Display helpers for rendering tool outputs with syntax highlighting."""

from __future__ import annotations

import json
from typing import Any


def format_tool_error(error_msg: str) -> str:
    """Format an error message with ANSI red color."""
    return f"\033[91mError: {error_msg}\033[0m"


def format_tool_success(msg: str) -> str:
    """Format a success message with ANSI green color."""
    return f"\033[92mSuccess: {msg}\033[0m"


def format_json_output(data: Any) -> str:
    """Format dictionary/JSON output as a pretty printed string."""
    try:
        return json.dumps(data, indent=2)
    except Exception:
        return str(data)

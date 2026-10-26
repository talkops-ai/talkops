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

"""JSON parsing utilities for plugins."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def json_object(value: Any) -> dict[str, Any]:
    """Execute json object."""
    if isinstance(value, dict):
        return value
    return {}


def load_json_file(path: Path) -> dict[str, Any] | None:
    """Load json file."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

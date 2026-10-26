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

"""Type definitions for subagent metadata."""

from typing import Any, Literal, NotRequired, TypedDict


class SubagentMetadata(TypedDict):
    """Metadata for a custom subagent loaded from filesystem."""

    name: str
    description: str
    system_prompt: str
    model: NotRequired[str | None]
    mode: NotRequired[Literal["isolated", "fork"] | None]
    response_format: NotRequired[Any | None]
    skills: NotRequired[list[str] | None]
    tools: NotRequired[list[str] | None]
    capabilities: NotRequired[list[dict[str, Any]] | None]
    mcp_config: NotRequired[dict[str, Any] | None]
    mcp_files: NotRequired[list[str] | None]
    middleware: NotRequired[list[Any] | None]
    permission_tier: NotRequired[str | None]
    is_plugin: NotRequired[bool]
    plugin_id: NotRequired[str | None]
    bundle_dir: NotRequired[str | None]
    skills_dir: NotRequired[str | None]
    source: str
    path: str

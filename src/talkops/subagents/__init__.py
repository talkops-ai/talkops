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

"""Subagents module for TalkOps.

Provides subagent metadata types, filesystem discovery, and dynamic agent plugin loading.
"""

from talkops.subagents.loader import (
    get_built_in_subagents,
    list_subagents,
    list_subagents_async,
    load_async_subagents,
    load_async_subagents_async,
    parse_subagent_file,
)
from talkops.subagents.subagents_parser import (
    parse_built_in_subagents,
    parse_subagent_bundle,
)
from talkops.subagents.types import SubagentMetadata

__all__ = [
    "SubagentMetadata",
    "get_built_in_subagents",
    "list_subagents",
    "list_subagents_async",
    "load_async_subagents",
    "load_async_subagents_async",
    "parse_built_in_subagents",
    "parse_subagent_bundle",
    "parse_subagent_file",
]

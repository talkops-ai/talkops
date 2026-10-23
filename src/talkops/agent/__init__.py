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

"""Agent construction package for TalkOps."""

from talkops.agent.config import (
    AVAILABLE_AGENTS,
    AgentContext,
    AgentContextSchema,
    CLIContextSchema,
    get_available_agent_names,
    load_default_agent,
    save_default_agent,
)
from talkops.agent.factory import (
    create_agent,
    create_talkops_agent,
)
from talkops.agent.profiles import (
    HarnessProfile,
    SubagentDeliverable,
    create_talkops_harness_profile,
    get_talkops_harness_profile,
    register_talkops_harness_profile,
)

__all__ = [
    "AVAILABLE_AGENTS",
    "AgentContext",
    "AgentContextSchema",
    "CLIContextSchema",
    "HarnessProfile",
    "SubagentDeliverable",
    "create_agent",
    "create_talkops_agent",
    "create_talkops_harness_profile",
    "get_available_agent_names",
    "get_talkops_harness_profile",
    "load_default_agent",
    "register_talkops_harness_profile",
    "save_default_agent",
]

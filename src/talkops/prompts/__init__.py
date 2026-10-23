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

"""TalkOps prompts module and dynamic cloud-native system prompt builder."""

from __future__ import annotations

from talkops.prompts.system import (
    MODEL_IDENTITY_RE,
    TALKOPS_MEMORY_HEADLESS_SYSTEM_PROMPT,
    TALKOPS_MEMORY_READONLY_SYSTEM_PROMPT,
    TALKOPS_MEMORY_SYSTEM_PROMPT,
    build_cloud_provider_section,
    build_fs_tool_guidance,
    build_model_identity_section,
    build_webhook_context_section,
    build_working_dir_section,
    get_base_system_prompt,
    get_memory_system_prompt,
    get_webhook_sre_system_prompt,
)
from talkops.prompts.types import (
    CloudProvider,
    FsToolName,
    normalize_cloud_provider,
)

__all__ = [
    "CloudProvider",
    "FsToolName",
    "MODEL_IDENTITY_RE",
    "TALKOPS_MEMORY_HEADLESS_SYSTEM_PROMPT",
    "TALKOPS_MEMORY_READONLY_SYSTEM_PROMPT",
    "TALKOPS_MEMORY_SYSTEM_PROMPT",
    "build_cloud_provider_section",
    "build_fs_tool_guidance",
    "build_model_identity_section",
    "build_webhook_context_section",
    "build_working_dir_section",
    "get_base_system_prompt",
    "get_memory_system_prompt",
    "get_webhook_sre_system_prompt",
    "normalize_cloud_provider",
]

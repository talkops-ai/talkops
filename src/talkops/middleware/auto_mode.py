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

"""Re-export classifier-backed Auto mode components."""

from __future__ import annotations

from talkops.middleware.auto_mode_hitl import (
    AUTO_MODE_COUNTERS_NAMESPACE,
    AUTO_MODE_EVENT_TYPE,
    READONLY_SAFE_TOOLS,
    USER_PROMPT_METADATA_KEY,
    AsyncApprovalHITLMiddleware,
    AutoDecision,
    AutoDecisionBatch,
    AutoDecisionCategory,
    AutoModeCounters,
    AutoModeHITLMiddleware,
    AutoModeState,
    AutoTempArtifact,
    AutoTempArtifactMutation,
    DynamicInterruptMapping,
    PlannedDecision,
    PromptMetadata,
    _async_routing_mode,
    _merge_temp_artifacts,
    user_prompt_metadata,
)
from talkops.middleware.headless_mcp_guard import (
    HeadlessMCPGuardMiddleware,
    gated_mcp_tool_names,
)


def is_potentially_destructive(command: str) -> bool:
    """Classify whether a shell command is potentially destructive."""
    from talkops.security.shell_safety import classify_command
    return classify_command(command) == "dangerous"


__all__ = [
    "AUTO_MODE_COUNTERS_NAMESPACE",
    "AUTO_MODE_EVENT_TYPE",
    "READONLY_SAFE_TOOLS",
    "USER_PROMPT_METADATA_KEY",
    "AsyncApprovalHITLMiddleware",
    "AutoDecision",
    "AutoDecisionBatch",
    "AutoDecisionCategory",
    "AutoModeCounters",
    "AutoModeHITLMiddleware",
    "AutoModeState",
    "AutoTempArtifact",
    "AutoTempArtifactMutation",
    "DynamicInterruptMapping",
    "HeadlessMCPGuardMiddleware",
    "PlannedDecision",
    "PromptMetadata",
    "_async_routing_mode",
    "_merge_temp_artifacts",
    "gated_mcp_tool_names",
    "is_potentially_destructive",
    "user_prompt_metadata",
]

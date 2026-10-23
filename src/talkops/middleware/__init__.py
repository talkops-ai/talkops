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

"""TalkOps middleware modules and centralized registry."""

from __future__ import annotations

from talkops.middleware._repository_bounds import (
    REPOSITORY_TOOL_NAMES,
    RepositoryBounds,
)
from talkops.middleware.ask_user import AskUserMiddleware
from talkops.middleware.auto_mode import (
    AUTO_MODE_EVENT_TYPE,
    is_potentially_destructive,
)
from talkops.middleware.auto_mode_hitl import AutoModeHITLMiddleware, AutoModeState
from talkops.middleware.background_commands import (
    BACKGROUND_EXEC_SYSTEM_INSTRUCTIONS,
    BACKGROUND_TASK_NOTIFICATION_SOURCE,
    AsyncExecuteSchema,
    BackgroundCommandMiddleware,
)
from talkops.middleware.btw import (
    BTW_PROMPT_PREFIX,
    BTW_TIMEOUT_SECONDS,
    BtwSideQuestionMiddleware,
    format_btw_prompt,
    is_btw_turn,
    is_ephemeral_message,
)
from talkops.middleware.compaction import create_cli_compaction_middleware
from talkops.middleware.configurable_model import ConfigurableModelMiddleware
from talkops.middleware.cost_tracking import (
    ACTIVE_SESSION_THREAD_ID,
    CostState,
    CostTrackingMiddleware,
)
from talkops.middleware.custom_commands import (
    CustomCommandsMiddleware,
    create_custom_command_skill_tool,
)
from talkops.middleware.glm_stall_recovery import GlmTerminalStallRecoveryMiddleware
from talkops.middleware.goal_criteria import (
    GoalAmendRequest,
    GoalCreateRequest,
    GoalCriteriaMiddleware,
    GoalCriteriaRequest,
    GoalCriteriaState,
    GoalProposal,
)
from talkops.middleware.goal_state_notice import (
    GOAL_CONTROL_MESSAGE_SOURCE,
    GOAL_STATE_MESSAGE_SOURCE,
    build_goal_state_notice,
    is_conversation_control_message,
)
from talkops.middleware.goal_tools import GoalToolsMiddleware
from talkops.middleware.headless_mcp_guard import HeadlessMCPGuardMiddleware
from talkops.middleware.jev_model_router import JevDynamicModelRouterMiddleware
from talkops.middleware.local_context import LocalContextMiddleware
from talkops.middleware.mcp_context import MCPContextMiddleware
from talkops.middleware.mcp_middleware import MCPToolMiddleware, normalize_mcp_arguments
from talkops.middleware.memory_guard import ManagedMemoryGuardMiddleware
from talkops.middleware.model_retry import CodeModelRetryMiddleware
from talkops.middleware.registry import (
    BaseAgentMiddleware,
    MiddlewareRegistry,
    get_middleware_registry,
    register_middleware,
)
from talkops.middleware.reliable_rubric import ReliableRubricMiddleware
from talkops.middleware.resume_state import ResumeStateMiddleware
from talkops.middleware.server_hooks import ServerHooksMiddleware, ServerHooksState
from talkops.middleware.shell_allow_list import ShellAllowListMiddleware
from talkops.middleware.skills import PluginSkillsMiddleware
from talkops.middleware.subagent_telemetry import SubagentTelemetryMiddleware
from talkops.middleware.subagents import SubagentsMiddleware
from talkops.middleware.tool_filter import TOOL_ALIAS_MAP, ToolFilterMiddleware
from talkops.middleware.unified_system_message import (
    UnifiedSystemMessageMiddleware,
    unify_system_message,
)

__all__ = [
    "BaseAgentMiddleware",
    "MiddlewareRegistry",
    "register_middleware",
    "get_middleware_registry",
    "ConfigurableModelMiddleware",
    "JevDynamicModelRouterMiddleware",
    "LocalContextMiddleware",
    "AskUserMiddleware",
    "ResumeStateMiddleware",
    "CostTrackingMiddleware",
    "CostState",
    "ACTIVE_SESSION_THREAD_ID",
    "create_cli_compaction_middleware",
    "PluginSkillsMiddleware",
    "AutoModeHITLMiddleware",
    "AutoModeState",
    "AUTO_MODE_EVENT_TYPE",
    "is_potentially_destructive",
    "SubagentsMiddleware",
    "SubagentTelemetryMiddleware",
    "GoalToolsMiddleware",
    "GoalCriteriaMiddleware",
    "GoalCriteriaRequest",
    "GoalCriteriaState",
    "GoalCreateRequest",
    "GoalAmendRequest",
    "GoalProposal",
    "ReliableRubricMiddleware",
    "UnifiedSystemMessageMiddleware",
    "unify_system_message",
    "GOAL_CONTROL_MESSAGE_SOURCE",
    "GOAL_STATE_MESSAGE_SOURCE",
    "build_goal_state_notice",
    "is_conversation_control_message",
    "RepositoryBounds",
    "REPOSITORY_TOOL_NAMES",
    "GlmTerminalStallRecoveryMiddleware",
    "HeadlessMCPGuardMiddleware",
    "MCPContextMiddleware",
    "MCPToolMiddleware",
    "normalize_mcp_arguments",
    "ManagedMemoryGuardMiddleware",
    "CodeModelRetryMiddleware",
    "ServerHooksMiddleware",
    "ServerHooksState",
    "ShellAllowListMiddleware",
    "ToolFilterMiddleware",
    "TOOL_ALIAS_MAP",
    "BTW_PROMPT_PREFIX",
    "BTW_TIMEOUT_SECONDS",
    "BtwSideQuestionMiddleware",
    "format_btw_prompt",
    "is_btw_turn",
    "is_ephemeral_message",
    "CustomCommandsMiddleware",
    "create_custom_command_skill_tool",
    "AsyncExecuteSchema",
    "BACKGROUND_EXEC_SYSTEM_INSTRUCTIONS",
    "BACKGROUND_TASK_NOTIFICATION_SOURCE",
    "BackgroundCommandMiddleware",
]

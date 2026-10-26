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

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "REPOSITORY_TOOL_NAMES": ("talkops.middleware._repository_bounds", "REPOSITORY_TOOL_NAMES"),
    "RepositoryBounds": ("talkops.middleware._repository_bounds", "RepositoryBounds"),
    "AskUserMiddleware": ("talkops.middleware.ask_user", "AskUserMiddleware"),
    "AUTO_MODE_EVENT_TYPE": ("talkops.middleware.auto_mode", "AUTO_MODE_EVENT_TYPE"),
    "is_potentially_destructive": ("talkops.middleware.auto_mode", "is_potentially_destructive"),
    "AutoModeHITLMiddleware": ("talkops.middleware.auto_mode_hitl", "AutoModeHITLMiddleware"),
    "AutoModeState": ("talkops.middleware.auto_mode_hitl", "AutoModeState"),
    "BACKGROUND_EXEC_SYSTEM_INSTRUCTIONS": ("talkops.middleware.background_commands", "BACKGROUND_EXEC_SYSTEM_INSTRUCTIONS"),
    "BACKGROUND_TASK_NOTIFICATION_SOURCE": ("talkops.middleware.background_commands", "BACKGROUND_TASK_NOTIFICATION_SOURCE"),
    "AsyncExecuteSchema": ("talkops.middleware.background_commands", "AsyncExecuteSchema"),
    "BackgroundCommandMiddleware": ("talkops.middleware.background_commands", "BackgroundCommandMiddleware"),
    "BTW_PROMPT_PREFIX": ("talkops.middleware.btw", "BTW_PROMPT_PREFIX"),
    "BTW_TIMEOUT_SECONDS": ("talkops.middleware.btw", "BTW_TIMEOUT_SECONDS"),
    "BtwSideQuestionMiddleware": ("talkops.middleware.btw", "BtwSideQuestionMiddleware"),
    "format_btw_prompt": ("talkops.middleware.btw", "format_btw_prompt"),
    "is_btw_turn": ("talkops.middleware.btw", "is_btw_turn"),
    "is_ephemeral_message": ("talkops.middleware.btw", "is_ephemeral_message"),
    "create_cli_compaction_middleware": ("talkops.middleware.compaction", "create_cli_compaction_middleware"),
    "ConfigurableModelMiddleware": ("talkops.middleware.configurable_model", "ConfigurableModelMiddleware"),
    "ACTIVE_SESSION_THREAD_ID": ("talkops.middleware.cost_tracking", "ACTIVE_SESSION_THREAD_ID"),
    "CostState": ("talkops.middleware.cost_tracking", "CostState"),
    "CostTrackingMiddleware": ("talkops.middleware.cost_tracking", "CostTrackingMiddleware"),
    "CustomCommandsMiddleware": ("talkops.middleware.custom_commands", "CustomCommandsMiddleware"),
    "create_custom_command_skill_tool": ("talkops.middleware.custom_commands", "create_custom_command_skill_tool"),
    "GlmTerminalStallRecoveryMiddleware": ("talkops.middleware.glm_stall_recovery", "GlmTerminalStallRecoveryMiddleware"),
    "GoalAmendRequest": ("talkops.middleware.goal_criteria", "GoalAmendRequest"),
    "GoalCreateRequest": ("talkops.middleware.goal_criteria", "GoalCreateRequest"),
    "GoalCriteriaMiddleware": ("talkops.middleware.goal_criteria", "GoalCriteriaMiddleware"),
    "GoalCriteriaRequest": ("talkops.middleware.goal_criteria", "GoalCriteriaRequest"),
    "GoalCriteriaState": ("talkops.middleware.goal_criteria", "GoalCriteriaState"),
    "GoalProposal": ("talkops.middleware.goal_criteria", "GoalProposal"),
    "GOAL_CONTROL_MESSAGE_SOURCE": ("talkops.middleware.goal_state_notice", "GOAL_CONTROL_MESSAGE_SOURCE"),
    "GOAL_STATE_MESSAGE_SOURCE": ("talkops.middleware.goal_state_notice", "GOAL_STATE_MESSAGE_SOURCE"),
    "build_goal_state_notice": ("talkops.middleware.goal_state_notice", "build_goal_state_notice"),
    "is_conversation_control_message": ("talkops.middleware.goal_state_notice", "is_conversation_control_message"),
    "GoalToolsMiddleware": ("talkops.middleware.goal_tools", "GoalToolsMiddleware"),
    "HeadlessMCPGuardMiddleware": ("talkops.middleware.headless_mcp_guard", "HeadlessMCPGuardMiddleware"),
    "JevDynamicModelRouterMiddleware": ("talkops.middleware.jev_model_router", "JevDynamicModelRouterMiddleware"),
    "LocalContextMiddleware": ("talkops.middleware.local_context", "LocalContextMiddleware"),
    "MCPContextMiddleware": ("talkops.middleware.mcp_context", "MCPContextMiddleware"),
    "MCPToolMiddleware": ("talkops.middleware.mcp_middleware", "MCPToolMiddleware"),
    "normalize_mcp_arguments": ("talkops.middleware.mcp_middleware", "normalize_mcp_arguments"),
    "ManagedMemoryGuardMiddleware": ("talkops.middleware.memory_guard", "ManagedMemoryGuardMiddleware"),
    "CodeModelRetryMiddleware": ("talkops.middleware.model_retry", "CodeModelRetryMiddleware"),
    "BaseAgentMiddleware": ("talkops.middleware.registry", "BaseAgentMiddleware"),
    "MiddlewareRegistry": ("talkops.middleware.registry", "MiddlewareRegistry"),
    "get_middleware_registry": ("talkops.middleware.registry", "get_middleware_registry"),
    "register_middleware": ("talkops.middleware.registry", "register_middleware"),
    "ReliableRubricMiddleware": ("talkops.middleware.reliable_rubric", "ReliableRubricMiddleware"),
    "ResumeStateMiddleware": ("talkops.middleware.resume_state", "ResumeStateMiddleware"),
    "ServerHooksMiddleware": ("talkops.middleware.server_hooks", "ServerHooksMiddleware"),
    "ServerHooksState": ("talkops.middleware.server_hooks", "ServerHooksState"),
    "ShellAllowListMiddleware": ("talkops.middleware.shell_allow_list", "ShellAllowListMiddleware"),
    "PluginSkillsMiddleware": ("talkops.middleware.skills", "PluginSkillsMiddleware"),
    "SubagentTelemetryMiddleware": ("talkops.middleware.subagent_telemetry", "SubagentTelemetryMiddleware"),
    "SubagentsMiddleware": ("talkops.middleware.subagents", "SubagentsMiddleware"),
    "TOOL_ALIAS_MAP": ("talkops.middleware.tool_filter", "TOOL_ALIAS_MAP"),
    "ToolFilterMiddleware": ("talkops.middleware.tool_filter", "ToolFilterMiddleware"),
    "UnifiedSystemMessageMiddleware": ("talkops.middleware.unified_system_message", "UnifiedSystemMessageMiddleware"),
    "unify_system_message": ("talkops.middleware.unified_system_message", "unify_system_message"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_IMPORTS:
        module_name, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_name)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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

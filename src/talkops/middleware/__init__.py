"""TalkOps middleware modules and centralized registry."""

from __future__ import annotations

from talkops.middleware.registry import (
    BaseAgentMiddleware,
    MiddlewareRegistry,
    get_middleware_registry,
    register_middleware,
)
from talkops.middleware.configurable_model import ConfigurableModelMiddleware
from talkops.middleware.jev_model_router import JevDynamicModelRouterMiddleware
from talkops.middleware.local_context import LocalContextMiddleware
from talkops.middleware.ask_user import AskUserMiddleware
from talkops.middleware.resume_state import ResumeStateMiddleware
from talkops.middleware.compaction import create_cli_compaction_middleware
from talkops.middleware.skills import PluginSkillsMiddleware
from talkops.middleware.cost_tracking import ACTIVE_SESSION_THREAD_ID, CostState, CostTrackingMiddleware
from talkops.middleware.auto_mode_hitl import AutoModeHITLMiddleware, AutoModeState
from talkops.middleware.auto_mode import AUTO_MODE_EVENT_TYPE, is_potentially_destructive
from talkops.middleware.subagents import SubagentsMiddleware
from talkops.middleware.subagent_telemetry import SubagentTelemetryMiddleware
from talkops.middleware.goal_state_notice import (
    GOAL_CONTROL_MESSAGE_SOURCE,
    GOAL_STATE_MESSAGE_SOURCE,
    build_goal_state_notice,
    is_conversation_control_message,
)
from talkops.middleware.goal_tools import GoalToolsMiddleware
from talkops.middleware.goal_criteria import (
    GoalAmendRequest,
    GoalCreateRequest,
    GoalCriteriaMiddleware,
    GoalCriteriaRequest,
    GoalCriteriaState,
    GoalProposal,
)
from talkops.middleware.reliable_rubric import ReliableRubricMiddleware
from talkops.middleware.unified_system_message import (
    UnifiedSystemMessageMiddleware,
    unify_system_message,
)
from talkops.middleware._repository_bounds import (
    RepositoryBounds,
    REPOSITORY_TOOL_NAMES,
)
from talkops.middleware.glm_stall_recovery import GlmTerminalStallRecoveryMiddleware
from talkops.middleware.headless_mcp_guard import HeadlessMCPGuardMiddleware
from talkops.middleware.mcp_context import MCPContextMiddleware
from talkops.middleware.mcp_middleware import MCPToolMiddleware, normalize_mcp_arguments
from talkops.middleware.memory_guard import ManagedMemoryGuardMiddleware
from talkops.middleware.model_retry import CodeModelRetryMiddleware
from talkops.middleware.server_hooks import ServerHooksMiddleware, ServerHooksState
from talkops.middleware.shell_allow_list import ShellAllowListMiddleware
from talkops.middleware.tool_filter import ToolFilterMiddleware, TOOL_ALIAS_MAP
from talkops.middleware.btw import (
    BTW_PROMPT_PREFIX,
    BTW_TIMEOUT_SECONDS,
    BtwSideQuestionMiddleware,
    format_btw_prompt,
    is_btw_turn,
    is_ephemeral_message,
)
from talkops.middleware.custom_commands import (
    CustomCommandsMiddleware,
    create_custom_command_skill_tool,
)
from talkops.middleware.background_commands import (
    AsyncExecuteSchema,
    BACKGROUND_EXEC_SYSTEM_INSTRUCTIONS,
    BACKGROUND_TASK_NOTIFICATION_SOURCE,
    BackgroundCommandMiddleware,
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

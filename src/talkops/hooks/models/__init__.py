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

"""Hook domain and transport models."""

from talkops.hooks.models.config import (
    CommandHandlerSpec,
    HooksConfig,
    MatcherGroup,
)
from talkops.hooks.models.domain import (
    AgentIdentity,
    BackgroundTaskSnapshot,
    BaseHookDecision,
    CompactTrigger,
    EffortLevel,
    HookContext,
    HookDecision,
    HookDiagnostic,
    HookDomainEvent,
    HookEffect,
    HookEvent,
    HookInvocation,
    HookOwner,
    NotificationDecision,
    NotificationEvent,
    PermissionEffect,
    PermissionRequestDecision,
    PermissionRequestEvent,
    PostToolUseDecision,
    PostToolUseEvent,
    PostToolUseFailureDecision,
    PostToolUseFailureEvent,
    PreCompactDecision,
    PreCompactEvent,
    PreToolUseDecision,
    PreToolUseEvent,
    SessionCronSnapshot,
    SessionEndCause,
    SessionEndDecision,
    SessionEndEvent,
    SessionStartCause,
    SessionStartDecision,
    SessionStartEvent,
    StopDecision,
    StopEvent,
    SubagentStartDecision,
    SubagentStartEvent,
    SubagentStopDecision,
    SubagentStopEvent,
    TalkOpsNotification,
    TalkOpsNotificationKind,
    ToolCallData,
    UserPromptSubmitDecision,
    UserPromptSubmitEvent,
)
from talkops.hooks.models.transport import (
    HookInvocationRequest,
    HookInvocationResponse,
)

__all__ = [
    "AgentIdentity",
    "BackgroundTaskSnapshot",
    "BaseHookDecision",
    "CommandHandlerSpec",
    "CompactTrigger",
    "EffortLevel",
    "HookContext",
    "HookDecision",
    "HookDiagnostic",
    "HookDomainEvent",
    "HookEffect",
    "HookEvent",
    "HookInvocation",
    "HookInvocationRequest",
    "HookInvocationResponse",
    "HookOwner",
    "HooksConfig",
    "MatcherGroup",
    "NotificationDecision",
    "NotificationEvent",
    "TalkOpsNotification",
    "TalkOpsNotificationKind",
    "PermissionEffect",
    "PermissionRequestDecision",
    "PermissionRequestEvent",
    "PostToolUseDecision",
    "PostToolUseEvent",
    "PostToolUseFailureDecision",
    "PostToolUseFailureEvent",
    "PreCompactDecision",
    "PreCompactEvent",
    "PreToolUseDecision",
    "PreToolUseEvent",
    "SessionCronSnapshot",
    "SessionEndCause",
    "SessionEndDecision",
    "SessionEndEvent",
    "SessionStartCause",
    "SessionStartDecision",
    "SessionStartEvent",
    "StopDecision",
    "StopEvent",
    "SubagentStartDecision",
    "SubagentStartEvent",
    "SubagentStopDecision",
    "SubagentStopEvent",
    "ToolCallData",
    "UserPromptSubmitDecision",
    "UserPromptSubmitEvent",
]

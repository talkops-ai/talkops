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

"""State models, cloud operational channels, and persistence for TalkOps."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from talkops.state import conversation_title as conversation_title
    from talkops.state.background_task_channels import (
        BackgroundTaskChannels,
        BackgroundTaskRecord,
        BackgroundTaskStatus,
        merge_background_tasks,
    )
    from talkops.state.base import (
        AgentState,
        BaseAgentState,
        TalkOpsAgentState,
    )
    from talkops.state.btw_channels import BtwChannels
    from talkops.state.cloud_context import (
        AWSContext,
        AzureContext,
        CloudContextState,
        CloudEnvironmentContext,
        CloudProvider,
        CloudResourceAction,
        CloudResourceRef,
        GCPContext,
        IaCContext,
        K8sContext,
        coerce_cloud_provider,
        format_cloud_badge,
        make_resource_ref,
    )
    from talkops.state.conversation_title import (
        CONVERSATION_TITLE_SYSTEM_PROMPT,
        clean_prompt_for_title,
        generate_conversation_title,
        get_thread_display_label,
        maybe_generate_conversation_title,
        postprocess_conversation_title,
        resolve_conversation_title_model,
        schedule_conversation_title_generation,
        truncate_summary_preview,
    )
    from talkops.state.goal_channels import (
        INHERIT_RUBRIC_MODEL,
        RUBRIC_RESULT_VALUES,
        GoalProposalKind,
        GoalRubricChannels,
        GoalStatus,
        coerce_goal_proposal_kind,
        coerce_goal_status,
        coerce_model_spec,
    )
    from talkops.state.goal_state_limits import (
        GOAL_APPLICATION_CHAR_LIMIT,
        GOAL_NOTICE_TEXT_CHAR_LIMIT,
        GOAL_OBJECTIVE_CHAR_LIMIT,
        GOAL_STATUS_NOTE_CHAR_LIMIT,
        GOAL_STATUS_VALUES,
        RUBRIC_CHAR_LIMIT,
        GoalStateSizeError,
        validate_goal_application,
        validate_goal_application_total,
        validate_goal_objective,
        validate_goal_status_note,
        validate_rubric,
    )
    from talkops.state.resume_state import ResumeState
    from talkops.state.service_context import (
        ManagedService,
        ServiceContextState,
        ServiceOperationRecord,
        ServiceOperationType,
        ServiceStatus,
        make_operation_record,
        make_service_record,
    )
    from talkops.state.session import (
        SessionManager,
        ThreadInfo,
        delete_thread,
        find_similar_threads,
        format_relative_timestamp,
        format_timestamp,
        generate_thread_id,
        get_checkpointer,
        get_db_path,
        get_most_recent,
        get_thread_title,
        list_threads,
        prewarm_thread_message_counts,
        reconcile_thread_summaries,
        set_db_path,
        set_thread_title,
        thread_exists,
        upsert_thread_summary,
    )
    from talkops.state.state_migration import migrate_legacy_state
    from talkops.state.webhook_sre_channels import WebhookSREChannels

_LAZY_IMPORTS: dict[str, tuple[str, str | None]] = {
    "conversation_title": ("talkops.state.conversation_title", None),
    "BackgroundTaskChannels": ("talkops.state.background_task_channels", "BackgroundTaskChannels"),
    "BackgroundTaskRecord": ("talkops.state.background_task_channels", "BackgroundTaskRecord"),
    "BackgroundTaskStatus": ("talkops.state.background_task_channels", "BackgroundTaskStatus"),
    "merge_background_tasks": ("talkops.state.background_task_channels", "merge_background_tasks"),
    "AgentState": ("talkops.state.base", "AgentState"),
    "BaseAgentState": ("talkops.state.base", "BaseAgentState"),
    "TalkOpsAgentState": ("talkops.state.base", "TalkOpsAgentState"),
    "BtwChannels": ("talkops.state.btw_channels", "BtwChannels"),
    "AWSContext": ("talkops.state.cloud_context", "AWSContext"),
    "AzureContext": ("talkops.state.cloud_context", "AzureContext"),
    "CloudContextState": ("talkops.state.cloud_context", "CloudContextState"),
    "CloudEnvironmentContext": ("talkops.state.cloud_context", "CloudEnvironmentContext"),
    "CloudProvider": ("talkops.state.cloud_context", "CloudProvider"),
    "CloudResourceAction": ("talkops.state.cloud_context", "CloudResourceAction"),
    "CloudResourceRef": ("talkops.state.cloud_context", "CloudResourceRef"),
    "GCPContext": ("talkops.state.cloud_context", "GCPContext"),
    "IaCContext": ("talkops.state.cloud_context", "IaCContext"),
    "K8sContext": ("talkops.state.cloud_context", "K8sContext"),
    "coerce_cloud_provider": ("talkops.state.cloud_context", "coerce_cloud_provider"),
    "format_cloud_badge": ("talkops.state.cloud_context", "format_cloud_badge"),
    "make_resource_ref": ("talkops.state.cloud_context", "make_resource_ref"),
    "CONVERSATION_TITLE_SYSTEM_PROMPT": ("talkops.state.conversation_title", "CONVERSATION_TITLE_SYSTEM_PROMPT"),
    "clean_prompt_for_title": ("talkops.state.conversation_title", "clean_prompt_for_title"),
    "generate_conversation_title": ("talkops.state.conversation_title", "generate_conversation_title"),
    "get_thread_display_label": ("talkops.state.conversation_title", "get_thread_display_label"),
    "maybe_generate_conversation_title": ("talkops.state.conversation_title", "maybe_generate_conversation_title"),
    "postprocess_conversation_title": ("talkops.state.conversation_title", "postprocess_conversation_title"),
    "resolve_conversation_title_model": ("talkops.state.conversation_title", "resolve_conversation_title_model"),
    "schedule_conversation_title_generation": ("talkops.state.conversation_title", "schedule_conversation_title_generation"),
    "truncate_summary_preview": ("talkops.state.conversation_title", "truncate_summary_preview"),
    "INHERIT_RUBRIC_MODEL": ("talkops.state.goal_channels", "INHERIT_RUBRIC_MODEL"),
    "RUBRIC_RESULT_VALUES": ("talkops.state.goal_channels", "RUBRIC_RESULT_VALUES"),
    "GoalProposalKind": ("talkops.state.goal_channels", "GoalProposalKind"),
    "GoalRubricChannels": ("talkops.state.goal_channels", "GoalRubricChannels"),
    "GoalStatus": ("talkops.state.goal_channels", "GoalStatus"),
    "coerce_goal_proposal_kind": ("talkops.state.goal_channels", "coerce_goal_proposal_kind"),
    "coerce_goal_status": ("talkops.state.goal_channels", "coerce_goal_status"),
    "coerce_model_spec": ("talkops.state.goal_channels", "coerce_model_spec"),
    "GOAL_APPLICATION_CHAR_LIMIT": ("talkops.state.goal_state_limits", "GOAL_APPLICATION_CHAR_LIMIT"),
    "GOAL_NOTICE_TEXT_CHAR_LIMIT": ("talkops.state.goal_state_limits", "GOAL_NOTICE_TEXT_CHAR_LIMIT"),
    "GOAL_OBJECTIVE_CHAR_LIMIT": ("talkops.state.goal_state_limits", "GOAL_OBJECTIVE_CHAR_LIMIT"),
    "GOAL_STATUS_NOTE_CHAR_LIMIT": ("talkops.state.goal_state_limits", "GOAL_STATUS_NOTE_CHAR_LIMIT"),
    "GOAL_STATUS_VALUES": ("talkops.state.goal_state_limits", "GOAL_STATUS_VALUES"),
    "RUBRIC_CHAR_LIMIT": ("talkops.state.goal_state_limits", "RUBRIC_CHAR_LIMIT"),
    "GoalStateSizeError": ("talkops.state.goal_state_limits", "GoalStateSizeError"),
    "validate_goal_application": ("talkops.state.goal_state_limits", "validate_goal_application"),
    "validate_goal_application_total": ("talkops.state.goal_state_limits", "validate_goal_application_total"),
    "validate_goal_objective": ("talkops.state.goal_state_limits", "validate_goal_objective"),
    "validate_goal_status_note": ("talkops.state.goal_state_limits", "validate_goal_status_note"),
    "validate_rubric": ("talkops.state.goal_state_limits", "validate_rubric"),
    "ResumeState": ("talkops.state.resume_state", "ResumeState"),
    "ManagedService": ("talkops.state.service_context", "ManagedService"),
    "ServiceContextState": ("talkops.state.service_context", "ServiceContextState"),
    "ServiceOperationRecord": ("talkops.state.service_context", "ServiceOperationRecord"),
    "ServiceOperationType": ("talkops.state.service_context", "ServiceOperationType"),
    "ServiceStatus": ("talkops.state.service_context", "ServiceStatus"),
    "make_operation_record": ("talkops.state.service_context", "make_operation_record"),
    "make_service_record": ("talkops.state.service_context", "make_service_record"),
    "SessionManager": ("talkops.state.session", "SessionManager"),
    "ThreadInfo": ("talkops.state.session", "ThreadInfo"),
    "delete_thread": ("talkops.state.session", "delete_thread"),
    "find_similar_threads": ("talkops.state.session", "find_similar_threads"),
    "format_relative_timestamp": ("talkops.state.session", "format_relative_timestamp"),
    "format_timestamp": ("talkops.state.session", "format_timestamp"),
    "generate_thread_id": ("talkops.state.session", "generate_thread_id"),
    "get_checkpointer": ("talkops.state.session", "get_checkpointer"),
    "get_db_path": ("talkops.state.session", "get_db_path"),
    "get_most_recent": ("talkops.state.session", "get_most_recent"),
    "get_thread_title": ("talkops.state.session", "get_thread_title"),
    "list_threads": ("talkops.state.session", "list_threads"),
    "prewarm_thread_message_counts": ("talkops.state.session", "prewarm_thread_message_counts"),
    "reconcile_thread_summaries": ("talkops.state.session", "reconcile_thread_summaries"),
    "set_db_path": ("talkops.state.session", "set_db_path"),
    "set_thread_title": ("talkops.state.session", "set_thread_title"),
    "thread_exists": ("talkops.state.session", "thread_exists"),
    "upsert_thread_summary": ("talkops.state.session", "upsert_thread_summary"),
    "migrate_legacy_state": ("talkops.state.state_migration", "migrate_legacy_state"),
    "WebhookSREChannels": ("talkops.state.webhook_sre_channels", "WebhookSREChannels"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_IMPORTS:
        module_name, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_name)
        value = module if attr_name is None else getattr(module, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Core Composite State
    "AgentState",
    "BackgroundTaskChannels",
    "BackgroundTaskRecord",
    "BackgroundTaskStatus",
    "BaseAgentState",
    "BtwChannels",
    "TalkOpsAgentState",
    "WebhookSREChannels",
    "merge_background_tasks",
    # Cloud Operations Context
    "AWSContext",
    "AzureContext",
    "CloudContextState",
    "CloudEnvironmentContext",
    "CloudProvider",
    "CloudResourceAction",
    "CloudResourceRef",
    "GCPContext",
    "IaCContext",
    "K8sContext",
    "coerce_cloud_provider",
    "format_cloud_badge",
    "make_resource_ref",
    # Service Management & Plugin State
    "ManagedService",
    "ServiceContextState",
    "ServiceOperationRecord",
    "ServiceOperationType",
    "ServiceStatus",
    "make_operation_record",
    "make_service_record",
    # Goal & Rubric Channels
    "GOAL_APPLICATION_CHAR_LIMIT",
    "GOAL_NOTICE_TEXT_CHAR_LIMIT",
    "GOAL_OBJECTIVE_CHAR_LIMIT",
    "GOAL_STATUS_NOTE_CHAR_LIMIT",
    "GOAL_STATUS_VALUES",
    "GoalProposalKind",
    "GoalRubricChannels",
    "GoalStateSizeError",
    "GoalStatus",
    "INHERIT_RUBRIC_MODEL",
    "RUBRIC_CHAR_LIMIT",
    "RUBRIC_RESULT_VALUES",
    "coerce_goal_proposal_kind",
    "coerce_goal_status",
    "coerce_model_spec",
    "validate_goal_application",
    "validate_goal_application_total",
    "validate_goal_objective",
    "validate_goal_status_note",
    "validate_rubric",
    # Resume State
    "ResumeState",
    # Conversation Title Generation & Session Persistence
    "CONVERSATION_TITLE_SYSTEM_PROMPT",
    "SessionManager",
    "ThreadInfo",
    "clean_prompt_for_title",
    "delete_thread",
    "find_similar_threads",
    "format_relative_timestamp",
    "format_timestamp",
    "generate_conversation_title",
    "generate_thread_id",
    "get_checkpointer",
    "get_db_path",
    "get_most_recent",
    "get_thread_display_label",
    "get_thread_title",
    "list_threads",
    "maybe_generate_conversation_title",
    "postprocess_conversation_title",
    "prewarm_thread_message_counts",
    "reconcile_thread_summaries",
    "resolve_conversation_title_model",
    "schedule_conversation_title_generation",
    "set_db_path",
    "set_thread_title",
    "thread_exists",
    "truncate_summary_preview",
    "upsert_thread_summary",
    # Migration
    "migrate_legacy_state",
]

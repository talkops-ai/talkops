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

"""Textual widgets for TalkOps.

Import directly from submodules, e.g.:

    from talkops.ui.widgets.chat_input import ChatInput
    from talkops.ui.widgets.messages import AssistantMessage, MessageList, UserMessage
    from talkops.ui.widgets.diff import EnhancedDiff, compose_diff_lines
    from talkops.ui.widgets.autocomplete import AutocompletePopup
    from talkops.ui.widgets.status import StatusBar
    from talkops.ui.widgets.subagent_panel import SubagentPanel
    from talkops.ui.widgets.approval import ApprovalMenu, ApprovalModalScreen
    from talkops.ui.widgets.ask_user import AskUserMenu
    from talkops.ui.widgets.goal_review import GoalReviewMenu, GoalReviewScreen
    from talkops.ui.widgets.model_selector import ModelSelectorScreen
    from talkops.ui.widgets.theme_selector import ThemeSelectorScreen
    from talkops.ui.widgets.thread_selector import ThreadSelectorScreen
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from talkops.ui.widgets.agent_selector import AgentSelectorScreen
    from talkops.ui.widgets.approval import (
        ApprovalDecided,
        ApprovalMenu,
        ApprovalModalScreen,
        assess_tool_risk,
    )
    from talkops.ui.widgets.ask_user import (
        OTHER_CHOICE_LABEL,
        AskUserMenu,
        AskUserTextArea,
    )
    from talkops.ui.widgets.auth import (
        CONFIGURATION_DOCS_URL,
        PROVIDER_API_KEY_URLS,
        AuthCheckbox,
        AuthPromptScreen,
        AuthResult,
        is_langsmith,
    )
    from talkops.ui.widgets.auth_manager import AuthManagerScreen
    from talkops.ui.widgets.auto_mode_notice import (
        AUTO_MODE_DOCS_URL,
        AUTO_MODE_NOTICE_BODY,
        AutoModeNoticeScreen,
    )
    from talkops.ui.widgets.autocomplete import (
        AutocompletePopup,
        CompletionController,
        CompletionResult,
        CompletionView,
        FuzzyFileController,
        MultiCompletionManager,
        SlashCommandController,
    )
    from talkops.ui.widgets.btw_card import BtwCardPanel, BtwExchange
    from talkops.ui.widgets.chat_input import (
        ChatInput,
        ChatTextArea,
        InputMode,
        detect_input_mode,
    )
    from talkops.ui.widgets.cloud_selector import (
        CloudProfileOption,
        CloudProfileSelectorScreen,
    )
    from talkops.ui.widgets.config_manager import ConfigManagerScreen
    from talkops.ui.widgets.debug_console import (
        DEBUG_TOGGLE_KEY,
        DebugConsoleScreen,
        SnapshotField,
    )
    from talkops.ui.widgets.devops_renderers import (
        AnsiblePlaybookRenderer,
        HelmDiffRenderer,
        KubectlRenderer,
        TerraformPlanRenderer,
        TerraformPlanWidget,
    )
    from talkops.ui.widgets.diff import EnhancedDiff, compose_diff_lines
    from talkops.ui.widgets.effort_selector import EffortSelectorScreen
    from talkops.ui.widgets.goal_review import (
        GoalReviewAccepted,
        GoalReviewCancelled,
        GoalReviewEdited,
        GoalReviewMenu,
        GoalReviewRejected,
        GoalReviewResult,
        GoalReviewScreen,
        GoalReviewTextArea,
    )
    from talkops.ui.widgets.goal_status import GoalStatusPanel
    from talkops.ui.widgets.infra_panel import InfraStatePanel
    from talkops.ui.widgets.input_dock import InputDock
    from talkops.ui.widgets.install_confirm import InstallProviderConfirmScreen
    from talkops.ui.widgets.loading import (
        BRAILLE_SPINNER_FRAMES,
        LoadingWidget,
        Spinner,
        format_duration,
    )
    from talkops.ui.widgets.mcp_viewer import (
        MCP_VIEWER_RECONNECT_REQUEST,
        MCPServerErrorScreen,
        MCPServerHeaderItem,
        MCPToolItem,
        MCPViewerScreen,
    )
    from talkops.ui.widgets.messages import (
        AppMessage,
        AssistantMessage,
        CommandOutputMessage,
        DiffMessage,
        ErrorMessage,
        FormattedOutput,
        MessageList,
        QueuedUserMessage,
        RubricResultMessage,
        SkillMessage,
        SystemMessage,
        ThinkingMessage,
        ToolCallMessage,
        ToolGroupSummary,
        TurnSummary,
        UserMessage,
        summarize_live_tool_group,
        summarize_tool_group,
    )
    from talkops.ui.widgets.modal_base import (
        ModalDialog,
        ModalOptionRow,
        ModalSearchInput,
        OpsModalScreen,
        PillTab,
        PillTabs,
        format_modal_hints,
        format_modal_option_row,
        truncate_cells,
    )
    from talkops.ui.widgets.model_selector import ModelOption, ModelSelectorScreen
    from talkops.ui.widgets.notification_center import NotificationCenter
    from talkops.ui.widgets.notification_settings import (
        WARNING_TOGGLES,
        NotificationSettingsScreen,
    )
    from talkops.ui.widgets.operation_card import OperationCard
    from talkops.ui.widgets.permissions_manager import PermissionsManagerScreen
    from talkops.ui.widgets.plugin_manager import (
        PluginManagerScreen,
        PluginTabLabel,
        PluginTabSelected,
    )
    from talkops.ui.widgets.pool_selector import PoolSelectorScreen
    from talkops.ui.widgets.preamble import _mode_color
    from talkops.ui.widgets.skills_viewer import (
        SkillDetailScreen,
        SkillItemWidget,
        SkillsViewerScreen,
    )
    from talkops.ui.widgets.status import (
        CONNECTION_STATES,
        PROVIDER_PREFIX_STRIPS,
        BranchLabel,
        ConnectionState,
        ModelLabel,
        StatusBar,
    )
    from talkops.ui.widgets.subagent_panel import (
        SubagentColumn,
        SubagentPanel,
        SubagentStatus,
        _format_timing,
        _Phase,
        _SubagentRecord,
        sanitize_control_chars,
    )
    from talkops.ui.widgets.theme_selector import ThemeSelectorScreen
    from talkops.ui.widgets.thread_selector import (
        CustomOptionList,
        SearchInput,
        ThreadSelector,
        ThreadSelectorScreen,
    )
    from talkops.ui.widgets.toast import ToastNotification, show_toast
    from talkops.ui.widgets.tool_display import (
        MAX_ARG_LENGTH,
        abbreviate_path,
        format_tool_display,
        format_tool_result_summary,
        get_tool_display_name,
        register_tool_display_name,
        register_tool_summary_formatter,
        truncate_value,
    )
    from talkops.ui.widgets.tool_renderers import (
        DeleteFileRenderer,
        EditFileRenderer,
        TaskRenderer,
        ToolRenderer,
        ToolRendererResult,
        WriteFileRenderer,
        get_renderer,
        render_tool_approval,
    )
    from talkops.ui.widgets.tool_widgets import (
        EditFileApprovalWidget,
        GenericApprovalWidget,
        TaskApprovalWidget,
        ToolApprovalWidget,
        WriteFileApprovalWidget,
        format_display_content,
    )
    from talkops.ui.widgets.welcome import (
        MAX_DISPLAY_ITEMS,
        MAX_INLINE_CHARS,
        WelcomeBanner,
    )
    from talkops.ui.widgets.welcome_popup import WelcomeDetailPopup

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "AgentSelectorScreen": ("talkops.ui.widgets.agent_selector", "AgentSelectorScreen"),
    "ApprovalDecided": ("talkops.ui.widgets.approval", "ApprovalDecided"),
    "ApprovalMenu": ("talkops.ui.widgets.approval", "ApprovalMenu"),
    "ApprovalModalScreen": ("talkops.ui.widgets.approval", "ApprovalModalScreen"),
    "assess_tool_risk": ("talkops.ui.widgets.approval", "assess_tool_risk"),
    "OTHER_CHOICE_LABEL": ("talkops.ui.widgets.ask_user", "OTHER_CHOICE_LABEL"),
    "AskUserMenu": ("talkops.ui.widgets.ask_user", "AskUserMenu"),
    "AskUserTextArea": ("talkops.ui.widgets.ask_user", "AskUserTextArea"),
    "CONFIGURATION_DOCS_URL": ("talkops.ui.widgets.auth", "CONFIGURATION_DOCS_URL"),
    "PROVIDER_API_KEY_URLS": ("talkops.ui.widgets.auth", "PROVIDER_API_KEY_URLS"),
    "AuthCheckbox": ("talkops.ui.widgets.auth", "AuthCheckbox"),
    "AuthPromptScreen": ("talkops.ui.widgets.auth", "AuthPromptScreen"),
    "AuthResult": ("talkops.ui.widgets.auth", "AuthResult"),
    "is_langsmith": ("talkops.ui.widgets.auth", "is_langsmith"),
    "AuthManagerScreen": ("talkops.ui.widgets.auth_manager", "AuthManagerScreen"),
    "AUTO_MODE_DOCS_URL": ("talkops.ui.widgets.auto_mode_notice", "AUTO_MODE_DOCS_URL"),
    "AUTO_MODE_NOTICE_BODY": ("talkops.ui.widgets.auto_mode_notice", "AUTO_MODE_NOTICE_BODY"),
    "AutoModeNoticeScreen": ("talkops.ui.widgets.auto_mode_notice", "AutoModeNoticeScreen"),
    "AutocompletePopup": ("talkops.ui.widgets.autocomplete", "AutocompletePopup"),
    "CompletionController": ("talkops.ui.widgets.autocomplete", "CompletionController"),
    "CompletionResult": ("talkops.ui.widgets.autocomplete", "CompletionResult"),
    "CompletionView": ("talkops.ui.widgets.autocomplete", "CompletionView"),
    "FuzzyFileController": ("talkops.ui.widgets.autocomplete", "FuzzyFileController"),
    "MultiCompletionManager": ("talkops.ui.widgets.autocomplete", "MultiCompletionManager"),
    "SlashCommandController": ("talkops.ui.widgets.autocomplete", "SlashCommandController"),
    "BtwCardPanel": ("talkops.ui.widgets.btw_card", "BtwCardPanel"),
    "BtwExchange": ("talkops.ui.widgets.btw_card", "BtwExchange"),
    "ChatInput": ("talkops.ui.widgets.chat_input", "ChatInput"),
    "ChatTextArea": ("talkops.ui.widgets.chat_input", "ChatTextArea"),
    "InputMode": ("talkops.ui.widgets.chat_input", "InputMode"),
    "detect_input_mode": ("talkops.ui.widgets.chat_input", "detect_input_mode"),
    "CloudProfileOption": ("talkops.ui.widgets.cloud_selector", "CloudProfileOption"),
    "CloudProfileSelectorScreen": ("talkops.ui.widgets.cloud_selector", "CloudProfileSelectorScreen"),
    "ConfigManagerScreen": ("talkops.ui.widgets.config_manager", "ConfigManagerScreen"),
    "DEBUG_TOGGLE_KEY": ("talkops.ui.widgets.debug_console", "DEBUG_TOGGLE_KEY"),
    "DebugConsoleScreen": ("talkops.ui.widgets.debug_console", "DebugConsoleScreen"),
    "SnapshotField": ("talkops.ui.widgets.debug_console", "SnapshotField"),
    "AnsiblePlaybookRenderer": ("talkops.ui.widgets.devops_renderers", "AnsiblePlaybookRenderer"),
    "HelmDiffRenderer": ("talkops.ui.widgets.devops_renderers", "HelmDiffRenderer"),
    "KubectlRenderer": ("talkops.ui.widgets.devops_renderers", "KubectlRenderer"),
    "TerraformPlanRenderer": ("talkops.ui.widgets.devops_renderers", "TerraformPlanRenderer"),
    "TerraformPlanWidget": ("talkops.ui.widgets.devops_renderers", "TerraformPlanWidget"),
    "EnhancedDiff": ("talkops.ui.widgets.diff", "EnhancedDiff"),
    "compose_diff_lines": ("talkops.ui.widgets.diff", "compose_diff_lines"),
    "EffortSelectorScreen": ("talkops.ui.widgets.effort_selector", "EffortSelectorScreen"),
    "GoalReviewAccepted": ("talkops.ui.widgets.goal_review", "GoalReviewAccepted"),
    "GoalReviewCancelled": ("talkops.ui.widgets.goal_review", "GoalReviewCancelled"),
    "GoalReviewEdited": ("talkops.ui.widgets.goal_review", "GoalReviewEdited"),
    "GoalReviewMenu": ("talkops.ui.widgets.goal_review", "GoalReviewMenu"),
    "GoalReviewRejected": ("talkops.ui.widgets.goal_review", "GoalReviewRejected"),
    "GoalReviewResult": ("talkops.ui.widgets.goal_review", "GoalReviewResult"),
    "GoalReviewScreen": ("talkops.ui.widgets.goal_review", "GoalReviewScreen"),
    "GoalReviewTextArea": ("talkops.ui.widgets.goal_review", "GoalReviewTextArea"),
    "GoalStatusPanel": ("talkops.ui.widgets.goal_status", "GoalStatusPanel"),
    "InfraStatePanel": ("talkops.ui.widgets.infra_panel", "InfraStatePanel"),
    "InputDock": ("talkops.ui.widgets.input_dock", "InputDock"),
    "InstallProviderConfirmScreen": ("talkops.ui.widgets.install_confirm", "InstallProviderConfirmScreen"),
    "BRAILLE_SPINNER_FRAMES": ("talkops.ui.widgets.loading", "BRAILLE_SPINNER_FRAMES"),
    "LoadingWidget": ("talkops.ui.widgets.loading", "LoadingWidget"),
    "Spinner": ("talkops.ui.widgets.loading", "Spinner"),
    "format_duration": ("talkops.ui.widgets.loading", "format_duration"),
    "MCP_VIEWER_RECONNECT_REQUEST": ("talkops.ui.widgets.mcp_viewer", "MCP_VIEWER_RECONNECT_REQUEST"),
    "MCPServerErrorScreen": ("talkops.ui.widgets.mcp_viewer", "MCPServerErrorScreen"),
    "MCPServerHeaderItem": ("talkops.ui.widgets.mcp_viewer", "MCPServerHeaderItem"),
    "MCPToolItem": ("talkops.ui.widgets.mcp_viewer", "MCPToolItem"),
    "MCPViewerScreen": ("talkops.ui.widgets.mcp_viewer", "MCPViewerScreen"),
    "AppMessage": ("talkops.ui.widgets.messages", "AppMessage"),
    "AssistantMessage": ("talkops.ui.widgets.messages", "AssistantMessage"),
    "CommandOutputMessage": ("talkops.ui.widgets.messages", "CommandOutputMessage"),
    "DiffMessage": ("talkops.ui.widgets.messages", "DiffMessage"),
    "ErrorMessage": ("talkops.ui.widgets.messages", "ErrorMessage"),
    "FormattedOutput": ("talkops.ui.widgets.messages", "FormattedOutput"),
    "MessageList": ("talkops.ui.widgets.messages", "MessageList"),
    "QueuedUserMessage": ("talkops.ui.widgets.messages", "QueuedUserMessage"),
    "RubricResultMessage": ("talkops.ui.widgets.messages", "RubricResultMessage"),
    "SkillMessage": ("talkops.ui.widgets.messages", "SkillMessage"),
    "SystemMessage": ("talkops.ui.widgets.messages", "SystemMessage"),
    "ThinkingMessage": ("talkops.ui.widgets.messages", "ThinkingMessage"),
    "ToolCallMessage": ("talkops.ui.widgets.messages", "ToolCallMessage"),
    "ToolGroupSummary": ("talkops.ui.widgets.messages", "ToolGroupSummary"),
    "TurnSummary": ("talkops.ui.widgets.messages", "TurnSummary"),
    "UserMessage": ("talkops.ui.widgets.messages", "UserMessage"),
    "summarize_live_tool_group": ("talkops.ui.widgets.messages", "summarize_live_tool_group"),
    "summarize_tool_group": ("talkops.ui.widgets.messages", "summarize_tool_group"),
    "ModalDialog": ("talkops.ui.widgets.modal_base", "ModalDialog"),
    "ModalOptionRow": ("talkops.ui.widgets.modal_base", "ModalOptionRow"),
    "ModalSearchInput": ("talkops.ui.widgets.modal_base", "ModalSearchInput"),
    "OpsModalScreen": ("talkops.ui.widgets.modal_base", "OpsModalScreen"),
    "PillTab": ("talkops.ui.widgets.modal_base", "PillTab"),
    "PillTabs": ("talkops.ui.widgets.modal_base", "PillTabs"),
    "format_modal_hints": ("talkops.ui.widgets.modal_base", "format_modal_hints"),
    "format_modal_option_row": ("talkops.ui.widgets.modal_base", "format_modal_option_row"),
    "truncate_cells": ("talkops.ui.widgets.modal_base", "truncate_cells"),
    "ModelOption": ("talkops.ui.widgets.model_selector", "ModelOption"),
    "ModelSelectorScreen": ("talkops.ui.widgets.model_selector", "ModelSelectorScreen"),
    "NotificationCenter": ("talkops.ui.widgets.notification_center", "NotificationCenter"),
    "WARNING_TOGGLES": ("talkops.ui.widgets.notification_settings", "WARNING_TOGGLES"),
    "NotificationSettingsScreen": ("talkops.ui.widgets.notification_settings", "NotificationSettingsScreen"),
    "OperationCard": ("talkops.ui.widgets.operation_card", "OperationCard"),
    "PermissionsManagerScreen": ("talkops.ui.widgets.permissions_manager", "PermissionsManagerScreen"),
    "PluginManagerScreen": ("talkops.ui.widgets.plugin_manager", "PluginManagerScreen"),
    "PluginTabLabel": ("talkops.ui.widgets.plugin_manager", "PluginTabLabel"),
    "PluginTabSelected": ("talkops.ui.widgets.plugin_manager", "PluginTabSelected"),
    "PoolSelectorScreen": ("talkops.ui.widgets.pool_selector", "PoolSelectorScreen"),
    "_mode_color": ("talkops.ui.widgets.preamble", "_mode_color"),
    "SkillDetailScreen": ("talkops.ui.widgets.skills_viewer", "SkillDetailScreen"),
    "SkillItemWidget": ("talkops.ui.widgets.skills_viewer", "SkillItemWidget"),
    "SkillsViewerScreen": ("talkops.ui.widgets.skills_viewer", "SkillsViewerScreen"),
    "CONNECTION_STATES": ("talkops.ui.widgets.status", "CONNECTION_STATES"),
    "PROVIDER_PREFIX_STRIPS": ("talkops.ui.widgets.status", "PROVIDER_PREFIX_STRIPS"),
    "BranchLabel": ("talkops.ui.widgets.status", "BranchLabel"),
    "ConnectionState": ("talkops.ui.widgets.status", "ConnectionState"),
    "ModelLabel": ("talkops.ui.widgets.status", "ModelLabel"),
    "StatusBar": ("talkops.ui.widgets.status", "StatusBar"),
    "SubagentColumn": ("talkops.ui.widgets.subagent_panel", "SubagentColumn"),
    "SubagentPanel": ("talkops.ui.widgets.subagent_panel", "SubagentPanel"),
    "SubagentStatus": ("talkops.ui.widgets.subagent_panel", "SubagentStatus"),
    "_format_timing": ("talkops.ui.widgets.subagent_panel", "_format_timing"),
    "_Phase": ("talkops.ui.widgets.subagent_panel", "_Phase"),
    "_SubagentRecord": ("talkops.ui.widgets.subagent_panel", "_SubagentRecord"),
    "sanitize_control_chars": ("talkops.ui.widgets.subagent_panel", "sanitize_control_chars"),
    "ThemeSelectorScreen": ("talkops.ui.widgets.theme_selector", "ThemeSelectorScreen"),
    "CustomOptionList": ("talkops.ui.widgets.thread_selector", "CustomOptionList"),
    "SearchInput": ("talkops.ui.widgets.thread_selector", "SearchInput"),
    "ThreadSelector": ("talkops.ui.widgets.thread_selector", "ThreadSelector"),
    "ThreadSelectorScreen": ("talkops.ui.widgets.thread_selector", "ThreadSelectorScreen"),
    "ToastNotification": ("talkops.ui.widgets.toast", "ToastNotification"),
    "show_toast": ("talkops.ui.widgets.toast", "show_toast"),
    "MAX_ARG_LENGTH": ("talkops.ui.widgets.tool_display", "MAX_ARG_LENGTH"),
    "abbreviate_path": ("talkops.ui.widgets.tool_display", "abbreviate_path"),
    "format_tool_display": ("talkops.ui.widgets.tool_display", "format_tool_display"),
    "format_tool_result_summary": ("talkops.ui.widgets.tool_display", "format_tool_result_summary"),
    "get_tool_display_name": ("talkops.ui.widgets.tool_display", "get_tool_display_name"),
    "register_tool_display_name": ("talkops.ui.widgets.tool_display", "register_tool_display_name"),
    "register_tool_summary_formatter": ("talkops.ui.widgets.tool_display", "register_tool_summary_formatter"),
    "truncate_value": ("talkops.ui.widgets.tool_display", "truncate_value"),
    "DeleteFileRenderer": ("talkops.ui.widgets.tool_renderers", "DeleteFileRenderer"),
    "EditFileRenderer": ("talkops.ui.widgets.tool_renderers", "EditFileRenderer"),
    "TaskRenderer": ("talkops.ui.widgets.tool_renderers", "TaskRenderer"),
    "ToolRenderer": ("talkops.ui.widgets.tool_renderers", "ToolRenderer"),
    "ToolRendererResult": ("talkops.ui.widgets.tool_renderers", "ToolRendererResult"),
    "WriteFileRenderer": ("talkops.ui.widgets.tool_renderers", "WriteFileRenderer"),
    "get_renderer": ("talkops.ui.widgets.tool_renderers", "get_renderer"),
    "render_tool_approval": ("talkops.ui.widgets.tool_renderers", "render_tool_approval"),
    "EditFileApprovalWidget": ("talkops.ui.widgets.tool_widgets", "EditFileApprovalWidget"),
    "GenericApprovalWidget": ("talkops.ui.widgets.tool_widgets", "GenericApprovalWidget"),
    "TaskApprovalWidget": ("talkops.ui.widgets.tool_widgets", "TaskApprovalWidget"),
    "ToolApprovalWidget": ("talkops.ui.widgets.tool_widgets", "ToolApprovalWidget"),
    "WriteFileApprovalWidget": ("talkops.ui.widgets.tool_widgets", "WriteFileApprovalWidget"),
    "format_display_content": ("talkops.ui.widgets.tool_widgets", "format_display_content"),
    "MAX_DISPLAY_ITEMS": ("talkops.ui.widgets.welcome", "MAX_DISPLAY_ITEMS"),
    "MAX_INLINE_CHARS": ("talkops.ui.widgets.welcome", "MAX_INLINE_CHARS"),
    "WelcomeBanner": ("talkops.ui.widgets.welcome", "WelcomeBanner"),
    "WelcomeDetailPopup": ("talkops.ui.widgets.welcome_popup", "WelcomeDetailPopup"),
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
    "AUTO_MODE_DOCS_URL",
    "AUTO_MODE_NOTICE_BODY",
    "AgentSelectorScreen",
    "AnsiblePlaybookRenderer",
    "AppMessage",
    "ApprovalDecided",
    "ApprovalMenu",
    "ApprovalModalScreen",
    "AskUserMenu",
    "AskUserTextArea",
    "AssistantMessage",
    "AuthCheckbox",
    "AuthManagerScreen",
    "AuthPromptScreen",
    "AuthResult",
    "AutocompletePopup",
    "AutoModeNoticeScreen",
    "BRAILLE_SPINNER_FRAMES",
    "BranchLabel",
    "BtwCardPanel",
    "BtwExchange",
    "CONFIGURATION_DOCS_URL",
    "CONNECTION_STATES",
    "ChatInput",
    "ChatTextArea",
    "CloudProfileOption",
    "CloudProfileSelectorScreen",
    "CommandOutputMessage",
    "CompletionController",
    "CompletionResult",
    "CompletionView",
    "ConfigManagerScreen",
    "ConnectionState",
    "CustomOptionList",
    "DEBUG_TOGGLE_KEY",
    "DebugConsoleScreen",
    "DeleteFileRenderer",
    "DiffMessage",
    "EditFileApprovalWidget",
    "EditFileRenderer",
    "EffortSelectorScreen",
    "EnhancedDiff",
    "ErrorMessage",
    "FormattedOutput",
    "FuzzyFileController",
    "GenericApprovalWidget",
    "GoalReviewAccepted",
    "GoalReviewCancelled",
    "GoalReviewEdited",
    "GoalReviewMenu",
    "GoalReviewRejected",
    "GoalReviewResult",
    "GoalReviewScreen",
    "GoalReviewTextArea",
    "GoalStatusPanel",
    "HelmDiffRenderer",
    "InfraStatePanel",
    "InputMode",
    "InstallProviderConfirmScreen",
    "KubectlRenderer",
    "LoadingWidget",
    "MAX_ARG_LENGTH",
    "MAX_DISPLAY_ITEMS",
    "MAX_INLINE_CHARS",
    "MCPServerErrorScreen",
    "MCPServerHeaderItem",
    "MCPToolItem",
    "MCPViewerScreen",
    "MCP_VIEWER_RECONNECT_REQUEST",
    "MessageList",
    "ModalDialog",
    "ModalOptionRow",
    "ModalSearchInput",
    "ModelLabel",
    "ModelOption",
    "ModelSelectorScreen",
    "MultiCompletionManager",
    "NotificationCenter",
    "NotificationSettingsScreen",
    "OTHER_CHOICE_LABEL",
    "OperationCard",
    "OpsModalScreen",
    "PillTab",
    "PillTabs",
    "PROVIDER_API_KEY_URLS",
    "PROVIDER_PREFIX_STRIPS",
    "PermissionsManagerScreen",
    "PluginManagerScreen",
    "PluginTabLabel",
    "PluginTabSelected",
    "PoolSelectorScreen",
    "QueuedUserMessage",
    "RubricResultMessage",
    "SearchInput",
    "SkillDetailScreen",
    "SkillItemWidget",
    "SkillMessage",
    "SkillsViewerScreen",
    "SlashCommandController",
    "SnapshotField",
    "Spinner",
    "StatusBar",
    "SubagentColumn",
    "SubagentPanel",
    "SubagentStatus",
    "SystemMessage",
    "TaskApprovalWidget",
    "TaskRenderer",
    "TerraformPlanRenderer",
    "TerraformPlanWidget",
    "ThemeSelectorScreen",
    "ThinkingMessage",
    "ThreadSelector",
    "ThreadSelectorScreen",
    "ToastNotification",
    "ToolApprovalWidget",
    "ToolCallMessage",
    "ToolGroupSummary",
    "ToolRenderer",
    "ToolRendererResult",
    "UserMessage",
    "WARNING_TOGGLES",
    "WelcomeBanner",
    "WelcomeDetailPopup",
    "WriteFileApprovalWidget",
    "WriteFileRenderer",
    "_Phase",
    "_SubagentRecord",
    "_format_timing",
    "_mode_color",
    "abbreviate_path",
    "assess_tool_risk",
    "compose_diff_lines",
    "detect_input_mode",
    "format_display_content",
    "format_duration",
    "format_modal_hints",
    "format_modal_option_row",
    "format_tool_display",
    "format_tool_result_summary",
    "get_renderer",
    "get_tool_display_name",
    "is_langsmith",
    "register_tool_display_name",
    "register_tool_summary_formatter",
    "render_tool_approval",
    "sanitize_control_chars",
    "show_toast",
    "summarize_live_tool_group",
    "summarize_tool_group",
    "truncate_cells",
    "truncate_value",
]

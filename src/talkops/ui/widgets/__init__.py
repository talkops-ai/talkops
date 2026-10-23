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

from talkops.ui.widgets.agent_selector import (
    AgentSelectorScreen,
)
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
from talkops.ui.widgets.auth_manager import (
    AuthManagerScreen,
)
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
from talkops.ui.widgets.btw_card import (
    BtwCardPanel,
    BtwExchange,
)
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
from talkops.ui.widgets.config_manager import (
    ConfigManagerScreen,
)
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
from talkops.ui.widgets.diff import (
    EnhancedDiff,
    compose_diff_lines,
)
from talkops.ui.widgets.effort_selector import (
    EffortSelectorScreen,
)
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
from talkops.ui.widgets.goal_status import (
    GoalStatusPanel,
)
from talkops.ui.widgets.infra_panel import (
    InfraStatePanel,
)
from talkops.ui.widgets.input_dock import (
    InputDock,
)
from talkops.ui.widgets.install_confirm import (
    InstallProviderConfirmScreen,
)
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
from talkops.ui.widgets.model_selector import (
    ModelOption,
    ModelSelectorScreen,
)
from talkops.ui.widgets.notification_center import (
    NotificationCenter,
)
from talkops.ui.widgets.notification_settings import (
    WARNING_TOGGLES,
    NotificationSettingsScreen,
)
from talkops.ui.widgets.operation_card import (
    OperationCard,
)
from talkops.ui.widgets.permissions_manager import (
    PermissionsManagerScreen,
)
from talkops.ui.widgets.plugin_manager import (
    PluginManagerScreen,
    PluginTabLabel,
    PluginTabSelected,
)
from talkops.ui.widgets.pool_selector import (
    PoolSelectorScreen,
)
from talkops.ui.widgets.preamble import (
    _mode_color,
)
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
from talkops.ui.widgets.theme_selector import (
    ThemeSelectorScreen,
)
from talkops.ui.widgets.thread_selector import (
    CustomOptionList,
    SearchInput,
    ThreadSelector,
    ThreadSelectorScreen,
)
from talkops.ui.widgets.toast import (
    ToastNotification,
    show_toast,
)
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
from talkops.ui.widgets.welcome_popup import (
    WelcomeDetailPopup,
)

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

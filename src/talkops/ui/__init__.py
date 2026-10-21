"""TalkOps TUI package.

Exports core application and adapter classes, as well as re-exports of primary
UI widgets from ``talkops.ui.widgets``.
"""

from talkops.ui.app import TalkOpsApp
from talkops.ui.textual_adapter import TextualAdapter
from talkops.ui.preferences import ThemeSelector
from talkops.ui.ui_help import (
    show_agents_help,
    show_auth_help,
    show_config_help,
    show_doctor_help,
    show_help,
    show_list_help,
    show_mcp_config_help,
    show_mcp_help,
    show_mcp_login_help,
    show_plugins_help,
    show_reset_help,
    show_skills_help,
    show_skills_info_help,
    show_skills_list_help,
    show_skills_trust_help,
    show_threads_delete_help,
    show_threads_help,
    show_threads_list_help,
    show_tools_help,
    show_tools_install_help,
    show_tools_list_help,
)
from talkops.ui.widgets.approval import (
    ApprovalDecided,
    ApprovalMenu,
    ApprovalModalScreen,
    assess_tool_risk,
)
from talkops.ui.widgets.ask_user import AskUserMenu
from talkops.ui.widgets.chat_input import ChatInput
from talkops.ui.widgets.devops_renderers import TerraformPlanWidget
from talkops.ui.widgets.diff import compose_diff_lines
from talkops.ui.widgets.infra_panel import InfraStatePanel
from talkops.ui.widgets.messages import (
    AssistantMessage,
    DiffMessage,
    ErrorMessage,
    MessageList,
    QueuedUserMessage,
    SkillMessage,
    SystemMessage,
    ToolCallMessage,
    ToolGroupSummary,
    UserMessage,
)
from talkops.ui.widgets.notification_center import NotificationCenter
from talkops.ui.widgets.operation_card import OperationCard
from talkops.ui.widgets.plugin_manager import PluginManagerScreen
from talkops.ui.widgets.skills_viewer import SkillsViewerScreen
from talkops.ui.widgets.status import StatusBar
from talkops.ui.widgets.subagent_panel import SubagentColumn, SubagentPanel
from talkops.ui.widgets.toast import ToastNotification, show_toast
from talkops.ui.widgets.welcome import WelcomeBanner
from talkops.ui.widgets.welcome_popup import WelcomeDetailPopup

__all__ = [
    "ApprovalDecided",
    "ApprovalMenu",
    "ApprovalModalScreen",
    "AskUserMenu",
    "AssistantMessage",
    "ChatInput",
    "TalkOpsApp",
    "DiffMessage",
    "ErrorMessage",
    "InfraStatePanel",
    "MessageList",
    "NotificationCenter",
    "OperationCard",
    "QueuedUserMessage",
    "SkillMessage",
    "StatusBar",
    "SubagentColumn",
    "SubagentPanel",
    "SystemMessage",
    "TerraformPlanWidget",
    "TextualAdapter",
    "ThemeSelector",
    "ToastNotification",
    "ToolCallMessage",
    "ToolGroupSummary",
    "UserMessage",
    "WelcomeBanner",
    "WelcomeDetailPopup",
    "assess_tool_risk",
    "compose_diff_lines",
    "show_agents_help",
    "show_auth_help",
    "show_config_help",
    "show_doctor_help",
    "show_help",
    "show_list_help",
    "show_mcp_config_help",
    "show_mcp_help",
    "show_mcp_login_help",
    "show_plugins_help",
    "show_reset_help",
    "show_skills_help",
    "show_skills_info_help",
    "show_skills_list_help",
    "show_skills_trust_help",
    "show_threads_delete_help",
    "show_threads_help",
    "show_threads_list_help",
    "show_toast",
    "show_tools_help",
    "show_tools_install_help",
    "show_tools_list_help",
]

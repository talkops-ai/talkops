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

"""Message widgets barrel re-export module.

For modularity, message widgets are split across domain-specific modules:
- `tool_call_message.py`: `ToolCallMessage`, `FormattedOutput`, and tool formatting constants/helpers
- `tool_group.py`: `ToolGroupSummary` and tool group summarization helpers
- `chat_messages.py`: `UserMessage`, `QueuedUserMessage`, `AssistantMessage`, `DiffMessage`, `ThinkingMessage`
- `status_messages.py`: `SkillMessage`, `ErrorMessage`, `SystemMessage`, `CommandOutputMessage`, `AppMessage`, `TurnSummary`, `RubricResultMessage`, and markdown wrappers
- `message_list.py`: `MessageList` and `compute_wheel_speed`

All public and internal symbols remain re-exported here for backwards compatibility.
"""

from __future__ import annotations

from talkops.ui.widgets.chat_messages import (
    _PROMPT_PREFIX_WIDTH,
    _USER_MSG_MAX_DISPLAY_CHARS,
    _USER_MSG_TRUNCATE_HEAD_CHARS,
    _USER_MSG_TRUNCATE_TAIL_CHARS,
    AssistantMessage,
    DiffMessage,
    QueuedUserMessage,
    ThinkingMessage,
    UserMessage,
    _assistant_markdown_class,
    _clean_thinking_line,
    _collapse_user_message,
    _mode_color,
    _select_prompt_body,
    _strip_prompt_prefix,
    _thinking_heading,
    _truncate_for_display,
    _UserMessageCollapsed,
    _UserMessageFull,
    _will_collapse,
)
from talkops.ui.widgets.message_list import (
    MessageList,
    compute_wheel_speed,
)
from talkops.ui.widgets.status_messages import (
    _MARKDOWN_MIN_RENDER_WIDTH,
    AppMessage,
    CommandOutputMessage,
    CompactionMessage,
    ErrorMessage,
    RubricResultMessage,
    SkillMessage,
    SystemMessage,
    TurnSummary,
    _format_turn_duration,
    _markdown_to_content,
    _MutedRichMarkdown,
    _RichMarkdownWrapper,
    _RubricResultToggle,
    _SkillToggle,
    _strip_frontmatter,
)
from talkops.ui.widgets.tool_call_message import (
    _ALWAYS_PREVIEW_TOOLS,
    _ASK_USER_ROW_SUMMARIES,
    _COLLAPSE_OUTPUT_BY_DEFAULT,
    _DEFAULT_TODO_WRAP_WIDTH,
    _FILE_DIFF_TOOLS,
    _MAX_INLINE_ARGS,
    _MAX_TODO_CONTENT_LEN,
    _MAX_WEB_CONTENT_LEN,
    _READ_FILE_GUTTER_RE,
    _STATUS_CLASSES,
    _SUCCESS_EXIT_RE,
    _TIMED_SUCCESS_TOOLS,
    _TODO_WRAP_GUARD_COLUMNS,
    _TOOL_AWAITING_APPROVAL_ACCESSORY_CLASS,
    _TOOL_GROUP_EXCLUSIONS,
    _TOOLS_WITH_HEADER_INFO,
    ASK_USER_ANSWERED_SUMMARY,
    ASK_USER_FAILED_SUMMARY,
    EXECUTE_HEADER_MAX_LENGTH,
    AskUserRowSummary,
    FormattedOutput,
    ToolCallMessage,
    _arg_shown_in_header,
    _strip_success_exit_line,
    _ToolStatus,
)
from talkops.ui.widgets.tool_group import (
    _TOOL_GROUP_COLLAPSED_ACCESSORY_CLASS,
    _TOOL_SUMMARY_CATEGORY,
    _TOOL_SUMMARY_PHRASES,
    ToolGroupSummary,
    _emphasize_counts,
    _join_segments,
    _summary_segment,
    summarize_live_tool_group,
    summarize_tool_group,
)

__all__ = [
    "ASK_USER_ANSWERED_SUMMARY",
    "ASK_USER_FAILED_SUMMARY",
    "AppMessage",
    "AskUserRowSummary",
    "AssistantMessage",
    "CommandOutputMessage",
    "CompactionMessage",
    "DiffMessage",
    "EXECUTE_HEADER_MAX_LENGTH",
    "ErrorMessage",
    "FormattedOutput",
    "MessageList",
    "QueuedUserMessage",
    "RubricResultMessage",
    "SkillMessage",
    "SystemMessage",
    "ThinkingMessage",
    "ToolCallMessage",
    "ToolGroupSummary",
    "TurnSummary",
    "UserMessage",
    "_ALWAYS_PREVIEW_TOOLS",
    "_ASK_USER_ROW_SUMMARIES",
    "_COLLAPSE_OUTPUT_BY_DEFAULT",
    "_DEFAULT_TODO_WRAP_WIDTH",
    "_FILE_DIFF_TOOLS",
    "_MARKDOWN_MIN_RENDER_WIDTH",
    "_MAX_INLINE_ARGS",
    "_MAX_TODO_CONTENT_LEN",
    "_MAX_WEB_CONTENT_LEN",
    "_MutedRichMarkdown",
    "_PROMPT_PREFIX_WIDTH",
    "_READ_FILE_GUTTER_RE",
    "_RichMarkdownWrapper",
    "_RubricResultToggle",
    "_STATUS_CLASSES",
    "_SUCCESS_EXIT_RE",
    "_SkillToggle",
    "_TIMED_SUCCESS_TOOLS",
    "_TODO_WRAP_GUARD_COLUMNS",
    "_TOOLS_WITH_HEADER_INFO",
    "_TOOL_AWAITING_APPROVAL_ACCESSORY_CLASS",
    "_TOOL_GROUP_COLLAPSED_ACCESSORY_CLASS",
    "_TOOL_GROUP_EXCLUSIONS",
    "_TOOL_SUMMARY_CATEGORY",
    "_TOOL_SUMMARY_PHRASES",
    "_ToolStatus",
    "_USER_MSG_MAX_DISPLAY_CHARS",
    "_USER_MSG_TRUNCATE_HEAD_CHARS",
    "_USER_MSG_TRUNCATE_TAIL_CHARS",
    "_UserMessageCollapsed",
    "_UserMessageFull",
    "_arg_shown_in_header",
    "_assistant_markdown_class",
    "_clean_thinking_line",
    "_collapse_user_message",
    "_emphasize_counts",
    "_format_turn_duration",
    "_join_segments",
    "_markdown_to_content",
    "_mode_color",
    "_select_prompt_body",
    "_strip_frontmatter",
    "_strip_prompt_prefix",
    "_strip_success_exit_line",
    "_summary_segment",
    "_thinking_heading",
    "_truncate_for_display",
    "_will_collapse",
    "compute_wheel_speed",
    "summarize_live_tool_group",
    "summarize_tool_group",
]
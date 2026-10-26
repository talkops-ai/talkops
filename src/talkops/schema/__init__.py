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

"""Schema definitions for TalkOps."""

from talkops.schema.interrupts import (
    # Ask User Protocol
    ASK_USER_ANSWERED_SUMMARY,
    ASK_USER_CANCELLED_ANSWER,
    ASK_USER_CANCELLED_SUMMARY,
    ASK_USER_ERROR_ANSWER_PREFIX,
    ASK_USER_FAILED_SUMMARY,
    ASK_USER_NO_ANSWER,
    ASK_USER_NOTHING_SELECTED,
    CHOICE_QUESTION_TYPES,
    # Hook Invocation Interrupts
    HOOK_INVOCATION_INTERRUPT_TYPE,
    QUESTION_TYPES,
    # Tool Approvals / HITL
    ActionRequest,
    ApproveDecision,
    AskUserAnswered,
    AskUserCancelled,
    AskUserRequest,
    AskUserResumePayload,
    AskUserWidgetResult,
    AutoApproveAllDecision,
    Choice,
    EditDecision,
    # Goal & Criteria Review
    GoalReviewRequest,
    GoalReviewResumePayload,
    HitlDecision,
    HITLRequest,
    HitlResumePayload,
    HookInvocationInterrupt,
    HookInvocationRequest,
    HookInvocationResponse,
    Question,
    QuestionType,
    RejectDecision,
    ReviewConfig,
    SmartApproveAllDecision,
    SwitchManualDecision,
    ValidatedQuestion,
    ask_user_answer_is_empty,
    build_hook_interrupt_payload,
    build_hook_resume_value,
    decode_multi_select_answer,
    encode_multi_select_answer,
    format_ask_user_error_answer,
    format_ask_user_transcript,
    is_hook_interrupt_payload,
    parse_hook_interrupt_payload,
    parse_hook_resume_value,
    render_ask_user_transcript_for_display,
)

__all__ = [
    # HITL & Tool Approval
    "ActionRequest",
    "ApproveDecision",
    "AutoApproveAllDecision",
    "EditDecision",
    "HITLRequest",
    "HitlDecision",
    "HitlResumePayload",
    "RejectDecision",
    "ReviewConfig",
    "SmartApproveAllDecision",
    "SwitchManualDecision",
    # Ask User Protocol
    "ASK_USER_ANSWERED_SUMMARY",
    "ASK_USER_CANCELLED_ANSWER",
    "ASK_USER_CANCELLED_SUMMARY",
    "ASK_USER_ERROR_ANSWER_PREFIX",
    "ASK_USER_FAILED_SUMMARY",
    "ASK_USER_NOTHING_SELECTED",
    "ASK_USER_NO_ANSWER",
    "AskUserAnswered",
    "AskUserCancelled",
    "AskUserRequest",
    "AskUserResumePayload",
    "AskUserWidgetResult",
    "CHOICE_QUESTION_TYPES",
    "Choice",
    "QUESTION_TYPES",
    "Question",
    "QuestionType",
    "ValidatedQuestion",
    "ask_user_answer_is_empty",
    "decode_multi_select_answer",
    "encode_multi_select_answer",
    "format_ask_user_error_answer",
    "format_ask_user_transcript",
    "render_ask_user_transcript_for_display",
    # Goal & Criteria Review
    "GoalReviewRequest",
    "GoalReviewResumePayload",
    # Hook Invocation Interrupts
    "HOOK_INVOCATION_INTERRUPT_TYPE",
    "HookInvocationInterrupt",
    "HookInvocationRequest",
    "HookInvocationResponse",
    "build_hook_interrupt_payload",
    "build_hook_resume_value",
    "is_hook_interrupt_payload",
    "parse_hook_interrupt_payload",
    "parse_hook_resume_value",
]

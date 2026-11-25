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

"""Shared channel exposure policies, text chunking, and Markdown formatting utilities.

Re-exports the canonical formatting and exposure engine shared between `talkops.webhooks`
and `talkops.channels` without duplicating AST/Block Kit/Adaptive Card compilation logic.
"""

from __future__ import annotations

from talkops.webhooks.formatting import (
    APPROVAL_REACTION_MAP,
    DEFAULT_MAX_TEXT_CHARS,
    DISCORD_MAX_TEXT_CHARS,
    SLACK_HEADER_MAX_CHARS,
    SLACK_MAX_BLOCKS,
    SLACK_MAX_TEXT_CHARS,
    SLACK_SECTION_MAX_CHARS,
    TEAMS_MAX_TEXT_CHARS,
    ChannelExposure,
    ExposureMode,
    MarkdownBlock,
    MarkdownTable,
    ParsedChannelOutput,
    apply_rca_feedback_state_to_discord_payload,
    apply_rca_feedback_state_to_slack_blocks,
    apply_rca_feedback_state_to_teams_cards,
    build_discord_incident_embed,
    build_discord_rca_embed,
    build_rca_markdown_attachment,
    build_slack_incident_attachment_payload,
    build_slack_incident_blocks,
    build_slack_rca_blocks,
    build_slack_response_blocks,
    build_slack_v2_interactive_blocks,
    build_teams_incident_card,
    build_teams_rca_card,
    build_teams_response_card,
    chunk_text,
    decode_slack_mrkdwn,
    exposure_from_mapping,
    extract_rca_takeaway,
    format_duration_human,
    format_for_channel,
    format_markdown_for_discord,
    format_markdown_for_slack,
    format_markdown_for_teams,
    normalize_reaction_decision,
    parse_channel_output,
    parse_incident_output,
    parse_markdown_blocks,
    parse_rca_output,
    render_sre_v2_hypothesis_summary,
    slack_incident_accent_color,
    split_csv,
    strip_slack_expand_flags,
    strip_slack_expand_flags_from_attachments,
)

__all__ = [
    "APPROVAL_REACTION_MAP",
    "DEFAULT_MAX_TEXT_CHARS",
    "DISCORD_MAX_TEXT_CHARS",
    "SLACK_HEADER_MAX_CHARS",
    "SLACK_MAX_BLOCKS",
    "SLACK_MAX_TEXT_CHARS",
    "SLACK_SECTION_MAX_CHARS",
    "TEAMS_MAX_TEXT_CHARS",
    "ChannelExposure",
    "ExposureMode",
    "MarkdownBlock",
    "MarkdownTable",
    "ParsedChannelOutput",
    "apply_rca_feedback_state_to_discord_payload",
    "apply_rca_feedback_state_to_slack_blocks",
    "apply_rca_feedback_state_to_teams_cards",
    "build_discord_incident_embed",
    "build_discord_rca_embed",
    "build_rca_markdown_attachment",
    "build_slack_incident_attachment_payload",
    "build_slack_incident_blocks",
    "build_slack_rca_blocks",
    "build_slack_response_blocks",
    "build_slack_v2_interactive_blocks",
    "build_teams_incident_card",
    "build_teams_rca_card",
    "build_teams_response_card",
    "chunk_text",
    "decode_slack_mrkdwn",
    "exposure_from_mapping",
    "extract_rca_takeaway",
    "format_duration_human",
    "format_for_channel",
    "format_markdown_for_discord",
    "format_markdown_for_slack",
    "format_markdown_for_teams",
    "normalize_reaction_decision",
    "parse_channel_output",
    "parse_incident_output",
    "parse_markdown_blocks",
    "parse_rca_output",
    "render_sre_v2_hypothesis_summary",
    "slack_incident_accent_color",
    "split_csv",
    "strip_slack_expand_flags",
    "strip_slack_expand_flags_from_attachments",
]

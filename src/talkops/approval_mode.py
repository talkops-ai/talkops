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

"""Approval modes for TalkOps.

Re-exports canonical definitions and store/acknowledgement utilities from `talkops.security.approval_mode`.
"""

from talkops.security.approval_mode import (
    ALL_APPROVAL_MODES,
    APPROVAL_MODE_NAMESPACE,
    AUTO_NOTICE_VERSION,
    INTERACTIVE_APPROVAL_MODES,
    YOLO_ACKNOWLEDGEMENT_POLICY_VERSION,
    ApprovalMode,
    ApprovalModePayload,
    approval_mode_key,
    approval_mode_payload,
    approval_mode_scope,
    approval_state_path,
    aread_approval_mode_from_store,
    awrite_approval_mode,
    coerce_approval_mode,
    get_approval_mode,
    has_auto_mode_notice,
    has_yolo_acknowledgement,
    next_approval_mode,
    read_approval_mode_from_store,
    save_auto_mode_notice,
    save_yolo_acknowledgement,
    set_approval_mode,
)

__all__ = [
    "ALL_APPROVAL_MODES",
    "APPROVAL_MODE_NAMESPACE",
    "AUTO_NOTICE_VERSION",
    "INTERACTIVE_APPROVAL_MODES",
    "YOLO_ACKNOWLEDGEMENT_POLICY_VERSION",
    "ApprovalMode",
    "ApprovalModePayload",
    "approval_mode_key",
    "approval_mode_payload",
    "approval_mode_scope",
    "approval_state_path",
    "aread_approval_mode_from_store",
    "awrite_approval_mode",
    "coerce_approval_mode",
    "get_approval_mode",
    "has_auto_mode_notice",
    "has_yolo_acknowledgement",
    "next_approval_mode",
    "read_approval_mode_from_store",
    "save_auto_mode_notice",
    "save_yolo_acknowledgement",
    "set_approval_mode",
]

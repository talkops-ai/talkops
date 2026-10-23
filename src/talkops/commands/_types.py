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

"""Command surface type definitions."""

from enum import StrEnum
from typing import Literal


class BypassTier(StrEnum):
    """Classification that controls whether a command can skip the message queue."""

    ALWAYS = "always"
    """Execute regardless of any busy state."""

    CONNECTING = "connecting"
    """Bypass only during initial server connection."""

    IMMEDIATE_UI = "immediate_ui"
    """Open modal UI immediately; real work deferred via callback."""

    IMMEDIATE = "immediate"
    """Execute immediately without queuing; does not open modal UI."""

    SIDE_EFFECT_FREE = "side_effect_free"
    """Execute side effect immediately; defer chat output until idle."""

    QUEUED = "queued"
    """Must wait in the queue when the app is busy."""

    NORMAL = "queued"
    """Backward compatibility alias for QUEUED."""

NotifySeverity = Literal["information", "warning", "error"]


class SafetyLevel(StrEnum):
    """Risk classification for every command."""

    READ_ONLY = "read_only"
    """No side effects: e.g. /help, /tokens, /context."""

    LOW_RISK = "low_risk"
    """Reversible changes: e.g. /theme, /effort, /model."""

    HIGH_RISK = "high_risk"
    """Significant changes: e.g. /tf-plan, /deploy (dry-run)."""

    DESTRUCTIVE = "destructive"
    """Irreversible changes: e.g. /tf-apply, /rollback on prod."""


class CommandCategory(StrEnum):
    """Taxonomy layer a command belongs to."""

    CORE = "core"
    POWER = "power"
    DEVOPS = "devops"
    AUTOMATION = "automation"


__all__ = ["BypassTier", "CommandCategory", "NotifySeverity", "SafetyLevel"]

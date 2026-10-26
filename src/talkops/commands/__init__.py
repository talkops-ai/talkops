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

"""Command surface framework package for TalkOps."""

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._router import CommandRouter, discover_command_handlers
from talkops.commands._types import (
    BypassTier,
    CommandCategory,
    NotifySeverity,
    SafetyLevel,
)
from talkops.commands.registry import CommandRegistry

__all__ = [
    "BaseCommandHandler",
    "BypassTier",
    "CommandCategory",
    "CommandContext",
    "CommandRegistry",
    "CommandResult",
    "CommandRouter",
    "NotifySeverity",
    "SafetyLevel",
    "discover_command_handlers",
]


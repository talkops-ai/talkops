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

"""Toast notification helper for TalkOps TUI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from textual.notifications import SeverityLevel

if TYPE_CHECKING:
    from textual.app import App


@dataclass
class ToastNotification:
    """Represent toast notification."""
    id: str
    title: str
    message: str
    severity: SeverityLevel = "information"
    timestamp: float = 0.0
    dismissed: bool = False


def show_toast(
    app: App[Any],
    message: str,
    title: str = "",
    severity: SeverityLevel = "information",
) -> None:
    """Trigger a Textual notification styled with severity."""
    app.notify(message=message, title=title, severity=severity)

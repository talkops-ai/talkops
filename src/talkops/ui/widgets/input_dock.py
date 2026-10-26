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

"""Bottom input dock container (`InputDock`) for the TalkOps TUI.

`InputDock` encapsulates the bottom container holding:
  - `SubagentPanel` (`#subagent-panel`)
  - `GoalStatusPanel` (`#goal-status-panel`)
  - `BtwCardPanel` (`#btw-card-panel`)
  - `ChatInput` (`#input-area`)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.containers import Container
from textual.css.query import NoMatches

from talkops.ui.widgets.background_task_panel import BackgroundTaskPanel
from talkops.ui.widgets.btw_card import BtwCardPanel
from talkops.ui.widgets.chat_input import ChatInput
from talkops.ui.widgets.goal_status import GoalStatusPanel
from talkops.ui.widgets.subagent_panel import SubagentPanel

if TYPE_CHECKING:
    from textual.app import ComposeResult


class InputDock(Container):
    """Bottom-dock multiplexer and status/card host above the prompt input."""

    def __init__(
        self,
        *,
        id: str = "bottom-container",
        image_tracker: Any = None,
        **kwargs: Any,
    ) -> None:
        """Initialize InputDock."""
        super().__init__(id=id, **kwargs)
        self._image_tracker = image_tracker

    def compose(self) -> ComposeResult:
        """Compose and yield child widgets for this component."""
        yield SubagentPanel(id="subagent-panel")
        yield BackgroundTaskPanel(id="background-task-panel")
        yield GoalStatusPanel(id="goal-status-panel")
        yield BtwCardPanel(id="btw-card-panel")
        yield ChatInput(id="input-area", image_tracker=self._image_tracker)

    @property
    def chat_input(self) -> ChatInput | None:
        """Return the `ChatInput` widget if mounted."""
        try:
            return self.query_one("#input-area", ChatInput)
        except NoMatches:
            return None

    @property
    def subagent_panel(self) -> SubagentPanel | None:
        """Return the `SubagentPanel` widget if mounted."""
        try:
            return self.query_one("#subagent-panel", SubagentPanel)
        except NoMatches:
            return None

    @property
    def background_task_panel(self) -> BackgroundTaskPanel | None:
        """Return the `BackgroundTaskPanel` widget if mounted."""
        try:
            return self.query_one("#background-task-panel", BackgroundTaskPanel)
        except NoMatches:
            return None

    @property
    def goal_status_panel(self) -> GoalStatusPanel | None:
        """Return the `GoalStatusPanel` widget if mounted."""
        try:
            return self.query_one("#goal-status-panel", GoalStatusPanel)
        except NoMatches:
            return None

    @property
    def btw_card_panel(self) -> BtwCardPanel | None:
        """Return the `BtwCardPanel` widget if mounted."""
        try:
            return self.query_one("#btw-card-panel", BtwCardPanel)
        except NoMatches:
            return None

    def focus_input(self) -> None:
        """Focus the active `ChatInput` widget."""
        ci = self.chat_input
        if ci is not None:
            ci.focus_input()

    def dismiss_popup_or_mode(self) -> bool:
        """Dismiss active autocomplete popup or exit slash/shell mode on Escape."""
        ci = self.chat_input
        if ci is None:
            return False
        if hasattr(ci, "dismiss_completion") and ci.dismiss_completion():
            return True
        if hasattr(ci, "exit_mode") and ci.exit_mode():
            return True
        return False


__all__ = ["InputDock"]

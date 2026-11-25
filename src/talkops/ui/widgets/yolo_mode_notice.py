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

"""First-enable confirmation modal for YOLO mode.

Shown at most once per install (per policy version) when YOLO becomes active
in the TUI. Enter keeps YOLO and records the acknowledgement; `m` or Esc
reverts to Manual and leaves the acknowledgement unsaved so it appears again
next time.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding, BindingType
from textual.widgets import Markdown, Static

from talkops.ui.widgets._links import open_checked_url_async
from talkops.ui.widgets.modal_base import ModalBodyScroll, OpsModalScreen

if TYPE_CHECKING:
    from textual.app import ComposeResult


class YoloModeNoticeResult(StrEnum):
    """Outcome of the YOLO first-enable notice."""

    ACKNOWLEDGE = "acknowledge"
    MANUAL = "manual"
    CANCEL = "cancel"


YOLO_MODE_DOCS_URL = (
    "https://docs.langchain.com/oss/python/deepagents/code/approval-modes"
)
"""Canonical docs page for Manual / Auto / Smart / YOLO behavior."""

YOLO_MODE_NOTICE_BODY = (
    "You are about to enable **YOLO mode**. The agent may run shell commands, "
    "edit files, make network calls, and use other tools on this machine "
    "**without asking you first**.\n\n"
    "Only continue if you're comfortable letting it act unsupervised.\n\n"
    "Leave YOLO any time with **Shift+Tab** or `/manual`.\n\n"
    "This notice appears **once** on this machine.\n\n"
    f"[Learn more about approval modes]({YOLO_MODE_DOCS_URL})"
)
"""Default Markdown body shown on first YOLO enable."""


class YoloModeNoticeScreen(OpsModalScreen[YoloModeNoticeResult]):
    """In-TUI acknowledgement shown when unrestricted YOLO mode is enabled."""

    DIALOG_CLASSES: ClassVar[str] = "-compact"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "confirm", "Enable YOLO", show=False, priority=True),
        Binding("m", "switch_to_manual", "Switch to Manual", show=False, priority=True),
        Binding("escape", "cancel", "Keep previous", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    YoloModeNoticeScreen .ops-modal-dialog {
        border: solid $error;
        border-title-color: $error;
    }

    YoloModeNoticeScreen .yolo-mode-notice-title {
        display: none;
    }

    YoloModeNoticeScreen .yolo-mode-notice-body {
        height: auto;
        color: $text;
        margin-bottom: 1;
        padding: 0;
    }

    YoloModeNoticeScreen .yolo-mode-notice-body > * {
        margin: 0 0 1 0;
    }

    YoloModeNoticeScreen .yolo-mode-notice-body > *:last-child {
        margin-bottom: 0;
    }

    YoloModeNoticeScreen .yolo-mode-notice-help {
        display: none;
    }
    """

    can_focus = True

    def __init__(self, body: str | None = None) -> None:
        """Initialize the YOLO mode notice modal."""
        super().__init__()
        self._body = YOLO_MODE_NOTICE_BODY if body is None else body

    def compose_modal(self) -> ComposeResult:
        """Compose the YOLO first-enable notice."""
        yield Static(
            "YOLO mode",
            classes="yolo-mode-notice-title",
            markup=False,
        )
        with ModalBodyScroll(notice=True):
            yield Markdown(
                self._body,
                classes="yolo-mode-notice-body",
                open_links=False,
            )
        yield Static(
            "Enter to enable YOLO · m for Manual · Esc to cancel",
            classes="yolo-mode-notice-help",
            markup=False,
        )

    def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        super().on_mount()
        self.set_modal_title("YOLO mode")
        self.set_modal_subtitle([
            ("enter", "enable yolo"),
            ("m", "manual"),
            ("esc", "cancel"),
        ])
        self.focus()

    async def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        """Open docs (or any body link) with the shared URL helper."""
        event.stop()
        await open_checked_url_async(event.href, app=self.app, notify_on_success=True)

    def action_confirm(self) -> None:
        """Acknowledge YOLO and mark the notice dismissed without re-showing."""
        self.dismiss(YoloModeNoticeResult.ACKNOWLEDGE)

    def action_switch_to_manual(self) -> None:
        """Switch to Manual instead of YOLO, without persisting acknowledgement."""
        self.dismiss(YoloModeNoticeResult.MANUAL)

    def action_cancel(self) -> None:
        """Cancel and revert without persisting acknowledgement."""
        self.dismiss(YoloModeNoticeResult.CANCEL)


__all__ = [
    "YOLO_MODE_DOCS_URL",
    "YOLO_MODE_NOTICE_BODY",
    "YoloModeNoticeResult",
    "YoloModeNoticeScreen",
]

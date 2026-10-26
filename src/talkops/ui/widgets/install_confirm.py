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

"""Confirmation modals for package installation in TalkOps TUI."""

from __future__ import annotations

from typing import ClassVar

from textual.binding import Binding, BindingType
from textual.widgets import Static

from talkops.model.config import PROVIDER_DISPLAY_NAMES
from talkops.ui.widgets.modal_base import OpsModalScreen
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


class InstallProviderConfirmScreen(OpsModalScreen[bool]):
    """Confirmation modal for installing a model provider's extra.

    Shown when the user selects a model whose provider integration package is not installed.
    Dismisses with True to install and False to cancel.
    """

    DIALOG_CLASSES: ClassVar[str] = "-compact"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "confirm", "Install", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    InstallProviderConfirmScreen .install-confirm-title {
        display: none;
    }

    InstallProviderConfirmScreen .install-confirm-body {
        height: auto;
        color: $text;
        margin-bottom: 1;
    }

    InstallProviderConfirmScreen .install-confirm-help {
        display: none;
    }
    """

    def __init__(
        self, provider: str, extra: str, model_spec: str | None = None
    ) -> None:
        """Initialize InstallProviderConfirmScreen."""
        super().__init__()
        self._provider = provider
        self._extra = extra
        self._model_spec = model_spec

    def compose_modal(self):
        """Execute compose modal on InstallProviderConfirmScreen."""
        provider_name = PROVIDER_DISPLAY_NAMES.get(
            self._provider, self._provider.replace("_", " ").title()
        )
        if self._model_spec is not None:
            body = (
                f"To use [bold]{self._model_spec}[/bold], talkops needs to "
                f"install the [bold]{self._extra}[/bold] integration. This will add "
                f"the provider package to your talkops environment."
            )
        else:
            body = (
                f"To add a key for [bold]{provider_name}[/bold], talkops needs to "
                f"install the [bold]{self._extra}[/bold] integration. This will add "
                f"the provider package to your talkops environment."
            )

        yield Static(
            f"Install {provider_name} support?",
            classes="install-confirm-title",
        )
        yield Static(
            body,
            classes="install-confirm-body",
            markup=True,
        )
        yield Static(
            "Enter to install, Esc to cancel",
            classes="install-confirm-help",
        )

    def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        super().on_mount()
        provider_name = PROVIDER_DISPLAY_NAMES.get(
            self._provider, self._provider.replace("_", " ").title()
        )
        self.set_modal_title(f"Install {provider_name} support?")
        self.set_modal_subtitle([
            ("enter", "install"),
            ("esc", "cancel"),
        ])

    def action_confirm(self) -> None:
        """Execute the confirm action."""
        self.dismiss(True)

    def action_cancel(self) -> None:
        """Execute the cancel action."""
        self.dismiss(False)


__all__ = ["InstallProviderConfirmScreen"]

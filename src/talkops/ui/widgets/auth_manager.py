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

"""Auth manager screen — multi-provider credential management.

Opened via /login or /auth. Lists providers with status, delegates to
AuthPromptScreen for individual key entry.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from textual.app import ComposeResult

from talkops.config.settings import get_glyphs
from talkops.model.config import get_credential_env_var, get_provider_auth_status
from talkops.ui.widgets.auth import PROVIDER_DISPLAY_NAMES, AuthPromptScreen, AuthResult
from talkops.ui.widgets.modal_base import OpsModalScreen, format_modal_option_row

logger = get_logger(__name__)

# Providers to list in the manager
_KNOWN_PROVIDERS: tuple[str, ...] = (
    "talkops", "google_genai", "openai", "openai_codex", "openrouter", "anthropic", "azure_openai",
    "bedrock", "google_vertexai", "google_anthropic_vertex", "ollama",
    "langsmith", "typesafe", "groq", "deepseek", "mistralai", "fireworks", "together",
    "xai", "cohere", "perplexity", "nvidia", "huggingface", "ibm", "baseten",
    "litellm", "meta", "cerebras", "sambanova",
)


class AuthManagerScreen(OpsModalScreen[None]):
    """Modal listing providers and their credential status.

    Reachable via /login, /auth, /connect. Dismisses with None.
    State changes persisted by AuthPromptScreen.
    """

    class CredentialSaved(Message):
        """Posted when a key is successfully persisted."""
        def __init__(self, provider: str) -> None:
            """Initialize CredentialSaved."""
            super().__init__()
            self.provider = provider

    class CredentialDeleted(Message):
        """Posted when a credential is removed."""
        def __init__(self, provider: str) -> None:
            """Initialize CredentialDeleted."""
            super().__init__()
            self.provider = provider

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close", show=False, priority=True),
        Binding("tab", "cursor_down", "Next", show=False, priority=True),
        Binding("shift+tab", "cursor_up", "Previous", show=False, priority=True),
        Binding("delete", "delete_selected", "Delete key", show=False, priority=True),
    ]

    DIALOG_CLASSES: ClassVar[str] = "-workspace"

    DEFAULT_CSS = """
    AuthManagerScreen .auth-mgr-title {
        display: none;
    }

    AuthManagerScreen .auth-mgr-desc {
        display: none;
    }

    AuthManagerScreen OptionList {
        height: 1fr;
        min-height: 3;
        background: transparent;
        border: solid $primary 20%;
    }

    AuthManagerScreen OptionList > .option-list--option-highlighted {
        background: $primary 18%;
        color: $text;
        text-style: bold;
    }

    AuthManagerScreen .auth-mgr-help {
        display: none;
    }
    """

    def __init__(self, *, initial_provider: str | None = None) -> None:
        """Initialize AuthManagerScreen."""
        super().__init__()
        self._initial_provider = initial_provider

    def compose_modal(self):
        """Execute compose modal on AuthManagerScreen."""
        options = self._build_options()
        yield Static("Manage API Keys", classes="auth-mgr-title")
        yield Static(
            "Select a provider to add, replace, or delete its API key. "
            "Keys are stored in ~/.talkops/.env.",
            classes="auth-mgr-desc",
        )
        yield OptionList(*options, id="auth-mgr-options")
        yield Static(
            "↑/↓ navigate • Enter add/replace • Delete remove • Esc close",
            classes="auth-mgr-help",
        )

    def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        super().on_mount()
        glyphs = get_glyphs()
        self.set_modal_title("API Key Manager", counter=f"{len(_KNOWN_PROVIDERS)} providers")
        self.set_modal_subtitle([
            (f"{glyphs.arrow_up}{glyphs.arrow_down}", "navigate"),
            ("enter", "set key"),
            ("del", "remove"),
            ("esc", "close"),
        ])
        self._highlight_initial_provider()
        try:
            self.query_one("#auth-mgr-options", OptionList).focus()
        except Exception:
            pass
        self._last_row_width: int | None = None
        self.call_after_refresh(self._reformat_options)

    def on_resize(self) -> None:
        """Handle terminal or container resize events."""
        width = self._option_content_width("#auth-mgr-options", fallback=70)
        if width != getattr(self, "_last_row_width", None):
            self._last_row_width = width
            self.call_after_refresh(self._reformat_options)

    def _reformat_options(self) -> None:
        try:
            ol = self.query_one("#auth-mgr-options", OptionList)
        except Exception:
            return
        highlighted = ol.highlighted if ol.highlighted is not None else 0
        w = self._option_content_width("#auth-mgr-options", fallback=70)
        ol.clear_options()
        for opt in self._build_options(width=w):
            ol.add_option(opt)
        if ol.option_count > 0 and 0 <= highlighted < ol.option_count:
            ol.highlighted = highlighted

    def _highlight_initial_provider(self) -> None:
        if self._initial_provider is None:
            return
        try:
            option_list = self.query_one("#auth-mgr-options", OptionList)
            index = option_list.get_option_index(self._initial_provider)
            option_list.highlighted = index
            option_list.scroll_to_highlight()
        except Exception:
            pass

    def _build_options(self, width: int | None = None) -> list[Option]:
        """Build OptionList entries with credential status badges."""
        options = []
        row_w = width if width is not None else self._option_content_width("#auth-mgr-options", fallback=70)
        for provider in _KNOWN_PROVIDERS:
            display_name = PROVIDER_DISPLAY_NAMES.get(provider, provider)
            if provider == "talkops":
                env_var = "TALKOPS_API_KEY"
                has_key = bool(
                    os.environ.get("TALKOPS_API_KEY")
                    or os.environ.get("TALKOPS_SERVER_API_KEY")
                )
                if not has_key:
                    try:
                        from talkops.auth.storage import get_credential_storage

                        sess = get_credential_storage().load_session()
                        has_key = bool(sess and (sess.api_key or sess.is_access_token_valid()))
                    except Exception:
                        pass
            elif provider == "langsmith":
                env_var = "LANGSMITH_API_KEY"
                has_key = bool(
                    os.environ.get("LANGSMITH_API_KEY")
                    or os.environ.get("LANGCHAIN_API_KEY")
                )
            else:
                env_var = (
                    get_credential_env_var(provider) or f"{provider.upper()}_API_KEY"
                )
                has_key = False
                try:
                    from talkops.auth.storage import get_credential_storage

                    has_key = bool(get_credential_storage().get_provider_key(provider))
                except Exception:
                    pass
                if not has_key and provider not in (
                    "bedrock",
                    "google_genai",
                    "google_vertexai",
                    "google_anthropic_vertex",
                ):
                    has_key = bool(os.environ.get(env_var))
                if not has_key and provider != "ollama":
                    try:
                        has_key = bool(get_provider_auth_status(provider).as_legacy_bool())
                    except Exception:
                        pass

            if has_key:
                badges: list[tuple[str, str]] = [("STORED", "bold $success")]
                status_text = "● Configured"
                status_style = "bold $success"
            else:
                badges = [("MISSING", "dim")]
                status_text = "○ Not set"
                status_style = "dim"

            content = format_modal_option_row(
                label=display_name,
                badges=badges,
                status=(status_text, status_style),
                width=row_w,
            )
            options.append(Option(content, id=provider))
        return options

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Handle option list option selected events."""
        provider = event.option.id
        if not provider:
            return
        env_var = (
            "TALKOPS_API_KEY"
            if provider == "talkops"
            else (get_credential_env_var(provider) or f"{provider.upper()}_API_KEY")
        )

        def _on_prompt_closed(result: AuthResult | None) -> None:
            if result == AuthResult.SAVED:
                self.post_message(self.CredentialSaved(provider))
                if hasattr(self.app, "maybe_start_deferred_server"):
                    import asyncio
                    import contextlib

                    with contextlib.suppress(Exception):
                        asyncio.create_task(self.app.maybe_start_deferred_server())
            self._refresh_options()

        self.app.push_screen(AuthPromptScreen(provider, env_var), _on_prompt_closed)

    def _refresh_options(self) -> None:
        """Rebuild option list after credential change."""
        try:
            option_list = self.query_one("#auth-mgr-options", OptionList)
            option_list.clear_options()
            for opt in self._build_options():
                option_list.add_option(opt)
        except Exception:
            pass

    def action_delete_selected(self) -> None:
        """Delete the credential for the highlighted provider."""
        try:
            option_list = self.query_one("#auth-mgr-options", OptionList)
            highlighted = option_list.highlighted
            if highlighted is None:
                return
            option = option_list.get_option_at_index(highlighted)
            provider = option.id
            if not provider:
                return

            from talkops.auth.storage import get_credential_storage
            from talkops.model.config import revoke_provider_credentials

            storage = get_credential_storage()
            if provider == "talkops":
                revoked = bool(storage.clear_session(clear_dotenv=True))
            else:
                settings = getattr(self.app, "_settings", None)
                revoked_list = revoke_provider_credentials(provider, settings=settings)
                revoked_store = storage.delete_provider_key(provider, sync_dotenv=False)
                revoked = bool(revoked_list or revoked_store)

            if revoked:
                self.app.notify(
                    f"Removed credentials for {PROVIDER_DISPLAY_NAMES.get(provider, provider)}.",
                    severity="warning",
                    markup=False,
                )
                self.post_message(self.CredentialDeleted(provider))
                self._refresh_options()
            else:
                self.app.notify(
                    f"No stored credentials for {PROVIDER_DISPLAY_NAMES.get(provider, provider)}.",
                    severity="information",
                    markup=False,
                )
        except Exception:
            pass

    def action_cancel(self) -> None:
        """Execute the cancel action."""
        self.dismiss(None)


__all__ = ["AuthManagerScreen", "_KNOWN_PROVIDERS"]

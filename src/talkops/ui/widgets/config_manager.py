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

"""Interactive configuration manager screen for /config.

Dynamically discovers all configurable options from the manifest and displays
them grouped by category tabs (`PillTabs`) with value source indicators. Every
setting is interactive: booleans toggle, choices cycle, strings/paths/lists open
an inline editor, credentials redirect to the auth manager, and theme/model/cloud
open their dedicated selector screens.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.content import Content
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from talkops.config.manifest import (
    ConfigOption,
    OptionKind,
    get_config_options,
    load_config_toml,
    resolve_scalar,
)
from talkops.config.settings import get_glyphs
from talkops.config.toml_config import delete_toml_field, save_toml_field
from talkops.ui.widgets.modal_base import (
    OpsModalScreen,
    PillTabs,
    format_modal_option_row,
)
from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from talkops.config.settings import Settings

logger = get_logger(__name__)

ConfigTabId = Literal["all", "cloud", "devops", "models", "security", "display"]

_CONFIG_TABS: tuple[tuple[ConfigTabId, str], ...] = (
    ("all", "All"),
    ("cloud", "Cloud & Infra"),
    ("devops", "DevOps & Tools"),
    ("models", "Models & Auth"),
    ("security", "Security & Shell"),
    ("display", "Display & Keys"),
)
_CONFIG_TAB_ORDER: tuple[ConfigTabId, ...] = (
    "all",
    "cloud",
    "devops",
    "models",
    "security",
    "display",
)
_CONFIG_TAB_GROUPS: dict[ConfigTabId, frozenset[str]] = {
    "all": frozenset(),
    "cloud": frozenset({"Cloud", "AWS", "Azure", "GCP", "Kubernetes"}),
    "devops": frozenset({"DevOps Tools", "Webhooks", "MCP", "Skills", "Plugins"}),
    "models": frozenset({"Models", "Credentials", "Memory"}),
    "security": frozenset({
        "Security",
        "Permissions",
        "Sandbox",
        "Shell",
        "Execution",
        "Runtime",
        "Updates",
    }),
    "display": frozenset({"Display", "Keybindings"}),
}


# ── Inline Edit Modal ─────────────────────────────────────


class _InlineEditScreen(OpsModalScreen[str | None]):
    """Small modal for editing a single string/path/list config value."""

    DIALOG_CLASSES: ClassVar[str] = "-compact"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    _InlineEditScreen .edit-title {
        display: none;
    }

    _InlineEditScreen Input {
        margin-bottom: 1;
    }

    _InlineEditScreen Horizontal {
        height: auto;
        align: right middle;
    }

    _InlineEditScreen Button {
        margin-left: 1;
    }
    """

    def __init__(self, label: str, current_value: str, placeholder: str = "") -> None:
        """Initialize _InlineEditScreen."""
        super().__init__()
        self._label = label
        self._current_value = current_value
        self._placeholder = placeholder

    def compose_modal(self):
        """Execute compose modal on _InlineEditScreen."""
        yield Static(f"Edit: {self._label}", classes="edit-title")
        yield Input(
            value=self._current_value,
            placeholder=self._placeholder or "Enter value...",
            id="edit-input",
        )
        with Horizontal():
            yield Button("Save", variant="primary", id="save-btn")
            yield Button("Cancel", variant="default", id="cancel-btn")

    def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        super().on_mount()
        self.set_modal_title(f"Edit: {self._label}")
        self.set_modal_subtitle([
            ("enter", "save"),
            ("esc", "cancel"),
        ])
        try:
            inp = self.query_one("#edit-input", Input)
            inp.focus()
            inp.cursor_position = len(inp.value)
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button pressed events."""
        if event.button.id == "save-btn":
            val = self.query_one("#edit-input", Input).value
            self.dismiss(val)
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle input submitted events."""
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        """Execute the cancel action."""
        self.dismiss(None)


# ── Config Manager Screen ─────────────────────────────────


class ConfigManagerScreen(OpsModalScreen[bool]):
    """Modal screen for searching, inspecting, and modifying TalkOps configuration settings.

    Features top category `PillTabs` (All, Cloud & Infra, DevOps & Tools, Models & Auth,
    Security & Shell, Display & Keys), a live search bar, and interactive editing for
    all cloud, DevOps, and runtime settings.
    """

    DIALOG_CLASSES: ClassVar[str] = "-workspace"

    class SettingChanged(Message):
        """Posted when a setting field is updated."""

        def __init__(self, key: str, value: Any) -> None:
            """Initialize SettingChanged."""
            super().__init__()
            self.key = key
            self.value = value

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close", show=False, priority=True),
        Binding("tab", "focus_next_widget", "Next", show=False, priority=True),
        Binding("shift+tab", "focus_prev_widget", "Previous", show=False, priority=True),
        Binding("ctrl+p", "cycle_tab", "Next Tab", show=False, priority=True),
        Binding("delete", "reset_selected", "Reset setting", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    ConfigManagerScreen .config-title {
        display: none;
    }

    ConfigManagerScreen PillTabs#config-tabs {
        margin-bottom: 1;
    }

    ConfigManagerScreen Input#config-search {
        margin-bottom: 1;
        border: solid $panel;
    }

    ConfigManagerScreen Input#config-search:focus {
        border: solid $primary;
    }

    ConfigManagerScreen OptionList#config-options {
        height: 1fr;
        min-height: 5;
        background: transparent;
        border: solid $primary 20%;
    }

    ConfigManagerScreen OptionList#config-options > .option-list--option-highlighted {
        background: $primary 18%;
        color: $text;
        text-style: bold;
    }

    ConfigManagerScreen .config-help {
        display: none;
    }
    """

    # Settings that should open dedicated selector screens rather than inline edit
    _SCREEN_DELEGATES: dict[str, str] = {
        "display.theme": "theme",
        "models.name": "model",
        "models.reasoning_effort": "effort",
        "models.context_limit": "model",
        "aws.profile": "cloud_profile",
    }

    # Settings that are read-only runtime state (informational, not user-editable)
    _READ_ONLY_KEYS: frozenset[str] = frozenset({
        "models.provider",
    })

    def __init__(
        self,
        settings: Settings | None = None,
        initial_tab: ConfigTabId = "all",
    ) -> None:
        """Initialize ConfigManagerScreen."""
        super().__init__()
        if settings is None:
            from talkops.config.settings import settings as global_settings

            settings = global_settings
        self._settings: Settings = settings
        self._active_tab: ConfigTabId = (
            initial_tab if initial_tab in _CONFIG_TAB_ORDER else "all"
        )
        self._filter_query: str = ""
        self._changes_made: bool = False
        self._toml_data: dict[str, Any] | None = None

    def compose_modal(self):
        """Execute compose modal on ConfigManagerScreen."""
        yield Static("TalkOps Configuration", classes="config-title")
        yield PillTabs(
            _CONFIG_TABS,
            id="config-tabs",
            initial_tab=self._active_tab,
        )
        yield Input(
            placeholder="Type to search cloud, DevOps, model, or runtime settings...",
            id="config-search",
        )
        yield OptionList(*self._build_options(), id="config-options")
        yield Static(
            "←/→ or Ctrl+P tab • ↑/↓ navigate • Enter edit/toggle • / search • Delete reset • Esc close",
            classes="config-help",
        )

    def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        super().on_mount()
        glyphs = get_glyphs()
        self._toml_data = load_config_toml()
        self._update_tab_counts()
        total_settings = len(get_config_options())
        self.set_modal_title("Configuration & DevOps Settings", counter=f"{total_settings} settings")
        self.set_modal_subtitle([
            (f"{glyphs.arrow_up}{glyphs.arrow_down}", "navigate"),
            ("←/→", "tab"),
            ("enter", "edit/toggle"),
            ("/", "search"),
            ("del", "reset"),
            ("esc", "close"),
        ])
        # Focus the OptionList so arrow keys navigate immediately
        try:
            self.query_one("#config-options", OptionList).focus()
        except Exception:
            pass
        self._last_row_width: int | None = None
        self.call_after_refresh(self._reformat_options)

    def _update_tab_counts(self) -> None:
        try:
            tabs = self.query_one("#config-tabs", PillTabs)
        except NoMatches:
            return
        all_opts = get_config_options()
        tabs.set_tab_count("all", len(all_opts))
        for tab_id, groups in _CONFIG_TAB_GROUPS.items():
            if tab_id == "all":
                continue
            cnt = sum(1 for o in all_opts if o.group in groups)
            tabs.set_tab_count(tab_id, cnt)

    def on_pill_tabs_tab_changed(self, event: PillTabs.TabChanged) -> None:
        """Handle pill tabs tab changed events."""
        if event.tab_id in _CONFIG_TAB_ORDER and event.tab_id != self._active_tab:
            self._active_tab = event.tab_id  # type: ignore[assignment]
            self._refresh_options()

    def action_cycle_tab(self) -> None:
        """Execute the cycle tab action."""
        try:
            idx = _CONFIG_TAB_ORDER.index(self._active_tab)
        except ValueError:
            idx = 0
        next_tab = _CONFIG_TAB_ORDER[(idx + 1) % len(_CONFIG_TAB_ORDER)]
        self._active_tab = next_tab
        try:
            self.query_one("#config-tabs", PillTabs).activate_tab(next_tab)
        except NoMatches:
            pass
        self._refresh_options()

    def on_resize(self) -> None:
        """Handle terminal or container resize events."""
        width = self._option_content_width("#config-options", fallback=84)
        if width != getattr(self, "_last_row_width", None):
            self._last_row_width = width
            self.call_after_refresh(self._reformat_options)

    def _reformat_options(self) -> None:
        try:
            ol = self.query_one("#config-options", OptionList)
        except Exception:
            return
        highlighted = ol.highlighted if ol.highlighted is not None else 0
        w = self._option_content_width("#config-options", fallback=84)
        ol.clear_options()
        for opt in self._build_options(width=w):
            ol.add_option(opt)
        if ol.option_count > 0 and 0 <= highlighted < ol.option_count:
            ol.highlighted = highlighted

    def on_key(self, event: Any) -> None:
        """Route keyboard between search Input, PillTabs, and OptionList."""
        focused = self.app.focused
        search_input = self.query_one("#config-search", Input)
        option_list = self.query_one("#config-options", OptionList)

        if focused is search_input and event.key in ("down", "enter"):
            event.prevent_default()
            option_list.focus()
        elif focused is option_list and event.key == "slash":
            event.prevent_default()
            search_input.focus()
        elif focused is option_list and event.key in ("left", "right"):
            event.prevent_default()
            try:
                idx = _CONFIG_TAB_ORDER.index(self._active_tab)
            except ValueError:
                idx = 0
            delta = 1 if event.key == "right" else -1
            next_tab = _CONFIG_TAB_ORDER[(idx + delta) % len(_CONFIG_TAB_ORDER)]
            self._active_tab = next_tab
            try:
                self.query_one("#config-tabs", PillTabs).activate_tab(next_tab)
            except NoMatches:
                pass
            self._refresh_options()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle input changed events."""
        if event.input.id == "config-search":
            self._filter_query = event.value.strip().lower()
            self._refresh_options()

    def _build_options(self, width: int | None = None) -> list[Option]:
        """Build filtered OptionList items dynamically from the config manifest."""
        options: list[Option] = []
        current_group: str | None = None
        row_w = width if width is not None else self._option_content_width("#config-options", fallback=84)
        allowed_groups = _CONFIG_TAB_GROUPS.get(self._active_tab, frozenset())

        for opt in get_config_options():
            if allowed_groups and opt.group not in allowed_groups:
                continue

            # Apply search filter across key, summary, and group
            if self._filter_query and not (
                self._filter_query in opt.key.lower()
                or self._filter_query in opt.summary.lower()
                or self._filter_query in opt.group.lower()
            ):
                continue

            # Insert group header separator
            if opt.group != current_group:
                current_group = opt.group
                options.append(
                    Option(
                        Content.from_markup(f"[bold $primary]── {current_group} ──[/]"),
                        id=f"_group_{current_group}",
                        disabled=True,
                    )
                )

            # Resolve effective value and its source
            value, source = resolve_scalar(
                opt, settings=self._settings, toml_data=self._toml_data,
            )
            val_str = self._format_value(opt, value)
            badges: list[tuple[str, str]] = []
            if source != "default":
                b_style = (
                    "bold $primary"
                    if source == "env"
                    else ("bold $warning" if source == "cli" else "dim")
                )
                badges.append((source.upper(), b_style))

            row_content = format_modal_option_row(
                label=opt.summary,
                badges=badges if badges else None,
                status=(val_str, "bold $secondary"),
                width=row_w,
            )
            options.append(Option(row_content, id=opt.key))

        if not options:
            options.append(Option("No matching settings found.", id="none", disabled=True))

        return options

    @staticmethod
    def _format_value(opt: ConfigOption, val: Any) -> str:
        """Format a value for display based on option kind and redaction."""
        if val is None:
            return "false" if opt.kind == OptionKind.BOOL else "None"
        if opt.redacted:
            s_val = str(val)
            return f"`{s_val[:4]}...{s_val[-4:]}`" if len(s_val) > 8 else "set ✓"
        if opt.kind == OptionKind.BOOL:
            return "true" if bool(val) else "false"
        if opt.kind == OptionKind.CHOICE:
            return str(val)
        if opt.kind == OptionKind.PATH:
            return str(val)
        if opt.kind == OptionKind.SHELL_LIST:
            if isinstance(val, list):
                return ", ".join(val) if val else "None"
            return str(val) if val else "None"
        return str(val)

    # ── Selection Handler ─────────────────────────────────

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Handle option list option selected events."""
        key = event.option.id
        if not key or key.startswith("_group_") or key == "none":
            return

        opt = self._find_option(key)
        if not opt:
            return

        # Read-only runtime state → informational message
        if key in self._READ_ONLY_KEYS:
            value, _ = resolve_scalar(opt, settings=self._settings, toml_data=self._toml_data)
            self.app.notify(
                f"{opt.summary}: `{value}` (read-only, set by model selection)",
                severity="information",
            )
            return

        # Dedicated screen delegates (theme, model, cloud_profile)
        if key in self._SCREEN_DELEGATES:
            self._open_delegate_screen(key)
            return

        # Toggle booleans
        if opt.kind == OptionKind.BOOL:
            self._toggle_bool(opt)
        # Cycle choice values
        elif opt.kind == OptionKind.CHOICE and opt.choices:
            self._cycle_choice(opt)
        # Secret fields: model credentials go to AuthManager, DevOps/Webhook tokens use inline editor
        elif opt.kind == OptionKind.SECRET:
            if opt.key.startswith("credentials."):
                self._open_secret_editor(opt)
            else:
                self._open_inline_editor(opt)
        # String, Path, Int, Float, ShellList → inline editor
        elif opt.kind in (
            OptionKind.STR,
            OptionKind.PATH,
            OptionKind.INT,
            OptionKind.FLOAT,
            OptionKind.SHELL_LIST,
        ):
            self._open_inline_editor(opt)
        else:
            self.app.notify(
                f"Edit `{opt.key}` in config.toml or set env var `{opt.env_var or opt.key}`",
                severity="information",
            )

    # ── Edit Actions ──────────────────────────────────────

    def _persist_toml_and_env(self, opt: ConfigOption, raw_value: str) -> None:
        """Persist a setting to config.toml and os.environ when updated."""
        cleaned = raw_value.strip()
        if opt.env_var:
            if cleaned:
                os.environ[opt.env_var] = cleaned
            else:
                os.environ.pop(opt.env_var, None)

        if opt.toml_keys and len(opt.toml_keys) >= 2:
            try:
                sec, fld = opt.toml_keys[0], opt.toml_keys[-1]
                if cleaned:
                    save_toml_field(sec, fld, cleaned)
                else:
                    delete_toml_field(sec, fld)
            except Exception:
                logger.debug("Failed to write %s to config.toml", opt.key, exc_info=True)

        self._toml_data = load_config_toml()

    def _toggle_bool(self, opt: ConfigOption) -> None:
        """Toggle a boolean setting."""
        if opt.settings_field and self._settings:
            cur_val = bool(getattr(self._settings, opt.settings_field, False))
            new_val = not cur_val
            ok, _ = self._settings.set_field(opt.settings_field, str(new_val))
            if ok:
                self._changes_made = True
                self.post_message(self.SettingChanged(opt.key, new_val))
                self.app.notify(f"Set {opt.summary} = {new_val}", severity="information")
                self._refresh_options()
        elif opt.key.startswith("permissions."):
            self._toggle_permission(opt)

    def _toggle_permission(self, opt: ConfigOption) -> None:
        """Toggle a permission scope on app._permission_store."""
        scope_name = opt.key.replace("permissions.", "").replace("_", ":")
        store = getattr(self.app, "_permission_store", None)
        if store is not None:
            current = store.evaluate(scope_name)
            cycle = {"allow": "ask", "ask": "deny", "deny": "allow"}
            new_status = cycle[current]
            store.add_rule(new_status, scope_name, source="session")
            self._changes_made = True
            self.app.notify(f"Permission `{scope_name}` → {new_status}", severity="information")
            self._refresh_options()

    def _cycle_choice(self, opt: ConfigOption) -> None:
        """Cycle through choice values (Settings-backed or TOML/env-backed)."""
        if not opt.choices:
            return
        cur_val_raw, _ = resolve_scalar(opt, settings=self._settings, toml_data=self._toml_data)
        cur_val = str(cur_val_raw or opt.choices[0])
        try:
            idx = list(opt.choices).index(cur_val)
            new_val = opt.choices[(idx + 1) % len(opt.choices)]
        except ValueError:
            new_val = opt.choices[0]

        if opt.settings_field and self._settings:
            ok, _ = self._settings.set_field(opt.settings_field, new_val)
            if ok:
                self._changes_made = True
                self.post_message(self.SettingChanged(opt.key, new_val))
                self.app.notify(f"Set {opt.summary} = {new_val}", severity="information")
                self._refresh_options()
        else:
            self._persist_toml_and_env(opt, new_val)
            self._changes_made = True
            self.post_message(self.SettingChanged(opt.key, new_val))
            self.app.notify(f"Set {opt.summary} = {new_val}", severity="information")
            self._refresh_options()

    def _open_secret_editor(self, opt: ConfigOption) -> None:
        """Redirect to auth manager for model provider secret values."""
        try:
            from talkops.ui.widgets.auth_manager import AuthManagerScreen

            provider = opt.key.replace("credentials.", "")
            self.app.push_screen(AuthManagerScreen(initial_provider=provider))
        except Exception:
            self.app.notify(
                f"Set `{opt.env_var or opt.key}` via environment or `/login`",
                severity="information",
            )

    def _open_delegate_screen(self, key: str) -> None:
        """Open a dedicated selector screen for theme, model, effort, or cloud profile."""
        delegate = self._SCREEN_DELEGATES[key]
        if delegate == "theme":
            try:
                from talkops.ui.widgets.theme_selector import ThemeSelectorScreen

                screen = ThemeSelectorScreen(current_theme=self.app.theme)

                def _on_theme_result(result: str | None) -> None:
                    if result:
                        self._changes_made = True
                        self.app.theme = result
                        from talkops.ui.theme import save_theme_preference

                        save_theme_preference(result)
                        if self._settings:
                            self._settings.theme = result
                        self.post_message(self.SettingChanged("display.theme", result))
                        self._refresh_options()

                self.app.push_screen(screen, callback=_on_theme_result)
            except Exception as exc:
                logger.debug("Failed opening ThemeSelectorScreen: %s", exc)
                self.app.notify("Theme selector not available", severity="warning")

        elif delegate == "model":
            try:
                from talkops.ui.widgets.model_selector import ModelSelectorScreen

                screen = ModelSelectorScreen(
                    current_model=self._settings.model_name,
                    current_provider=self._settings.model_provider,
                    current_effort=self._settings.reasoning_effort,
                )

                def _on_model_result(result: Any) -> None:
                    if result:
                        self._changes_made = True
                        self.post_message(self.SettingChanged("models.name", result))
                        self._refresh_options()

                self.app.push_screen(screen, callback=_on_model_result)
            except Exception as exc:
                logger.debug("Failed opening ModelSelectorScreen: %s", exc)
                self.app.notify("Use `/model` to switch models", severity="information")

        elif delegate == "effort":
            try:
                from talkops.model.reasoning import (
                    default_effort_for_model,
                    supported_efforts_for_model,
                )
                from talkops.ui.widgets.effort_selector import EffortSelectorScreen

                model_spec: str = getattr(self.app, "_model", None) or "default"
                efforts = supported_efforts_for_model(model_spec) or ("low", "medium", "high")
                current_eff = self._settings.reasoning_effort
                default_eff = default_effort_for_model(model_spec) or "high"

                screen = EffortSelectorScreen(
                    model_spec=model_spec,
                    efforts=efforts,
                    current_effort=current_eff,
                    default_effort=default_eff,
                )

                def _on_effort_result(result: str | None) -> None:
                    if result is not None:
                        self._changes_made = True
                        self._settings.reasoning_effort = result
                        if hasattr(self.app, "_reasoning_effort"):
                            self.app._reasoning_effort = result  # type: ignore[attr-defined]
                        try:
                            from talkops.ui.widgets.status import StatusBar

                            sb = self.app.query_one("#status-bar", StatusBar)
                            _spec = getattr(self.app, "_model", "") or ""
                            _prov, _mod = (_spec.split(":", 1) if ":" in _spec else ("", _spec))
                            sb.set_model(provider=_prov, model=_mod, effort=result)
                        except Exception:
                            pass
                        self.post_message(self.SettingChanged("models.reasoning_effort", result))
                        self._refresh_options()

                self.app.push_screen(screen, callback=_on_effort_result)
            except Exception as exc:
                logger.debug("Failed opening EffortSelectorScreen: %s", exc)
                self.app.notify("Use `/effort` to change reasoning effort", severity="information")

        elif delegate == "cloud_profile":
            try:
                from talkops.ui.widgets.cloud_selector import CloudProfileSelectorScreen

                screen = CloudProfileSelectorScreen(
                    current_profile=self._settings.aws_profile,
                )

                def _on_cloud_result(result: tuple[str, str | None] | None) -> None:
                    if result:
                        self._changes_made = True
                        from talkops.config.cloud_profiles import set_active_aws_profile

                        profile_name = result[0] if isinstance(result, tuple) else str(result)
                        set_active_aws_profile(profile_name, persist=True)
                        self._settings.aws_profile = profile_name
                        self._toml_data = load_config_toml()
                        self.post_message(self.SettingChanged("aws.profile", profile_name))
                        self._refresh_options()

                self.app.push_screen(screen, callback=_on_cloud_result)
            except Exception as exc:
                logger.debug("Failed opening CloudProfileSelectorScreen: %s", exc)
                self.app.notify("Use `/cloud` to switch cloud contexts", severity="information")

    def _open_inline_editor(self, opt: ConfigOption) -> None:
        """Open a small inline editor for string/path/int/float/list/secret values."""
        value, _ = resolve_scalar(opt, settings=self._settings, toml_data=self._toml_data)
        current_str = "" if (value is None or opt.redacted) else str(value)

        if opt.kind == OptionKind.SHELL_LIST:
            placeholder = "Comma-separated commands (e.g. git status, ls, cat)"
        elif opt.kind == OptionKind.PATH:
            placeholder = "Filesystem path (e.g. /home/user/project)"
        elif opt.kind == OptionKind.INT:
            placeholder = "Integer value"
        elif opt.kind == OptionKind.FLOAT:
            placeholder = "Numeric value"
        else:
            placeholder = f"Value for {opt.summary}"

        screen = _InlineEditScreen(
            label=opt.summary,
            current_value=current_str,
            placeholder=placeholder,
        )

        def _on_edit_result(result: str | None) -> None:
            if result is None:
                return
            self._apply_edit(opt, result)

        self.app.push_screen(screen, callback=_on_edit_result)

    def _apply_edit(self, opt: ConfigOption, raw_value: str) -> None:
        """Apply an edited value to settings, config.toml, and environment."""
        if not opt.settings_field or not self._settings:
            self._persist_toml_and_env(opt, raw_value)
            self._changes_made = True
            self.post_message(self.SettingChanged(opt.key, raw_value))
            self.app.notify(
                f"Set {opt.summary} = {raw_value or '(cleared)'}",
                severity="information",
            )
            self._refresh_options()
            return

        ok, msg = self._settings.set_field(opt.settings_field, raw_value)
        if ok:
            self._toml_data = load_config_toml()
            self._changes_made = True
            self.post_message(self.SettingChanged(opt.key, raw_value))
            self.app.notify(f"Set {opt.summary} = {raw_value}", severity="information")
            self._refresh_options()
        else:
            self.app.notify(msg, severity="error")

    # ── Reset / Navigation ────────────────────────────────

    def action_reset_selected(self) -> None:
        """Reset highlighted setting to its default value."""
        try:
            option_list = self.query_one("#config-options", OptionList)
            highlighted = option_list.highlighted
            if highlighted is None:
                return
            option = option_list.get_option_at_index(highlighted)
            key = option.id
            if not key or key.startswith("_group_") or key == "none":
                return
            opt = self._find_option(key)
            if not opt:
                return

            # Permission scopes
            if key.startswith("permissions."):
                scope_name = key.replace("permissions.", "").replace("_", ":")
                store = getattr(self.app, "_permission_store", None)
                if store is not None:
                    from talkops.ui.permission_store import (
                        _DEFAULT_ALLOW,
                        _DEFAULT_ASK,
                        _DEFAULT_DENY,
                    )

                    if scope_name in _DEFAULT_ALLOW:
                        store.add_rule("allow", scope_name, source="default")
                    elif scope_name in _DEFAULT_ASK:
                        store.add_rule("ask", scope_name, source="default")
                    elif scope_name in _DEFAULT_DENY:
                        store.add_rule("deny", scope_name, source="default")
                    self._changes_made = True
                    self.app.notify(f"Reset `{scope_name}` to default", severity="warning")
                    self._refresh_options()
                return

            # Settings-backed fields
            if opt.settings_field:
                ok, msg = self._settings.reset_field(opt.settings_field)
                if ok:
                    self._toml_data = load_config_toml()
                    self._changes_made = True
                    self.app.notify(msg, severity="warning")
                    self.post_message(self.SettingChanged(opt.key, None))
                    self._refresh_options()
            else:
                self._persist_toml_and_env(opt, "")
                self._changes_made = True
                self.app.notify(f"Reset `{opt.summary}` to default", severity="warning")
                self.post_message(self.SettingChanged(opt.key, None))
                self._refresh_options()
        except Exception:
            pass

    def action_focus_next_widget(self) -> None:
        """Execute the focus next widget action."""
        self.screen.focus_next()

    def action_focus_prev_widget(self) -> None:
        """Execute the focus prev widget action."""
        self.screen.focus_previous()

    def _refresh_options(self) -> None:
        try:
            option_list = self.query_one("#config-options", OptionList)
            option_list.clear_options()
            for opt in self._build_options():
                option_list.add_option(opt)
        except Exception:
            pass

    def action_cancel(self) -> None:
        """Execute the cancel action."""
        self.dismiss(self._changes_made)

    @staticmethod
    def _find_option(key: str) -> ConfigOption | None:
        """Look up a ConfigOption by key from the manifest."""
        from talkops.config.manifest import get_option

        return get_option(key)


__all__ = ["ConfigManagerScreen"]

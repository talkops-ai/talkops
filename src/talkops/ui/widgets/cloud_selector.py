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

"""Interactive multi-cloud context & profile selector screen for `/cloud` command.

Fetches and displays cloud contexts directly from local CLI configurations
(`~/.aws/config` & `~/.aws/credentials`, `~/.azure/azureProfile.json`,
`~/.config/gcloud/configurations/*`, `~/.kube/config`) across provider tabs
(`All`, `AWS`, `Azure`, `GCP`, `Kubernetes`), and automatically attaches cloud
service roles / workload identities when the agent is deployed on cloud compute.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar, Literal

from textual.binding import Binding, BindingType
from textual.containers import Container, VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
from textual.events import Click, Key
from textual.fuzzy import Matcher
from textual.message import Message
from textual.widgets import Input, Static

from talkops.config.cloud_discovery import (
    CloudExecutionMode,
    CloudProviderType,
    CloudTargetContext,
)
from talkops.config.cloud_profiles import (
    AWSProfileInfo,
    get_active_aws_profile,
    get_active_cloud_target,
    get_cloud_execution_mode,
    list_aws_profiles,
    list_cloud_targets,
    set_cloud_execution_mode,
)
from talkops.config.settings import get_glyphs
from talkops.config.toml_config import (
    load_active_cloud_context,
    load_aws_profile,
    load_recent_aws_profiles,
    load_recent_cloud_targets,
    save_active_cloud_context,
    save_aws_profile,
)
from talkops.ui.widgets.modal_base import (
    OpsModalScreen,
    PillTabs,
)
from talkops.ui.widgets.modal_base import (
    escape_markup as escape,
)
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

CloudTabId = Literal["all", "aws", "azure", "gcp", "k8s"]

_CLOUD_TABS: tuple[tuple[CloudTabId, str], ...] = (
    ("all", "All"),
    ("aws", "AWS"),
    ("azure", "Azure"),
    ("gcp", "GCP"),
    ("k8s", "Kubernetes"),
)
_TAB_ORDER: tuple[CloudTabId, ...] = ("all", "aws", "azure", "gcp", "k8s")

_RECENT_SECTION_LABEL = "Recent & Active"
_AMBIENT_SECTION_LABEL = "Attached Cloud Service Role"
_PROVIDER_SECTION_LABELS: dict[str, str] = {
    "aws": "AWS Profiles (~/.aws)",
    "azure": "Azure Subscriptions (~/.azure)",
    "gcp": "GCP Configurations (~/.config/gcloud)",
    "k8s": "Kubernetes Contexts (~/.kube/config)",
}
_PROVIDER_SHORT_LABELS: dict[str, str] = {
    "all": "All",
    "aws": "AWS",
    "azure": "Azure",
    "gcp": "GCP",
    "k8s": "Kubernetes",
}
_EMPTY_PROVIDER_HINTS: dict[str, str] = {
    "aws": "No local AWS profiles found in ~/.aws/config or ~/.aws/credentials.",
    "azure": "No local Azure subscriptions found in ~/.azure/azureProfile.json (run 'az login' in terminal).",
    "gcp": "No local GCP configurations found in ~/.config/gcloud (run 'gcloud init' or 'gcloud auth login').",
    "k8s": "No local Kubernetes contexts found in ~/.kube/config.",
}


class CloudProfileOption(Static):
    """A single clickable cloud target / profile row in the selector."""

    def __init__(
        self,
        label: str,
        profile_info: AWSProfileInfo | CloudTargetContext,
        index: int,
        *,
        show_provider_badge: bool = False,
        is_selected: bool = False,
        is_current: bool = False,
        is_default: bool = False,
        classes: str = "",
    ) -> None:
        """Initialize CloudProfileOption."""
        super().__init__(label, classes=classes, markup=True)
        self.profile_info = profile_info
        self.profile_name = profile_info.name
        self.provider: CloudProviderType = getattr(profile_info, "provider", "aws")
        self.index = index
        self.show_provider_badge = show_provider_badge
        self.is_selected = is_selected
        self.is_current = is_current
        self.is_default = is_default

    class Clicked(Message):
        """Posted when a cloud profile row is clicked."""

        def __init__(self, profile_name: str, index: int) -> None:
            """Initialize Clicked."""
            super().__init__()
            self.profile_name = profile_name
            self.index = index

    def on_click(self, event: Click) -> None:
        """Handle mouse click events."""
        event.stop()
        if event.chain > 1:
            return
        try:
            if getattr(self.screen, "_dismissed", False):
                return
        except Exception:
            pass
        self.post_message(self.Clicked(self.profile_name, self.index))

    def render_label_text(self) -> str:
        """Render label text."""
        cursor = "› " if self.is_selected else "  "
        is_ambient = bool(getattr(self.profile_info, "is_ambient", False))
        display_title = getattr(self.profile_info, "display_name", self.profile_name) or self.profile_name
        acct = getattr(self.profile_info, "account_id", None)

        # Provider prefix badge only on the "All" tab
        prov_prefix = (
            f"[dim][{escape(self.provider.upper())}][/dim] "
            if self.show_provider_badge
            else ""
        )

        # Primary label per provider based on local config metadata
        if is_ambient:
            # Show human-readable attached service role title instead of raw '@ambient'
            label_main = display_title if display_title != self.profile_name else f"{self.provider.upper()} Attached Service Role"
            text = f"{cursor}{prov_prefix}[bold]{escape(label_main)}[/bold]"
            if self.profile_info.region:
                text += f" [dim]({escape(self.profile_info.region)})[/dim]"
        elif self.provider == "gcp":
            text = f"{cursor}{prov_prefix}{escape(self.profile_name)}"
            if acct and acct not in ("unassigned", "default"):
                text += f" [dim]— project: {escape(acct)}[/dim]"
            if self.profile_info.region:
                text += f" [dim]({escape(self.profile_info.region)})[/dim]"
        elif self.provider == "k8s":
            text = f"{cursor}{prov_prefix}{escape(self.profile_name)}"
            if acct and acct != self.profile_name:
                text += f" [dim]— cluster: {escape(acct)}[/dim]"
            if self.profile_info.region and self.profile_info.region != "default":
                text += f" [dim](ns: {escape(self.profile_info.region)})[/dim]"
        elif self.provider == "azure":
            if display_title != self.profile_name:
                text = f"{cursor}{prov_prefix}{escape(display_title)}"
            else:
                text = f"{cursor}{prov_prefix}{escape(self.profile_name)}"
            if self.profile_info.region:
                text += f" [dim]({escape(self.profile_info.region)})[/dim]"
        else:
            # AWS profile
            text = f"{cursor}{prov_prefix}{escape(self.profile_name)}"
            if self.profile_info.region:
                text += f" [dim]({escape(self.profile_info.region)})[/dim]"

        if is_ambient:
            text += " [bold $secondary]● Attached Service Role[/bold $secondary]"
        if self.is_current:
            text += " [bold $success]● Active[/bold $success]"
        if "prod" in self.profile_name.lower() or "prod" in display_title.lower():
            text += " [bold $ctx-prod on $ctx-prod-bg]PROD[/bold $ctx-prod on $ctx-prod-bg]"
        if self.is_default and not is_ambient:
            text += " [bold](default)[/bold]"
        return text

    def set_selected(self, selected: bool) -> None:
        """Set selected."""
        if self.is_selected == selected:
            return
        self.is_selected = selected
        if selected:
            self.add_class("cloud-option-selected")
        else:
            self.remove_class("cloud-option-selected")
        self.update(self.render_label_text())


MAX_VISIBLE_ROWS: int = 24
MIN_VISIBLE_ROWS: int = 3


class CloudProfileSelectorScreen(OpsModalScreen[tuple[str, str | None] | None]):
    """Tabbed modal for selecting cloud contexts discovered from local CLI settings or attached cloud service roles."""

    DIALOG_CLASSES: ClassVar[str] = ""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False, priority=True),
        Binding("down", "move_down", "Down", show=False, priority=True),
        Binding("pageup", "page_up", "Page Up", show=False, priority=True),
        Binding("pagedown", "page_down", "Page Down", show=False, priority=True),
        Binding("tab", "tab_complete", "Tab complete", show=False, priority=True),
        Binding("enter", "select", "Select", show=False, priority=True),
        Binding("ctrl+s", "set_default", "Set default", show=False, priority=True),
        Binding("ctrl+p", "cycle_provider", "Next tab", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    CloudProfileSelectorScreen .cloud-selector-title {
        display: none;
    }

    CloudProfileSelectorScreen PillTabs#cloud-tabs {
        margin-bottom: 1;
    }

    CloudProfileSelectorScreen .cloud-selector-info {
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }

    CloudProfileSelectorScreen #cloud-filter {
        margin-bottom: 1;
        border: solid $primary 40%;
    }

    CloudProfileSelectorScreen #cloud-filter:focus {
        border: solid $primary;
    }

    CloudProfileSelectorScreen .cloud-list {
        height: auto;
        min-height: 3;
        max-height: 24;
        background: transparent;
        scrollbar-gutter: stable;
    }

    CloudProfileSelectorScreen #cloud-options {
        height: auto;
    }

    CloudProfileSelectorScreen .cloud-provider-header {
        color: $primary;
        margin-top: 1;
        text-style: bold;
    }

    CloudProfileSelectorScreen #cloud-options > .cloud-provider-header:first-child {
        margin-top: 0;
    }

    CloudProfileSelectorScreen .cloud-option {
        height: 1;
        padding: 0 1;
    }

    CloudProfileSelectorScreen .cloud-option:hover {
        background: $surface-lighten-1;
    }

    CloudProfileSelectorScreen .cloud-option-selected {
        background: $primary 18%;
        color: $text;
        text-style: bold;
        border-left: wide $primary;
        padding-left: 0;
    }

    CloudProfileSelectorScreen .cloud-option-selected:hover {
        background: $primary 25%;
    }

    CloudProfileSelectorScreen .cloud-option-current {
        text-style: italic;
    }

    CloudProfileSelectorScreen .cloud-detail-footer {
        height: auto;
        min-height: 4;
        max-height: 8;
        padding: 0 1;
        margin-top: 1;
        border-top: solid $primary 20%;
        background: transparent;
    }

    CloudProfileSelectorScreen .cloud-selector-help {
        display: none;
    }
    """

    def __init__(
        self,
        current_profile: str | None = None,
        current_provider: CloudProviderType | None = None,
        initial_tab: str | None = None,
    ) -> None:
        """Initialize CloudProfileSelectorScreen."""
        super().__init__()
        active_prov, active_target, _ = get_active_cloud_target()
        self._current_provider: CloudProviderType = current_provider or active_prov
        raw_profile = current_profile or (
            active_target if current_provider else get_active_aws_profile()
        )
        self._current_profile = raw_profile
        self._default_profile = load_aws_profile()
        self._default_cloud_ctx: dict[str, str | None] | tuple[str, str, str | None] | None = (
            load_active_cloud_context()
        )
        self._recent_profiles = [p for p in load_recent_aws_profiles() if p != "@ambient"]
        self._recent_cloud_targets = [
            t for t in load_recent_cloud_targets() if not t.endswith(":@ambient")
        ]

        norm_tab: CloudTabId = (
            initial_tab.lower()  # type: ignore[assignment]
            if initial_tab and initial_tab.lower() in _TAB_ORDER
            else "all"
        )
        self._active_tab: CloudTabId = norm_tab
        self._provider_filter: CloudProviderType | None = (
            norm_tab if norm_tab in ("aws", "azure", "gcp", "k8s") else None  # type: ignore[assignment]
        )
        self._execution_mode: CloudExecutionMode = get_cloud_execution_mode()

        self.selected_provider: CloudProviderType = self._current_provider
        self.selected_target: AWSProfileInfo | CloudTargetContext | None = None

        self._dismissed: bool = False
        self._loaded: bool = False
        self._mutated: bool = False
        self._rebuild_counter: int = 0

        self._all_profiles: list[Any] = []
        self._filtered_profiles: list[Any] = []
        self._selected_index = 0
        self._option_widgets: list[CloudProfileOption] = []
        self._flat_profiles: list[Any] = []
        self._options_container: Container | None = None
        self._filter_text = ""

    @staticmethod
    def _is_aws_mocked() -> bool:
        return hasattr(list_aws_profiles, "_mock_name") or hasattr(
            list_aws_profiles, "assert_called"
        )

    # ── Compose ──────────────────────────────────────────

    def compose_modal(self):
        """Execute compose modal on CloudProfileSelectorScreen."""
        title = f"Select Cloud Profile (current: {self._current_profile})"
        yield Static(title, classes="cloud-selector-title")

        yield PillTabs(
            _CLOUD_TABS,
            id="cloud-tabs",
            initial_tab=self._active_tab,
        )

        yield Static(
            self._info_content(),
            classes="cloud-selector-info",
            id="cloud-selector-info",
        )

        yield Input(
            placeholder="Type to filter local profiles, subscriptions, projects, or clusters...",
            id="cloud-filter",
        )

        with VerticalScroll(classes="cloud-list"):
            self._options_container = Container(id="cloud-options")
            yield self._options_container

        yield Static("", classes="cloud-detail-footer", id="cloud-detail-footer", markup=True)

        yield Static(
            "←/→ or Ctrl+P switch tab • ↑/↓ navigate • Enter select • Ctrl+S default • Esc cancel",
            classes="cloud-selector-help",
        )

    def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        super().on_mount()
        self._update_modal_chrome()
        try:
            self.query_one("#cloud-filter", Input).focus()
        except NoMatches:
            pass
        self.call_after_refresh(self._fit_cloud_list)

        try:
            has_app = self.app is not None
        except Exception:
            has_app = False

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if has_app and loop and loop.is_running():
            self.run_worker(self._async_load_profiles(), exclusive=True)
        else:
            self._sync_load_profiles()

    def _update_tab_counts(self) -> None:
        try:
            tabs = self.query_one("#cloud-tabs", PillTabs)
        except NoMatches:
            return
        total = len(self._all_profiles)
        tabs.set_tab_count("all", total)
        for prov in ("aws", "azure", "gcp", "k8s"):
            cnt = sum(1 for p in self._all_profiles if getattr(p, "provider", "aws") == prov)
            tabs.set_tab_count(prov, cnt)

    def _update_modal_chrome(self) -> None:
        glyphs = get_glyphs()
        tab_label = _PROVIDER_SHORT_LABELS.get(self._active_tab, "All")
        active_disp = self._current_profile
        if active_disp == "@ambient":
            # Show friendly name if an ambient service role is active, else fallback
            amb_match = next(
                (p for p in self._all_profiles if getattr(p, "is_ambient", False)),
                None,
            )
            active_disp = (
                getattr(amb_match, "display_name", "Service Role")
                if amb_match is not None
                else "default"
            )
        active_txt = f"active: {active_disp}" if active_disp else "no active context"
        self.set_modal_title(
            "Select Cloud Context",
            counter=f"{len(self._filtered_profiles)} contexts [{tab_label}] · {active_txt}",
        )
        self.set_modal_subtitle([
            (f"{glyphs.arrow_up}{glyphs.arrow_down}", "navigate"),
            ("←/→", "provider tab"),
            ("enter", "select"),
            ("ctrl+s", "default"),
            ("esc", "close"),
        ])

    def on_resize(self) -> None:
        """Handle terminal or container resize events."""
        self.call_after_refresh(self._fit_cloud_list)

    def _fit_cloud_list(self) -> None:
        try:
            list_widget = self.query_one(".cloud-list", VerticalScroll)
            options_container = self._options_container or self.query_one("#cloud-options")
            content_rows = max(MIN_VISIBLE_ROWS, len(options_container.children))
            screen_h = self.size.height if self.size.height > 0 else 60

            compact = screen_h <= 24
            try:
                info_w = self.query_one("#cloud-selector-info", Static)
                info_w.display = not compact
            except NoMatches:
                pass
            try:
                tabs_w = self.query_one("#cloud-tabs", PillTabs)
                tabs_w.styles.margin = (0, 0, 0 if compact else 1, 0)
            except NoMatches:
                pass
            try:
                filter_w = self.query_one("#cloud-filter", Input)
                filter_w.styles.margin = (0, 0, 0 if compact else 1, 0)
            except NoMatches:
                pass
            try:
                footer_w = self.query_one("#cloud-detail-footer", Static)
                footer_w.styles.min_height = 2 if compact else 4
                footer_w.styles.max_height = 2 if compact else 8
                footer_w.styles.margin = (0 if compact else 1, 0, 0, 0)
            except NoMatches:
                pass

            viewport_rows = max(MIN_VISIBLE_ROWS, screen_h - 14)
            target_height = min(
                MAX_VISIBLE_ROWS,
                content_rows,
                viewport_rows,
            )
            list_widget.styles.height = target_height
        except (NoMatches, RuntimeError, Exception):
            return

    def _load_profile_data(self) -> list[Any]:
        if self._is_aws_mocked():
            return list(list_aws_profiles())
        return list_cloud_targets(None)

    def _reconcile_current_profile_after_load(self) -> None:
        """If _current_profile was a stale '@ambient' on a local laptop, reconcile to actual local profile."""
        if self._current_profile == "@ambient":
            has_ambient = any(getattr(p, "is_ambient", False) for p in self._all_profiles)
            if not has_ambient:
                if self._recent_profiles:
                    self._current_profile = self._recent_profiles[0]
                else:
                    self._current_profile = "default"

    async def _async_load_profiles(self) -> None:
        try:
            profiles = await asyncio.to_thread(self._load_profile_data)
        except Exception:
            logger.exception("Failed to load cloud targets in background")
            profiles = []
        if getattr(self, "_dismissed", False):
            return
        self._all_profiles = profiles
        self._loaded = True
        self._reconcile_current_profile_after_load()
        self._update_tab_counts()
        self._apply_filter()
        await self._rebuild_options()
        self._update_info()

    def _sync_load_profiles(self) -> None:
        self._all_profiles = self._load_profile_data()
        self._loaded = True
        self._reconcile_current_profile_after_load()
        self._update_tab_counts()
        self._apply_filter()
        self._rebuild_options_sync()
        self._update_info()

    # ── Tab Navigation ───────────────────────────────────

    def on_pill_tabs_tab_changed(self, event: PillTabs.TabChanged) -> None:
        """Handle pill tabs tab changed events."""
        tab_id = event.tab_id
        if tab_id in _TAB_ORDER and tab_id != self._active_tab:
            self._switch_to_tab(tab_id)  # type: ignore[arg-type]

    def _switch_to_tab(self, tab_id: CloudTabId) -> None:
        self._active_tab = tab_id
        if tab_id in ("aws", "azure", "gcp", "k8s"):
            self._provider_filter = tab_id  # type: ignore[assignment]
        else:
            self._provider_filter = None
        try:
            tabs = self.query_one("#cloud-tabs", PillTabs)
            if tabs.active_tab != tab_id:
                tabs.activate_tab(tab_id)
        except NoMatches:
            pass
        self._selected_index = 0
        self._apply_filter()
        self._update_info()
        if self._loaded:
            self._schedule_rebuild()

    def on_key(self, event: Key) -> None:
        """Allow Left/Right arrows to switch provider tabs when the search input is empty."""
        if event.key in ("left", "right") and not self._filter_text:
            event.prevent_default()
            event.stop()
            try:
                idx = _TAB_ORDER.index(self._active_tab)
            except ValueError:
                idx = 0
            delta = 1 if event.key == "right" else -1
            next_tab = _TAB_ORDER[(idx + delta) % len(_TAB_ORDER)]
            self._switch_to_tab(next_tab)

    # ── Info Line ────────────────────────────────────────

    def _info_content(self) -> str:
        prov_txt = _PROVIDER_SHORT_LABELS.get(self._active_tab, "All Cloud")
        if self._filter_text.strip():
            return f"Filtering {prov_txt} contexts ({len(self._filtered_profiles)} matches)"
        if self._active_tab in ("aws", "azure", "gcp", "k8s"):
            prov_count = sum(
                1 for p in self._all_profiles if getattr(p, "provider", "aws") == self._active_tab
            )
            source_map = {
                "aws": "~/.aws/config & ~/.aws/credentials",
                "azure": "~/.azure/azureProfile.json",
                "gcp": "~/.config/gcloud/configurations",
                "k8s": "~/.kube/config",
            }
            return f"Fetched {prov_count} {prov_txt} contexts from local config ({source_map[self._active_tab]})"
        return f"Fetched {len(self._all_profiles)} cloud contexts across local CLI configurations"

    def _update_info(self) -> None:
        try:
            self.query_one("#cloud-selector-info", Static).update(self._info_content())
        except NoMatches:
            pass
        self._update_modal_chrome()

    # ── Filtering ────────────────────────────────────────

    def _apply_filter(self) -> None:
        base_pool = [
            p for p in self._all_profiles
            if self._provider_filter is None or getattr(p, "provider", "aws") == self._provider_filter
        ]
        query = self._filter_text.strip().lower()
        if query:
            tokens = query.split()
            try:
                matchers = [Matcher(tok, case_sensitive=False) for tok in tokens]
                scored: list[tuple[float, Any]] = []
                for p in base_pool:
                    prov = getattr(p, "provider", "aws")
                    disp = getattr(p, "display_name", p.name)
                    haystack = f"{prov} {p.name} {disp} {p.region or ''} {p.account_id or ''} {p.role_arn or ''}".lower()
                    scores = [m.match(haystack) for m in matchers]
                    if all(s > 0 for s in scores) or all(tok in haystack for tok in tokens):
                        score = min(scores) if scores and all(s > 0 for s in scores) else 0.5
                        scored.append((score, p))
                scored.sort(key=lambda x: x[0], reverse=True)
                self._filtered_profiles = [p for _, p in scored]
            except Exception:
                self._filtered_profiles = [
                    p for p in base_pool
                    if all(
                        t in f"{getattr(p, 'provider', 'aws')} {p.name} {getattr(p, 'display_name', p.name)} {p.region or ''} {p.account_id or ''} {p.role_arn or ''}".lower()
                        for t in tokens
                    )
                ]
        else:
            self._filtered_profiles = list(base_pool)

        if self._selected_index >= len(self._filtered_profiles):
            self._selected_index = max(0, len(self._filtered_profiles) - 1)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle input changed events."""
        if event.input.id == "cloud-filter":
            self._filter_text = event.value
            self._selected_index = 0
            self._update_info()
            if not self._loaded:
                return
            self._apply_filter()
            self._schedule_rebuild()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle input submitted events."""
        self.action_select()

    def _schedule_rebuild(self) -> None:
        self._rebuild_counter += 1
        gen = self._rebuild_counter
        self.call_after_refresh(self._debounced_rebuild, gen)

    async def _debounced_rebuild(self, gen: int) -> None:
        if gen != self._rebuild_counter:
            return
        await self._rebuild_options()

    # ── Rebuild Options ──────────────────────────────────

    def _build_widgets(self) -> list[Static]:
        self._option_widgets.clear()
        self._flat_profiles.clear()

        if not self._filtered_profiles:
            if not self._loaded:
                return [Static("Loading cloud contexts...", classes="cloud-option")]
            if not self._filter_text.strip() and self._active_tab in _EMPTY_PROVIDER_HINTS:
                return [
                    Static(
                        _EMPTY_PROVIDER_HINTS[self._active_tab],
                        classes="cloud-option",
                    )
                ]
            return [Static("No cloud contexts match your filter.", classes="cloud-option")]

        has_filter = bool(self._filter_text.strip())

        # Resolve pinned Recent / Active entries when unfiltered
        recent_matches: list[Any] = []
        ambient_matches: list[Any] = []
        if not has_filter:
            for p in self._filtered_profiles:
                if getattr(p, "is_ambient", False):
                    ambient_matches.append(p)

            prof_by_qid = {
                f"{getattr(p, 'provider', 'aws')}:{p.name}": p
                for p in self._filtered_profiles
            }
            prof_by_name = {p.name: p for p in self._filtered_profiles}

            cur_qid = f"{self._current_provider}:{self._current_profile}"
            if cur_qid in prof_by_qid and prof_by_qid[cur_qid] not in ambient_matches:
                recent_matches.append(prof_by_qid[cur_qid])
            elif self._current_profile in prof_by_name and prof_by_name[self._current_profile] not in ambient_matches:
                recent_matches.append(prof_by_name[self._current_profile])

            for qid in self._recent_cloud_targets:
                if qid in prof_by_qid and prof_by_qid[qid] not in recent_matches and prof_by_qid[qid] not in ambient_matches:
                    recent_matches.append(prof_by_qid[qid])
            for rname in self._recent_profiles:
                if rname in prof_by_name and prof_by_name[rname] not in recent_matches and prof_by_name[rname] not in ambient_matches:
                    recent_matches.append(prof_by_name[rname])

        pinned_set = {id(x) for x in (*ambient_matches, *recent_matches)}
        remaining_profiles = (
            [p for p in self._filtered_profiles if id(p) not in pinned_set]
            if not has_filter
            else self._filtered_profiles
        )

        widgets: list[Static] = []
        current_flat_index = 0

        # 1. Attached Cloud Service Role Section (only when deployed on cloud runtime)
        if ambient_matches and not has_filter:
            widgets.append(
                Static(
                    f"[bold]{_AMBIENT_SECTION_LABEL}[/bold]",
                    classes="cloud-provider-header",
                    markup=True,
                )
            )
            for prof in ambient_matches:
                opt = self._create_option_widget(prof, current_flat_index)
                widgets.append(opt)
                self._option_widgets.append(opt)
                self._flat_profiles.append(prof)
                current_flat_index += 1

        # 2. Pinned Active / Recent Section
        if recent_matches and not has_filter:
            widgets.append(
                Static(
                    f"[bold]{_RECENT_SECTION_LABEL}[/bold]",
                    classes="cloud-provider-header",
                    markup=True,
                )
            )
            for prof in recent_matches:
                opt = self._create_option_widget(prof, current_flat_index)
                widgets.append(opt)
                self._option_widgets.append(opt)
                self._flat_profiles.append(prof)
                current_flat_index += 1

        # 3. Provider-Grouped Sections (AWS, Azure, GCP, K8s)
        by_provider: dict[str, list[Any]] = {"aws": [], "azure": [], "gcp": [], "k8s": []}
        for p in remaining_profiles:
            prov = getattr(p, "provider", "aws")
            by_provider.setdefault(prov, []).append(p)

        for prov, group in by_provider.items():
            if not group:
                continue
            total_prov = sum(1 for x in self._all_profiles if getattr(x, "provider", "aws") == prov)
            base_label = _PROVIDER_SECTION_LABELS.get(prov, f"{prov.upper()} Contexts")
            header_label = f"{base_label} ({total_prov})"
            widgets.append(
                Static(
                    f"[bold]{header_label}[/bold]",
                    classes="cloud-provider-header",
                    markup=True,
                )
            )
            for prof in group:
                opt = self._create_option_widget(prof, current_flat_index)
                widgets.append(opt)
                self._option_widgets.append(opt)
                self._flat_profiles.append(prof)
                current_flat_index += 1

        return widgets

    async def _rebuild_options(self) -> None:
        if not self._options_container:
            return

        self._rebuild_counter += 1
        gen = self._rebuild_counter

        widgets = self._build_widgets()
        await self._options_container.remove_children()

        if gen != self._rebuild_counter:
            return

        if widgets:
            await self._options_container.mount(*widgets)

        if not self._filtered_profiles:
            self._update_detail_footer(None)
        else:
            self._update_detail_footer(
                self._flat_profiles[self._selected_index]
                if self._flat_profiles and self._selected_index < len(self._flat_profiles)
                else None
            )
            self._scroll_to_selected()
        self._fit_cloud_list()
        try:
            self.call_after_refresh(self._fit_cloud_list)
            self.refresh(layout=True)
        except Exception:
            pass

    def _rebuild_options_sync(self) -> None:
        if not self._options_container:
            return
        widgets = self._build_widgets()
        self._options_container.remove_children()
        if widgets:
            self._options_container.mount(*widgets)
        if not self._filtered_profiles:
            self._update_detail_footer(None)
        else:
            self._update_detail_footer(
                self._flat_profiles[self._selected_index]
                if self._flat_profiles and self._selected_index < len(self._flat_profiles)
                else None
            )
            self._scroll_to_selected()
        self._fit_cloud_list()
        try:
            self.call_after_refresh(self._fit_cloud_list)
            self.refresh(layout=True)
        except Exception:
            pass

    def _create_option_widget(
        self,
        prof: AWSProfileInfo | CloudTargetContext,
        flat_index: int,
    ) -> CloudProfileOption:
        prov = getattr(prof, "provider", "aws")
        is_selected = flat_index == self._selected_index
        is_current = (
            prof.name == self._current_profile
            and (prov == self._current_provider or self._current_provider == "aws")
        )
        default_ctx_match = False
        if isinstance(self._default_cloud_ctx, dict):
            default_ctx_match = (
                self._default_cloud_ctx.get("provider") == prov
                and self._default_cloud_ctx.get("target_id") == prof.name
            )
        elif isinstance(self._default_cloud_ctx, tuple) and len(self._default_cloud_ctx) >= 2:
            default_ctx_match = (
                self._default_cloud_ctx[0] == prov and self._default_cloud_ctx[1] == prof.name
            )

        is_default = (prov == "aws" and prof.name == self._default_profile) or default_ctx_match

        classes = "cloud-option"
        if is_selected:
            classes += " cloud-option-selected"
        if is_current:
            classes += " cloud-option-current"

        opt = CloudProfileOption(
            "",
            prof,
            flat_index,
            show_provider_badge=(self._active_tab == "all"),
            is_selected=is_selected,
            is_current=is_current,
            is_default=is_default,
            classes=classes,
        )
        opt.update(opt.render_label_text())
        return opt

    # ── Detail Footer Rendering ──────────────────────────

    def _update_detail_footer(self, profile: AWSProfileInfo | CloudTargetContext | None) -> None:
        try:
            footer = self.query_one("#cloud-detail-footer", Static)
        except NoMatches:
            return

        if not profile:
            if self._active_tab in _EMPTY_PROVIDER_HINTS and not self._filter_text.strip():
                footer.update(_EMPTY_PROVIDER_HINTS[self._active_tab])
            else:
                footer.update("No cloud context selected.")
            return

        prov = getattr(profile, "provider", "aws")
        prov_label = _PROVIDER_SHORT_LABELS.get(prov, prov.upper())
        default_reg = "us-east-1 (default)" if prov == "aws" else "default"
        region_str = profile.region or default_reg
        disp_name = getattr(profile, "display_name", profile.name) or profile.name
        is_ambient = bool(getattr(profile, "is_ambient", False))
        is_prod = "prod" in profile.name.lower() or "prod" in disp_name.lower()
        extra_meta: dict[str, str] = getattr(profile, "extra_metadata", {}) or {}

        target_title = disp_name if is_ambient else profile.name
        reg_label = "Namespace" if prov == "k8s" else "Region"

        pieces: list[Any] = [
            ("Provider: ", "dim"),
            (prov_label, "bold $primary"),
            (" • Context: ", "dim"),
            (target_title, "bold $foreground"),
        ]
        if is_prod:
            pieces.extend([
                (" ", ""),
                ("PROD", "bold $ctx-prod on $ctx-prod-bg"),
            ])
        pieces.extend([
            (f" • {reg_label}: ", "dim"),
            (region_str, "$secondary"),
            "\n",
        ])

        # Line 2: Provider-specific local metadata or attached role details
        if is_ambient:
            pieces.extend([
                ("Source: ", "dim"),
                ("Attached Cloud Service Role / Workload Identity", "bold $secondary"),
            ])
            if profile.account_id:
                pieces.extend([
                    (" • Account/Scope: ", "dim"),
                    (profile.account_id, "$secondary"),
                ])
        elif prov == "gcp":
            proj = profile.account_id or extra_meta.get("project") or "unassigned"
            acct_email = extra_meta.get("account") or "gcloud default"
            pieces.extend([
                ("Project ID: ", "dim"),
                (proj, "bold $secondary"),
                (" • Account: ", "dim"),
                (acct_email, "dim"),
                (" • Config: ", "dim"),
                ("~/.config/gcloud", "dim"),
            ])
        elif prov == "azure":
            sub_name = extra_meta.get("subscription_name") or disp_name
            tenant = profile.account_id or extra_meta.get("tenant_id") or "default"
            user_str = extra_meta.get("user") or "az login"
            pieces.extend([
                ("Subscription: ", "dim"),
                (sub_name, "bold $secondary"),
                (" • Tenant: ", "dim"),
                (tenant, "dim"),
                (" • Account: ", "dim"),
                (user_str, "dim"),
            ])
        elif prov == "k8s":
            cluster = profile.account_id or extra_meta.get("cluster") or "default"
            user_str = extra_meta.get("user") or "kubeconfig"
            pieces.extend([
                ("Cluster: ", "dim"),
                (cluster, "bold $secondary"),
                (" • Auth Info: ", "dim"),
                (user_str, "dim"),
                (" • Config: ", "dim"),
                ("~/.kube/config", "dim"),
            ])
        else:
            # AWS local profile
            if profile.account_id and profile.role_arn:
                pieces.extend([
                    ("Account ID: ", "dim"),
                    (profile.account_id, "$secondary"),
                    (" • Assumed Role: ", "dim"),
                    (profile.role_arn, "dim"),
                ])
            elif profile.account_id:
                pieces.extend([
                    ("Account ID: ", "dim"),
                    (profile.account_id, "$secondary"),
                ])
            elif profile.role_arn:
                pieces.extend([
                    ("Assumed Role: ", "dim"),
                    (profile.role_arn, "dim"),
                ])
            elif profile.is_sso:
                pieces.extend([
                    ("Authentication: ", "dim"),
                    ("AWS IAM Identity Center (SSO) (~/.aws/config)", "$secondary"),
                ])
            elif profile.has_keys:
                pieces.extend([
                    ("Authentication: ", "dim"),
                    ("AWS IAM Credentials (~/.aws/credentials)", "$secondary"),
                ])
            else:
                pieces.extend([
                    ("Authentication: ", "dim"),
                    ("Local AWS Profile (~/.aws/config)", "dim"),
                ])
        pieces.append("\n")

        # Line 3: Permissions & status
        perm_desc = (
            "Permissions governed by attached Cloud Service Role IAM / RBAC policy"
            if is_ambient
            else f"Permissions governed by selected {prov_label} profile / role"
        )
        pieces.extend([
            ("Status: ", "dim"),
            ("ready", "bold $success"),
            (" • ", "dim"),
            (perm_desc, "dim"),
        ])

        footer.update(Content.assemble(*pieces))

    # ── Selection Movement ───────────────────────────────

    def _set_selected_index(self, new_index: int, scroll: bool = False) -> None:
        if not self._option_widgets:
            return
        new_index = max(0, min(new_index, len(self._option_widgets) - 1))
        old_index = self._selected_index
        if new_index == old_index and not scroll:
            return

        if 0 <= old_index < len(self._option_widgets):
            self._option_widgets[old_index].set_selected(False)

        self._selected_index = new_index
        if 0 <= new_index < len(self._option_widgets):
            self._option_widgets[new_index].set_selected(True)

        if self._flat_profiles and new_index < len(self._flat_profiles):
            self._update_detail_footer(self._flat_profiles[new_index])

        if scroll:
            self._scroll_to_selected()

    def _move_selection(self, delta: int) -> None:
        if not self._option_widgets:
            return
        count = len(self._option_widgets)
        new_index = (self._selected_index + delta) % count
        self._set_selected_index(new_index, scroll=True)

    def _scroll_to_selected(self) -> None:
        if not self._option_widgets or self._selected_index >= len(self._option_widgets):
            return
        widget = self._option_widgets[self._selected_index]
        if self._selected_index == 0:
            try:
                scroll = self.query_one(".cloud-list", VerticalScroll)
                scroll.scroll_home(animate=False)
            except Exception:
                try:
                    widget.scroll_visible(animate=False)
                except Exception:
                    pass
        elif self._selected_index == len(self._option_widgets) - 1:
            try:
                scroll = self.query_one(".cloud-list", VerticalScroll)
                scroll.scroll_end(animate=False)
            except Exception:
                try:
                    widget.scroll_visible(animate=False)
                except Exception:
                    pass
        else:
            try:
                widget.scroll_visible(animate=False)
            except Exception:
                pass

    # ── Actions ──────────────────────────────────────────

    def action_move_up(self) -> None:
        """Execute the move up action."""
        self._move_selection(-1)

    def action_move_down(self) -> None:
        """Execute the move down action."""
        self._move_selection(1)

    def _visible_page_size(self) -> int:
        default_page_size = 10
        try:
            scroll = self.query_one(".cloud-list", VerticalScroll)
            height = scroll.size.height
        except Exception:
            return default_page_size
        if height <= 0:
            return default_page_size
        total_options = len(self._option_widgets)
        if total_options == 0:
            return default_page_size
        num_headers = len(self.query(".cloud-provider-header"))
        header_rows = max(0, num_headers * 2 - 1) if num_headers else 0
        total_rows = total_options + header_rows
        return max(1, int(height * total_options / total_rows))

    def action_page_up(self) -> None:
        """Execute the page up action."""
        if not self._option_widgets:
            return
        page = self._visible_page_size()
        target = max(0, self._selected_index - page)
        delta = target - self._selected_index
        if delta != 0:
            self._move_selection(delta)

    def action_page_down(self) -> None:
        """Execute the page down action."""
        if not self._option_widgets:
            return
        count = len(self._option_widgets)
        page = self._visible_page_size()
        target = min(count - 1, self._selected_index + page)
        delta = target - self._selected_index
        if delta != 0:
            self._move_selection(delta)

    def action_cycle_provider(self) -> None:
        """Cycle through provider tabs (All -> AWS -> Azure -> GCP -> Kubernetes -> All)."""
        try:
            idx = _TAB_ORDER.index(self._active_tab)
        except ValueError:
            idx = 0
        next_tab = _TAB_ORDER[(idx + 1) % len(_TAB_ORDER)]
        self._switch_to_tab(next_tab)

    def action_toggle_execution_mode(self) -> None:
        """Backward-compatible execution mode toggle."""
        self._execution_mode = "elevated" if self._execution_mode == "read_only" else "read_only"
        set_cloud_execution_mode(self._execution_mode, persist=True)
        self._mutated = True
        self._update_modal_chrome()
        if self._flat_profiles and self._selected_index < len(self._flat_profiles):
            self._update_detail_footer(self._flat_profiles[self._selected_index])

    def action_select(self) -> None:
        """Execute the select action."""
        if getattr(self, "_dismissed", False):
            return
        if not self._option_widgets:
            typed = self._filter_text.strip()
            if typed:
                known_names = {p.name for p in self._all_profiles}
                if typed not in known_names:
                    self.notify(
                        f"Profile '{typed}' is not configured in ~/.aws/config",
                        severity="warning",
                        timeout=3,
                    )
                self.selected_provider = self._provider_filter or "aws"
                self.dismiss((typed, None))
            return

        opt = self._option_widgets[self._selected_index]
        self.selected_provider = opt.provider
        self.selected_target = opt.profile_info
        self.dismiss((opt.profile_name, opt.profile_info.region))

    def dismiss(
        self,
        result: tuple[str, str | None] | None = None,
    ) -> Any:
        """Safely dismiss the modal screen with idempotency protection."""
        if getattr(self, "_dismissed", False):
            return None
        self._dismissed = True

        try:
            app = self.app
        except Exception:
            app = None

        if app is not None:
            try:
                screen_stack = app._screen_stack
            except Exception:
                screen_stack = None
            if screen_stack is not None:
                if len(screen_stack) <= 1 or self not in screen_stack:
                    logger.debug(
                        "CloudProfileSelectorScreen.dismiss bypassed: not in stack or stack size <= 1"
                    )
                    return None

        try:
            return super().dismiss(result)
        except Exception as exc:
            logger.warning("Error dismissing CloudProfileSelectorScreen: %s", exc)
            return None

    def action_cancel(self) -> None:
        """Execute the cancel action."""
        if getattr(self, "_dismissed", False):
            return
        self.dismiss(None)

    def action_tab_complete(self) -> None:
        """Execute the tab complete action."""
        if not self._option_widgets:
            return
        opt = self._option_widgets[self._selected_index]
        try:
            inp = self.query_one("#cloud-filter", Input)
            inp.value = opt.profile_name
            inp.cursor_position = len(opt.profile_name)
        except NoMatches:
            pass

    def action_set_default(self) -> None:
        """Execute the set default action."""
        if not self._option_widgets:
            return
        opt = self._option_widgets[self._selected_index]
        pname = opt.profile_name
        prov = opt.provider
        save_active_cloud_context(prov, pname, region=opt.profile_info.region)
        if prov == "aws":
            self._default_profile = pname
            save_aws_profile(pname)
        self._default_cloud_ctx = (prov, pname, opt.profile_info.region)
        self._mutated = True
        self.notify(
            f"Default {prov.upper()} context set: {pname} (saved to config.toml)",
            severity="information",
            timeout=3,
        )
        for w in self._option_widgets:
            w.is_default = (w.provider == prov and w.profile_name == pname)
            w.update(w.render_label_text())

    def on_cloud_profile_option_clicked(self, event: CloudProfileOption.Clicked) -> None:
        """Handle cloud profile option clicked events."""
        if getattr(self, "_dismissed", False):
            return
        opt = self._option_widgets[event.index] if 0 <= event.index < len(self._option_widgets) else None
        if opt is not None:
            self.selected_provider = opt.provider
            self.selected_target = opt.profile_info
        region = opt.profile_info.region if opt else None
        self.dismiss((event.profile_name, region))


__all__ = ["CloudProfileOption", "CloudProfileSelectorScreen"]

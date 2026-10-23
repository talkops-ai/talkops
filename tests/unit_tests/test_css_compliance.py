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

"""Unit tests for Step 9: CSS compliance and modal styling standards."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult

from talkops.config.settings import (
    get_glyphs,
    is_ascii_mode,
    reset_settings_for_testing,
)
from talkops.ui.theme import register_app_themes
from talkops.ui.widgets.cloud_selector import CloudProfileSelectorScreen
from talkops.ui.widgets.effort_selector import EffortSelectorScreen
from talkops.ui.widgets.theme_selector import ThemeSelectorScreen


class _TestHarnessApp(App[None]):
    """Test app harness for mounting modal screens."""

    def __init__(self, screen_instance: Any) -> None:
        """Initialize _TestHarnessApp."""
        super().__init__()
        self._test_screen = screen_instance
        register_app_themes(self)
        self.theme = "talkops-dark"

    def compose(self) -> ComposeResult:
        """Compose and yield child widgets for this component."""
        yield from ()

    async def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        await self.push_screen(self._test_screen)


def test_ui_css_uses_semantic_colors() -> None:
    """Verify all TCSS files in src/talkops/ui use semantic theme variables rather than concrete hex/rgb colors."""
    ui_root = Path("src/talkops/ui")
    concrete_color = re.compile(
        r"#[0-9a-fA-F]{3,8}\b|rgba?\(",
        re.IGNORECASE,
    )
    offenders: list[str] = []

    for path in ui_root.rglob("*.tcss"):
        text = path.read_text(encoding="utf-8")
        if concrete_color.search(text):
            offenders.append(str(path))

    assert offenders == [], f"Found concrete colors in TCSS stylesheets: {offenders}"


def test_modal_backdrop_ownership() -> None:
    """Verify OpsModalScreen owns the modal backdrop and app.tcss does not compete."""
    app_tcss = Path("src/talkops/ui/app.tcss").read_text(encoding="utf-8")
    modal_tcss = Path("src/talkops/ui/modal.tcss").read_text(encoding="utf-8")

    # app.tcss must not declare competing ModalScreen backdrop
    assert "ModalScreen" not in app_tcss, "app.tcss must not declare ModalScreen rules"
    assert "rgba(0, 0, 0" not in app_tcss, "app.tcss must not contain concrete backdrop colors"

    # modal.tcss must declare semantic OpsModalScreen backdrop
    assert "OpsModalScreen {" in modal_tcss
    assert "background: $background 70%;" in modal_tcss


@pytest.mark.asyncio
async def test_ascii_mode_modal_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify representative command modals render correctly under ASCII charset mode."""
    monkeypatch.setenv("TALKOPS_UI_CHARSET_MODE", "ascii")
    reset_settings_for_testing()
    assert is_ascii_mode() is True
    assert get_glyphs().bullet == "-"
    assert get_glyphs().circle_filled == "[*]"

    try:
        # Test Cloud Profile Selector in ASCII mode
        app1 = _TestHarnessApp(CloudProfileSelectorScreen())
        async with app1.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert app1.screen is not None
            # Modal must mount successfully and contain no crashed widgets
            assert app1.screen.is_mounted

        # Test Theme Selector in ASCII mode
        app2 = _TestHarnessApp(ThemeSelectorScreen(current_theme="talkops-dark"))
        async with app2.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app2.screen is not None
            assert app2.screen.is_mounted

        # Test Effort Selector in ASCII mode
        app3 = _TestHarnessApp(
            EffortSelectorScreen(
                model_spec="claude-3-7-sonnet",
                efforts=("low", "medium", "high"),
                current_effort="medium",
            )
        )
        async with app3.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app3.screen is not None
            assert app3.screen.is_mounted

    finally:
        reset_settings_for_testing()

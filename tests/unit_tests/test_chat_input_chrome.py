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

"""Tests for the ChatInput chrome: mode borders, keycap hints, newlines."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.content import Content

from talkops.ui.widgets import chat_input as chat_input_module
from talkops.ui.widgets.chat_input import (
    ChatInput,
    format_input_hints,
    input_hints,
)


class _ThemedApp(App):
    """Minimal app carrying the TalkOps theme variables ChatInput's CSS uses."""

    def __init__(self) -> None:
        """Initialize _ThemedApp."""
        super().__init__()
        from talkops.ui.theme import register_app_themes

        register_app_themes(self)
        self.theme = "talkops-dark"

    def get_theme_variable_defaults(self) -> dict[str, str]:
        """Provide get theme variable defaults test helper or fixture."""
        from talkops.ui.theme import get_css_variable_defaults, get_theme_colors

        return get_css_variable_defaults(colors=get_theme_colors(self))

    def compose(self) -> ComposeResult:
        """Compose and yield child widgets for this component."""
        yield ChatInput(id="ci")


# ── input_hints ─────────────────────────────────────────


def test_normal_hints_lead_with_send_and_newline(monkeypatch):
    """Verify normal hints lead with send and newline."""
    monkeypatch.setattr(chat_input_module, "_newline_key_label", lambda: "opt+enter")
    hints = input_hints("normal")
    assert hints[0] == ("enter", "send")
    assert hints[1] == ("opt+enter", "newline")
    assert [k for k, _ in hints[2:]] == ["/", "!", "@"]


@pytest.mark.parametrize(
    ("mode", "first", "last"),
    [
        ("command", ("enter", "run"), ("esc", "exit")),
        ("shell", ("enter", "run"), ("esc", "exit")),
        ("shell_incognito", ("enter", "run"), ("esc", "exit")),
    ],
)
def test_mode_hints_run_and_exit(mode, first, last):
    """Verify mode hints run and exit."""
    hints = input_hints(mode)
    assert hints[0] == first
    assert hints[-1] == last


def test_shell_hint_mentions_private_toggle():
    """Verify shell hint mentions private toggle."""
    assert ("!", "private") in input_hints("shell")


# ── format_input_hints ──────────────────────────────────


def test_format_hints_styles_keys_bold_actions_dim():
    """Verify format hints styles keys bold actions dim."""
    content = format_input_hints([("enter", "send"), ("tab", "complete")], 80)
    assert content.plain == " enter send · tab complete "
    styled = {content.plain[s.start : s.end]: str(s.style) for s in content.spans}
    assert styled["enter"] == "bold"
    assert styled["tab"] == "bold"
    assert styled[" send"] == "dim"
    assert styled[" · "] == "dim"


def test_format_hints_drops_trailing_hints_that_do_not_fit():
    """Verify format hints drops trailing hints that do not fit."""
    hints = [("enter", "send"), ("tab", "complete"), ("esc", "exit")]
    # "enter send" (10) + " · tab complete" (15) = 25; esc would need 11 more.
    content = format_input_hints(hints, 30)
    assert content.plain == " enter send · tab complete "
    assert "esc" not in content.plain


def test_format_hints_never_cuts_mid_word():
    """Verify format hints never cuts mid word."""
    content = format_input_hints([("enter", "send")], 5)
    assert content.plain == ""


# ── Mounted behaviour ───────────────────────────────────


@pytest.mark.asyncio
async def test_border_title_and_class_follow_mode():
    """Verify border title and class follow mode."""
    app = _ThemedApp()
    async with app.run_test(size=(100, 12)) as pilot:
        ci = app.query_one(ChatInput)
        box = ci._input_box
        assert box is not None
        assert box.border_title is None
        assert "send" in str(box.border_subtitle)

        for mode, cls, title in (
            ("command", "mode-command", "command"),
            ("shell", "mode-shell", "shell"),
            ("shell_incognito", "mode-shell-incognito", "private shell"),
        ):
            ci.mode = mode
            await pilot.pause()
            assert ci.has_class(cls)
            assert str(box.border_title).strip() == title
            assert "exit" in str(box.border_subtitle)
            assert ci._text_area.placeholder == ""

        ci.mode = "normal"
        await pilot.pause()
        assert not ci.has_class("mode-command", "mode-shell", "mode-shell-incognito")
        assert box.border_title is None
        # No auto-filled example prompt: the input starts empty.
        assert ci._text_area.placeholder == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["shift+enter", "alt+enter", "ctrl+j", "ctrl+enter"])
async def test_newline_keys_insert_line_break(key):
    """Verify newline keys insert line break."""
    app = _ThemedApp()
    async with app.run_test(size=(100, 12)) as pilot:
        ci = app.query_one(ChatInput)
        ci.focus()
        await pilot.press("a", key, "b")
        await pilot.pause()
        # A submit would have cleared the input, so both lines surviving
        # proves the key inserted a newline instead of sending.
        assert ci.text == "a\nb"


@pytest.mark.asyncio
async def test_hints_refit_on_narrow_width():
    """Verify hints refit on narrow width."""
    app = _ThemedApp()
    async with app.run_test(size=(40, 12)) as pilot:
        await pilot.pause()
        ci = app.query_one(ChatInput)
        assert ci._input_box is not None
        subtitle = ci._input_box.border_subtitle
        # Textual stores the subtitle as markup; measure the visible text.
        plain = Content.from_markup(str(subtitle)).plain
        assert "send" in plain
        assert "files" not in plain
        assert len(plain) <= 40

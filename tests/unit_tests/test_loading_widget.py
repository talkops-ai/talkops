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

"""Tests for the refined LoadingWidget (shimmer, label split, pause/resume)."""

from __future__ import annotations

from time import monotonic

from talkops.ui.widgets.loading import LoadingWidget, format_duration


def _plain(widget: LoadingWidget) -> str:
    return widget.render().plain


def test_format_duration() -> None:
    """Verify format duration."""
    assert format_duration(5) == "5s"
    assert format_duration(60) == "1m"
    assert format_duration(130) == "2m 10s"


def test_render_default_line() -> None:
    """Verify render default line."""
    w = LoadingWidget("Thinking", started_at=monotonic() - 12)
    text = _plain(w)
    assert "Thinking" in text
    assert "\u00b7 12s \u00b7" in text
    assert text.endswith("esc to interrupt")


def test_label_detail_split() -> None:
    """Verify label detail split."""
    w = LoadingWidget("Subagent: aws-security")
    text = _plain(w)
    assert "Subagent \u00b7 aws-security" in text
    assert "Subagent:" not in text


def test_shimmer_highlights_single_glint() -> None:
    """Verify shimmer highlights single glint."""
    w = LoadingWidget("Thinking")
    w._shimmer_pos = 3
    styles = [style for _, style in w._shimmer("Thinking")]
    assert styles[3] == "bold $foreground"
    assert styles[2] == styles[4] == "bold $primary"
    assert styles.count("bold $foreground") == 1
    assert all(s == "$primary" for i, s in enumerate(styles) if abs(i - 3) > 1)


def test_shimmer_gap_leaves_label_plain() -> None:
    """Verify shimmer gap leaves label plain."""
    w = LoadingWidget("Thinking")
    # Centre lands in the gap after the label: only the tail may be lit.
    w._shimmer_pos = len("Thinking") + 2
    styles = [style for _, style in w._shimmer("Thinking")]
    assert "bold $foreground" not in styles


def test_pause_freezes_elapsed_and_resume_continues() -> None:
    """Verify pause freezes elapsed and resume continues."""
    w = LoadingWidget("Thinking", started_at=monotonic() - 30)
    w.pause()
    assert w.paused
    assert w.status == "Awaiting decision"
    text = _plain(w)
    assert "Awaiting decision" in text
    assert "paused at 30s" in text
    assert "esc to interrupt" not in text

    w.resume()
    assert not w.paused
    assert w.status == "Thinking"
    assert "30s" in _plain(w)


def test_set_status_updates_property() -> None:
    """Verify set status updates property."""
    w = LoadingWidget()
    w.set_status("Running tools")
    assert w.status == "Running tools"
    assert "Running tools" in _plain(w)

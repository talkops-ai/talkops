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

"""Live docked panel (`BackgroundTaskPanel`) for non-blocking background shell commands.

Renders up to 5 active/recent background processes above `ChatInput` with live elapsed
timers, spinner frames, recent stdout activity badges, and interactive stdin alerts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from textual import events
from textual.containers import Vertical
from textual.content import Content
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widgets import Static

from talkops.config.settings import get_glyphs
from talkops.ui.theme import get_theme_colors
from talkops.ui.widgets.loading import Spinner
from talkops.ui.widgets.subagent_panel import (
    format_duration,
    sanitize_control_chars,
)

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.timer import Timer

_TICK_INTERVAL = 0.1
_COMPLETED_RETENTION_SECONDS = 8.0
_MAX_VISIBLE_ROWS = 5
_FALLBACK_WIDTH = 90


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    return format_duration(seconds)


def _sanitize(text: str, *, max_chars: int) -> str:
    return sanitize_control_chars(text, keep_newlines=False, max_length=max_chars)


@dataclass
class _TaskRowRecord:
    """UI state for a single tracked background shell command."""

    task_id: str
    command: str
    status: str = "running"
    is_daemon: bool = False
    pid: int | None = None
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    exit_code: int | None = None
    last_line: str = ""
    log_path: str = ""

    def is_active(self) -> bool:
        """Return whether active."""
        return self.status in {"running", "waiting_for_input"}

    def elapsed_seconds(self) -> float:
        """Execute elapsed seconds on _TaskRowRecord."""
        end = self.completed_at if self.completed_at is not None else time.time()
        return max(0.0, end - self.started_at)

    def start_time_str(self) -> str:
        """Start time str."""
        try:
            return datetime.fromtimestamp(self.started_at).strftime("%H:%M:%S")
        except Exception:
            return "--:--:--"


class BackgroundTaskPanel(Vertical):
    """Docked live status bar for detached background shell commands."""

    DEFAULT_CSS = """
    BackgroundTaskPanel {
        height: auto;
        background: $background;
        border-top: solid $primary 35%;
        display: none;
        padding: 0 2;
        margin: 0 0 1 0;
    }

    BackgroundTaskPanel.-visible {
        display: block;
    }

    BackgroundTaskPanel #bg-task-header {
        width: 1fr;
        height: 1;
        text-style: bold;
    }

    BackgroundTaskPanel #bg-task-rows {
        width: 1fr;
        height: auto;
    }

    BackgroundTaskPanel #bg-task-rows.-collapsed {
        display: none;
    }
    """

    expanded: reactive[bool] = reactive(default=True, init=False)

    def __init__(self, *, id: str = "background-task-panel", **kwargs: Any) -> None:
        """Initialize BackgroundTaskPanel."""
        super().__init__(id=id, **kwargs)
        self._records: dict[str, _TaskRowRecord] = {}
        self._order: list[str] = []
        self._spinner = Spinner()
        self._timer: Timer | None = None

    def compose(self) -> ComposeResult:
        """Compose and yield child widgets for this component."""
        yield Static("", id="bg-task-header", markup=False)
        yield Static("", id="bg-task-rows", markup=False)

    def on_mount(self) -> None:
        """Initialize component state when mounted into the DOM."""
        if self._visible_records():
            self._ensure_timer()
            self._refresh()

    def on_unmount(self) -> None:
        """Clean up component resources when unmounted from the DOM."""
        self._stop_timer()

    def on_click(self, event: events.Click) -> None:
        """Handle mouse click events."""
        if getattr(event, "chain", 1) > 1 or bool(
            getattr(self.screen, "selections", None)
        ):
            return
        self.expanded = not self.expanded
        try:
            rows_widget = self.query_one("#bg-task-rows", Static)
            rows_widget.set_class(not self.expanded, "-collapsed")
        except NoMatches:
            pass
        self._refresh()
        event.stop()

    def apply_snapshot(self, tasks: list[dict[str, Any]]) -> None:
        """Populate or reconcile rows from a server ``task_snapshot`` payload."""
        now = time.time()
        for item in tasks:
            if not isinstance(item, dict):
                continue
            tid = str(item.get("task_id") or "").strip()
            if not tid:
                continue
            status = str(item.get("status") or "running")
            completed_at = item.get("completed_at")
            comp_float = float(completed_at) if isinstance(completed_at, (int, float)) else None
            if status not in {"running", "waiting_for_input"} and comp_float is not None:
                if (now - comp_float) > _COMPLETED_RETENTION_SECONDS:
                    continue
            self._upsert_from_dict(item)
        self._sync_visibility_and_timer()
        self._refresh()

    def on_task_event(self, event: dict[str, Any]) -> None:
        """Apply a live ``background_task`` stream or SSE event."""
        if not isinstance(event, dict):
            return
        if event.get("type") == "task_snapshot":
            tasks = event.get("tasks")
            if isinstance(tasks, list):
                self.apply_snapshot(tasks)
            return

        tid = str(event.get("task_id") or "").strip()
        if not tid:
            return
        self._upsert_from_dict(event)
        self._sync_visibility_and_timer()
        self._refresh()

    def clear_tasks(self) -> None:
        """Clear all displayed tasks (e.g. on session/thread switch)."""
        self._records.clear()
        self._order.clear()
        self._stop_timer()
        self.remove_class("-visible")

    def _upsert_from_dict(self, data: dict[str, Any]) -> None:
        tid = str(data.get("task_id") or "").strip()
        if not tid:
            return
        rec = self._records.get(tid)
        cmd = str(data.get("command") or (rec.command if rec else "command"))
        status = str(data.get("status") or (rec.status if rec else "running"))
        phase = str(data.get("phase") or "")
        if phase == "waiting_for_input":
            status = "waiting_for_input"
        elif phase == "complete" and status == "running":
            exit_code = data.get("exit_code")
            status = "completed" if exit_code in (0, None) else "failed"

        started_at = data.get("started_at")
        started_val = (
            float(started_at)
            if isinstance(started_at, (int, float))
            else (rec.started_at if rec else time.time())
        )
        completed_at = data.get("completed_at")
        completed_val = (
            float(completed_at)
            if isinstance(completed_at, (int, float))
            else (rec.completed_at if rec else None)
        )
        if status not in {"running", "waiting_for_input"} and completed_val is None:
            completed_val = time.time()

        last_line = str(data.get("last_line") or "").strip()
        if not last_line and isinstance(data.get("last_output_tail"), str):
            lines = [ln.strip() for ln in data["last_output_tail"].splitlines() if ln.strip()]
            if lines:
                last_line = lines[-1]

        if rec is None:
            rec = _TaskRowRecord(
                task_id=tid,
                command=_sanitize(cmd, max_chars=160),
                status=status,
                is_daemon=bool(data.get("is_daemon", False)),
                pid=data.get("pid") if isinstance(data.get("pid"), int) else None,
                started_at=started_val,
                completed_at=completed_val,
                exit_code=data.get("exit_code") if isinstance(data.get("exit_code"), int) else None,
                last_line=_sanitize(last_line, max_chars=100) if last_line else "",
                log_path=str(data.get("log_path") or ""),
            )
            self._records[tid] = rec
            self._order.append(tid)
        else:
            rec.command = _sanitize(cmd, max_chars=160)
            rec.status = status
            rec.is_daemon = bool(data.get("is_daemon", rec.is_daemon))
            if isinstance(data.get("pid"), int):
                rec.pid = data["pid"]
            rec.completed_at = completed_val
            if isinstance(data.get("exit_code"), int):
                rec.exit_code = data["exit_code"]
            if last_line:
                rec.last_line = _sanitize(last_line, max_chars=100)
            if data.get("log_path"):
                rec.log_path = str(data["log_path"])

    def _visible_records(self) -> list[_TaskRowRecord]:
        now = time.time()
        visible: list[_TaskRowRecord] = []
        expired_ids: list[str] = []
        for tid in self._order:
            rec = self._records.get(tid)
            if rec is None:
                continue
            if rec.is_active():
                visible.append(rec)
            else:
                comp = rec.completed_at or rec.started_at
                if (now - comp) <= _COMPLETED_RETENTION_SECONDS:
                    visible.append(rec)
                else:
                    expired_ids.append(tid)

        for tid in expired_ids:
            self._records.pop(tid, None)
            if tid in self._order:
                self._order.remove(tid)

        return visible

    def _sync_visibility_and_timer(self) -> None:
        visible = self._visible_records()
        if visible:
            self.add_class("-visible")
            self._ensure_timer()
        else:
            self._stop_timer()
            self.remove_class("-visible")

    def _ensure_timer(self) -> None:
        if self._timer is None and self.is_mounted:
            self._timer = self.set_interval(_TICK_INTERVAL, self._on_tick)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _on_tick(self) -> None:
        self._spinner.next_frame()
        visible = self._visible_records()
        if not visible:
            self._stop_timer()
            self.remove_class("-visible")
            return
        self._refresh()

    def _panel_width(self) -> int:
        try:
            w = self.size.width
        except Exception:
            w = 0
        return w if w and w > 20 else _FALLBACK_WIDTH

    def _refresh(self) -> None:
        if not self.is_mounted:
            return
        visible = self._visible_records()
        if not visible:
            return

        glyphs = get_glyphs()
        colors = get_theme_colors(self)
        active_count = sum(1 for r in visible if r.is_active())
        waiting_count = sum(1 for r in visible if r.status == "waiting_for_input")

        caret = (
            glyphs.disclosure_expanded
            if self.expanded
            else glyphs.disclosure_collapsed
        )
        if waiting_count > 0:
            icon = "⚠"
            tint = colors.warning
        elif active_count > 0:
            icon = self._spinner.current_frame()
            tint = colors.primary
        else:
            icon = glyphs.checkmark
            tint = colors.success

        summary = (
            f"{active_count} running"
            if active_count > 0
            else f"{len(visible)} finished"
        )
        if waiting_count > 0:
            summary += f" · {waiting_count} waiting for input"

        lead = f"{caret} {icon}  background tasks   {summary}"
        hint = "click to collapse" if self.expanded else "click to expand"
        spacer = max(2, self._panel_width() - len(lead) - len(hint) - 4)
        header_content = Content.assemble(
            Content.styled(lead, tint),
            Content.styled(" " * spacer + hint, colors.muted),
        )
        try:
            self.query_one("#bg-task-header", Static).update(header_content)
        except NoMatches:
            pass

        if not self.expanded:
            return

        shown = visible[:_MAX_VISIBLE_ROWS]
        extra = len(visible) - len(shown)
        row_contents: list[Content] = [
            self._render_row(rec, glyphs, colors) for rec in shown
        ]
        if extra > 0:
            row_contents.append(
                Content.styled(f"  … +{extra} more background task(s) (`/tasks ps`)", colors.muted)
            )
        try:
            self.query_one("#bg-task-rows", Static).update(
                Content("\n").join(row_contents)
            )
        except NoMatches:
            pass

    def _render_row(self, rec: _TaskRowRecord, glyphs: Any, colors: Any) -> Content:
        if rec.status == "waiting_for_input":
            icon = "⌨"
            tint = colors.warning
        elif rec.status == "running":
            icon = self._spinner.current_frame()
            tint = colors.primary
        elif rec.status == "completed":
            icon = glyphs.checkmark
            tint = colors.success
        elif rec.status == "killed":
            icon = glyphs.circle_empty
            tint = colors.muted
        else:
            icon = glyphs.error
            tint = colors.error

        elapsed = _format_elapsed(rec.elapsed_seconds()).rjust(7)
        mode_tag = "daemon" if rec.is_daemon else "shell"
        prefix = f"  {icon}  [{rec.start_time_str()}] {rec.task_id}  "
        cmd_text = _sanitize(rec.command, max_chars=48)

        if rec.status == "waiting_for_input":
            badge = f"  ⚠ waiting for stdin ({rec.last_line[:36]})" if rec.last_line else "  ⚠ waiting for stdin"
            badge_style = f"bold {colors.warning}"
        elif rec.status == "running" and rec.last_line:
            badge = f"  · {rec.last_line[:40]}"
            badge_style = colors.muted
        elif rec.status == "completed":
            badge = "  · exit 0"
            badge_style = colors.success
        elif rec.exit_code is not None:
            badge = f"  · exit {rec.exit_code}"
            badge_style = colors.error
        else:
            badge = ""
            badge_style = colors.muted

        return Content.assemble(
            Content.styled(prefix, tint),
            Content.styled(cmd_text, colors.foreground),
            Content.styled(badge, badge_style),
            Content.styled(f"   {mode_tag}  ", f"{colors.secondary} dim"),
            Content.styled(elapsed, colors.muted),
        )


__all__ = ["BackgroundTaskPanel"]

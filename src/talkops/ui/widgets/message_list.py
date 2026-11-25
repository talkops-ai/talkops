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

"""Scrollable message transcript viewport (`MessageList`) and wheel-scroll acceleration.

Extracted from `talkops.ui.widgets.messages` (Phase 5 UI modularization).
"""

from __future__ import annotations

import math
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from textual import events
from textual.containers import VerticalScroll

from talkops.ui.widgets.chat_messages import (
    AssistantMessage,
    DiffMessage,
    QueuedUserMessage,
    ThinkingMessage,
    UserMessage,
)
from talkops.ui.widgets.status_messages import (
    ErrorMessage,
    SkillMessage,
    SystemMessage,
)
from talkops.ui.widgets.tool_call_message import (
    _TOOL_GROUP_EXCLUSIONS,
    ToolCallMessage,
    _strip_success_exit_line,
)
from talkops.ui.widgets.tool_group import ToolGroupSummary
from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from textual.widget import Widget

logger = get_logger(__name__)


@dataclass
class HistoryEntry:
    """Lightweight virtualized representation of a historical transcript item."""

    kind: str
    text: str = ""
    skill_kwargs: dict[str, Any] | None = None
    compaction_kwargs: dict[str, Any] | None = None
    tool_spec: dict[str, Any] | None = None
    tool_group_specs: list[dict[str, Any]] | None = None
    lc_messages: list[Any] | None = None

    def to_widget(self) -> Widget:
        """Instantiate the Textual widget corresponding to this entry."""
        from talkops.ui.widgets.status_messages import CompactionMessage

        if self.kind == "user":
            return UserMessage(self.text)
        if self.kind == "assistant":
            return AssistantMessage(self.text)
        if self.kind == "thinking":
            return ThinkingMessage(
                content=self.text, duration_seconds=0.0, live=False
            )
        if self.kind == "skill":
            return SkillMessage(**(self.skill_kwargs or {}))
        if self.kind == "compaction":
            return CompactionMessage(**(self.compaction_kwargs or {}))
        if self.kind == "tool_group":
            cleaned_specs: list[dict[str, Any]] = []
            for spec in self.tool_group_specs or []:
                item = dict(spec)
                name = str(item.get("name") or "tool")
                raw_res = str(item.get("result") or "")
                if item.get("success", True) and name != "ask_user":
                    item["result"] = _strip_success_exit_line(raw_res)
                cleaned_specs.append(item)
            return ToolGroupSummary(lazy_specs=cleaned_specs, live=False)
        if self.kind == "tool_call":
            spec = self.tool_spec or {}
            name = str(spec.get("name") or "tool")
            msg = ToolCallMessage(name, dict(spec.get("args") or {}))
            msg._call_id = str(spec.get("call_id") or "")
            if spec.get("rejected"):
                msg._deferred_status = "rejected"
            elif "result" in spec:
                raw_res = str(spec.get("result") or "")
                is_ok = bool(spec.get("success", True))
                msg._deferred_status = "success" if is_ok else "error"
                msg._deferred_output = (
                    _strip_success_exit_line(raw_res)
                    if (is_ok and name != "ask_user")
                    else raw_res
                )
            return msg
        return SystemMessage(self.text)

    def to_lc_messages(self) -> list[Any]:
        """Return LangChain message representations for UI fallback inspection."""
        if self.lc_messages is not None:
            return list(self.lc_messages)
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        if self.kind == "user" and self.text:
            return [HumanMessage(content=self.text)]
        if self.kind == "assistant" and self.text:
            return [AIMessage(content=self.text)]
        if self.kind == "tool_call" and self.tool_spec:
            res = str(self.tool_spec.get("result") or "")
            cid = str(self.tool_spec.get("call_id") or "call_1")
            if res:
                return [ToolMessage(content=res, tool_call_id=cid)]
        if self.kind == "tool_group" and self.tool_group_specs:
            out: list[Any] = []
            for spec in self.tool_group_specs:
                res = str(spec.get("result") or "")
                cid = str(spec.get("call_id") or "call_1")
                if res:
                    out.append(ToolMessage(content=res, tool_call_id=cid))
            return out
        return []


def compute_wheel_speed(streak: int, viewport_height: int = 0) -> int:
    """Compute sub-linear scroll lines per wheel event.

    Uses ``min(12, 1 + isqrt(streak))`` clamped to ``max(1, viewport_height // 3)``
    when ``viewport_height > 0``.
    """
    speed = min(12, 1 + math.isqrt(max(0, streak)))
    if viewport_height > 0:
        speed = min(speed, max(1, viewport_height // 3))
    return max(1, speed)


class MessageList(VerticalScroll):
    """Scrollable list of conversation messages with auto-scroll lock controls."""

    FOCUS_ON_CLICK = False

    DEFAULT_CSS = """
    MessageList {
        height: 1fr;
    }
    """

    _WHEEL_STREAK_WINDOW_S: ClassVar[float] = 0.150
    """Max interval (150ms) between same-direction wheel events to build streak."""

    _SCROLL_COALESCE_INTERVAL_S: ClassVar[float] = 0.016
    """16ms (60fps) frame budget for coalescing burst wheel scroll deltas."""

    _HOVER_SUPPRESS_WINDOW_S: ClassVar[float] = 0.120
    """Suppress post-refresh hover hit-testing while actively scrolling."""

    _STICKY_BOTTOM_THRESHOLD: ClassVar[float] = 2.0
    """Distance from max_scroll_y within which downward scroll re-locks auto-scroll."""

    INITIAL_HISTORY_WINDOW: ClassVar[int] = 40
    """Number of most-recent history items mounted immediately on resume."""

    MAX_MOUNTED_ITEMS: ClassVar[int] = 120
    """Soft ceiling on top-level mounted transcript items while auto-scroll is locked."""

    HYDRATE_BATCH_SIZE: ClassVar[int] = 20
    """Number of archived entries prepended per scroll-to-top hydration batch."""

    def __init__(self, **kwargs: Any) -> None:
        """Initialize MessageList."""
        super().__init__(**kwargs)
        self._current_assistant: AssistantMessage | None = None
        self._current_thinking: ThinkingMessage | None = None
        self._accumulated_thinking: str = ""
        self._tool_calls: dict[str, ToolCallMessage] = {}
        self._tool_group_by_call_id: dict[str, ToolGroupSummary] = {}
        self._tool_call_total: int = 0
        """Monotonic count of tool-call widgets created (per-turn deltas)."""
        self._active_tool_group: ToolGroupSummary | None = None
        self._archived_entries: list[HistoryEntry] = []
        self._hydrating_older: bool = False
        self._timestamps_visible: bool = False
        self._auto_scroll_locked = True
        self._scroll_end_pending = False
        self._last_wheel_time: float = 0.0
        self._last_wheel_dir: int = 0
        self._wheel_streak: int = 0
        self._pending_scroll_delta: int = 0
        self._last_scroll_flush_time: float = 0.0
        self._scroll_flush_timer: Any = None
        self._hover_settle_timer: Any = None

    def on_unmount(self) -> None:
        """Stop any pending scroll coalescer or hover settle timers."""
        self._stop_scroll_timers()

    def _stop_scroll_timers(self) -> None:
        if self._scroll_flush_timer is not None:
            self._scroll_flush_timer.stop()
            self._scroll_flush_timer = None
        if self._hover_settle_timer is not None:
            self._hover_settle_timer.stop()
            self._hover_settle_timer = None

    def _mount_child(self, widget: Widget, before: Widget | None = None) -> None:
        """Mount child widget, ensuring it mounts before the bottom loading widget if active."""
        if before is not None:
            self.mount(widget, before=before)
            return
        loading = (
            getattr(self.app, "_loading_widget", None)
            if self.is_attached and self.app
            else None
        )
        if (
            loading is not None
            and getattr(loading, "is_attached", False)
            and loading.parent is self
            and widget is not loading
        ):
            self.mount(widget, before=loading)
        else:
            self.mount(widget)

    def _is_drag_selecting(self) -> bool:
        """Return True when an active mouse drag-selection is in progress."""
        if not self.is_attached:
            return False
        try:
            screen = self.screen
            if not getattr(screen, "_selecting", False):
                return False
            select_state = getattr(screen, "_select_state", None)
            if select_state is not None and getattr(select_state, "end", None) is not None:
                return True
            return bool(getattr(screen, "selections", None))
        except Exception:
            return False

    def on_scroll_up(self) -> None:
        """Suspend auto-scroll when user manually scrolls up."""
        self._auto_scroll_locked = False
        if self._anchored and not self._anchor_released:
            self.release_anchor()

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """Release bottom anchor when scroll_y moves upward during drag-selection."""
        super().watch_scroll_y(old_value, new_value)
        if (
            new_value < old_value - 0.5
            and (self.max_scroll_y - new_value) > self._STICKY_BOTTOM_THRESHOLD
            and self._is_drag_selecting()
        ):
            self.on_scroll_up()

    def _scroll_update(self, virtual_size: Any) -> None:
        """Keep viewport pinned to bottom when content height grows while auto-scroll is locked."""
        super()._scroll_update(virtual_size)
        if (
            self._auto_scroll_locked
            and self.is_attached
            and not self._is_drag_selecting()
            and self.scroll_y < self.max_scroll_y
        ):
            self.scroll_end(animate=False, immediate=True, x_axis=False)

    def _mark_user_scrolling(self) -> None:
        """Suppress post-refresh hover hit-testing while a scroll gesture is active."""
        if not self.is_attached or self.app is None:
            return
        setattr(
            self.app,
            "_talkops_scroll_suppress_hover_until",
            time.monotonic() + self._HOVER_SUPPRESS_WINDOW_S,
        )
        if self._hover_settle_timer is not None:
            self._hover_settle_timer.stop()
        self._hover_settle_timer = self.set_timer(
            self._HOVER_SUPPRESS_WINDOW_S, self._on_scroll_hover_settled
        )

    def _on_scroll_hover_settled(self) -> None:
        """Restore hover hit-testing once the scroll gesture settles."""
        self._hover_settle_timer = None
        if not self.is_attached or self.app is None:
            return
        setattr(self.app, "_talkops_scroll_suppress_hover_until", 0.0)
        try:
            self.app._update_mouse_over(self.screen)
        except Exception:
            pass

    def _next_wheel_speed(self, direction: int, now: float | None = None) -> int:
        """Compute accelerated wheel scroll lines for ``direction`` (-1 up, +1 down)."""
        ts = time.monotonic() if now is None else now
        if (
            self._last_wheel_dir == direction
            and (ts - self._last_wheel_time) < self._WHEEL_STREAK_WINDOW_S
        ):
            self._wheel_streak += 1
        else:
            self._wheel_streak = 0
        self._last_wheel_time = ts
        self._last_wheel_dir = direction
        vh = self.scrollable_content_region.height if self.is_attached else 0
        return compute_wheel_speed(self._wheel_streak, vh)

    def _queue_wheel_scroll(self, delta: int, now: float | None = None) -> None:
        """Coalesce wheel deltas within a 16ms frame budget."""
        if delta == 0:
            return
        ts = time.monotonic() if now is None else now
        if delta < 0:
            self.on_scroll_up()
        self._mark_user_scrolling()
        if (self._pending_scroll_delta > 0 and delta < 0) or (
            self._pending_scroll_delta < 0 and delta > 0
        ):
            self._pending_scroll_delta = delta
        else:
            self._pending_scroll_delta += delta

        if not self.is_attached:
            self._flush_pending_scroll()
            return

        if (
            self._scroll_flush_timer is None
            and (ts - self._last_scroll_flush_time) >= self._SCROLL_COALESCE_INTERVAL_S
        ):
            self._flush_pending_scroll()
        elif self._scroll_flush_timer is None:
            self._scroll_flush_timer = self.set_timer(
                self._SCROLL_COALESCE_INTERVAL_S, self._flush_pending_scroll
            )

    def _flush_pending_scroll(self) -> None:
        """Flush accumulated wheel delta in a single immediate compositor scroll."""
        if self._scroll_flush_timer is not None:
            self._scroll_flush_timer.stop()
            self._scroll_flush_timer = None
        delta = self._pending_scroll_delta
        self._pending_scroll_delta = 0
        if delta == 0:
            return
        self._last_scroll_flush_time = time.monotonic()
        target_y = max(
            0.0, min(float(self.max_scroll_y), float(self.scroll_y) + delta)
        )
        self.scroll_to(y=target_y, animate=False, immediate=True)
        if delta > 0 and (self.max_scroll_y - self.scroll_y) <= self._STICKY_BOTTOM_THRESHOLD:
            self._auto_scroll_locked = True
        elif delta < 0 and (self.max_scroll_y - self.scroll_y) > self._STICKY_BOTTOM_THRESHOLD:
            self._auto_scroll_locked = False
        if delta < 0:
            self._maybe_hydrate_older()

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        """Handle mouse/trackpad scroll up with WheelSpeed + 16ms coalescing."""
        if not self.allow_vertical_scroll:
            return
        self.on_scroll_up()
        if self.scroll_y > 0 or self._pending_scroll_delta > 0:
            speed = self._next_wheel_speed(-1)
            self._queue_wheel_scroll(-speed)
            event.stop()
        elif self._archived_entries:
            self._maybe_hydrate_older()
            event.stop()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        """Handle mouse/trackpad scroll down with WheelSpeed + 16ms coalescing."""
        if not self.allow_vertical_scroll:
            return
        if self.scroll_y < self.max_scroll_y or self._pending_scroll_delta < 0:
            speed = self._next_wheel_speed(1)
            self._queue_wheel_scroll(speed)
            event.stop()
        else:
            self._auto_scroll_locked = True

    def action_scroll_up(self) -> None:
        """Scroll up one line immediately and unlock auto-scroll."""
        if not self.allow_vertical_scroll:
            return
        self.on_scroll_up()
        self._mark_user_scrolling()
        self.scroll_up(animate=False, immediate=True)
        self._maybe_hydrate_older()

    def action_scroll_down(self) -> None:
        """Scroll down one line immediately and re-lock if at bottom."""
        if not self.allow_vertical_scroll:
            return
        self._mark_user_scrolling()
        self.scroll_down(animate=False, immediate=True)
        if (self.max_scroll_y - self.scroll_y) <= self._STICKY_BOTTOM_THRESHOLD:
            self._auto_scroll_locked = True

    def action_page_up(self) -> None:
        """Scroll one page up without animation jitter and unlock auto-scroll."""
        if not self.allow_vertical_scroll:
            return
        self.on_scroll_up()
        self._mark_user_scrolling()
        self.scroll_page_up(animate=False)
        self._maybe_hydrate_older()

    def action_page_down(self) -> None:
        """Scroll one page down without animation jitter and re-lock at bottom."""
        if not self.allow_vertical_scroll:
            return
        self._mark_user_scrolling()
        self.scroll_page_down(animate=False)
        if (self.max_scroll_y - self.scroll_y) <= self._STICKY_BOTTOM_THRESHOLD:
            self._auto_scroll_locked = True

    def action_scroll_home(self) -> None:
        """Jump to top of transcript and unlock auto-scroll."""
        if not self._allow_scroll:
            return
        self.on_scroll_up()
        self._mark_user_scrolling()
        self.scroll_home(animate=False, immediate=True, x_axis=False)
        self._maybe_hydrate_older()

    def action_scroll_end(self) -> None:
        """Jump to bottom of transcript and re-lock auto-scroll."""
        if not self._allow_scroll:
            return
        self._auto_scroll_locked = True
        self._mark_user_scrolling()
        self.scroll_end(animate=False, immediate=True, x_axis=False)

    def _on_scroll_up(self, event: Any) -> None:
        """Handle scrollbar track page-up click."""
        if self.allow_vertical_scroll:
            self.on_scroll_up()
            self._mark_user_scrolling()
            self.scroll_page_up(animate=False)
            self._maybe_hydrate_older()
            event.stop()

    def _on_scroll_down(self, event: Any) -> None:
        """Handle scrollbar track page-down click."""
        if self.allow_vertical_scroll:
            self._mark_user_scrolling()
            self.scroll_page_down(animate=False)
            if (self.max_scroll_y - self.scroll_y) <= self._STICKY_BOTTOM_THRESHOLD:
                self._auto_scroll_locked = True
            event.stop()

    def _on_scroll_to(self, message: Any) -> None:
        """Handle scrollbar thumb drag without 0.1s animation lag."""
        if self._allow_scroll:
            self._mark_user_scrolling()
            if message.y is not None:
                if message.y < self.scroll_y:
                    self.on_scroll_up()
                elif (self.max_scroll_y - message.y) <= self._STICKY_BOTTOM_THRESHOLD:
                    self._auto_scroll_locked = True
            self.scroll_to(message.x, message.y, animate=False, immediate=True)
            if message.y is not None and message.y <= 6:
                self._maybe_hydrate_older()
            message.stop()

    async def load_virtualized_history(self, entries: list[HistoryEntry]) -> None:
        """Mount only the most recent window of historical entries in a single batch."""
        for e in entries:
            if e.kind == "tool_call":
                self._tool_call_total += 1
            elif e.kind == "tool_group" and e.tool_group_specs:
                self._tool_call_total += len(e.tool_group_specs)

        if len(entries) > self.INITIAL_HISTORY_WINDOW:
            self._archived_entries = list(entries[: -self.INITIAL_HISTORY_WINDOW])
            tail = entries[-self.INITIAL_HISTORY_WINDOW :]
        else:
            self._archived_entries = []
            tail = list(entries)

        widgets: list[Widget] = []
        for entry in tail:
            w = entry.to_widget()
            if isinstance(w, ToolCallMessage) and w._call_id:
                self._tool_calls[w._call_id] = w
            if self._timestamps_visible:
                fn = getattr(w, "set_timestamp_visible", None)
                if callable(fn):
                    fn(True)
            widgets.append(w)

        if widgets:
            loading = (
                getattr(self.app, "_loading_widget", None)
                if self.is_attached and self.app
                else None
            )
            batch_ctx = (
                self.app.batch_update()
                if (self.is_attached and self.app is not None)
                else nullcontext()
            )
            with batch_ctx:
                if (
                    loading is not None
                    and getattr(loading, "is_attached", False)
                    and loading.parent is self
                ):
                    await self.mount_all(widgets, before=loading)
                else:
                    await self.mount_all(widgets)

        self._auto_scroll_locked = True
        self._scroll_to_end()

    def _maybe_hydrate_older(self) -> None:
        """Schedule lazy hydration of older archived messages when scrolled near top."""
        if (
            not self._archived_entries
            or self._hydrating_older
            or not self.is_attached
            or self.scroll_y > 6
        ):
            return
        self._hydrating_older = True
        self.run_worker(self._hydrate_older_batch(), exclusive=False)

    async def _hydrate_older_batch(self) -> None:
        """Mount a batch of older archived entries at the top while preserving viewport position."""
        from talkops.ui.widgets.welcome import WelcomeBanner

        try:
            if not self._archived_entries or not self.is_attached:
                return
            count = min(self.HYDRATE_BATCH_SIZE, len(self._archived_entries))
            batch = self._archived_entries[-count:]
            del self._archived_entries[-count:]

            widgets: list[Widget] = []
            for entry in batch:
                w = entry.to_widget()
                if isinstance(w, ToolCallMessage) and w._call_id:
                    self._tool_calls[w._call_id] = w
                if self._timestamps_visible:
                    fn = getattr(w, "set_timestamp_visible", None)
                    if callable(fn):
                        fn(True)
                widgets.append(w)

            if not widgets:
                return

            anchor: Widget | None = None
            for child in self.children:
                if not isinstance(child, WelcomeBanner):
                    anchor = child
                    break

            old_max_y = float(self.max_scroll_y)
            old_scroll_y = float(self.scroll_y)

            batch_ctx = (
                self.app.batch_update()
                if (self.is_attached and self.app is not None)
                else nullcontext()
            )
            with batch_ctx:
                if anchor is not None and anchor.is_attached and anchor.parent is self:
                    await self.mount_all(widgets, before=anchor)
                else:
                    await self.mount_all(widgets)

            def _restore_scroll() -> None:
                if not self.is_attached:
                    return
                added_height = max(0.0, float(self.max_scroll_y) - old_max_y)
                if added_height > 0:
                    self.scroll_to(
                        y=old_scroll_y + added_height,
                        animate=False,
                        immediate=True,
                    )

            self.call_after_refresh(_restore_scroll)
        except Exception:
            logger.debug("Older history hydration failed", exc_info=True)
        finally:
            self._hydrating_older = False

    def _widget_to_history_entry(self, widget: Widget) -> HistoryEntry | None:
        """Convert a settled top-level widget into an archivable `HistoryEntry`."""
        from talkops.ui.widgets.status_messages import CompactionMessage

        if isinstance(widget, UserMessage):
            return HistoryEntry(
                kind="user",
                text=getattr(widget, "_raw_content", "") or "",
            )
        if isinstance(widget, AssistantMessage) and widget is not self._current_assistant:
            txt = getattr(widget, "content_text", None) or "".join(
                getattr(widget, "_fragments", [])
            )
            return HistoryEntry(kind="assistant", text=txt)
        if isinstance(widget, ThinkingMessage) and widget is not self._current_thinking:
            return HistoryEntry(
                kind="thinking",
                text=getattr(widget, "_content", "") or "",
            )
        if (
            isinstance(widget, ToolGroupSummary)
            and widget is not self._active_tool_group
            and widget._finalized
        ):
            specs: list[dict[str, Any]] = []
            for t in widget._tools:
                specs.append(
                    {
                        "name": t.tool_name,
                        "call_id": getattr(t, "_call_id", "") or "",
                        "args": dict(getattr(t, "_args", {}) or {}),
                        "result": getattr(t, "_output", None)
                        or getattr(t, "_deferred_output", None)
                        or "",
                        "success": not t.is_failed,
                    }
                )
            specs.extend(dict(s) for s in widget._lazy_specs)
            return HistoryEntry(kind="tool_group", tool_group_specs=specs)
        if (
            isinstance(widget, ToolCallMessage)
            and not widget.has_class("-grouped")
            and not widget.is_pending
        ):
            spec: dict[str, Any] = {
                "name": widget.tool_name,
                "call_id": getattr(widget, "_call_id", "") or "",
                "args": dict(getattr(widget, "_args", {}) or {}),
            }
            if getattr(widget, "_status", "") == "rejected":
                spec["rejected"] = True
            else:
                spec["result"] = (
                    getattr(widget, "_output", None)
                    or getattr(widget, "_deferred_output", None)
                    or ""
                )
                spec["success"] = widget.is_success
            return HistoryEntry(kind="tool_call", tool_spec=spec)
        if isinstance(widget, SkillMessage):
            return HistoryEntry(
                kind="skill",
                skill_kwargs={
                    "skill_name": getattr(widget, "_skill_name", ""),
                    "description": getattr(widget, "_description", ""),
                    "source": getattr(widget, "_source", "built-in"),
                    "body": getattr(widget, "_body", ""),
                    "args": getattr(widget, "_args", ""),
                },
            )
        if isinstance(widget, CompactionMessage):
            return HistoryEntry(
                kind="compaction",
                compaction_kwargs={
                    "steps_summarized": getattr(widget, "_steps_summarized", 0),
                    "pre_tokens": getattr(widget, "_pre_tokens", 0),
                    "post_tokens": getattr(widget, "_post_tokens", 0),
                    "summary_tokens": getattr(widget, "_summary_tokens", 0),
                    "duration_seconds": getattr(widget, "_duration_seconds", 0.0),
                },
            )
        if isinstance(widget, SystemMessage):
            return HistoryEntry(
                kind="system",
                text=str(getattr(widget, "_content", "") or ""),
            )
        return None

    def _prune_excess_mounted_items(self) -> None:
        """Evict oldest settled top-level items into `_archived_entries` when DOM exceeds cap."""
        if (
            not self._auto_scroll_locked
            or self._hydrating_older
            or len(self.children) <= self.MAX_MOUNTED_ITEMS + 20
        ):
            return
        from talkops.ui.widgets.welcome import WelcomeBanner

        loading = (
            getattr(self.app, "_loading_widget", None)
            if self.is_attached and self.app
            else None
        )
        top_level: list[Widget] = [
            c
            for c in self.children
            if not isinstance(c, WelcomeBanner)
            and c is not loading
            and not c.has_class("-grouped")
        ]
        excess = len(top_level) - self.MAX_MOUNTED_ITEMS
        if excess <= 0:
            return

        to_remove: list[Widget] = []
        for child in top_level[:excess]:
            entry = self._widget_to_history_entry(child)
            if entry is None:
                # Stop pruning at first live/interactive widget so transcript order stays exact
                break
            self._archived_entries.append(entry)
            to_remove.append(child)
            if isinstance(child, ToolGroupSummary):
                for member in list(child._collapsible):
                    cid = getattr(member, "_call_id", "")
                    if cid:
                        self._tool_calls.pop(cid, None)
                        self._tool_group_by_call_id.pop(cid, None)
                    if member.is_attached:
                        to_remove.append(member)
            elif isinstance(child, ToolCallMessage):
                cid = getattr(child, "_call_id", "")
                if cid:
                    self._tool_calls.pop(cid, None)
                    self._tool_group_by_call_id.pop(cid, None)

        if to_remove:
            self.remove_children(to_remove)

    async def mount_inline_prompt(self, widget: Widget) -> None:
        """Mount an inline prompt widget into the message transcript area."""
        loading = (
            getattr(self.app, "_loading_widget", None)
            if self.is_attached and self.app
            else None
        )
        if (
            loading is not None
            and getattr(loading, "is_attached", False)
            and loading.parent is self
            and widget is not loading
        ):
            await self.mount(widget, before=loading)
        else:
            await self.mount(widget)
        if self._auto_scroll_locked:
            self._scroll_to_end()

    def add_thinking_message(
        self, content: str = "", duration_seconds: float = 0.0, *, live: bool = False
    ) -> ThinkingMessage:
        """Add a thinking message widget to the conversation."""
        self.close_active_tool_group()
        msg = ThinkingMessage(
            content=content, duration_seconds=duration_seconds, live=live
        )
        self._mount_child(msg)
        if self._auto_scroll_locked:
            self._scroll_to_end()
        return msg

    def append_thinking_token(
        self, token: str, duration_seconds: float = 0.0
    ) -> None:
        """Append a token to the current (live) thinking message."""
        if self._current_thinking is None:
            self._current_thinking = self.add_thinking_message(
                "", duration_seconds, live=True
            )
            self._accumulated_thinking = ""
        self._accumulated_thinking += token
        self._current_thinking.update_thinking(
            self._accumulated_thinking, duration_seconds
        )
        if self._auto_scroll_locked:
            self._scroll_to_end()

    def _end_thinking(self) -> None:
        """Collapse the live thinking block, if any."""
        current = self._current_thinking
        self._current_thinking = None
        if current is not None:
            try:
                current.finish()
            except Exception:
                logger.exception("Failed to finish thinking block")

    def finish_thinking(self) -> None:
        """Finalize and collapse the current live thinking block, if any."""
        self._end_thinking()

    def close_active_tool_group(self) -> None:
        """Close the active inline tool group so subsequent tool calls form a new group."""
        group = self._active_tool_group
        self._active_tool_group = None
        if group is not None and getattr(group, "is_attached", False):
            try:
                group.close()
            except Exception:
                logger.exception("Failed to close active tool group")

    def reveal_pending_tool_calls(self) -> None:
        """Surface grouped tool calls before asking for approval."""
        group = self._active_tool_group
        self.close_active_tool_group()
        if group is None or not getattr(group, "is_attached", False):
            return
        try:
            group.reveal_pending()
        except Exception:
            logger.exception("Failed to reveal pending tool calls")

    async def regroup_completed_tools(self) -> None:
        """Fold runs of completed tool calls into collapsible group summaries."""
        self.close_active_tool_group()

        run_tools: list[ToolCallMessage] = []
        run_collapsible: list[Widget] = []
        run_anchor: Widget | None = None
        folded_runs_count = 0

        async def flush() -> None:
            """Execute flush."""
            nonlocal run_tools, run_collapsible, run_anchor, folded_runs_count
            if (
                run_tools
                and run_anchor is not None
                and getattr(run_anchor, "is_attached", False)
            ):
                summary = ToolGroupSummary(
                    tools=list(run_tools),
                    collapsible=list(run_collapsible),
                    live=False,
                )
                for t in run_tools:
                    cid = getattr(t, "_call_id", "")
                    if cid:
                        self._tool_group_by_call_id[cid] = summary
                for widget in run_collapsible:
                    widget.add_class("-grouped")
                try:
                    await self.mount(summary, before=run_anchor)
                    folded_runs_count += 1
                except Exception:
                    logger.warning("Failed to mount tool group summary", exc_info=True)
            run_tools = []
            run_collapsible = []
            run_anchor = None


        batch_ctx = (
            self.app.batch_update()
            if (self.is_attached and self.app is not None)
            else nullcontext()
        )
        with batch_ctx:
            for child in list(self.children):
                if isinstance(child, ToolGroupSummary):
                    await flush()
                    if not child._finalized:
                        child.close()
                    continue
                if isinstance(child, ToolCallMessage):
                    groupable = (
                        child.tool_name not in _TOOL_GROUP_EXCLUSIONS
                        and child.is_success
                        and not child.has_class("-grouped")
                    )
                    if not groupable:
                        await flush()
                        continue
                    if run_anchor is None:
                        run_anchor = child
                    run_tools.append(child)
                    run_collapsible.append(child)
                    continue
                await flush()
            await flush()

        self._prune_excess_mounted_items()

        logger.debug(
            "TUI: MessageList.regroup_completed_tools folded %d runs",
            folded_runs_count,
        )

    def add_user_message(self, text: str) -> None:
        """Add a user message and reset auto-scroll."""
        self._auto_scroll_locked = True
        self.close_active_tool_group()
        self._mount_child(UserMessage(text))
        self._prune_excess_mounted_items()
        self._scroll_to_end()

    def add_queued_user_message(self, text: str) -> QueuedUserMessage:
        """Add a queued user message."""
        msg = QueuedUserMessage(text)
        self._mount_child(msg)
        self._scroll_to_end()
        return msg

    def remove_queued_user_messages(self) -> None:
        """Remove any QueuedUserMessage widgets currently in the list."""
        for msg in self.query(QueuedUserMessage):
            msg.remove()

    def start_assistant_message(self) -> None:
        """Start a streaming assistant message."""
        self.close_active_tool_group()
        msg = AssistantMessage()
        self._current_assistant = msg
        self._mount_child(msg)
        self._scroll_to_end()

    def append_assistant_token(self, token: str) -> None:
        """Append a token to current assistant message."""
        self._end_thinking()
        if self._current_assistant is None:
            self.start_assistant_message()
        if self._current_assistant is not None:
            self._current_assistant.append_token(token)

    def update_assistant_message(self, text: str) -> None:
        """Replace current assistant message text with full state."""
        self._end_thinking()
        if self._current_assistant is None:
            self.start_assistant_message()
        if self._current_assistant is not None:
            self._current_assistant.update_text(text)
            if self._auto_scroll_locked:
                self._scroll_to_end()

    def finish_assistant_message(self) -> None:
        """Finalize assistant message."""
        self._end_thinking()
        if self._current_assistant:
            self._current_assistant.finish()
            self._current_assistant = None
            self._prune_excess_mounted_items()
            self._scroll_to_end()

    def add_tool_call(
        self, name: str, call_id: str, args: dict[str, Any], live: bool = True
    ) -> None:
        """Add a tool-call widget."""
        self._end_thinking()
        msg = ToolCallMessage(name, args)
        msg._call_id = call_id or ""
        self._tool_call_total += 1
        if live:
            msg.set_running()
        self._tool_calls[call_id] = msg

        batch_ctx = (
            self.app.batch_update()
            if (self.is_attached and self.app is not None)
            else nullcontext()
        )
        with batch_ctx:
            if live and name not in _TOOL_GROUP_EXCLUSIONS:
                if self._active_tool_group is None or not getattr(
                    self._active_tool_group, "is_attached", False
                ):
                    self._active_tool_group = ToolGroupSummary(live=True)
                    self._mount_child(self._active_tool_group)
                msg.add_class("-grouped")
                msg.display = False
                self._active_tool_group.add_member(msg)
                if call_id:
                    self._tool_group_by_call_id[call_id] = self._active_tool_group
            self._mount_child(msg)
        self._scroll_to_end()

    def update_tool_result(
        self,
        call_id: str,
        result: str,
        success: bool = True,
        name: str | None = None,
        live: bool = True,
    ) -> None:
        """Update a tool-call widget with its result."""
        if call_id and call_id in self._tool_calls:
            msg = self._tool_calls[call_id]
            if success:
                msg.set_success(result)
            else:
                msg.set_error(result)
            group = self._tool_group_by_call_id.get(call_id)
            if group is not None and msg in group._tools:
                group.sync_members()
            elif (
                self._active_tool_group is not None
                and msg in self._active_tool_group._tools
            ):
                self._active_tool_group.sync_members()
            elif msg.has_class("-grouped"):
                for child in list(self.children):
                    if isinstance(child, ToolGroupSummary) and msg in child._tools:
                        self._tool_group_by_call_id[call_id] = child
                        child.sync_members()
                        break
        else:
            msg_name = name if (name and name != "None") else "tool"
            msg = ToolCallMessage(msg_name, {})
            msg._call_id = call_id or ""
            self._tool_call_total += 1
            if call_id:
                self._tool_calls[call_id] = msg

            batch_ctx = (
                self.app.batch_update()
                if (self.is_attached and self.app is not None)
                else nullcontext()
            )
            with batch_ctx:
                if live and msg_name not in _TOOL_GROUP_EXCLUSIONS:
                    if self._active_tool_group is None or not getattr(
                        self._active_tool_group, "is_attached", False
                    ):
                        self._active_tool_group = ToolGroupSummary(live=True)
                        self._mount_child(self._active_tool_group)
                    msg.add_class("-grouped")
                    msg.display = False
                    self._active_tool_group.add_member(msg)
                    if call_id:
                        self._tool_group_by_call_id[call_id] = self._active_tool_group
                self._mount_child(msg)
                if success:
                    msg.set_success(result)
                else:
                    msg.set_error(result)
                if (
                    self._active_tool_group is not None
                    and msg in self._active_tool_group._tools
                ):
                    self._active_tool_group.sync_members()
        self._scroll_to_end()

    def add_skill_message(self, name: str, content: str) -> None:
        """Add a skill loaded card."""
        self._mount_child(SkillMessage(skill_name=name, body=content))
        self._scroll_to_end()

    def add_diff_message(self, patch: str, file_path: str = "") -> None:
        """Add an inline diff message."""
        self._mount_child(DiffMessage(patch, file_path))
        self._scroll_to_end()

    def add_error_message(self, text: str) -> None:
        """Add an error message."""
        self._mount_child(ErrorMessage(text))
        self._scroll_to_end()

    def add_system_message(self, text: str) -> None:
        """Add a system/info message."""
        self._mount_child(SystemMessage(text))
        self._scroll_to_end()

    def has_any_conversation_messages(self) -> bool:
        """Return True if either archived or mounted items contain user/assistant messages."""
        if any(e.kind in {"user", "assistant"} for e in self._archived_entries):
            return True
        return any(
            isinstance(child, (UserMessage, AssistantMessage))
            for child in self.children
        )

    def get_all_thread_messages(self) -> list[Any]:
        """Return LangChain messages across both archived and mounted transcript items."""
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        msgs: list[Any] = []
        for entry in self._archived_entries:
            msgs.extend(entry.to_lc_messages())
        for child in self.children:
            if isinstance(child, UserMessage):
                txt = getattr(child, "_raw_content", "") or ""
                if txt:
                    msgs.append(HumanMessage(content=txt))
            elif isinstance(child, AssistantMessage):
                txt = getattr(child, "content_text", None) or "".join(
                    getattr(child, "_fragments", [])
                )
                if txt:
                    msgs.append(AIMessage(content=txt))
            elif isinstance(child, ToolGroupSummary):
                for cid, out in child.iter_tool_outputs():
                    msgs.append(ToolMessage(content=out, tool_call_id=cid))
            elif isinstance(child, ToolCallMessage) and not child.has_class("-grouped"):
                out = (
                    getattr(child, "_output", None)
                    or getattr(child, "_deferred_output", None)
                    or getattr(child, "_result", None)
                    or ""
                )
                cid = getattr(child, "_call_id", "") or "call_1"
                if out:
                    msgs.append(ToolMessage(content=str(out), tool_call_id=cid))
        return msgs

    def clear(self) -> None:
        """Clear all conversation message widgets while keeping the welcome banner."""
        from talkops.ui.widgets.welcome import WelcomeBanner

        self._stop_scroll_timers()
        self._pending_scroll_delta = 0
        self._scroll_end_pending = False
        self._auto_scroll_locked = True
        self._hydrating_older = False
        self._archived_entries.clear()
        self._tool_group_by_call_id.clear()
        to_remove = [
            child for child in self.children if not isinstance(child, WelcomeBanner)
        ]
        if to_remove:
            self.remove_children(to_remove)
        self._current_assistant = None
        self._current_thinking = None
        self._tool_calls.clear()
        self._active_tool_group = None

    def set_timestamps_visible(self, visible: bool) -> None:
        """Show or hide timestamp footers across all mounted messages."""
        self._timestamps_visible = visible
        for child in self.children:
            fn = getattr(child, "set_timestamp_visible", None)
            if callable(fn):
                fn(visible)

    def _scroll_to_end(self) -> None:
        """Scroll to the end if auto-scroll is locked, coalesced per refresh frame."""
        if not self._auto_scroll_locked:
            return
        if not self.is_attached:
            self.scroll_end(animate=False, x_axis=False)
            return
        if self._is_drag_selecting():
            return
        if self._scroll_end_pending:
            return
        self._scroll_end_pending = True

        def _do_scroll_end() -> None:
            self._scroll_end_pending = False
            if self._is_drag_selecting():
                return
            if self._auto_scroll_locked and self.is_attached:
                self.scroll_end(animate=False, immediate=True, x_axis=False)

        self.call_after_refresh(_do_scroll_end)

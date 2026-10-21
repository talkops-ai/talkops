"""Background task execution and lifecycle management package."""

from __future__ import annotations

from talkops.tasks.manager import (
    DEFAULT_WAIT_MS_BEFORE_ASYNC,
    MAX_WAIT_MS_BEFORE_ASYNC,
    MIN_WAIT_MS_BEFORE_ASYNC,
    BackgroundTaskHandle,
    BackgroundTaskManager,
    DetachedExecResult,
    FastExecResult,
    TaskNotification,
    clamp_wait_ms,
    get_background_task_manager,
)

__all__ = [
    "BackgroundTaskHandle",
    "BackgroundTaskManager",
    "DEFAULT_WAIT_MS_BEFORE_ASYNC",
    "DetachedExecResult",
    "FastExecResult",
    "MAX_WAIT_MS_BEFORE_ASYNC",
    "MIN_WAIT_MS_BEFORE_ASYNC",
    "TaskNotification",
    "clamp_wait_ms",
    "get_background_task_manager",
]

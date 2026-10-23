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

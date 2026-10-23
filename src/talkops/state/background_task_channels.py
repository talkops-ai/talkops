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

"""State channels for non-blocking background shell command execution."""

from __future__ import annotations

from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain.agents.middleware.types import AgentState, OmitFromInput

BackgroundTaskStatus = Literal[
    "running",
    "waiting_for_input",
    "completed",
    "failed",
    "killed",
    "timed_out",
]


class BackgroundTaskRecord(TypedDict):
    """Persisted metadata for a detached background shell command.

    Stored in a dedicated ``background_tasks`` state channel so task IDs,
    PIDs, and log file paths survive conversation compaction.
    """

    task_id: str
    thread_id: str
    command: str
    cwd: str
    pid: int | None
    status: BackgroundTaskStatus
    is_daemon: bool
    started_at: float
    updated_at: float
    completed_at: NotRequired[float | None]
    exit_code: NotRequired[int | None]
    log_path: str
    last_output_tail: NotRequired[str]
    notified: NotRequired[bool]


def merge_background_tasks(
    existing: dict[str, BackgroundTaskRecord] | None,
    new: dict[str, BackgroundTaskRecord] | None,
) -> dict[str, BackgroundTaskRecord]:
    """Reducer that merges background task records by ``task_id``."""
    if not existing:
        return dict(new or {})
    if not new:
        return dict(existing)
    merged: dict[str, Any] = dict(existing)
    for task_id, record in new.items():
        if task_id in merged and isinstance(merged[task_id], dict) and isinstance(record, dict):
            merged[task_id] = {**merged[task_id], **record}
        else:
            merged[task_id] = record
    return merged


class BackgroundTaskChannels(AgentState):
    """State channel tracking detached background commands across turns."""

    background_tasks: Annotated[
        NotRequired[dict[str, BackgroundTaskRecord]],
        OmitFromInput,
        merge_background_tasks,
    ]


__all__ = [
    "BackgroundTaskChannels",
    "BackgroundTaskRecord",
    "BackgroundTaskStatus",
    "merge_background_tasks",
]

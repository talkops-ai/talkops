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

"""``/tasks`` — Session TODO list and non-blocking background shell task manager."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.tasks.manager import get_background_task_manager

TaskStatus = Literal["pending", "in-progress", "done"]


@dataclass
class TaskItem:
    """A single session-scoped TODO item."""

    id: int
    text: str
    status: TaskStatus = "pending"
    created_at: float = field(default_factory=time.time)


class SessionTaskStore:
    """In-memory store for session TODO tasks.

    Session-scoped — all tasks are lost when the session ends.
    This keeps things lightweight; persistent task storage belongs
    in a future persistence layer.
    """

    def __init__(self) -> None:
        """Initialize SessionTaskStore."""
        self._tasks: list[TaskItem] = []
        self._next_id: int = 1

    def add(self, text: str) -> TaskItem:
        """Add a new pending task."""
        task = TaskItem(id=self._next_id, text=text)
        self._tasks.append(task)
        self._next_id += 1
        return task

    def done(self, task_id: int) -> TaskItem | None:
        """Mark a task as done. Returns the task or ``None``."""
        for task in self._tasks:
            if task.id == task_id:
                task.status = "done"
                return task
        return None

    def progress(self, task_id: int) -> TaskItem | None:
        """Mark a task as in-progress."""
        for task in self._tasks:
            if task.id == task_id:
                task.status = "in-progress"
                return task
        return None

    def remove(self, task_id: int) -> bool:
        """Remove a task entirely."""
        for i, task in enumerate(self._tasks):
            if task.id == task_id:
                self._tasks.pop(i)
                return True
        return False

    def clear_done(self) -> int:
        """Remove all completed tasks. Returns count removed."""
        before = len(self._tasks)
        self._tasks = [t for t in self._tasks if t.status != "done"]
        return before - len(self._tasks)

    @property
    def all(self) -> list[TaskItem]:
        """Return the all property."""
        return list(self._tasks)

    @property
    def pending(self) -> list[TaskItem]:
        """Return the pending property."""
        return [t for t in self._tasks if t.status != "done"]


# Module-level singleton so the store persists across commands within a session.
_session_store = SessionTaskStore()


def get_task_store() -> SessionTaskStore:
    """Return the singleton session task store."""
    return _session_store


class TasksHandler(BaseCommandHandler):
    """Session-scoped TODO list and background command task manager.

    Subcommands:
      ``/tasks``                        — list background commands & session TODOs
      ``/tasks ps``                     — list background shell commands
      ``/tasks logs <task_id> [lines]`` — view recent log output of a background command
      ``/tasks input <task_id> <text>`` — send stdin text to a running background command
      ``/tasks kill <task_id>``         — terminate a running background command
      ``/tasks add <text>``             — add a session TODO item
      ``/tasks done <id>``              — mark a TODO item complete
      ``/tasks rm <id>``                — remove a TODO item
      ``/tasks clear``                  — remove completed TODO items
    """

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/tasks"

    @property
    def category(self) -> CommandCategory:
        """Return the command category classification."""
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        """Return the safety level required for this command."""
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        """Return the execution queue bypass tier for this command."""
        return BypassTier.IMMEDIATE

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute the command handler with the provided command context."""
        store = get_task_store()
        args = ctx.args.strip()

        if not args:
            return self._list_tasks(store, ctx)

        sub, _, remainder = args.partition(" ")
        sub = sub.lower()
        remainder = remainder.strip()

        if sub in {"ps", "bg", "processes"}:
            return self._list_background_processes(ctx)

        if sub in {"logs", "log", "output"}:
            return self._show_background_logs(remainder, ctx)

        if sub in {"input", "stdin", "send"}:
            return await self._send_background_input(remainder, ctx)

        if sub in {"kill", "stop", "terminate"}:
            return await self._kill_background_task(remainder, ctx)

        if sub == "add":
            if not remainder:
                return CommandResult(success=False, message="Usage: /tasks add <description>")
            task = store.add(remainder)
            return CommandResult(
                success=True,
                message=f"Added task #{task.id}: {task.text}",
            )

        if sub == "done":
            task_id = _parse_id(remainder)
            if task_id is None:
                return CommandResult(success=False, message="Usage: /tasks done <id>")
            task = store.done(task_id)
            if task is None:
                return CommandResult(success=False, message=f"Task #{task_id} not found.")
            return CommandResult(success=True, message=f"Completed task #{task.id}: {task.text}")

        if sub in {"rm", "remove", "delete"}:
            task_id = _parse_id(remainder)
            if task_id is None:
                return CommandResult(success=False, message="Usage: /tasks rm <id>")
            if store.remove(task_id):
                return CommandResult(success=True, message=f"Removed task #{task_id}.")
            return CommandResult(success=False, message=f"Task #{task_id} not found.")

        if sub == "clear":
            count = store.clear_done()
            if count == 0:
                return CommandResult(success=True, message="No completed tasks to clear.")
            return CommandResult(success=True, message=f"Cleared {count} completed task(s).")

        # Unknown subcommand — treat first token as task text (convenience)
        task = store.add(args)
        return CommandResult(
            success=True,
            message=f"Added task #{task.id}: {task.text}",
        )

    def _list_background_processes(self, ctx: CommandContext) -> CommandResult:
        manager = get_background_task_manager()
        thread_id = getattr(ctx, "thread_id", None)
        bg_tasks = manager.list_tasks(thread_id) if thread_id else manager.list_tasks()
        if not bg_tasks and thread_id:
            bg_tasks = manager.list_tasks()

        if not bg_tasks:
            return CommandResult(
                success=True,
                message="No background shell commands in this session.",
            )

        lines = ["### Background Shell Commands", ""]
        now = time.time()
        for rec in bg_tasks:
            tid = rec["task_id"]
            status = rec["status"]
            cmd = rec["command"]
            pid = rec.get("pid")
            exit_code = rec.get("exit_code")
            started_at = float(rec.get("started_at") or now)
            completed_at = rec.get("completed_at")
            end_ts = float(completed_at) if isinstance(completed_at, (int, float)) else now
            elapsed = max(0.0, end_ts - started_at)
            daemon_tag = " `[daemon]`" if rec.get("is_daemon") else ""
            exit_tag = f" (exit `{exit_code}`)" if exit_code is not None else ""
            lines.append(
                f"- `{tid}`{daemon_tag} — **{status}**{exit_tag} · pid `{pid}` · `{elapsed:.1f}s` · `{cmd}`"
            )
            lines.append(f"  log: `{rec.get('log_path')}`")
        return CommandResult(success=True, message="\n".join(lines))

    def _show_background_logs(self, remainder: str, ctx: CommandContext) -> CommandResult:
        parts = remainder.split()
        if not parts:
            return CommandResult(
                success=False,
                message="Usage: /tasks logs <task_id> [lines]",
            )
        task_id = parts[0].strip()
        tail_lines = 80
        if len(parts) > 1:
            try:
                tail_lines = max(5, min(1000, int(parts[1])))
            except ValueError:
                pass

        manager = get_background_task_manager()
        handle = manager.get_task(task_id, thread_id=getattr(ctx, "thread_id", None))
        if handle is None:
            handle = manager.get_task(task_id)
        if handle is None:
            return CommandResult(
                success=False,
                message=f"Background task `{task_id}` not found.",
            )

        output = handle.get_output(tail_chars=16000, tail_lines=tail_lines).strip() or "(no output)"
        return CommandResult(
            success=True,
            message=(
                f"### Logs for `{handle.task_id}` ({handle.status})\n"
                f"Command: `{handle.command}`\n"
                f"Log file: `{handle.log_path}`\n\n"
                f"```text\n{output}\n```"
            ),
        )

    async def _send_background_input(
        self,
        remainder: str,
        ctx: CommandContext,
    ) -> CommandResult:
        task_id, _, input_text = remainder.partition(" ")
        task_id = task_id.strip()
        if not task_id or not input_text:
            return CommandResult(
                success=False,
                message="Usage: /tasks input <task_id> <text>",
            )
        manager = get_background_task_manager()
        ok, msg, _ = await manager.send_input(
            task_id,
            input_text,
            thread_id=getattr(ctx, "thread_id", None),
        )
        return CommandResult(success=ok, message=msg)

    async def _kill_background_task(
        self,
        remainder: str,
        ctx: CommandContext,
    ) -> CommandResult:
        task_id = remainder.strip()
        if not task_id:
            return CommandResult(
                success=False,
                message="Usage: /tasks kill <task_id>",
            )
        manager = get_background_task_manager()
        ok, msg, _ = await manager.kill_task(
            task_id,
            thread_id=getattr(ctx, "thread_id", None),
        )
        return CommandResult(success=ok, message=msg)

    def _list_tasks(self, store: SessionTaskStore, ctx: CommandContext) -> CommandResult:
        """Format and return background shell commands and session TODOs."""
        manager = get_background_task_manager()
        thread_id = getattr(ctx, "thread_id", None)
        bg_tasks = manager.list_tasks(thread_id) if thread_id else manager.list_tasks()
        if not bg_tasks and thread_id:
            bg_tasks = manager.list_tasks()

        tasks = store.all
        if not tasks and not bg_tasks:
            return CommandResult(
                success=True,
                message=(
                    "No tasks yet. Use `/tasks add <description>` for session TODOs "
                    "or `/tasks ps` for background shell commands."
                ),
            )

        lines: list[str] = []
        if bg_tasks:
            lines.append("### Background Shell Commands")
            lines.append("")
            now = time.time()
            for rec in bg_tasks:
                tid = rec["task_id"]
                status = rec["status"]
                cmd = rec["command"]
                started_at = float(rec.get("started_at") or now)
                completed_at = rec.get("completed_at")
                end_ts = float(completed_at) if isinstance(completed_at, (int, float)) else now
                elapsed = max(0.0, end_ts - started_at)
                exit_code = rec.get("exit_code")
                exit_tag = f" (exit `{exit_code}`)" if exit_code is not None else ""
                lines.append(
                    f"- `{tid}`: **{status}**{exit_tag} · `{elapsed:.1f}s` · `{cmd}`"
                )
            if tasks:
                lines.append("")

        if tasks:
            lines.append("### Session Tasks")
            lines.append("")
            for t in tasks:
                if t.status == "done":
                    marker = "- [x]"
                    style = "~~"
                elif t.status == "in-progress":
                    marker = "- [ ]"
                    style = "**"
                else:
                    marker = "- [ ]"
                    style = ""

                text = f"{style}{t.text}{style}" if style else t.text
                if t.status == "in-progress":
                    text = f"{text} *(in progress)*"
                lines.append(f"{marker} `#{t.id}` {text}")

            pending = len(store.pending)
            total = len(tasks)
            done = total - pending
            lines.append("")
            lines.append(f"_{done}/{total} completed_")

        return CommandResult(success=True, message="\n".join(lines))


def _parse_id(s: str) -> int | None:
    """Parse a task ID, stripping optional '#' prefix."""
    s = s.strip().lstrip("#")
    try:
        return int(s)
    except (ValueError, TypeError):
        return None

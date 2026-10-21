"""Non-blocking background process execution manager and notification hub.

Provides Antigravity-style ``wait_ms_before_async`` command execution:
- Short commands that finish within ``wait_ms_before_async`` return synchronously
  as normal tool outputs (fast path).
- Long-running commands automatically detach into background tasks with
  persistent disk logs, ring-buffered live output, interactive stdin support,
  SSE event broadcasting, and reactive agent completion notifications.
"""

from __future__ import annotations

import asyncio
import collections
import os
import re
import signal
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping

from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from talkops.state.background_task_channels import (
        BackgroundTaskRecord,
        BackgroundTaskStatus,
    )

logger = get_logger(__name__)

DEFAULT_WAIT_MS_BEFORE_ASYNC = 5000
MIN_WAIT_MS_BEFORE_ASYNC = 100
MAX_WAIT_MS_BEFORE_ASYNC = 10000
DEFAULT_BACKGROUND_HARD_TIMEOUT_SECONDS = 3600.0
MAX_RING_BUFFER_CHARS = 131_072  # 128 KB in-memory tail buffer

# Heuristic patterns indicating a command is blocked waiting for interactive stdin
_STDIN_PROMPT_RE = re.compile(
    r"(?:"
    r"(?:\[y/n\]|\[yes/no\]|\(y/n\)|\(yes/no\))\s*:?"
    r"|password\s*:|passphrase\s*:|token\s*:|secret\s*:"
    r"|enter\s+(?:a\s+|the\s+|your\s+)?[\w\s\-]{1,35}:"
    r"|continue\?\s*:?|proceed\?\s*:?|overwrite.*\?\s*:?"
    r"|do you want to [^\n]{1,80}\?\s*(?:\[[^\]]+\]\s*:?)?"
    r"|are you sure [^\n]{1,80}\?\s*(?:\[[^\]]+\]\s*:?)?"
    r"|press enter to continue\s*:?"
    r")\s*$",
    re.IGNORECASE,
)


def clamp_wait_ms(wait_ms: int | float | None) -> int:
    """Clamp ``wait_ms_before_async`` into the valid ``[100, 10000]`` ms window."""
    if wait_ms is None:
        return DEFAULT_WAIT_MS_BEFORE_ASYNC
    try:
        val = int(wait_ms)
    except (TypeError, ValueError):
        return DEFAULT_WAIT_MS_BEFORE_ASYNC
    return max(MIN_WAIT_MS_BEFORE_ASYNC, min(MAX_WAIT_MS_BEFORE_ASYNC, val))


def _default_tasks_root() -> Path:
    """Return the root directory for background task log files."""
    base = Path(os.environ.get("TALKOPS_HOME", Path.home() / ".talkops"))
    root = base / "runtime" / "tasks"
    root.mkdir(parents=True, exist_ok=True)
    return root


@dataclass
class TaskNotification:
    """Notification queued for the agent when a background task changes state."""

    notification_id: str
    task_id: str
    thread_id: str
    command: str
    kind: Literal["completed", "failed", "waiting_for_input", "timed_out", "killed"]
    status: BackgroundTaskStatus
    exit_code: int | None
    started_at: float
    timestamp: float
    duration_seconds: float
    log_path: str
    output_tail: str
    is_daemon: bool = False

    def format_for_agent(self) -> str:
        """Render the notification as a structured block for the LLM."""
        elapsed = f"{self.duration_seconds:.1f}s"
        exit_str = f"exit_code={self.exit_code}" if self.exit_code is not None else "running"
        header = (
            f"<background_task_notification "
            f'task_id="{self.task_id}" '
            f'kind="{self.kind}" '
            f'status="{self.status}" '
            f'{exit_str} '
            f'elapsed="{elapsed}" '
            f'log_path="{self.log_path}">'
        )
        tail = self.output_tail.strip() or "(no output)"
        if len(tail) > 6000:
            tail = "...[truncated]...\n" + tail[-6000:]
        guidance = ""
        if self.kind == "waiting_for_input":
            guidance = (
                "\nAction Required: The background command appears to be waiting for stdin input. "
                "Use `manage_task(action=\"send_input\", task_id=\""
                f"{self.task_id}\", input_text=\"...\")` to send input, or ask the user if needed."
            )
        return (
            f"{header}\n"
            f"Command: {self.command}\n"
            f"Recent Output:\n{tail}"
            f"{guidance}\n"
            f"</background_task_notification>"
        )


@dataclass
class FastExecResult:
    """Returned when a command finishes within ``wait_ms_before_async``."""

    command: str
    output: str
    exit_code: int
    duration_seconds: float
    truncated: bool = False


@dataclass
class DetachedExecResult:
    """Returned when a command exceeds ``wait_ms_before_async`` and is backgrounded."""

    handle: BackgroundTaskHandle
    partial_output: str
    wait_ms: int


@dataclass
class BackgroundTaskHandle:
    """Live runtime handle for a running or completed background command."""

    task_id: str
    thread_id: str
    command: str
    cwd: str
    log_path: Path
    is_daemon: bool = False
    pid: int | None = None
    status: BackgroundTaskStatus = "running"
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    exit_code: int | None = None
    notified: bool = False

    proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    stdout_task: asyncio.Task[None] | None = field(default=None, repr=False)
    monitor_task: asyncio.Task[None] | None = field(default=None, repr=False)

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _chunks: collections.deque[str] = field(default_factory=collections.deque, repr=False)
    _total_chars: int = field(default=0, repr=False)
    _last_output_at: float = field(default_factory=time.time, repr=False)
    _last_prompt_notified_len: int = field(default=-1, repr=False)
    _detached: bool = field(default=False, repr=False)

    def append_output(self, text: str) -> None:
        """Append decoded output to the in-memory ring buffer."""
        if not text:
            return
        now = time.time()
        with self._lock:
            self._chunks.append(text)
            self._total_chars += len(text)
            while self._total_chars > MAX_RING_BUFFER_CHARS and len(self._chunks) > 1:
                removed = self._chunks.popleft()
                self._total_chars -= len(removed)
            self._last_output_at = now
            self.updated_at = now
            if self.status == "waiting_for_input":
                self.status = "running"

    def get_output(
        self,
        *,
        tail_chars: int = 8000,
        tail_lines: int | None = None,
    ) -> str:
        """Return recent output from the ring buffer (or disk log fallback)."""
        with self._lock:
            combined = "".join(self._chunks)
        if not combined and self.log_path.exists():
            try:
                combined = self.log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                combined = ""
        if tail_lines is not None and tail_lines > 0:
            lines = combined.splitlines()
            if len(lines) > tail_lines:
                combined = "\n".join(lines[-tail_lines:])
        if tail_chars > 0 and len(combined) > tail_chars:
            combined = combined[-tail_chars:]
        return combined

    def last_non_empty_line(self) -> str:
        """Return the most recent non-empty line of output for UI badges."""
        with self._lock:
            combined = "".join(self._chunks)
        for line in reversed(combined.splitlines()):
            cleaned = line.strip()
            if cleaned:
                return cleaned[:160]
        return ""

    def elapsed_seconds(self) -> float:
        """Return elapsed execution time in seconds."""
        end = self.completed_at if self.completed_at is not None else time.time()
        return max(0.0, end - self.started_at)

    def to_record(self) -> BackgroundTaskRecord:
        """Serialize to a state-safe ``BackgroundTaskRecord`` dictionary."""
        tail = self.get_output(tail_chars=1200, tail_lines=25)
        record: BackgroundTaskRecord = {
            "task_id": self.task_id,
            "thread_id": self.thread_id,
            "command": self.command,
            "cwd": self.cwd,
            "pid": self.pid,
            "status": self.status,
            "is_daemon": self.is_daemon,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "exit_code": self.exit_code,
            "log_path": str(self.log_path),
            "last_output_tail": tail,
            "notified": self.notified,
        }
        return record


class BackgroundTaskManager:
    """Singleton manager for detached background shell commands and notifications."""

    def __init__(self, tasks_root: Path | None = None) -> None:
        self._tasks_root = tasks_root or _default_tasks_root()
        self._lock = threading.RLock()
        self._tasks_by_thread: dict[str, dict[str, BackgroundTaskHandle]] = (
            collections.defaultdict(dict)
        )
        self._tasks_by_id: dict[str, BackgroundTaskHandle] = {}
        self._notifications: dict[str, collections.deque[TaskNotification]] = (
            collections.defaultdict(collections.deque)
        )
        self._subscribers: dict[
            str, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]]
        ] = collections.defaultdict(list)

    # ── Execution & WaitMsBeforeAsync Race ──────────────────────────────────

    async def run_or_detach(
        self,
        *,
        thread_id: str,
        command: str,
        cwd: str | Path,
        env: Mapping[str, str] | None = None,
        wait_ms_before_async: int | None = DEFAULT_WAIT_MS_BEFORE_ASYNC,
        timeout: float | None = None,
        is_daemon: bool = False,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
    ) -> FastExecResult | DetachedExecResult:
        """Execute ``command`` and race completion against ``wait_ms_before_async``.

        If the command finishes before ``wait_ms_before_async`` (and ``is_daemon``
        is False), returns a ``FastExecResult`` synchronously. Otherwise detaches
        the process into a tracked ``BackgroundTaskHandle`` and returns a
        ``DetachedExecResult`` immediately so the agent turn is not blocked.
        """
        effective_thread_id = str(thread_id or "default").strip() or "default"
        wait_ms = clamp_wait_ms(wait_ms_before_async)
        task_id = f"cmd-{uuid.uuid4().hex[:8]}"
        cwd_str = str(Path(cwd).resolve())

        thread_dir = self._tasks_root / self._sanitize_thread_id(effective_thread_id)
        thread_dir.mkdir(parents=True, exist_ok=True)
        log_path = thread_dir / f"{task_id}.log"

        proc_env = dict(os.environ)
        if env:
            proc_env.update({str(k): str(v) for k, v in env.items()})
        proc_env.setdefault("PAGER", "cat")
        proc_env.setdefault("GIT_PAGER", "cat")

        started_at = time.time()
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd_str,
            env=proc_env,
            start_new_session=True,
        )

        handle = BackgroundTaskHandle(
            task_id=task_id,
            thread_id=effective_thread_id,
            command=command,
            cwd=cwd_str,
            log_path=log_path,
            is_daemon=bool(is_daemon),
            pid=proc.pid,
            status="running",
            started_at=started_at,
            updated_at=started_at,
            proc=proc,
        )

        # Start draining stdout/stderr immediately
        handle.stdout_task = asyncio.create_task(
            self._pump_stdout(handle, on_event=on_event),
            name=f"talkops-task-stdout-{task_id}",
        )

        # Daemon processes detach after a brief startup check (min 400ms or wait_ms)
        race_seconds = (
            min(wait_ms / 1000.0, 0.5) if is_daemon else (wait_ms / 1000.0)
        )
        wait_proc_task = asyncio.create_task(proc.wait())
        timer_task = asyncio.create_task(asyncio.sleep(race_seconds))

        try:
            done, _ = await asyncio.wait(
                {wait_proc_task, timer_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            wait_proc_task.cancel()
            timer_task.cancel()
            await self._terminate_process(handle)
            raise

        if wait_proc_task in done and not is_daemon:
            # Fast path: command completed within wait_ms_before_async!
            timer_task.cancel()
            exit_code = wait_proc_task.result()
            if handle.stdout_task is not None:
                try:
                    await asyncio.wait_for(handle.stdout_task, timeout=0.5)
                except (TimeoutError, asyncio.CancelledError):
                    pass
            duration = max(0.0, time.time() - started_at)
            output = handle.get_output(tail_chars=0)
            # Clean up log file for fast-path commands so disk stays clean
            try:
                if log_path.exists():
                    log_path.unlink()
            except OSError:
                pass
            return FastExecResult(
                command=command,
                output=output,
                exit_code=int(exit_code if exit_code is not None else 0),
                duration_seconds=duration,
            )

        # Detached path: process is still running (or is_daemon=True)
        timer_task.cancel()
        if not wait_proc_task.done():
            wait_proc_task.cancel()

        with self._lock:
            handle._detached = True
            self._tasks_by_thread[effective_thread_id][task_id] = handle
            self._tasks_by_id[task_id] = handle

        hard_timeout = (
            None
            if is_daemon
            else (
                float(timeout)
                if timeout is not None and timeout > 0
                else DEFAULT_BACKGROUND_HARD_TIMEOUT_SECONDS
            )
        )
        handle.monitor_task = asyncio.create_task(
            self._monitor_task(handle, hard_timeout=hard_timeout, on_event=on_event),
            name=f"talkops-task-monitor-{task_id}",
        )

        start_event = self._build_event(handle, phase="start")
        self._publish_event(effective_thread_id, start_event)
        if on_event is not None:
            self._safe_invoke_callback(on_event, start_event)

        partial = handle.get_output(tail_chars=4000, tail_lines=60)
        return DetachedExecResult(
            handle=handle,
            partial_output=partial,
            wait_ms=wait_ms,
        )

    # ── Background Streamers & Monitors ─────────────────────────────────────

    async def _pump_stdout(
        self,
        handle: BackgroundTaskHandle,
        *,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        """Continuously drain process stdout into the ring buffer and disk log."""
        proc = handle.proc
        if proc is None or proc.stdout is None:
            return

        last_emit = 0.0
        try:
            with handle.log_path.open("a", encoding="utf-8", errors="replace") as log_file:
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    handle.append_output(text)
                    try:
                        log_file.write(text)
                        log_file.flush()
                    except OSError:
                        pass

                    now = time.time()
                    if handle._detached and (now - last_emit) >= 0.25:
                        last_emit = now
                        evt = self._build_event(handle, phase="output")
                        self._publish_event(handle.thread_id, evt)
                        if on_event is not None:
                            self._safe_invoke_callback(on_event, evt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Stdout pump error for %s: %s", handle.task_id, exc)

    async def _monitor_task(
        self,
        handle: BackgroundTaskHandle,
        *,
        hard_timeout: float | None = None,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        """Monitor a detached process until exit, timeout, or kill."""
        proc = handle.proc
        if proc is None:
            return

        try:
            while proc.returncode is None:
                try:
                    await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=0.5)
                    break
                except TimeoutError:
                    pass

                now = time.time()
                # 1. Check hard timeout for non-daemon tasks
                if (
                    hard_timeout is not None
                    and (now - handle.started_at) >= hard_timeout
                    and proc.returncode is None
                ):
                    handle.status = "timed_out"
                    await self._terminate_process(handle)
                    break

                # 2. Check interactive stdin prompt heuristic
                self._check_stdin_prompt(handle, now=now, on_event=on_event)

            # Process has exited; ensure stdout pump finishes draining
            if handle.stdout_task is not None:
                try:
                    await asyncio.wait_for(handle.stdout_task, timeout=1.0)
                except (TimeoutError, asyncio.CancelledError):
                    pass

            exit_code = proc.returncode
            completed_at = time.time()
            with self._lock:
                handle.exit_code = int(exit_code) if exit_code is not None else -1
                handle.completed_at = completed_at
                handle.updated_at = completed_at
                if handle.status not in ("killed", "timed_out"):
                    handle.status = "completed" if handle.exit_code == 0 else "failed"

            should_wake = not handle.is_daemon and handle.status != "killed"
            if should_wake:
                notification = TaskNotification(
                    notification_id=f"ntf-{uuid.uuid4().hex[:8]}",
                    task_id=handle.task_id,
                    thread_id=handle.thread_id,
                    command=handle.command,
                    kind=(
                        "timed_out"
                        if handle.status == "timed_out"
                        else ("completed" if handle.exit_code == 0 else "failed")
                    ),
                    status=handle.status,
                    exit_code=handle.exit_code,
                    started_at=handle.started_at,
                    timestamp=completed_at,
                    duration_seconds=handle.elapsed_seconds(),
                    log_path=str(handle.log_path),
                    output_tail=handle.get_output(tail_chars=6000, tail_lines=80),
                    is_daemon=handle.is_daemon,
                )
                with self._lock:
                    self._notifications[handle.thread_id].append(notification)

            complete_event = self._build_event(
                handle,
                phase="complete",
                should_wake_agent=should_wake,
            )
            self._publish_event(handle.thread_id, complete_event)
            if on_event is not None:
                self._safe_invoke_callback(on_event, complete_event)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Background monitor failed for %s: %s", handle.task_id, exc)

    def _check_stdin_prompt(
        self,
        handle: BackgroundTaskHandle,
        *,
        now: float,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        """Detect when a running process has paused at an interactive stdin prompt."""
        with self._lock:
            if handle.status != "running":
                return
            idle_for = now - handle._last_output_at
            total_chars = handle._total_chars
            if idle_for < 1.5 or total_chars <= 0 or total_chars == handle._last_prompt_notified_len:
                return
            last_line = handle.last_non_empty_line()
            if not last_line or not _STDIN_PROMPT_RE.search(last_line):
                return
            handle.status = "waiting_for_input"
            handle.updated_at = now
            handle._last_prompt_notified_len = total_chars

        notification = TaskNotification(
            notification_id=f"ntf-{uuid.uuid4().hex[:8]}",
            task_id=handle.task_id,
            thread_id=handle.thread_id,
            command=handle.command,
            kind="waiting_for_input",
            status="waiting_for_input",
            exit_code=None,
            started_at=handle.started_at,
            timestamp=now,
            duration_seconds=handle.elapsed_seconds(),
            log_path=str(handle.log_path),
            output_tail=handle.get_output(tail_chars=4000, tail_lines=40),
            is_daemon=handle.is_daemon,
        )
        with self._lock:
            self._notifications[handle.thread_id].append(notification)

        evt = self._build_event(
            handle,
            phase="waiting_for_input",
            should_wake_agent=not handle.is_daemon,
        )
        self._publish_event(handle.thread_id, evt)
        if on_event is not None:
            self._safe_invoke_callback(on_event, evt)

    # ── Task Control Operations (manage_task) ───────────────────────────────

    def get_task(self, task_id: str, thread_id: str | None = None) -> BackgroundTaskHandle | None:
        """Look up a task handle by ID (optionally scoped to ``thread_id``)."""
        with self._lock:
            if thread_id:
                handle = self._tasks_by_thread.get(thread_id, {}).get(task_id)
                if handle is not None:
                    return handle
            return self._tasks_by_id.get(task_id)

    def list_tasks(
        self,
        thread_id: str | None = None,
        *,
        include_completed: bool = True,
    ) -> list[BackgroundTaskRecord]:
        """Return serialized task records for ``thread_id`` (or all threads if None)."""
        with self._lock:
            if thread_id:
                handles = list(self._tasks_by_thread.get(thread_id, {}).values())
            else:
                handles = list(self._tasks_by_id.values())
        records = [
            h.to_record()
            for h in sorted(handles, key=lambda item: item.started_at)
            if include_completed or h.status in ("running", "waiting_for_input")
        ]
        return records

    async def send_input(
        self,
        task_id: str,
        input_text: str,
        *,
        thread_id: str | None = None,
    ) -> tuple[bool, str, BackgroundTaskRecord | None]:
        """Write ``input_text`` to a running background process's stdin pipe."""
        handle = self.get_task(task_id, thread_id=thread_id)
        if handle is None:
            return False, f"Background task `{task_id}` not found.", None

        proc = handle.proc
        if proc is None or proc.returncode is not None or proc.stdin is None:
            return (
                False,
                f"Background task `{task_id}` is no longer running (status={handle.status}).",
                handle.to_record(),
            )

        payload = input_text if input_text.endswith("\n") else f"{input_text}\n"
        try:
            proc.stdin.write(payload.encode("utf-8"))
            await proc.stdin.drain()
        except Exception as exc:
            return False, f"Failed to write stdin to `{task_id}`: {exc}", handle.to_record()

        with self._lock:
            handle.status = "running"
            handle.updated_at = time.time()

        # Wait briefly (up to 350ms) so any immediate response output is captured
        await asyncio.sleep(0.25)
        evt = self._build_event(handle, phase="output")
        self._publish_event(handle.thread_id, evt)
        tail = handle.get_output(tail_chars=2500, tail_lines=40)
        return (
            True,
            f"Sent input to `{task_id}`.\nRecent output:\n{tail or '(no new output yet)'}",
            handle.to_record(),
        )

    async def kill_task(
        self,
        task_id: str,
        *,
        thread_id: str | None = None,
    ) -> tuple[bool, str, BackgroundTaskRecord | None]:
        """Terminate a running background task and its process group."""
        handle = self.get_task(task_id, thread_id=thread_id)
        if handle is None:
            return False, f"Background task `{task_id}` not found.", None

        if handle.proc is None or handle.proc.returncode is not None:
            return (
                True,
                f"Background task `{task_id}` already finished (status={handle.status}).",
                handle.to_record(),
            )

        with self._lock:
            handle.status = "killed"
            handle.updated_at = time.time()

        await self._terminate_process(handle)
        with self._lock:
            handle.completed_at = time.time()
            handle.updated_at = handle.completed_at
            handle.exit_code = handle.proc.returncode if handle.proc else -15

        evt = self._build_event(handle, phase="complete", should_wake_agent=False)
        self._publish_event(handle.thread_id, evt)
        return (
            True,
            f"Terminated background task `{task_id}` (pid={handle.pid}).",
            handle.to_record(),
        )

    async def _terminate_process(self, handle: BackgroundTaskHandle) -> None:
        """Send SIGTERM then SIGKILL to the subprocess process group."""
        proc = handle.proc
        if proc is None or proc.returncode is not None:
            return
        pid = proc.pid
        try:
            if pid and hasattr(os, "killpg"):
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            else:
                proc.terminate()
        except ProcessLookupError:
            return
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
            return
        except TimeoutError:
            pass

        try:
            if pid and hasattr(os, "killpg"):
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except Exception:
            pass

    # ── Notification Draining & Pub/Sub ─────────────────────────────────────

    def drain_notifications(self, thread_id: str) -> list[TaskNotification]:
        """Atomically pop all pending task notifications for ``thread_id``."""
        with self._lock:
            queue = self._notifications.get(thread_id)
            if not queue:
                return []
            items = list(queue)
            queue.clear()
            for item in items:
                handle = self._tasks_by_id.get(item.task_id)
                if handle is not None:
                    handle.notified = True
            return items

    def peek_notifications(self, thread_id: str) -> list[TaskNotification]:
        """Return pending notifications for ``thread_id`` without draining them."""
        with self._lock:
            queue = self._notifications.get(thread_id)
            return list(queue) if queue else []

    def subscribe(self, thread_id: str) -> asyncio.Queue[dict[str, Any]]:
        """Subscribe to real-time task lifecycle events for ``thread_id``."""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subscribers[thread_id].append((loop, q))
        return q

    def unsubscribe(self, thread_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove an event subscription queue for ``thread_id``."""
        with self._lock:
            subs = self._subscribers.get(thread_id)
            if not subs:
                return
            self._subscribers[thread_id] = [
                (loop, queue) for (loop, queue) in subs if queue is not q
            ]

    def _publish_event(self, thread_id: str, event: dict[str, Any]) -> None:
        """Broadcast a task event to all subscribers for ``thread_id`` and ``*``."""
        with self._lock:
            targets = list(self._subscribers.get(thread_id, [])) + list(
                self._subscribers.get("*", [])
            )
        for loop, q in targets:
            try:
                if loop.is_closed():
                    continue
                loop.call_soon_threadsafe(self._offer_queue, q, event)
            except RuntimeError:
                pass

    @staticmethod
    def _offer_queue(q: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass

    @staticmethod
    def _build_event(
        handle: BackgroundTaskHandle,
        *,
        phase: str,
        should_wake_agent: bool = False,
    ) -> dict[str, Any]:
        return {
            "type": "background_task",
            "phase": phase,
            "task_id": handle.task_id,
            "thread_id": handle.thread_id,
            "command": handle.command,
            "cwd": handle.cwd,
            "pid": handle.pid,
            "status": handle.status,
            "is_daemon": handle.is_daemon,
            "started_at": handle.started_at,
            "updated_at": handle.updated_at,
            "completed_at": handle.completed_at,
            " elapsed_seconds": round(handle.elapsed_seconds(), 2),
            "elapsed_seconds": round(handle.elapsed_seconds(), 2),
            "exit_code": handle.exit_code,
            "log_path": str(handle.log_path),
            "last_line": handle.last_non_empty_line(),
            "should_wake_agent": should_wake_agent,
        }

    @staticmethod
    def _safe_invoke_callback(
        callback: Callable[[dict[str, Any]], Any],
        event: dict[str, Any],
    ) -> None:
        try:
            callback(event)
        except Exception as exc:
            logger.debug("Task event callback error: %s", exc)

    @staticmethod
    def _sanitize_thread_id(thread_id: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_\-.]", "_", thread_id)
        return cleaned[:96] or "default"


_MANAGER_LOCK = threading.Lock()
_GLOBAL_TASK_MANAGER: BackgroundTaskManager | None = None


def get_background_task_manager() -> BackgroundTaskManager:
    """Return the process-wide ``BackgroundTaskManager`` singleton."""
    global _GLOBAL_TASK_MANAGER
    with _MANAGER_LOCK:
        if _GLOBAL_TASK_MANAGER is None:
            _GLOBAL_TASK_MANAGER = BackgroundTaskManager()
        return _GLOBAL_TASK_MANAGER


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

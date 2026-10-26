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

"""Background task watcher and reactive agent wakeup controller for `TalkOpsApp`.

Coordinates background process monitoring and agent wakeup:
- Subscribes to background task lifecycle events (both in-process via
  `BackgroundTaskManager` and over SSE `/api/v1/threads/{thread_id}/tasks/stream`
  when connected to the LangGraph server).
- Streams live updates into `BackgroundTaskPanel` in `InputDock`.
- Automatically wakes the agent when a non-daemon background task completes or
  pauses at an interactive stdin prompt while the agent is idle, deferring
  politely if the user is actively typing.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import httpx

from talkops.tasks.manager import get_background_task_manager
from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from talkops.ui.app import TalkOpsApp

logger = get_logger(__name__)


class TaskWatcherController:
    """Watches background shell tasks for the active thread and wakes the idle agent."""

    def __init__(self, app: TalkOpsApp) -> None:
        """Initialize TaskWatcherController."""
        self._app = app
        self._watch_task: asyncio.Task[None] | None = None
        self._watched_thread_id: str | None = None
        self._woken_keys: set[tuple[str, str]] = set()
        self._pending_wake_tasks: set[asyncio.Task[None]] = set()

    def start(self, thread_id: str | None = None) -> None:
        """Start or restart the background watcher for ``thread_id``."""
        target_tid = (thread_id or getattr(self._app, "_agent_thread_id", None) or "").strip()
        if not target_tid:
            return
        if (
            self._watch_task is not None
            and not self._watch_task.done()
            and self._watched_thread_id == target_tid
        ):
            return

        self.stop()
        self._watched_thread_id = target_tid
        try:
            loop = asyncio.get_running_loop()
            self._watch_task = loop.create_task(
                self._run_watcher(target_tid),
                name=f"talkops-task-watcher-{target_tid}",
            )
        except RuntimeError:
            pass

    def stop(self) -> None:
        """Cancel the active watcher task and any deferred wakeup tasks."""
        if self._watch_task is not None and not self._watch_task.done():
            self._watch_task.cancel()
        self._watch_task = None
        self._watched_thread_id = None
        for task in list(self._pending_wake_tasks):
            if not task.done():
                task.cancel()
        self._pending_wake_tasks.clear()

    async def _run_watcher(self, thread_id: str) -> None:
        """Multiplex local `BackgroundTaskManager` events and remote server SSE events."""
        local_task = asyncio.create_task(
            self._watch_local_manager(thread_id),
            name=f"talkops-local-task-watch-{thread_id}",
        )
        remote_task = asyncio.create_task(
            self._watch_remote_sse(thread_id),
            name=f"talkops-remote-task-watch-{thread_id}",
        )
        try:
            await asyncio.gather(local_task, remote_task)
        except asyncio.CancelledError:
            local_task.cancel()
            remote_task.cancel()
            raise

    async def _watch_local_manager(self, thread_id: str) -> None:
        """Subscribe directly to the in-process `BackgroundTaskManager`."""
        manager = get_background_task_manager()
        queue = manager.subscribe(thread_id)
        try:
            initial_tasks = manager.list_tasks(thread_id, include_completed=True)
            if initial_tasks:
                self._handle_event(
                    {
                        "type": "task_snapshot",
                        "thread_id": thread_id,
                        "tasks": initial_tasks,
                    }
                )
            while not getattr(self._app, "_exit", False):
                evt = await queue.get()
                self._handle_event(evt)
        except asyncio.CancelledError:
            raise
        finally:
            manager.unsubscribe(thread_id, queue)

    async def _watch_remote_sse(self, thread_id: str) -> None:
        """Connect to `/api/v1/threads/{thread_id}/tasks/stream` when using a remote server."""
        backoff = 1.5
        while not getattr(self._app, "_exit", False):
            if self._is_in_process_mode():
                return
            server_url, headers = self._resolve_remote_endpoint()
            if not server_url:
                await asyncio.sleep(2.0)
                continue

            stream_url = f"{server_url.rstrip('/')}/api/v1/threads/{thread_id}/tasks/stream"
            try:
                timeout = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)
                async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                    async with client.stream("GET", stream_url) as response:
                        if response.status_code != 200:
                            await asyncio.sleep(backoff)
                            backoff = min(10.0, backoff * 1.5)
                            continue
                        backoff = 1.5
                        async for line in response.aiter_lines():
                            if getattr(self._app, "_exit", False):
                                return
                            if not line or not line.startswith("data:"):
                                continue
                            raw_json = line[5:].strip()
                            if not raw_json:
                                continue
                            try:
                                payload = json.loads(raw_json)
                            except ValueError:
                                continue
                            if isinstance(payload, dict):
                                self._handle_event(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Task SSE stream reconnect for %s: %s", thread_id, exc)
                await asyncio.sleep(backoff)
                backoff = min(10.0, backoff * 1.5)

    def _is_in_process_mode(self) -> bool:
        """Return True when the app is connected to an in-process runtime (where local queue handles all task events)."""
        client = getattr(self._app, "_client", None)
        if client is None and getattr(self._app, "_adapter", None) is not None:
            client = getattr(self._app._adapter, "_client", None)
        if client is not None and getattr(client, "is_in_process", False):
            return True
        app_url = getattr(self._app, "_server_url", None)
        if isinstance(app_url, str) and app_url.strip().startswith("inprocess://"):
            return True
        return False

    def _resolve_remote_endpoint(self) -> tuple[str | None, dict[str, str]]:
        """Extract base URL and auth headers from `self._app._client` if connected."""
        client = getattr(self._app, "_client", None)
        if client is None and getattr(self._app, "_adapter", None) is not None:
            client = getattr(self._app._adapter, "_client", None)
        if client is None or getattr(client, "is_in_process", False):
            return None, {}

        base_url = (
            getattr(client, "server_url", None)
            or getattr(client, "_server_url", None)
            or getattr(client, "base_url", None)
        )
        if not isinstance(base_url, str) or not base_url.strip() or base_url.strip().startswith("inprocess://"):
            return None, {}

        headers: dict[str, str] = {}
        headers_fn = getattr(client, "_headers", None)
        if callable(headers_fn):
            try:
                raw_h = headers_fn()
                if isinstance(raw_h, dict):
                    headers.update({str(k): str(v) for k, v in raw_h.items()})
            except Exception:
                pass
        return base_url.strip(), headers

    def _handle_event(self, event: dict[str, Any]) -> None:
        """Update `BackgroundTaskPanel` and schedule reactive wakeup when appropriate."""
        panel = getattr(self._app, "_get_background_task_panel", lambda: None)()
        if panel is not None:
            try:
                panel.on_task_event(event)
            except Exception as exc:
                logger.debug("BackgroundTaskPanel update error: %s", exc)

        phase = str(event.get("phase") or "")
        should_wake = bool(event.get("should_wake_agent", False))
        task_id = str(event.get("task_id") or "").strip()
        if not should_wake or not task_id or phase not in {"complete", "waiting_for_input"}:
            return

        dedup_key = (
            task_id,
            f"{phase}:{event.get('updated_at')}" if phase == "waiting_for_input" else "complete",
        )
        if dedup_key in self._woken_keys:
            return
        self._woken_keys.add(dedup_key)

        try:
            loop = asyncio.get_running_loop()
            wake_task = loop.create_task(
                self._maybe_wake_agent(event),
                name=f"talkops-task-wake-{task_id}",
            )
            self._pending_wake_tasks.add(wake_task)
            wake_task.add_done_callback(self._pending_wake_tasks.discard)
        except RuntimeError:
            pass

    async def _maybe_wake_agent(self, event: dict[str, Any]) -> None:
        """Trigger a reactive agent turn if the agent is idle once user typing settles."""
        # Brief settle window so if the command completed during an active turn cleanup,
        # or if abefore_model already consumed it in-flight, we don't double-trigger.
        await asyncio.sleep(0.35)

        if getattr(self._app, "_exit", False):
            return

        # If an agent turn is currently running, abefore_model will deliver the notification
        if getattr(self._app, "_agent_running", False):
            return

        # Defer while the user is actively typing or an interactive prompt/shell is open
        while not getattr(self._app, "_exit", False):
            if getattr(self._app, "_agent_running", False):
                return
            user_typing = (
                self._app._is_user_typing()
                if hasattr(self._app, "_is_user_typing")
                else False
            )
            shell_busy = bool(getattr(self._app, "_shell_running", False))
            approval_open = getattr(self._app, "_pending_approval_widget", None) is not None
            ask_open = getattr(self._app, "_pending_ask_user_widget", None) is not None
            if not (user_typing or shell_busy or approval_open or ask_open):
                break
            await asyncio.sleep(0.6)

        if getattr(self._app, "_exit", False) or getattr(self._app, "_agent_running", False):
            return

        task_id = str(event.get("task_id") or "")
        command = str(event.get("command") or "")
        phase = str(event.get("phase") or "complete")
        status = str(event.get("status") or "completed")
        exit_code = event.get("exit_code")
        elapsed = event.get("elapsed_seconds") or 0.0
        log_path = str(event.get("log_path") or "")

        from talkops.ui.widgets.messages import SystemMessage

        if phase == "waiting_for_input":
            banner = (
                f"⌨ Background task `{task_id}` (`{command[:60]}`) is waiting for interactive stdin input."
            )
            wakeup_prompt = (
                f"Background command `{task_id}` (`{command}`) has paused and appears to be "
                f"waiting for stdin input (log: `{log_path}`). Inspect its recent output and "
                "either send the appropriate input with `manage_task(action=\"send_input\", ...)` "
                "or ask the user how to proceed."
            )
        else:
            exit_str = f"exit {exit_code}" if exit_code is not None else status
            banner = (
                f"⚡ Background task `{task_id}` (`{command[:60]}`) {status} "
                f"({exit_str} in {float(elapsed):.1f}s)."
            )
            wakeup_prompt = (
                f"Background command `{task_id}` (`{command}`) finished with status `{status}` "
                f"({exit_str}, elapsed {float(elapsed):.1f}s, log: `{log_path}`). "
                "Review the notification output and report the result or continue the task."
            )

        try:
            await self._app._mount_message(SystemMessage(banner))
            await self._app._send_to_agent(
                wakeup_prompt,
                message_kwargs={
                    "lc_source": "background_task_wakeup",
                    "task_id": task_id,
                    "phase": phase,
                },
            )
        except Exception as exc:
            logger.debug("Reactive wakeup failed for %s: %s", task_id, exc)


__all__ = ["TaskWatcherController"]

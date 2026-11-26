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
- If a task finishes while a turn is still running (e.g. during the turn's final
  model call, after `abefore_model` already ran), the wakeup is deferred until the
  turn ends and then re-verified against the pending notification queue, so the
  completion is never silently dropped.
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
        self._wake_lock = asyncio.Lock()

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
        queue = manager.subscribe("*")
        try:
            active_tid = (getattr(self._app, "_agent_thread_id", None) or thread_id or "").strip()
            initial_tasks = manager.list_tasks(active_tid or thread_id, include_completed=True)
            if initial_tasks:
                self._handle_event(
                    {
                        "type": "task_snapshot",
                        "thread_id": active_tid or thread_id,
                        "tasks": initial_tasks,
                    }
                )
            while not getattr(self._app, "_exit", False):
                evt = await queue.get()
                if not isinstance(evt, dict):
                    continue
                evt_tid = str(evt.get("thread_id") or "").strip()
                current_tid = (getattr(self._app, "_agent_thread_id", None) or thread_id or "").strip()
                panel = getattr(self._app, "_get_background_task_panel", lambda: None)()
                tracked_ids = getattr(panel, "_records", {}) if panel is not None else {}
                task_id = str(evt.get("task_id") or "").strip()
                if (
                    not evt_tid
                    or evt_tid in {current_tid, thread_id, "default"}
                    or (task_id and task_id in tracked_ids)
                ):
                    self._handle_event(evt)
        except asyncio.CancelledError:
            raise
        finally:
            manager.unsubscribe("*", queue)

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
                        if response.status_code in {404, 405, 501}:
                            logger.debug(
                                "Remote server does not expose background task SSE stream (%d); stopping SSE watcher",
                                response.status_code,
                            )
                            return
                        if response.status_code == 401:
                            client_obj = getattr(self._app, "_client", None)
                            prep = getattr(client_obj, "_prepare_client_for_call", None)
                            if callable(prep):
                                try:
                                    await prep(force_token_refresh=True)
                                except Exception:
                                    pass
                            await asyncio.sleep(backoff)
                            backoff = min(10.0, backoff * 1.5)
                            continue
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
            or getattr(client, "_url", None)
            or getattr(client, "base_url", None)
        )
        if not isinstance(base_url, str) or not base_url.strip() or base_url.strip().startswith("inprocess://"):
            return None, {}

        headers: dict[str, str] = {}
        raw_headers = getattr(client, "_headers", None)
        if callable(raw_headers):
            try:
                raw_h = raw_headers()
                if isinstance(raw_h, dict):
                    headers.update({str(k): str(v) for k, v in raw_h.items()})
            except Exception:
                pass
        elif isinstance(raw_headers, dict):
            headers.update({str(k): str(v) for k, v in raw_headers.items()})
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
        evt_tid = str(event.get("thread_id") or "").strip()
        current_tid = (getattr(self._app, "_agent_thread_id", None) or self._watched_thread_id or "").strip()
        if evt_tid and current_tid and evt_tid not in {current_tid, "default"}:
            return
        if not should_wake or not task_id or phase not in {"complete", "waiting_for_input"}:
            return

        dedup_key = (
            task_id,
            f"{phase}:{event.get('updated_at')}" if phase == "waiting_for_input" else "complete",
        )
        if dedup_key in self._woken_keys:
            return
        self._woken_keys.add(dedup_key)
        self._schedule_wake(event)

    def check_pending_notifications(self) -> None:
        """Schedule a wakeup for task notifications still undelivered at a turn boundary.

        Called by ``TalkOpsApp`` after every agent turn cleanup. Covers completions whose
        lifecycle event was missed (watcher restart, thread switch) or that raced the
        final model call of the previous turn, so they are never stranded in the queue.
        """
        if getattr(self._app, "_exit", False):
            return
        if any(not t.done() for t in self._pending_wake_tasks):
            # An in-flight wake task will re-verify delivery once the agent is idle.
            return
        thread_id = (
            getattr(self._app, "_agent_thread_id", None) or self._watched_thread_id or ""
        ).strip()
        if not thread_id:
            return
        try:
            pending = get_background_task_manager().peek_notifications(thread_id)
        except Exception:
            return
        if not pending:
            return
        ntf = pending[0]
        # At most one sweep-triggered wakeup per notification, so a failing wake turn
        # (e.g. model/provider error before `abefore_model`) cannot loop forever.
        sweep_key = (ntf.notification_id, "sweep")
        if sweep_key in self._woken_keys:
            return
        self._woken_keys.add(sweep_key)
        phase = "waiting_for_input" if ntf.kind == "waiting_for_input" else "complete"
        event: dict[str, Any] = {
            "type": "background_task",
            "phase": phase,
            "task_id": ntf.task_id,
            "thread_id": ntf.thread_id,
            "command": ntf.command,
            "status": ntf.status,
            "exit_code": ntf.exit_code,
            "elapsed_seconds": round(ntf.duration_seconds, 2),
            "log_path": ntf.log_path,
            "updated_at": ntf.timestamp,
            "should_wake_agent": True,
        }
        self._schedule_wake(event)

    def _schedule_wake(self, event: dict[str, Any]) -> None:
        task_id = str(event.get("task_id") or "")
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

    def _app_busy(self) -> bool:
        """Return True while an agent turn, turn cleanup, or interactive UI blocks a wakeup."""
        app = self._app
        if getattr(app, "_agent_running", False) or getattr(app, "_agent_reconciling", False):
            return True
        user_typing = app._is_user_typing() if hasattr(app, "_is_user_typing") else False
        shell_busy = bool(getattr(app, "_shell_running", False))
        approval_open = getattr(app, "_pending_approval_widget", None) is not None
        ask_open = getattr(app, "_pending_ask_user_widget", None) is not None
        return bool(user_typing or shell_busy or approval_open or ask_open)

    async def _is_notification_pending(self, event: dict[str, Any]) -> bool:
        """Return True if the task notification has not yet been delivered to the agent.

        A completion can race an in-flight turn: if it lands after that turn's last
        ``abefore_model`` hook, nothing drains it and the agent never hears about it.
        Delivery is checked against the in-process manager queue when the task is
        local, otherwise against the checkpointed ``background_tasks[...].notified``.
        """
        task_id = str(event.get("task_id") or "").strip()
        evt_tid = str(event.get("thread_id") or "").strip()
        thread_id = (
            evt_tid
            or getattr(self._app, "_agent_thread_id", None)
            or self._watched_thread_id
            or ""
        ).strip()
        if not task_id:
            return False

        try:
            manager = get_background_task_manager()
            handle = manager.get_task(task_id, thread_id=thread_id or None)
            if handle is not None:
                pending = manager.peek_notifications(handle.thread_id)
                return any(n.task_id == task_id for n in pending)
        except Exception:
            logger.debug("Local notification check failed for %s", task_id, exc_info=True)

        # Remote server: consult the persisted task record from the checkpoint.
        get_state = getattr(self._app, "_get_thread_state_values", None)
        if callable(get_state) and thread_id:
            try:
                values = await get_state(thread_id)
                records = (values or {}).get("background_tasks") or {}
                rec = records.get(task_id) if isinstance(records, dict) else None
                if isinstance(rec, dict) and rec.get("notified"):
                    return False
            except Exception:
                logger.debug("Checkpoint notification check failed for %s", task_id, exc_info=True)
        # When delivery cannot be confirmed, prefer waking over stranding the result.
        return True

    async def _maybe_wake_agent(self, event: dict[str, Any]) -> None:
        """Trigger a reactive agent turn once the agent is idle and user typing settles.

        Never drops a wakeup just because a turn is running: the completion may have
        landed after that turn's final ``abefore_model`` drain. Instead it waits for
        the turn (and its cleanup/queue drain) to finish, then re-verifies delivery.
        """
        # Brief settle window so if abefore_model consumes it in-flight we don't double-trigger.
        await asyncio.sleep(0.35)

        while True:
            # Wait for any running turn, turn cleanup, typing, or interactive prompt.
            while not getattr(self._app, "_exit", False) and self._app_busy():
                await asyncio.sleep(0.4)
            if getattr(self._app, "_exit", False):
                return

            # Serialize concurrent wakeups: one wake turn drains every pending notification.
            async with self._wake_lock:
                if getattr(self._app, "_exit", False):
                    return
                if self._app_busy():
                    continue
                if not await self._is_notification_pending(event):
                    return
                if self._app_busy():
                    continue
                await self._dispatch_wakeup(event)
                return

    async def _dispatch_wakeup(self, event: dict[str, Any]) -> None:
        """Mount the wakeup banner and start a reactive agent turn for ``event``."""
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

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

"""Unit and integration tests for non-blocking background command execution."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command
from pydantic import BaseModel

from talkops.commands._base import CommandContext
from talkops.commands.power.tasks import TasksHandler
from talkops.middleware.background_commands import (
    BACKGROUND_TASK_NOTIFICATION_SOURCE,
    AsyncExecuteSchema,
    BackgroundCommandMiddleware,
)
from talkops.server.webhooks_app import create_webhooks_app
from talkops.state.background_task_channels import merge_background_tasks
from talkops.tasks.manager import BackgroundTaskManager


class _DummyExecuteSchema(BaseModel):
    command: str
    timeout: int | None = None


def _make_request(
    *,
    name: str = "execute",
    args: dict[str, Any],
    thread_id: str = "test-thread-1",
    state: dict[str, Any] | None = None,
) -> Any:
    runtime = SimpleNamespace(
        config={"configurable": {"thread_id": thread_id}},
        context={"thread_id": thread_id},
        stream_writer=None,
    )
    return SimpleNamespace(
        tool_call={
            "id": "call-123",
            "name": name,
            "args": args,
        },
        state=state or {},
        runtime=runtime,
    )


@pytest.mark.asyncio
async def test_fast_command_returns_synchronously(tmp_path: Path) -> None:
    """Commands finishing within `wait_ms_before_async` return a synchronous ToolMessage."""
    manager = BackgroundTaskManager(tasks_root=tmp_path / "tasks")
    middleware = BackgroundCommandMiddleware(
        working_dir=tmp_path,
        task_manager=manager,
    )

    req = _make_request(
        args={
            "command": "echo 'hello fast path'",
            "wait_ms_before_async": 3000,
        },
        thread_id="thread-fast",
    )

    async def _unused_handler(_req: Any) -> ToolMessage:
        raise AssertionError("handler should not be called for intercepted execute")

    result = await middleware.awrap_tool_call(req, _unused_handler)
    assert isinstance(result, ToolMessage)
    assert "hello fast path" in str(result.content)
    assert result.artifact is not None
    assert result.artifact["background"] is False
    assert result.artifact["exit_code"] == 0
    assert manager.list_tasks("thread-fast") == []


@pytest.mark.asyncio
async def test_slow_command_detaches_and_delivers_completion_in_before_model(
    tmp_path: Path,
) -> None:
    """Commands exceeding `wait_ms_before_async` detach immediately and notify via `abefore_model`."""
    manager = BackgroundTaskManager(tasks_root=tmp_path / "tasks")
    middleware = BackgroundCommandMiddleware(
        working_dir=tmp_path,
        task_manager=manager,
    )

    py_cmd = (
        f'"{sys.executable}" -c '
        '"import time; print(\'phase-1\', flush=True); time.sleep(0.6); print(\'phase-2-done\', flush=True)"'
    )
    req = _make_request(
        args={
            "command": py_cmd,
            "wait_ms_before_async": 150,
        },
        thread_id="thread-detach",
    )

    async def _unused_handler(_req: Any) -> ToolMessage:
        raise AssertionError("handler should not be called")

    result = await middleware.awrap_tool_call(req, _unused_handler)
    assert isinstance(result, Command)
    update = result.update
    assert isinstance(update, dict)
    bg_tasks = update["background_tasks"]
    assert len(bg_tasks) == 1
    task_id, record = next(iter(bg_tasks.items()))
    assert record["status"] == "running"

    tool_msg = update["messages"][0]
    assert isinstance(tool_msg, ToolMessage)
    assert task_id in str(tool_msg.content)
    assert tool_msg.artifact["background"] is True

    # Wait for the detached command to finish in the background
    await asyncio.sleep(0.8)

    runtime = SimpleNamespace(
        config={"configurable": {"thread_id": "thread-detach"}},
        context={"thread_id": "thread-detach"},
    )
    state: dict[str, Any] = {
        "messages": [HumanMessage(content="working")],
        "background_tasks": bg_tasks,
    }
    model_update = await middleware.abefore_model(state, runtime)  # type: ignore[arg-type]
    assert model_update is not None
    assert model_update["background_tasks"][task_id]["status"] == "completed"
    assert model_update["background_tasks"][task_id]["exit_code"] == 0

    injected_msgs = model_update["messages"]
    assert len(injected_msgs) == 1
    ntf_msg = injected_msgs[0]
    assert isinstance(ntf_msg, HumanMessage)
    assert ntf_msg.additional_kwargs.get("lc_source") == BACKGROUND_TASK_NOTIFICATION_SOURCE
    assert "phase-2-done" in str(ntf_msg.content)
    assert f'task_id="{task_id}"' in str(ntf_msg.content)


@pytest.mark.asyncio
async def test_interactive_stdin_detection_and_manage_task_send_input(
    tmp_path: Path,
) -> None:
    """Interactive prompts transition to `waiting_for_input` and unblock via `manage_task`."""
    manager = BackgroundTaskManager(tasks_root=tmp_path / "tasks")
    middleware = BackgroundCommandMiddleware(
        working_dir=tmp_path,
        task_manager=manager,
    )
    manage_tool = middleware.tools[0]

    script = tmp_path / "interactive_prompt.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.write('Do you want to continue? [y/N]: ')\n"
        "sys.stdout.flush()\n"
        "line = sys.stdin.readline().strip()\n"
        "print(f'USER_ANSWER={line}', flush=True)\n",
        encoding="utf-8",
    )

    req = _make_request(
        args={
            "command": f'"{sys.executable}" "{script}"',
            "wait_ms_before_async": 150,
        },
        thread_id="thread-stdin",
    )

    async def _unused_handler(_req: Any) -> ToolMessage:
        raise AssertionError("handler should not be called")

    res = await middleware.awrap_tool_call(req, _unused_handler)
    assert isinstance(res, Command)
    assert res.update is not None
    task_id = next(iter(res.update["background_tasks"].keys()))

    # Wait for the 1.5s stdin prompt heuristic in BackgroundTaskManager
    await asyncio.sleep(2.1)
    handle = manager.get_task(task_id, thread_id="thread-stdin")
    assert handle is not None
    assert handle.status == "waiting_for_input"

    # Send input via manage_task tool
    send_res = await manage_tool.ainvoke(
        {
            "name": "manage_task",
            "id": "call-stdin",
            "type": "tool_call",
            "args": {
                "action": "send_input",
                "task_id": task_id,
                "input_text": "yes",
                "state": {"thread_id": "thread-stdin"},
            },
        }
    )
    assert isinstance(send_res, Command)
    assert send_res.update is not None
    msg = send_res.update["messages"][0]
    assert "USER_ANSWER=yes" in str(msg.content)

    await asyncio.sleep(0.3)
    assert handle.status == "completed"
    assert handle.exit_code == 0


@pytest.mark.asyncio
async def test_manage_task_kill_terminates_process(tmp_path: Path) -> None:
    """`manage_task(action='kill')` cleanly terminates a running process."""
    manager = BackgroundTaskManager(tasks_root=tmp_path / "tasks")
    middleware = BackgroundCommandMiddleware(
        working_dir=tmp_path,
        task_manager=manager,
    )
    manage_tool = middleware.tools[0]

    req = _make_request(
        args={
            "command": f'"{sys.executable}" -c "import time; time.sleep(30)"',
            "wait_ms_before_async": 120,
        },
        thread_id="thread-kill",
    )

    async def _unused_handler(_req: Any) -> ToolMessage:
        raise AssertionError("handler should not be called")

    res = await middleware.awrap_tool_call(req, _unused_handler)
    assert isinstance(res, Command)
    assert res.update is not None
    task_id = next(iter(res.update["background_tasks"].keys()))

    kill_res = await manage_tool.ainvoke(
        {
            "name": "manage_task",
            "id": "call-kill",
            "type": "tool_call",
            "args": {
                "action": "kill",
                "task_id": task_id,
                "state": {"thread_id": "thread-kill"},
            },
        }
    )
    assert isinstance(kill_res, Command)
    assert kill_res.update is not None
    assert kill_res.update["background_tasks"][task_id]["status"] == "killed"


def test_prepare_model_request_extends_execute_schema_and_system_prompt(
    tmp_path: Path,
) -> None:
    """`BackgroundCommandMiddleware` upgrades `execute` schema to `AsyncExecuteSchema`."""
    middleware = BackgroundCommandMiddleware(working_dir=tmp_path)
    dummy_execute = StructuredTool.from_function(
        func=lambda command, timeout=None: command,
        name="execute",
        description="Execute a command.",
        args_schema=_DummyExecuteSchema,
    )
    captured: dict[str, Any] = {}

    class _FakeModelRequest:
        def __init__(self) -> None:
            """Initialize _FakeModelRequest."""
            self.tools = [dummy_execute]
            self.system_message = SystemMessage(content="Base system prompt.")

        def override(self, **kwargs: Any) -> _FakeModelRequest:
            """Provide override test helper or fixture."""
            captured.update(kwargs)
            return self

    req = _FakeModelRequest()
    middleware._prepare_model_request(req)  # type: ignore[arg-type]

    assert "tools" in captured
    upgraded_tool = captured["tools"][0]
    assert upgraded_tool.args_schema is AsyncExecuteSchema
    assert "wait_ms_before_async" in upgraded_tool.description
    assert "manage_task" in str(captured["system_message"].content)


@pytest.mark.asyncio
async def test_webhooks_app_task_routes_and_tasks_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test `/api/v1/threads/{thread_id}/tasks` routes and `/tasks` power command."""
    manager = BackgroundTaskManager(tasks_root=tmp_path / "tasks")
    monkeypatch.setattr(
        "talkops.server.webhooks_app.get_background_task_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "talkops.addons.builtin.client_gateway.get_background_task_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "talkops.commands.power.tasks.get_background_task_manager",
        lambda: manager,
    )

    res = await manager.run_or_detach(
        thread_id="thread-http",
        command=f'"{sys.executable}" -c "import time; print(\'server-log-line\', flush=True); time.sleep(10)"',
        cwd=tmp_path,
        wait_ms_before_async=150,
    )
    assert hasattr(res, "handle")
    task_id = res.handle.task_id  # type: ignore[union-attr]

    starlette_app = create_webhooks_app()
    transport = httpx.ASGITransport(app=starlette_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        list_resp = await client.get("/api/v1/threads/thread-http/tasks")
        assert list_resp.status_code == 200
        payload = list_resp.json()
        assert len(payload["tasks"]) == 1
        assert payload["tasks"][0]["task_id"] == task_id

        logs_resp = await client.get(f"/api/v1/threads/thread-http/tasks/{task_id}/logs")
        assert logs_resp.status_code == 200
        assert "server-log-line" in logs_resp.json()["output"]

        # Also verify /tasks ps and /tasks logs via TasksHandler
        handler = TasksHandler()
        ps_ctx = CommandContext(
            app=None,
            session=None,
            agent=None,
            settings=None,
            raw_command="/tasks ps",
            args="ps",
            thread_id="thread-http",
        )
        ps_res = await handler.execute(ps_ctx)
        assert ps_res.success is True
        assert ps_res.message is not None and task_id in ps_res.message

        kill_resp = await client.post(
            f"/api/v1/threads/thread-http/tasks/{task_id}/action",
            json={"action": "kill"},
        )
        assert kill_resp.status_code == 200
        assert kill_resp.json()["ok"] is True


def test_merge_background_tasks_reducer() -> None:
    """Verify `merge_background_tasks` merges task records cleanly."""
    existing: dict[str, Any] = {
        "cmd-1": {"task_id": "cmd-1", "status": "running", "command": "sleep 5"}
    }
    new: dict[str, Any] = {
        "cmd-1": {"task_id": "cmd-1", "status": "completed", "exit_code": 0},
        "cmd-2": {"task_id": "cmd-2", "status": "running", "command": "pytest"},
    }
    merged = merge_background_tasks(existing, new)
    assert merged["cmd-1"]["status"] == "completed"
    assert merged["cmd-1"]["command"] == "sleep 5"
    assert merged["cmd-1"]["exit_code"] == 0
    assert merged["cmd-2"]["status"] == "running"


@pytest.mark.asyncio
async def test_task_watcher_controller_reactive_wakeup_and_typing_deferral(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify `TaskWatcherController` updates the panel and wakes the idle agent after typing settles."""
    from talkops.ui.controllers.task_watcher_controller import TaskWatcherController
    from talkops.ui.widgets.background_task_panel import BackgroundTaskPanel

    manager = BackgroundTaskManager(tasks_root=tmp_path / "tasks")
    monkeypatch.setattr(
        "talkops.ui.controllers.task_watcher_controller.get_background_task_manager",
        lambda: manager,
    )

    panel = BackgroundTaskPanel()
    sent_prompts: list[tuple[str, dict[str, Any] | None]] = []
    mounted_messages: list[Any] = []
    is_typing = True

    class _FakeApp:
        _exit = False
        _agent_running = False
        _shell_running = False
        _pending_approval_widget = None
        _pending_ask_user_widget = None
        _agent_thread_id = "thread-wake"
        _client = None
        _adapter = None

        def _get_background_task_panel(self) -> BackgroundTaskPanel:
            return panel

        def _is_user_typing(self) -> bool:
            return is_typing

        async def _mount_message(self, widget: Any) -> None:
            mounted_messages.append(widget)

        async def _send_to_agent(
            self,
            prompt: str,
            *,
            message_kwargs: dict[str, Any] | None = None,
        ) -> None:
            sent_prompts.append((prompt, message_kwargs))

    fake_app = _FakeApp()
    watcher = TaskWatcherController(fake_app)  # type: ignore[arg-type]
    watcher.start("thread-wake")

    try:
        res = await manager.run_or_detach(
            thread_id="thread-wake",
            command=f'"{sys.executable}" -c "import time; print(\'running-bg\', flush=True); time.sleep(0.45); print(\'done-bg\', flush=True)"',
            cwd=tmp_path,
            wait_ms_before_async=120,
        )
        assert hasattr(res, "handle")
        task_id = res.handle.task_id  # type: ignore[union-attr]

        # Wait for the process to finish while `is_typing` is True
        await asyncio.sleep(0.9)
        assert task_id in panel._records
        assert panel._records[task_id].status == "completed"
        # Because user is typing, wakeup must be deferred!
        assert sent_prompts == []

        # Stop typing -> wakeup should fire automatically
        is_typing = False
        await asyncio.sleep(0.8)
        assert len(sent_prompts) == 1
        wakeup_text, kwargs = sent_prompts[0]
        assert task_id in wakeup_text
        assert kwargs is not None
        assert kwargs["lc_source"] == "background_task_wakeup"
        assert len(mounted_messages) == 1
    finally:
        watcher.stop()


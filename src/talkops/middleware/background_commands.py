"""LangChain middleware for non-blocking background shell command execution.

Brings Antigravity-style ``wait_ms_before_async`` command execution, live task
management (``manage_task``), state channel persistence (``background_tasks``),
and reactive completion notification delivery into TalkOps.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast, override

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables.config import ensure_config
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import BaseModel, Field

from talkops.middleware.cost_tracking import ACTIVE_SESSION_THREAD_ID
from talkops.middleware.registry import register_middleware
from talkops.state.background_task_channels import (
    BackgroundTaskChannels,
    BackgroundTaskRecord,
)
from talkops.tasks.manager import (
    DEFAULT_WAIT_MS_BEFORE_ASYNC,
     MAX_WAIT_MS_BEFORE_ASYNC,
    MIN_WAIT_MS_BEFORE_ASYNC,
    BackgroundTaskManager,
    DetachedExecResult,
    FastExecResult,
    clamp_wait_ms,
    get_background_task_manager,
)
from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from langgraph.runtime import Runtime

logger = get_logger(__name__)

BACKGROUND_TASK_NOTIFICATION_SOURCE = "background_task_notification"
"""Message ``additional_kwargs['lc_source']`` tag for synthetic task notifications."""

MAX_INLINE_FAST_OUTPUT_CHARS = 80_000

BACKGROUND_EXEC_SYSTEM_INSTRUCTIONS = """\
## Non-Blocking Background Command Execution (`execute` & `manage_task`)
- The `execute` tool supports `wait_ms_before_async` (default `5000` ms, range `100`–`10000` ms) and `is_daemon` (default `False`).
- Commands that complete within `wait_ms_before_async` return their full output synchronously.
- Commands still running after `wait_ms_before_async` are automatically sent to the background and return a `task_id`, `pid`, `log_path`, and partial output immediately so your turn is never blocked.
- For interactive or known long-running commands (`terraform apply`, `kubectl rollout status`, test suites, builds), set `wait_ms_before_async` to a smaller value (e.g. `1000`–`3000` ms).
- For persistent background services (dev servers, port-forwards, tunnels, watchers), set `is_daemon=True`.
- **IMPORTANT — Reactive Wakeup (Do NOT Poll in a Loop)**:
  - Never loop or repeatedly call `manage_task(action="status")` to wait for a background command to finish.
  - Once a command is detached to the background, inform the user or proceed with other independent work and end your turn.
  - The system automatically wakes you with a `<background_task_notification>` as soon as a non-daemon background command completes or pauses for interactive stdin input.
- Use `manage_task` to:
  - `list`: View all active and recent background tasks in this session.
  - `status`: Check a specific task's status and recent log lines (`task_id`).
  - `send_input`: Send stdin text (`input_text`) to a task waiting at an interactive prompt (`task_id`).
  - `kill`: Terminate a running background task (`task_id`)."""


class AsyncExecuteSchema(BaseModel):
    """Extended schema for the ``execute`` tool with non-blocking async support."""

    command: str = Field(
        description="Shell command to execute in the workspace environment.",
    )
    timeout: int | None = Field(
        default=None,
        description=(
            "Optional maximum hard timeout in seconds for the command. "
            "If omitted, uses the default timeout ceiling."
        ),
    )
    wait_ms_before_async: int | None = Field(
        default=DEFAULT_WAIT_MS_BEFORE_ASYNC,
        description=(
            "Milliseconds to wait before sending the command to the background "
            f"({MIN_WAIT_MS_BEFORE_ASYNC}–{MAX_WAIT_MS_BEFORE_ASYNC}ms, default "
            f"{DEFAULT_WAIT_MS_BEFORE_ASYNC}ms). If the command finishes within this "
            "window, its output is returned synchronously; otherwise it detaches "
            "as a background task and notifies you when done."
        ),
    )
    is_daemon: bool = Field(
        default=False,
        description=(
            "Set True for long-running background processes (dev servers, tunnels, "
            "port-forwards, file watchers) that are meant to keep running indefinitely."
        ),
    )


def _resolve_thread_id(runtime: Any = None, state: Mapping[str, Any] | None = None) -> str:
    """Resolve the active LangGraph ``thread_id`` from runtime, config, or ContextVar."""
    if runtime is not None:
        execution_info = getattr(runtime, "execution_info", None)
        tid = getattr(execution_info, "thread_id", None)
        if isinstance(tid, str) and tid.strip():
            return tid.strip()

        ctx = getattr(runtime, "context", None)
        if isinstance(ctx, dict):
            ctx_tid = ctx.get("thread_id")
            if isinstance(ctx_tid, str) and ctx_tid.strip():
                return ctx_tid.strip()

        cfg = getattr(runtime, "config", None)
        if isinstance(cfg, dict):
            configurable = cfg.get("configurable")
            if isinstance(configurable, dict):
                cfg_tid = configurable.get("thread_id")
                if isinstance(cfg_tid, str) and cfg_tid.strip():
                    return cfg_tid.strip()

    try:
        ensured = ensure_config()
        configurable = ensured.get("configurable") if isinstance(ensured, dict) else None
        if isinstance(configurable, dict):
            tid = configurable.get("thread_id")
            if isinstance(tid, str) and tid.strip():
                return tid.strip()
    except Exception:
        pass

    ctx_var_tid = ACTIVE_SESSION_THREAD_ID.get()
    if isinstance(ctx_var_tid, str) and ctx_var_tid.strip():
        return ctx_var_tid.strip()

    if isinstance(state, dict):
        state_tid = state.get("thread_id") or state.get("_thread_id")
        if isinstance(state_tid, str) and state_tid.strip():
            return state_tid.strip()

    return "default"


def _emit_stream_event(payload: dict[str, Any], runtime: Any = None) -> None:
    """Emit a ``background_task`` custom stream event to LangGraph and LangChain listeners."""
    writer = getattr(runtime, "stream_writer", None) if runtime is not None else None
    if callable(writer):
        try:
            writer(payload)
        except Exception:
            pass

    try:
        from langgraph.config import get_stream_writer

        sw = get_stream_writer()
        if callable(sw):
            sw(payload)
    except Exception:
        pass

    try:
        from langchain_core.callbacks.manager import dispatch_custom_event

        config = getattr(runtime, "config", None) if runtime is not None else None
        dispatch_custom_event("background_task", payload, config=config)
    except Exception:
        pass


def _format_fast_output(result: FastExecResult) -> str:
    """Format synchronous ``FastExecResult`` output matching ``FilesystemMiddleware``."""
    output = result.output or ""
    truncated = result.truncated
    if len(output) > MAX_INLINE_FAST_OUTPUT_CHARS:
        output = (
            output[:MAX_INLINE_FAST_OUTPUT_CHARS]
            + f"\n\n... [output truncated at {MAX_INLINE_FAST_OUTPUT_CHARS} chars] ..."
        )
        truncated = True

    parts: list[str] = []
    if output.strip():
        parts.append(output.rstrip())
    else:
        parts.append("(no output)")

    if result.exit_code != 0:
        parts.append(f"\n[Command exited with code {result.exit_code}]")
    if truncated:
        parts.append("\n[Output was truncated]")
    return "\n".join(parts)


def _format_detached_output(result: DetachedExecResult) -> str:
    """Format the immediate ``ToolMessage`` returned when a command is detached."""
    handle = result.handle
    partial = result.partial_output.strip()
    partial_section = (
        f"Partial output so far:\n{partial}\n\n"
        if partial
        else "Partial output so far: (no output yet)\n\n"
    )
    mode_label = "daemon background task" if handle.is_daemon else "background task"
    followup = (
        "This process was started with `is_daemon=True` and will keep running in the "
        "background. Use `manage_task` (`status`, `send_input`, `kill`, `list`) to inspect or stop it."
        if handle.is_daemon
        else (
            "IMPORTANT: Do NOT poll `manage_task` in a loop to wait for completion. "
            "You will be automatically notified when this command finishes or if it needs stdin input. "
            "Proceed with other work or finish your turn and inform the user."
        )
    )
    return (
        f"Command is still running after {result.wait_ms}ms and was detached as a {mode_label}.\n"
        f"- task_id: `{handle.task_id}`\n"
        f"- pid: `{handle.pid}`\n"
        f"- status: `{handle.status}`\n"
        f"- log_path: `{handle.log_path}`\n\n"
        f"{partial_section}"
        f"{followup}"
    )


@register_middleware(name="background_commands")
class BackgroundCommandMiddleware(AgentMiddleware[BackgroundTaskChannels, ContextT]):
    """LangChain middleware enabling non-blocking command execution and task management.

    Responsibilities:
    1. Contributes the ``manage_task`` tool (`list`, `status`, `send_input`, `kill`).
    2. Extends the ``execute`` tool schema with ``wait_ms_before_async`` and ``is_daemon``
       and appends operational instructions in ``wrap_model_call`` / ``awrap_model_call``.
    3. Intercepts ``execute`` calls in ``awrap_tool_call`` / ``wrap_tool_call`` after
       allow-list and HITL approval middlewares have run, racing process completion
       against ``wait_ms_before_async``.
    4. Drains completed or input-waiting task notifications in ``before_model`` /
       ``abefore_model`` so the agent is automatically synchronized both mid-turn
       and on reactive wakeup turns.
    """

    state_schema = BackgroundTaskChannels

    def __init__(
        self,
        *,
        backend: Any = None,
        working_dir: str | Path | None = None,
        task_manager: BackgroundTaskManager | None = None,
        default_wait_ms_before_async: int = DEFAULT_WAIT_MS_BEFORE_ASYNC,
    ) -> None:
        super().__init__()
        self._backend = backend
        self._working_dir = Path(working_dir).resolve() if working_dir else Path.cwd().resolve()
        self._task_manager = task_manager or get_background_task_manager()
        self._default_wait_ms = clamp_wait_ms(default_wait_ms_before_async)
        self.tools = [self._create_manage_task_tool()]

    # ── Tool Definition: manage_task ────────────────────────────────────────

    def _create_manage_task_tool(self) -> BaseTool:
        manager = self._task_manager

        @tool("manage_task")
        async def manage_task(
            action: Annotated[
                Literal["list", "status", "send_input", "kill"],
                Field(
                    description=(
                        "Action to perform: 'list' (list all tasks in this session), "
                        "'status' (check status and recent log output of a task), "
                        "'send_input' (send stdin input to a running task), or "
                        "'kill' (terminate a running task)."
                    )
                ),
            ],
            task_id: Annotated[
                str | None,
                Field(
                    default=None,
                    description="The background task ID (e.g. 'cmd-a1b2c3d4'). Required for 'status', 'send_input', and 'kill'.",
                ),
            ] = None,
            input_text: Annotated[
                str | None,
                Field(
                    default=None,
                    description="Text to send to the task's stdin when action='send_input'.",
                ),
            ] = None,
            tail_lines: Annotated[
                int,
                Field(
                    default=80,
                    ge=5,
                    le=500,
                    description="Number of recent output lines to return when action='status'.",
                ),
            ] = 80,
            tool_call_id: Annotated[str, InjectedToolCallId] = "",
            state: Annotated[dict[str, Any], InjectedState] = None,  # type: ignore[assignment]
        ) -> Command[Any] | ToolMessage:
            """Manage background shell tasks started by `execute`.

            Use `list` to inspect active/recent tasks, `status` to read recent logs
            and exit code, `send_input` to respond to interactive prompts, and `kill`
            to stop a running process. Do NOT poll `status` in a loop.
            """
            thread_id = _resolve_thread_id(state=state)

            if action == "list":
                records = manager.list_tasks(thread_id, include_completed=True)
                state_records = (
                    dict(state.get("background_tasks") or {})
                    if isinstance(state, dict)
                    else {}
                )
                merged_by_id: dict[str, BackgroundTaskRecord] = {
                    **state_records,
                    **{r["task_id"]: r for r in records},
                }
                if not merged_by_id:
                    return ToolMessage(
                        content="No background tasks in this session.",
                        name="manage_task",
                        tool_call_id=tool_call_id,
                        status="success",
                    )
                lines = ["### Background Tasks", ""]
                for tid, rec in sorted(
                    merged_by_id.items(),
                    key=lambda item: float(item[1].get("started_at", 0.0)),
                ):
                    status = rec.get("status", "unknown")
                    pid = rec.get("pid")
                    cmd = rec.get("command", "")
                    exit_code = rec.get("exit_code")
                    log_path = rec.get("log_path", "")
                    daemon_tag = " [daemon]" if rec.get("is_daemon") else ""
                    exit_tag = f" (exit={exit_code})" if exit_code is not None else ""
                    lines.append(
                        f"- `{tid}`{daemon_tag}: **{status}**{exit_tag} · pid=`{pid}` · cmd=`{cmd}` · log=`{log_path}`"
                    )
                return Command(
                    update={
                        "background_tasks": merged_by_id,
                        "messages": [
                            ToolMessage(
                                content="\n".join(lines),
                                name="manage_task",
                                tool_call_id=tool_call_id,
                                status="success",
                            )
                        ],
                    }
                )

            clean_id = (task_id or "").strip()
            if not clean_id:
                return ToolMessage(
                    content=f"Error: `task_id` is required when action={action!r}.",
                    name="manage_task",
                    tool_call_id=tool_call_id,
                    status="error",
                )

            if action == "status":
                handle = manager.get_task(clean_id, thread_id=thread_id)
                if handle is None:
                    persisted = (
                        (state.get("background_tasks") or {}).get(clean_id)
                        if isinstance(state, dict)
                        else None
                    )
                    if isinstance(persisted, dict):
                        tail = persisted.get("last_output_tail") or "(no output)"
                        log_file = Path(str(persisted.get("log_path") or ""))
                        if log_file.exists():
                            try:
                                raw = log_file.read_text(encoding="utf-8", errors="replace")
                                tail = "\n".join(raw.splitlines()[-tail_lines:])
                            except OSError:
                                pass
                        msg = (
                            f"Task `{clean_id}`\n"
                            f"- status: `{persisted.get('status')}`\n"
                            f"- exit_code: `{persisted.get('exit_code')}`\n"
                            f"- command: `{persisted.get('command')}`\n"
                            f"- log_path: `{persisted.get('log_path')}`\n\n"
                            f"Recent output:\n{tail}"
                        )
                        return ToolMessage(
                            content=msg,
                            name="manage_task",
                            tool_call_id=tool_call_id,
                            status="success",
                        )
                    return ToolMessage(
                        content=f"Background task `{clean_id}` not found.",
                        name="manage_task",
                        tool_call_id=tool_call_id,
                        status="error",
                    )

                rec = handle.to_record()
                output = handle.get_output(tail_chars=8000, tail_lines=tail_lines).strip() or "(no output yet)"
                msg = (
                    f"Task `{handle.task_id}`\n"
                    f"- status: `{handle.status}`\n"
                    f"- pid: `{handle.pid}`\n"
                    f"- elapsed: `{handle.elapsed_seconds():.1f}s`\n"
                    f"- exit_code: `{handle.exit_code}`\n"
                    f"- command: `{handle.command}`\n"
                    f"- log_path: `{handle.log_path}`\n\n"
                    f"Recent output (last {tail_lines} lines):\n{output}"
                )
                return Command(
                    update={
                        "background_tasks": {handle.task_id: rec},
                        "messages": [
                            ToolMessage(
                                content=msg,
                                name="manage_task",
                                tool_call_id=tool_call_id,
                                status="success",
                            )
                        ],
                    }
                )

            if action == "send_input":
                if input_text is None:
                    return ToolMessage(
                        content="Error: `input_text` is required when action='send_input'.",
                        name="manage_task",
                        tool_call_id=tool_call_id,
                        status="error",
                    )
                ok, message, rec = await manager.send_input(
                    clean_id,
                    input_text,
                    thread_id=thread_id,
                )
                if rec is not None:
                    return Command(
                        update={
                            "background_tasks": {clean_id: rec},
                            "messages": [
                                ToolMessage(
                                    content=message,
                                    name="manage_task",
                                    tool_call_id=tool_call_id,
                                    status="success" if ok else "error",
                                )
                            ],
                        }
                    )
                return ToolMessage(
                    content=message,
                    name="manage_task",
                    tool_call_id=tool_call_id,
                    status="success" if ok else "error",
                )

            if action == "kill":
                ok, message, rec = await manager.kill_task(clean_id, thread_id=thread_id)
                if rec is not None:
                    return Command(
                        update={
                            "background_tasks": {clean_id: rec},
                            "messages": [
                                ToolMessage(
                                    content=message,
                                    name="manage_task",
                                    tool_call_id=tool_call_id,
                                    status="success" if ok else "error",
                                )
                            ],
                        }
                    )
                return ToolMessage(
                    content=message,
                    name="manage_task",
                    tool_call_id=tool_call_id,
                    status="success" if ok else "error",
                )

            return ToolMessage(
                content=f"Unsupported action: {action!r}",
                name="manage_task",
                tool_call_id=tool_call_id,
                status="error",
            )

        return manage_task

    # ── Model Call Wrapping: Schema Extension & System Instructions ─────────

    def _prepare_model_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Extend the `execute` tool schema with async options and inject instructions."""
        updated_tools: list[Any] = []
        tools_changed = False
        for t in request.tools or ():
            if isinstance(t, BaseTool) and getattr(t, "name", None) == "execute":
                cloned = copy.copy(t)
                cloned.args_schema = AsyncExecuteSchema
                base_desc = getattr(t, "description", "") or ""
                if "wait_ms_before_async" not in base_desc:
                    cloned.description = (
                        f"{base_desc.rstrip()}\n\n"
                        "Non-blocking execution: Specify `wait_ms_before_async` (default 5000ms) "
                        "to automatically detach long-running commands to the background without "
                        "blocking the turn, or `is_daemon=True` for persistent background servers/watchers."
                    )
                updated_tools.append(cloned)
                tools_changed = True
            else:
                updated_tools.append(t)

        sys_msg = request.system_message
        new_sys_msg = sys_msg
        if sys_msg is not None:
            content = getattr(sys_msg, "content", "")
            if isinstance(content, str) and "manage_task" not in content:
                new_sys_msg = SystemMessage(
                    content=f"{content.rstrip()}\n\n{BACKGROUND_EXEC_SYSTEM_INSTRUCTIONS}"
                )
        else:
            new_sys_msg = SystemMessage(content=BACKGROUND_EXEC_SYSTEM_INSTRUCTIONS)

        overrides: dict[str, Any] = {}
        if tools_changed:
            overrides["tools"] = updated_tools
        if new_sys_msg is not sys_msg:
            overrides["system_message"] = new_sys_msg

        if overrides:
            return request.override(**overrides)
        return request

    @override
    def wrap_model_call[ResponseT](
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | ExtendedModelResponse[ResponseT]:
        return handler(self._prepare_model_request(request))

    @override
    async def awrap_model_call[ResponseT](
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | ExtendedModelResponse[ResponseT]:
        return await handler(self._prepare_model_request(request))

    # ── Before Model Hook: Deliver Completed & Stdin-Waiting Notifications ──

    def _collect_notification_update(
        self,
        state: AgentState[Any],
        runtime: Runtime[ContextT] | None = None,
    ) -> dict[str, Any] | None:
        """Drain pending task notifications for this thread and sync `background_tasks`."""
        values = cast("dict[str, Any]", state) if isinstance(state, dict) else {}
        thread_id = _resolve_thread_id(runtime=runtime, state=values)
        notifications = self._task_manager.drain_notifications(thread_id)

        # Reconcile any state records whose live handle status has changed
        existing_records = dict(values.get("background_tasks") or {})
        updated_records: dict[str, BackgroundTaskRecord] = {}
        for task_id, prev_rec in existing_records.items():
            handle = self._task_manager.get_task(task_id, thread_id=thread_id)
            if handle is not None:
                live_rec = handle.to_record()
                if (
                    live_rec.get("status") != prev_rec.get("status")
                    or live_rec.get("exit_code") != prev_rec.get("exit_code")
                    or live_rec.get("notified") != prev_rec.get("notified")
                ):
                    updated_records[task_id] = live_rec

        for ntf in notifications:
            handle = self._task_manager.get_task(ntf.task_id, thread_id=thread_id)
            if handle is not None:
                updated_records[ntf.task_id] = handle.to_record()

        update: dict[str, Any] = {}
        if updated_records:
            update["background_tasks"] = updated_records

        if notifications:
            # Check if the latest message in state is already a wakeup prompt for these notifications
            raw_messages = values.get("messages") or []
            already_has_wakeup = False
            if isinstance(raw_messages, list) and raw_messages:
                last_msg = raw_messages[-1]
                last_kwargs = getattr(last_msg, "additional_kwargs", None) or {}
                if last_kwargs.get("lc_source") == BACKGROUND_TASK_NOTIFICATION_SOURCE:
                    already_has_wakeup = True
                else:
                    last_content = str(getattr(last_msg, "content", "") or "")
                    if "<background_task_notification" in last_content and all(
                        ntf.task_id in last_content for ntf in notifications
                    ):
                        already_has_wakeup = True

            if not already_has_wakeup:
                blocks = [ntf.format_for_agent() for ntf in notifications]
                combined_text = (
                    "The following background command notification(s) arrived:\n\n"
                    + "\n\n".join(blocks)
                )
                update["messages"] = [
                    HumanMessage(
                        content=combined_text,
                        additional_kwargs={
                            "lc_source": BACKGROUND_TASK_NOTIFICATION_SOURCE,
                            "task_ids": [ntf.task_id for ntf in notifications],
                        },
                    )
                ]

        return update or None

    @override
    def before_model(
        self,
        state: AgentState[Any],
        runtime: Runtime[ContextT] | None = None,
    ) -> dict[str, Any] | None:
        return self._collect_notification_update(state, runtime)

    @override
    async def abefore_model(
        self,
        state: AgentState[Any],
        runtime: Runtime[ContextT] | None = None,
    ) -> dict[str, Any] | None:
        return self._collect_notification_update(state, runtime)

    # ── Tool Call Interception: Non-Blocking `execute` Race ─────────────────

    def _resolve_execution_cwd_and_env(self) -> tuple[Path, dict[str, str] | None]:
        """Resolve working directory and synced cloud environment from the backend."""
        backend = self._backend
        target = getattr(backend, "default", backend) if backend is not None else None
        if target is not None:
            sync_fn = getattr(target, "_sync_cloud_env", None)
            if callable(sync_fn):
                try:
                    sync_fn()
                except Exception as exc:
                    logger.debug("Cloud env sync failed before execute: %s", exc)
            cwd = getattr(target, "cwd", None)
            env = getattr(target, "_env", None)
            resolved_cwd = Path(cwd).resolve() if cwd else self._working_dir
            resolved_env = dict(env) if isinstance(env, dict) else None
            return resolved_cwd, resolved_env
        return self._working_dir, None

    async def _aexecute_with_background_race(
        self,
        request: ToolCallRequest,
    ) -> ToolMessage | Command[Any]:
        tool_call = request.tool_call
        tool_call_id = str(tool_call.get("id") or "")
        args = dict(tool_call.get("args") or {})
        command = str(args.get("command") or "").strip()
        if not command:
            return ToolMessage(
                content="Error: `command` must be a non-empty string.",
                name="execute",
                tool_call_id=tool_call_id,
                status="error",
            )

        timeout_raw = args.get("timeout")
        timeout: float | None = None
        if timeout_raw is not None:
            try:
                timeout = float(timeout_raw)
            except (TypeError, ValueError):
                return ToolMessage(
                    content=f"Error: invalid timeout value {timeout_raw!r}.",
                    name="execute",
                    tool_call_id=tool_call_id,
                    status="error",
                )
            if timeout < 0:
                return ToolMessage(
                    content=f"Error: timeout must be non-negative, got {timeout_raw}.",
                    name="execute",
                    tool_call_id=tool_call_id,
                    status="error",
                )

        wait_ms = clamp_wait_ms(
            args.get("wait_ms_before_async", self._default_wait_ms)
        )
        is_daemon = bool(args.get("is_daemon", False))
        state = getattr(request, "state", None)
        runtime = getattr(request, "runtime", None)
        thread_id = _resolve_thread_id(
            runtime=runtime,
            state=state if isinstance(state, dict) else None,
        )
        cwd, env = self._resolve_execution_cwd_and_env()

        result = await self._task_manager.run_or_detach(
            thread_id=thread_id,
            command=command,
            cwd=cwd,
            env=env,
            wait_ms_before_async=wait_ms,
            timeout=timeout,
            is_daemon=is_daemon,
            on_event=lambda evt: _emit_stream_event(evt, runtime=runtime),
        )

        if isinstance(result, FastExecResult):
            content = _format_fast_output(result)
            return ToolMessage(
                content=content,
                name="execute",
                tool_call_id=tool_call_id,
                artifact={
                    "exit_code": result.exit_code,
                    "truncated": result.truncated,
                    "background": False,
                    "duration_seconds": round(result.duration_seconds, 3),
                },
                status="success",
            )

        handle = result.handle
        record = handle.to_record()
        content = _format_detached_output(result)
        return Command(
            update={
                "background_tasks": {handle.task_id: record},
                "messages": [
                    ToolMessage(
                        content=content,
                        name="execute",
                        tool_call_id=tool_call_id,
                        artifact={
                            "task_id": handle.task_id,
                            "pid": handle.pid,
                            "status": handle.status,
                            "background": True,
                            "is_daemon": handle.is_daemon,
                            "log_path": str(handle.log_path),
                        },
                        status="success",
                    )
                ],
            }
        )

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        tool_call = getattr(request, "tool_call", None)
        if isinstance(tool_call, dict) and tool_call.get("name") == "execute":
            return await self._aexecute_with_background_race(request)
        return await handler(request)

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        tool_call = getattr(request, "tool_call", None)
        if isinstance(tool_call, dict) and tool_call.get("name") == "execute":
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is None:
                return asyncio.run(self._aexecute_with_background_race(request))
            # If called synchronously inside a running loop, strip custom args and fall back
            cleaned_args = {
                k: v
                for k, v in (tool_call.get("args") or {}).items()
                if k not in ("wait_ms_before_async", "is_daemon")
            }
            tool_call["args"] = cleaned_args
        return handler(request)


__all__ = [
    "AsyncExecuteSchema",
    "BACKGROUND_EXEC_SYSTEM_INSTRUCTIONS",
    "BACKGROUND_TASK_NOTIFICATION_SOURCE",
    "BackgroundCommandMiddleware",
]

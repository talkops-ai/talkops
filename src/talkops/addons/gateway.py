"""In-memory AgentRunGateway and HookEventBus for TalkOps Server Addons.

`AgentRunGateway` provides a unified, zero-loopback interface for all ServerAddons
(`ClientGatewayAddon`, `WebhookAddon`, `ChatOpsAddon`, `EventBusAndQueueAddon`, custom addons)
to create threads, trigger or stream agent runs, inspect/update thread state, cancel runs,
and resume Human-in-the-Loop (HITL) interrupts via `Command(resume=...)`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
import uuid
from typing import Any, Literal

from talkops.utils.logger import get_logger

logger = get_logger(__name__)

# Deterministic namespace matching WebhookDispatcher's thread resolution
ADDON_THREAD_NAMESPACE = uuid.UUID("9f8e7d6c-5b4a-3f2e-1d0c-9b8a7f6e5d4c")

AddonEventHandler = Callable[["AddonEvent"], Awaitable[None] | None]


@dataclass(frozen=True)
class AddonEvent:
    """Lifecycle event published on `HookEventBus` across all server addons."""

    event_type: str
    thread_id: str | None = None
    run_id: str | None = None
    source_addon: str = "kernel"
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class HookEventBus:
    """In-memory asynchronous pub/sub bus connecting agent runs and ServerAddons.

    Standard event types:
    - `addon.run.started`
    - `addon.run.interrupted` (emitted when HITL approval is required)
    - `addon.run.completed`
    - `addon.run.failed`
    - `addon.run.cancelled`
    - `addon.tool.pre_use` / `addon.tool.post_use`
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[AddonEventHandler]] = {}
        self._recent_events: list[AddonEvent] = []
        self._max_history: int = 200

    def subscribe(self, event_type: str, handler: AddonEventHandler) -> Callable[[], None]:
        """Subscribe `handler` to `event_type` (use `'*'` to receive all events).

        Returns an unsubscribe callable.
        """
        handlers = self._subscribers.setdefault(event_type, [])
        if handler not in handlers:
            handlers.append(handler)

        def _unsubscribe() -> None:
            if handler in handlers:
                handlers.remove(handler)

        return _unsubscribe

    async def publish(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        thread_id: str | None = None,
        run_id: str | None = None,
        source_addon: str = "kernel",
    ) -> AddonEvent:
        """Publish an `AddonEvent` to all matching subscribers."""
        event = AddonEvent(
            event_type=event_type,
            thread_id=thread_id,
            run_id=run_id,
            source_addon=source_addon,
            payload=dict(payload or {}),
        )
        self._recent_events.append(event)
        if len(self._recent_events) > self._max_history:
            self._recent_events = self._recent_events[-self._max_history :]

        matching_handlers = list(self._subscribers.get(event_type, []))
        for wildcard_key in ("*", "addon.*"):
            if wildcard_key != event_type:
                matching_handlers.extend(self._subscribers.get(wildcard_key, []))

        for handler in matching_handlers:
            try:
                res = handler(event)
                if asyncio.iscoroutine(res) or isinstance(res, Awaitable):
                    await res
            except Exception as exc:
                logger.warning(
                    "HookEventBus subscriber failed for event %s: %s",
                    event_type,
                    exc,
                    exc_info=True,
                )
        return event

    @property
    def recent_events(self) -> list[AddonEvent]:
        """Return a snapshot of recent events published on the bus."""
        return list(self._recent_events)


@dataclass
class RunHandle:
    """Represents an in-flight or completed agent run managed by `AgentRunGateway`."""

    run_id: str
    thread_id: str
    source_addon: str
    status: Literal["pending", "running", "interrupted", "success", "error", "cancelled"] = "pending"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str | None = None
    analysis_text: str = ""
    error: str | None = None
    interrupts: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    _task: asyncio.Task[Any] | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "source_addon": self.source_addon,
            "status": self.status,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "analysis_text": self.analysis_text,
            "error": self.error,
            "interrupts": list(self.interrupts),
            "metadata": dict(self.metadata),
        }


class AgentRunGateway:
    """Unified in-memory gateway to the compiled LangGraph `Pregel` runtime.

    Eliminates HTTP loopback overhead (`X-TalkOps-Internal-Webhook`) by invoking
    `graph.astream()`, `graph.aget_state()`, and `graph.aupdate_state()` directly in
    memory on the shared event loop while publishing lifecycle events to `HookEventBus`.
    """

    def __init__(
        self,
        *,
        graph: Any = None,
        checkpointer: Any = None,
        store: Any = None,
        event_bus: HookEventBus | None = None,
        graph_factory: Callable[[], Awaitable[Any]] | None = None,
        cwd: str | None = None,
    ) -> None:
        self._graph = graph
        self._checkpointer = checkpointer
        self._checkpointer_cm: Any = None
        self._sqlite_conn: Any = None
        self._store = store
        self._event_bus = event_bus or HookEventBus()
        self._graph_factory = graph_factory
        self._cwd = cwd
        self._init_lock = asyncio.Lock()

        # Thread metadata & active run registries
        self._threads: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, RunHandle] = {}
        self._thread_runs: dict[str, list[str]] = {}

    @property
    def event_bus(self) -> HookEventBus:
        return self._event_bus

    @property
    def graph(self) -> Any:
        return self._graph

    def bind_runtime(
        self,
        graph: Any,
        *,
        checkpointer: Any = None,
        store: Any = None,
    ) -> None:
        """Bind a compiled LangGraph `Pregel` instance, checkpointer, and store."""
        self._graph = graph
        if checkpointer is not None:
            self._checkpointer = checkpointer
        if store is not None:
            self._store = store

    async def _ensure_persistent_checkpointer(self) -> Any:
        """Initialize and return a persistent PostgreSQL or SQLite (`TalkOpsCheckpointer`) saver."""
        if self._checkpointer is not None:
            return self._checkpointer
        try:
            from talkops.state.postgres_session import (
                TalkOpsPostgresCheckpointer,
                get_postgres_session_uri,
            )

            pg_uri = get_postgres_session_uri()
            if pg_uri:
                self._checkpointer_cm = TalkOpsPostgresCheckpointer.from_conn_string(pg_uri)
                saver = await self._checkpointer_cm.__aenter__()
                await saver.setup()
                self._checkpointer = saver
                return self._checkpointer
        except Exception as exc:
            logger.debug("AgentRunGateway Postgres checkpointer init skipped: %s", exc)

        try:
            import aiosqlite
            from talkops.state.session import (
                TalkOpsCheckpointer,
                _guard_sqlite_handle,
                _patch_aiosqlite,
                get_db_path,
            )

            _patch_aiosqlite()
            db_path = get_db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(str(db_path), timeout=30.0)
            await _guard_sqlite_handle(conn)
            saver = TalkOpsCheckpointer(conn)
            await saver.setup()
            self._sqlite_conn = conn
            self._checkpointer = saver
            return self._checkpointer
        except Exception as exc:
            logger.warning(
                "AgentRunGateway SQLite checkpointer init failed, falling back to memory: %s",
                exc,
            )
            from langgraph.checkpoint.memory import InMemorySaver

            self._checkpointer = InMemorySaver()
            return self._checkpointer

    async def ensure_runtime(self) -> Any:
        """Return the bound `Pregel` graph or lazily initialize it via `graph_factory` / `server_graph.make_graph`."""
        if self._graph is not None:
            if getattr(self._graph, "checkpointer", None) is None:
                self._graph.checkpointer = await self._ensure_persistent_checkpointer()
            return self._graph
        async with self._init_lock:
            if self._graph is not None:
                if getattr(self._graph, "checkpointer", None) is None:
                    self._graph.checkpointer = await self._ensure_persistent_checkpointer()
                return self._graph
            if self._graph_factory is not None:
                self._graph = await self._graph_factory()
                if self._graph is not None and getattr(self._graph, "checkpointer", None) is None:
                    self._graph.checkpointer = await self._ensure_persistent_checkpointer()
                return self._graph
            try:
                from talkops.server.server_graph import make_graph

                self._graph = await make_graph()
                if self._graph is not None and getattr(self._graph, "checkpointer", None) is None:
                    self._graph.checkpointer = await self._ensure_persistent_checkpointer()
                if self._graph is not None and getattr(self._graph, "store", None) is None and self._store is not None:
                    self._graph.store = self._store
                return self._graph
            except Exception as exc:
                logger.warning("AgentRunGateway failed to lazily initialize server_graph: %s", exc)
        return None

    @staticmethod
    def resolve_thread_id(
        thread_id: str | None = None,
        *,
        source_key: str | None = None,
    ) -> str:
        """Normalize a UUID thread ID or deterministically derive a UUIDv5 from `source_key`."""
        if thread_id:
            try:
                uuid.UUID(str(thread_id))
                return str(thread_id)
            except ValueError:
                return str(uuid.uuid5(ADDON_THREAD_NAMESPACE, str(thread_id)))
        if source_key:
            return str(uuid.uuid5(ADDON_THREAD_NAMESPACE, str(source_key)))
        return str(uuid.uuid4())

    async def ensure_thread(
        self,
        thread_id: str | None = None,
        *,
        source_key: str | None = None,
        metadata: dict[str, Any] | None = None,
        owner: str | None = None,
    ) -> dict[str, Any]:
        """Idempotently register or update a thread record in the gateway."""
        resolved_tid = self.resolve_thread_id(thread_id, source_key=source_key)
        now = datetime.now(UTC).isoformat()
        existing = self._threads.get(resolved_tid)
        if existing is None:
            meta = dict(metadata or {})
            if owner and "owner" not in meta:
                meta["owner"] = owner
            record = {
                "thread_id": resolved_tid,
                "created_at": now,
                "updated_at": now,
                "metadata": meta,
                "status": "idle",
                "values": {},
            }
            self._threads[resolved_tid] = record
        else:
            existing["updated_at"] = now
            if metadata:
                existing["metadata"].update(metadata)
            if owner and "owner" not in existing["metadata"]:
                existing["metadata"]["owner"] = owner
            record = existing
        return dict(record)

    async def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        resolved_tid = self.resolve_thread_id(thread_id)
        record = self._threads.get(resolved_tid)
        if record is not None:
            return dict(record)
        state = await self.get_thread_state(resolved_tid)
        if state is not None:
            return await self.ensure_thread(resolved_tid)
        return None

    async def delete_thread(self, thread_id: str) -> bool:
        resolved_tid = self.resolve_thread_id(thread_id)
        await self.cancel_thread_runs(resolved_tid)
        existed = resolved_tid in self._threads
        self._threads.pop(resolved_tid, None)
        self._thread_runs.pop(resolved_tid, None)
        return existed

    async def copy_thread(self, source_thread_id: str) -> dict[str, Any]:
        """Fork/copy a thread's state into a new UUID thread."""
        norm_source = self.resolve_thread_id(source_thread_id)
        new_tid = str(uuid.uuid4())
        source_meta = (self._threads.get(norm_source) or {}).get("metadata", {})
        new_thread = await self.ensure_thread(
            new_tid,
            metadata={**source_meta, "forked_from": norm_source},
        )
        state = await self.get_thread_state(norm_source)
        values = getattr(state, "values", None) or (
            state.get("values") if isinstance(state, Mapping) else None
        )
        if isinstance(values, Mapping) and values:
            try:
                await self.update_thread_state(new_tid, dict(values))
            except Exception:
                logger.debug("Could not copy checkpoint values to forked thread %s", new_tid, exc_info=True)
        return new_thread

    async def stream_run(
        self,
        input_payload: dict[str, Any] | Any,
        *,
        thread_id: str,
        run_id: str | None = None,
        source_addon: str = "client-gateway",
        stream_mode: list[str] | str | None = None,
        subgraphs: bool = True,
        context: Mapping[str, Any] | Any | None = None,
        configurable: Mapping[str, Any] | None = None,
        durability: str | None = None,
    ) -> AsyncIterator[tuple[tuple[str, ...], str, Any]]:
        """Stream execution directly from the in-memory `Pregel` graph and publish lifecycle events."""
        from talkops.ui.remote_client import _convert_interrupts, _parse_stream_item

        graph = await self.ensure_runtime()
        if graph is None:
            raise RuntimeError("AgentRunGateway has no bound LangGraph runtime")

        resolved_tid = self.resolve_thread_id(thread_id)
        await self.ensure_thread(resolved_tid, metadata={"source_addon": source_addon})
        effective_run_id = run_id or str(uuid.uuid4())

        handle = self._runs.get(effective_run_id)
        if handle is None:
            handle = RunHandle(
                run_id=effective_run_id,
                thread_id=resolved_tid,
                source_addon=source_addon,
                status="running",
            )
            self._runs[effective_run_id] = handle
            self._thread_runs.setdefault(resolved_tid, []).append(effective_run_id)
        else:
            handle.status = "running"

        self._threads[resolved_tid]["status"] = "busy"

        # Prepare context & config matching InProcessAgent semantics
        ctx_payload: dict[str, Any] = (
            dict(context)
            if isinstance(context, Mapping)
            else (dict(context.__dict__) if hasattr(context, "__dict__") else {})
        )
        ctx_payload.setdefault("thread_id", resolved_tid)
        ctx_payload.setdefault("source_addon", source_addon)
        ctx_payload.setdefault("interactive", False)
        if self._cwd and "cwd" not in ctx_payload:
            ctx_payload["cwd"] = self._cwd
        if configurable:
            for k, v in configurable.items():
                if k != "thread_id" and k not in ctx_payload:
                    ctx_payload[k] = v

        if ctx_payload.get("thread_id") and not ctx_payload.get("approval_mode_key"):
            try:
                from talkops.security.approval_mode import approval_mode_key

                ctx_payload["approval_mode_key"] = approval_mode_key(str(ctx_payload["thread_id"]))
            except Exception:
                pass

        try:
            from talkops.middleware.cost_tracking import ACTIVE_SESSION_THREAD_ID

            ACTIVE_SESSION_THREAD_ID.set(resolved_tid)
        except Exception:
            pass

        stream_context: Any = ctx_payload
        try:
            from talkops.agent.config import CLIContextSchema

            stream_context = CLIContextSchema.from_payload(ctx_payload)
        except Exception:
            stream_context = ctx_payload

        run_metadata: dict[str, Any] = {
            "agent_name": "agent",
            "updated_at": datetime.now(UTC).isoformat(),
            "owner": "webhook",
            "source_addon": source_addon,
        }
        if ctx_payload.get("channel"):
            run_metadata["channel"] = str(ctx_payload["channel"])
        if ctx_payload.get("conversation_id"):
            run_metadata["conversation_id"] = str(ctx_payload["conversation_id"])
        if ctx_payload.get("cwd") or self._cwd:
            run_metadata["cwd"] = str(ctx_payload.get("cwd") or self._cwd)

        run_config: dict[str, Any] = {
            "configurable": {"thread_id": resolved_tid},
            "run_id": effective_run_id,
            "metadata": run_metadata,
        }
        stream_kwargs: dict[str, Any] = {}
        if durability is not None:
            stream_kwargs["durability"] = durability

        await self._event_bus.publish(
            "addon.run.started",
            {
                "thread_id": resolved_tid,
                "run_id": effective_run_id,
                "source_addon": source_addon,
            },
            thread_id=resolved_tid,
            run_id=effective_run_id,
            source_addon=source_addon,
        )

        last_ai_text = ""
        detected_interrupts: list[dict[str, Any]] = []

        try:
            async for raw_item in graph.astream(
                input_payload,
                stream_mode=stream_mode or ["messages", "updates"],
                subgraphs=subgraphs,
                config=run_config,
                context=stream_context,
                **stream_kwargs,
            ):
                parsed = _parse_stream_item(raw_item)
                if parsed is None:
                    continue
                ns, mode, data = parsed

                if mode == "updates" and isinstance(data, Mapping):
                    if "__interrupt__" in data:
                        converted = _convert_interrupts(data["__interrupt__"])
                        for intr in converted:
                            intr_dict = (
                                {"value": getattr(intr, "value", intr), "id": getattr(intr, "id", None)}
                                if not isinstance(intr, dict)
                                else intr
                            )
                            detected_interrupts.append(intr_dict)
                        handle.status = "interrupted"
                        handle.interrupts = list(detected_interrupts)
                        await self._event_bus.publish(
                            "addon.run.interrupted",
                            {
                                "thread_id": resolved_tid,
                                "run_id": effective_run_id,
                                "interrupts": list(detected_interrupts),
                            },
                            thread_id=resolved_tid,
                            run_id=effective_run_id,
                            source_addon=source_addon,
                        )
                    # Extract latest assistant text from node updates if present
                    for node_val in data.values():
                        if isinstance(node_val, Mapping) and isinstance(node_val.get("messages"), list):
                            extracted = _extract_last_assistant_message(node_val["messages"])
                            if extracted:
                                last_ai_text = extracted

                yield (ns, mode, data)

            if not last_ai_text:
                state = await self.get_thread_state(resolved_tid)
                values = getattr(state, "values", None) or (
                    state.get("values") if isinstance(state, Mapping) else None
                )
                if isinstance(values, Mapping) and isinstance(values.get("messages"), list):
                    last_ai_text = _extract_last_assistant_message(values["messages"])

            handle.analysis_text = last_ai_text
            handle.completed_at = datetime.now(UTC).isoformat()
            if handle.status != "interrupted":
                handle.status = "success"
                self._threads[resolved_tid]["status"] = "idle"
                await self._event_bus.publish(
                    "addon.run.completed",
                    {
                        "thread_id": resolved_tid,
                        "run_id": effective_run_id,
                        "status": handle.status,
                        "analysis_text": last_ai_text,
                    },
                    thread_id=resolved_tid,
                    run_id=effective_run_id,
                    source_addon=source_addon,
                )
            else:
                self._threads[resolved_tid]["status"] = "interrupted"
        except asyncio.CancelledError:
            handle.status = "cancelled"
            handle.completed_at = datetime.now(UTC).isoformat()
            self._threads[resolved_tid]["status"] = "idle"
            await self._event_bus.publish(
                "addon.run.cancelled",
                {"thread_id": resolved_tid, "run_id": effective_run_id},
                thread_id=resolved_tid,
                run_id=effective_run_id,
                source_addon=source_addon,
            )
            raise
        except Exception as exc:
            handle.status = "error"
            handle.error = f"{type(exc).__name__}: {exc}"
            handle.completed_at = datetime.now(UTC).isoformat()
            self._threads[resolved_tid]["status"] = "error"
            await self._event_bus.publish(
                "addon.run.failed",
                {
                    "thread_id": resolved_tid,
                    "run_id": effective_run_id,
                    "error": handle.error,
                },
                thread_id=resolved_tid,
                run_id=effective_run_id,
                source_addon=source_addon,
            )
            raise

    async def trigger_run(
        self,
        prompt_or_input: str | dict[str, Any] | Any,
        *,
        thread_id: str | None = None,
        source_key: str | None = None,
        source_addon: str = "webhooks",
        addon_name: str | None = None,
        context: Mapping[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        wait: bool = True,
    ) -> RunHandle:
        """Trigger an agent run in-memory (either awaiting completion or running in the background)."""
        effective_addon = addon_name or source_addon
        resolved_tid = self.resolve_thread_id(thread_id, source_key=source_key)
        await self.ensure_thread(resolved_tid, metadata=metadata)
        run_id = str(uuid.uuid4())

        if isinstance(prompt_or_input, str):
            input_payload: Any = {"messages": [{"role": "user", "content": prompt_or_input}]}
        else:
            input_payload = prompt_or_input

        handle = RunHandle(
            run_id=run_id,
            thread_id=resolved_tid,
            source_addon=effective_addon,
            status="pending",
            metadata=dict(metadata or {}),
        )
        self._runs[run_id] = handle
        self._thread_runs.setdefault(resolved_tid, []).append(run_id)

        async def _consume() -> RunHandle:
            try:
                async for _ in self.stream_run(
                    input_payload,
                    thread_id=resolved_tid,
                    run_id=run_id,
                    source_addon=effective_addon,
                    context=context,
                ):
                    pass
            except Exception as exc:
                logger.warning("Gateway run %s failed: %s", run_id, exc)
            return handle

        task = asyncio.create_task(_consume(), name=f"talkops-run-{run_id[:8]}")
        handle._task = task
        if wait:
            await task
        return handle

    async def submit_run(
        self,
        prompt: str | dict[str, Any] | Any,
        *,
        thread_id: str | None = None,
        source_key: str | None = None,
        addon_name: str = "custom",
        source_addon: str | None = None,
        context: Mapping[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        wait: bool = False,
    ) -> RunHandle:
        """Submit a background (or foreground) agent run from an addon and return its `RunHandle`."""
        return await self.trigger_run(
            prompt,
            thread_id=thread_id,
            source_key=source_key,
            source_addon=source_addon or addon_name,
            context=context,
            metadata=metadata,
            wait=wait,
        )

    async def resume_run(
        self,
        thread_id: str,
        resume_payload: Any = None,
        *,
        decision: str | dict[str, Any] | None = None,
        source_addon: str = "chatops",
        addon_name: str | None = None,
        context: Mapping[str, Any] | None = None,
        wait: bool = True,
    ) -> RunHandle:
        """Resume an interrupted Human-in-the-Loop run using `langgraph.types.Command(resume=...)`."""
        from langgraph.types import Command

        effective_resume = resume_payload
        if effective_resume is None and decision is not None:
            if isinstance(decision, str):
                effective_resume = {"decisions": [{"type": decision}]}
            else:
                effective_resume = decision
        cmd = Command(resume=effective_resume)
        return await self.trigger_run(
            cmd,
            thread_id=thread_id,
            source_addon=addon_name or source_addon,
            context=context,
            wait=wait,
        )

    async def get_thread_state(self, thread_id: str) -> Any:
        """Read checkpoint state for `thread_id` directly from the in-memory `Pregel` graph."""
        graph = await self.ensure_runtime()
        if graph is None:
            return None
        resolved_tid = self.resolve_thread_id(thread_id)
        try:
            return await graph.aget_state({"configurable": {"thread_id": resolved_tid}})
        except Exception as exc:
            logger.debug("AgentRunGateway.get_thread_state(%s) returned None: %s", resolved_tid, exc)
            return None

    async def update_thread_state(
        self,
        thread_id: str,
        values: dict[str, Any],
        *,
        as_node: str | None = None,
    ) -> Any:
        """Write state updates into the thread's checkpoint."""
        graph = await self.ensure_runtime()
        if graph is None:
            raise RuntimeError("AgentRunGateway has no bound LangGraph runtime")
        resolved_tid = self.resolve_thread_id(thread_id)
        await self.ensure_thread(resolved_tid)
        cfg = {"configurable": {"thread_id": resolved_tid}}
        if as_node is not None:
            return await graph.aupdate_state(cfg, values, as_node=as_node)
        return await graph.aupdate_state(cfg, values)

    def get_run(self, run_id: str) -> RunHandle | None:
        return self._runs.get(run_id)

    def list_thread_runs(
        self,
        thread_id: str,
        *,
        status: str | None = None,
        limit: int = 20,
    ) -> list[RunHandle]:
        resolved_tid = self.resolve_thread_id(thread_id)
        run_ids = self._thread_runs.get(resolved_tid, [])
        handles = [self._runs[rid] for rid in reversed(run_ids) if rid in self._runs]
        if status:
            handles = [h for h in handles if h.status == status]
        return handles[:limit]

    async def cancel_run(self, thread_id: str, run_id: str) -> bool:
        handle = self._runs.get(run_id)
        if handle is None or handle.thread_id != self.resolve_thread_id(thread_id):
            return False
        if handle._task is not None and not handle._task.done():
            handle._task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(handle._task), timeout=2.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass
        handle.status = "cancelled"
        handle.completed_at = datetime.now(UTC).isoformat()
        return True

    async def cancel_thread_runs(self, thread_id: str) -> list[str]:
        resolved_tid = self.resolve_thread_id(thread_id)
        cancelled: list[str] = []
        for handle in self.list_thread_runs(resolved_tid):
            if handle.status in ("pending", "running"):
                if await self.cancel_run(resolved_tid, handle.run_id):
                    cancelled.append(handle.run_id)
        return cancelled

    async def put_store_item(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: dict[str, Any],
    ) -> None:
        if self._store is not None and hasattr(self._store, "aput"):
            await self._store.aput(namespace, key, value)
        elif self._store is not None and hasattr(self._store, "put"):
            self._store.put(namespace, key, value)

    async def get_store_item(
        self,
        namespace: tuple[str, ...],
        key: str,
    ) -> Any:
        if self._store is not None and hasattr(self._store, "aget"):
            return await self._store.aget(namespace, key)
        if self._store is not None and hasattr(self._store, "get"):
            return self._store.get(namespace, key)
        return None


def _extract_last_assistant_message(messages: list[Any]) -> str:
    """Extract text from the most recent assistant message in a LangGraph message list."""
    for msg in reversed(messages):
        if isinstance(msg, Mapping):
            role = str(msg.get("type") or msg.get("role") or "").lower()
            content = msg.get("content", "")
        else:
            role = str(getattr(msg, "type", "") or getattr(msg, "role", "")).lower()
            content = getattr(msg, "content", "")

        if role not in ("ai", "assistant", "aimessage", "aimessagechunk"):
            continue

        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, Mapping) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
            joined = "\n".join(p for p in parts if p.strip()).strip()
            if joined:
                return joined
    return ""

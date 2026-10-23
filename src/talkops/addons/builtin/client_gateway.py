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

"""Built-in ClientGatewayAddon (`client-gateway`) for TalkOps Server Mode.

Exposes the `/api/v1/*` remote TUI/CLI session, thread lifecycle, server info,
and real-time background task SSE streaming endpoints. Enabled by default.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import BaseRoute, Route

from talkops.addons.config import AddonSettings
from talkops.addons.protocol import (
    AddonCapabilities,
    AddonContext,
    AddonHealthStatus,
    AddonTier,
    BaseServerAddon,
)
from talkops.server.auth import get_auth_mode, resolve_user_from_headers
from talkops.state.postgres_session import get_postgres_session_uri
from talkops.state.session import (
    SessionManager,
    bind_user_context,
    delete_thread,
    find_similar_threads,
    fork_thread,
    get_most_recent,
    get_thread_title,
    list_threads,
    set_thread_title,
    thread_exists,
)
from talkops.tasks.manager import get_background_task_manager


class ClientGatewaySettings(AddonSettings):
    """Configuration schema for `ClientGatewayAddon`."""

    enabled: bool = True
    max_threads_page_size: int = Field(default=500, ge=1, le=5000)
    sse_keepalive_seconds: float = Field(default=15.0, gt=0.0)


async def authenticate_client_request(request: Request) -> tuple[str | None, JSONResponse | None]:
    """Authenticate an `/api/v1/*` request and return `(user_id, error_response)`."""
    try:
        # Prefer active IdentityMapper on AddonContext if an enterprise auth addon is active
        addon_mgr = getattr(request.app.state, "addon_manager", None)
        ctx = getattr(addon_mgr, "context", None) if addon_mgr is not None else None
        mapper = getattr(ctx, "identity_mapper", None) if ctx is not None else None
        if mapper is not None and getattr(mapper, "name", "") == "enterprise-auth":
            user_dict = await mapper.authenticate_request(dict(request.headers))
        else:
            user_dict = await resolve_user_from_headers(request.headers, path=request.url.path)
        user_id = str(user_dict.get("identity") or "default")
        return user_id, None
    except Exception as exc:
        status_code = getattr(exc, "status_code", 401)
        detail = getattr(exc, "detail", str(exc))
        return None, JSONResponse({"error": detail}, status_code=status_code)


async def server_info_endpoint(request: Request) -> JSONResponse:
    """Return server mode metadata, storage backend, auth mode, active addons, and user."""
    user_id, err_resp = await authenticate_client_request(request)
    if err_resp is not None:
        return err_resp

    storage_backend = "postgres" if get_postgres_session_uri() else "sqlite"
    addon_mgr = getattr(request.app.state, "addon_manager", None)
    active_addons = (
        [a.name for a in addon_mgr.active_addons]
        if addon_mgr is not None
        else ["client-gateway", "core-auth", "webhooks"]
    )
    profile = getattr(addon_mgr, "profile", "default") if addon_mgr is not None else "default"

    return JSONResponse(
        {
            "status": "ok",
            "service": "talkops-central-server",
            "auth_mode": get_auth_mode(),
            "storage_backend": storage_backend,
            "user_id": user_id,
            "profile": profile,
            "active_addons": active_addons,
        },
        status_code=200,
    )


async def api_list_threads_endpoint(request: Request) -> JSONResponse:
    """List threads for the authenticated user (plus shared webhook threads)."""
    user_id, err_resp = await authenticate_client_request(request)
    if err_resp is not None:
        return err_resp

    qp = request.query_params
    try:
        limit = max(1, min(500, int(qp.get("limit", "50"))))
    except ValueError:
        limit = 50
    include_msg_cnt = qp.get("include_message_count", "true").lower() not in {"false", "0", "no"}
    sort_by = qp.get("sort_by", "updated")
    agent_name = qp.get("agent_name") or None
    branch = qp.get("branch") or None
    cwd = qp.get("cwd") or None

    with bind_user_context(user_id):
        threads = await list_threads(
            agent_name=agent_name,
            limit=limit,
            include_message_count=include_msg_cnt,
            sort_by=sort_by,
            branch=branch,
            cwd=cwd,
            user_id=user_id,
        )
    return JSONResponse({"status": "ok", "user_id": user_id, "threads": threads}, status_code=200)


async def api_recent_thread_endpoint(request: Request) -> JSONResponse:
    """Return the most recently updated thread for the authenticated user."""
    user_id, err_resp = await authenticate_client_request(request)
    if err_resp is not None:
        return err_resp

    qp = request.query_params
    agent_name = qp.get("agent_name") or None
    branch = qp.get("branch") or None
    cwd = qp.get("cwd") or None

    with bind_user_context(user_id):
        recent = await get_most_recent(
            agent_name=agent_name,
            branch=branch,
            cwd=cwd,
            user_id=user_id,
        )
    return JSONResponse({"status": "ok", "thread": recent}, status_code=200)


async def api_similar_threads_endpoint(request: Request) -> JSONResponse:
    """Return thread IDs matching a prefix for the authenticated user."""
    user_id, err_resp = await authenticate_client_request(request)
    if err_resp is not None:
        return err_resp

    qp = request.query_params
    prefix = qp.get("prefix", "").strip()
    try:
        limit = max(1, min(50, int(qp.get("limit", "3"))))
    except ValueError:
        limit = 3

    if not prefix:
        return JSONResponse({"status": "ok", "thread_ids": []}, status_code=200)

    with bind_user_context(user_id):
        matches = await find_similar_threads(prefix, limit=limit, user_id=user_id)
    return JSONResponse({"status": "ok", "thread_ids": matches}, status_code=200)


async def api_get_thread_endpoint(request: Request) -> JSONResponse:
    """Return thread existence and title for `thread_id`."""
    user_id, err_resp = await authenticate_client_request(request)
    if err_resp is not None:
        return err_resp

    thread_id = str(request.path_params.get("thread_id") or "").strip()
    if not thread_id:
        return JSONResponse({"error": "thread_id is required"}, status_code=400)

    with bind_user_context(user_id):
        exists = await thread_exists(thread_id, user_id=user_id)
        title = await get_thread_title(thread_id, user_id=user_id)
    return JSONResponse(
        {
            "status": "ok",
            "thread_id": thread_id,
            "exists": exists,
            "title": title,
        },
        status_code=200,
    )


async def api_set_thread_title_endpoint(request: Request) -> JSONResponse:
    """Persist a conversation title and optional preview for `thread_id`."""
    user_id, err_resp = await authenticate_client_request(request)
    if err_resp is not None:
        return err_resp

    thread_id = str(request.path_params.get("thread_id") or "").strip()
    if not thread_id:
        return JSONResponse({"error": "thread_id is required"}, status_code=400)

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    title = str(body.get("title") or "").strip()
    preview = body.get("preview")
    only_if_empty = bool(body.get("only_if_empty", True))

    with bind_user_context(user_id):
        updated = await set_thread_title(
            thread_id,
            title,
            preview=str(preview) if preview is not None else None,
            only_if_empty=only_if_empty,
            user_id=user_id,
        )
        effective_title = await get_thread_title(thread_id, user_id=user_id)
    return JSONResponse(
        {
            "status": "ok",
            "thread_id": thread_id,
            "updated": updated,
            "title": effective_title,
        },
        status_code=200,
    )


async def api_fork_thread_endpoint(request: Request) -> JSONResponse:
    """Fork `thread_id` into a new thread on the server."""
    user_id, err_resp = await authenticate_client_request(request)
    if err_resp is not None:
        return err_resp

    source_thread_id = str(request.path_params.get("thread_id") or "").strip()
    if not source_thread_id:
        return JSONResponse({"error": "thread_id is required"}, status_code=400)

    target_thread_id: str | None = None
    try:
        body = await request.json()
        if isinstance(body, dict) and body.get("target_thread_id"):
            target_thread_id = str(body["target_thread_id"]).strip()
    except Exception:
        pass

    with bind_user_context(user_id):
        new_thread_id = await fork_thread(
            source_thread_id,
            target_thread_id=target_thread_id,
            user_id=user_id,
        )
    return JSONResponse(
        {
            "status": "ok",
            "source_thread_id": source_thread_id,
            "thread_id": new_thread_id,
        },
        status_code=200,
    )


async def api_prune_thread_endpoint(request: Request) -> JSONResponse:
    """Compact/prune checkpoints for `thread_id` on the server."""
    user_id, err_resp = await authenticate_client_request(request)
    if err_resp is not None:
        return err_resp

    thread_id = str(request.path_params.get("thread_id") or "").strip()
    if not thread_id:
        return JSONResponse({"error": "thread_id is required"}, status_code=400)

    strategy = "keep_latest"
    turn_completed = False
    try:
        body = await request.json()
        if isinstance(body, dict):
            strategy = str(body.get("strategy") or "keep_latest")
            turn_completed = bool(body.get("turn_completed", False))
    except Exception:
        pass

    with bind_user_context(user_id):
        await SessionManager.prune_thread(
            thread_id,
            strategy=strategy,
            turn_completed=turn_completed,
        )
    return JSONResponse({"status": "ok", "thread_id": thread_id, "pruned": True}, status_code=200)


async def api_delete_thread_endpoint(request: Request) -> JSONResponse:
    """Delete `thread_id` and its checkpoints for the authenticated user."""
    user_id, err_resp = await authenticate_client_request(request)
    if err_resp is not None:
        return err_resp

    thread_id = str(request.path_params.get("thread_id") or "").strip()
    if not thread_id:
        return JSONResponse({"error": "thread_id is required"}, status_code=400)

    with bind_user_context(user_id):
        deleted = await delete_thread(thread_id, user_id=user_id)
    return JSONResponse(
        {
            "status": "ok",
            "thread_id": thread_id,
            "deleted": deleted,
        },
        status_code=200,
    )


async def api_list_thread_tasks_endpoint(request: Request) -> JSONResponse:
    """List active and recent background tasks for `thread_id`."""
    _, err_resp = await authenticate_client_request(request)
    if err_resp is not None:
        return err_resp

    thread_id = str(request.path_params.get("thread_id") or "").strip()
    if not thread_id:
        return JSONResponse({"error": "thread_id is required"}, status_code=400)

    include_completed = request.query_params.get("include_completed", "true").lower() != "false"
    manager = get_background_task_manager()
    tasks = manager.list_tasks(thread_id, include_completed=include_completed)
    pending_notifications = [
        {
            "notification_id": n.notification_id,
            "task_id": n.task_id,
            "kind": n.kind,
            "status": n.status,
            "exit_code": n.exit_code,
            "timestamp": n.timestamp,
        }
        for n in manager.peek_notifications(thread_id)
    ]
    return JSONResponse(
        {
            "thread_id": thread_id,
            "tasks": tasks,
            "pending_notifications": pending_notifications,
        },
        status_code=200,
    )


async def api_stream_thread_tasks_endpoint(request: Request) -> StreamingResponse | JSONResponse:
    """SSE stream of real-time background task lifecycle events for `thread_id`."""
    _, err_resp = await authenticate_client_request(request)
    if err_resp is not None:
        return err_resp

    thread_id = str(request.path_params.get("thread_id") or "").strip()
    if not thread_id:
        return JSONResponse({"error": "thread_id is required"}, status_code=400)

    manager = get_background_task_manager()
    queue = manager.subscribe(thread_id)

    async def _event_generator():
        try:
            snapshot = {
                "type": "task_snapshot",
                "thread_id": thread_id,
                "tasks": manager.list_tasks(thread_id, include_completed=True),
            }
            yield f"data: {json.dumps(snapshot)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(evt)}\n\n"
                except TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            manager.unsubscribe(thread_id, queue)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def api_get_task_logs_endpoint(request: Request) -> JSONResponse:
    """Return recent log lines for a background task (`GET /api/v1/threads/{thread_id}/tasks/{task_id}/logs`)."""
    _, err_resp = await authenticate_client_request(request)
    if err_resp is not None:
        return err_resp

    thread_id = str(request.path_params.get("thread_id") or "").strip()
    task_id = str(request.path_params.get("task_id") or "").strip()
    if not thread_id or not task_id:
        return JSONResponse({"error": "thread_id and task_id are required"}, status_code=400)

    try:
        tail_lines = int(request.query_params.get("tail", "100"))
    except ValueError:
        tail_lines = 100
    tail_lines = max(5, min(2000, tail_lines))

    manager = get_background_task_manager()
    handle = manager.get_task(task_id, thread_id=thread_id)
    if handle is None:
        return JSONResponse({"error": f"Task {task_id!r} not found"}, status_code=404)

    output = handle.get_output(tail_chars=32000, tail_lines=tail_lines)
    return JSONResponse(
        {
            "task": handle.to_record(),
            "output": output,
        },
        status_code=200,
    )


async def api_task_action_endpoint(request: Request) -> JSONResponse:
    """Control a background task (`kill` or `send_input`)."""
    _, err_resp = await authenticate_client_request(request)
    if err_resp is not None:
        return err_resp

    thread_id = str(request.path_params.get("thread_id") or "").strip()
    task_id = str(request.path_params.get("task_id") or "").strip()
    if not thread_id or not task_id:
        return JSONResponse({"error": "thread_id and task_id are required"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    action = str(body.get("action") or "").strip().lower()
    manager = get_background_task_manager()

    if action == "kill":
        ok, message, record = await manager.kill_task(task_id, thread_id=thread_id)
        return JSONResponse(
            {"ok": ok, "message": message, "task": record},
            status_code=200 if ok else 404,
        )

    if action == "send_input":
        input_text = body.get("input_text")
        if not isinstance(input_text, str):
            return JSONResponse({"error": "input_text is required for send_input"}, status_code=400)
        ok, message, record = await manager.send_input(
            task_id,
            input_text,
            thread_id=thread_id,
        )
        return JSONResponse(
            {"ok": ok, "message": message, "task": record},
            status_code=200 if ok else 400,
        )

    return JSONResponse({"error": f"Unsupported action: {action!r}"}, status_code=400)


class ClientGatewayAddon(BaseServerAddon):
    """Built-in OSS addon exposing `/api/v1/*` remote session & background task routes."""

    name = "client-gateway"
    tier: AddonTier = "oss"
    version = "1.0.0"
    description = "Remote TUI/CLI session management, thread lifecycle, and SSE task streaming API"
    dependencies = ("core-auth",)
    capabilities = AddonCapabilities(routes=True)
    settings_schema = ClientGatewaySettings
    enabled_by_default = True

    def get_routes(self) -> list[BaseRoute]:
        """Retrieve routes."""
        return [
            Route("/api/v1/server/info", endpoint=server_info_endpoint, methods=["GET"]),
            Route("/api/v1/threads", endpoint=api_list_threads_endpoint, methods=["GET"]),
            Route("/api/v1/threads/recent", endpoint=api_recent_thread_endpoint, methods=["GET"]),
            Route("/api/v1/threads/similar", endpoint=api_similar_threads_endpoint, methods=["GET"]),
            Route("/api/v1/threads/{thread_id}", endpoint=api_get_thread_endpoint, methods=["GET"]),
            Route("/api/v1/threads/{thread_id}/title", endpoint=api_set_thread_title_endpoint, methods=["PATCH"]),
            Route("/api/v1/threads/{thread_id}/fork", endpoint=api_fork_thread_endpoint, methods=["POST"]),
            Route("/api/v1/threads/{thread_id}/prune", endpoint=api_prune_thread_endpoint, methods=["POST"]),
            Route("/api/v1/threads/{thread_id}", endpoint=api_delete_thread_endpoint, methods=["DELETE"]),
            Route("/api/v1/threads/{thread_id}/tasks", endpoint=api_list_thread_tasks_endpoint, methods=["GET"]),
            Route("/api/v1/threads/{thread_id}/tasks/stream", endpoint=api_stream_thread_tasks_endpoint, methods=["GET"]),
            Route("/api/v1/threads/{thread_id}/tasks/{task_id}/logs", endpoint=api_get_task_logs_endpoint, methods=["GET"]),
            Route("/api/v1/threads/{thread_id}/tasks/{task_id}/action", endpoint=api_task_action_endpoint, methods=["POST"]),
        ]

    async def health_check(self) -> AddonHealthStatus:
        """Execute health check on ClientGatewayAddon."""
        storage_backend = "postgres" if get_postgres_session_uri() else "sqlite"
        return AddonHealthStatus(
            name=self.name,
            status="ok",
            tier=self.tier,
            version=self.version,
            source=self.source,
            details={
                "storage_backend": storage_backend,
                "routes_count": len(self.get_routes()),
            },
        )

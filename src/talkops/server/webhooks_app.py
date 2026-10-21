"""Starlette custom routes application (`http.app`) mounted into the LangGraph Agent Server.

Exposes:
- Webhook Ingestion Routes (also mountable on a dedicated `--webhook-port`):
  - `GET /webhooks` — Discovery endpoint listing all supported webhook providers and active routes.
  - `GET /webhooks/deliveries` — Recent accepted webhook deliveries and mapped LangGraph thread IDs.
  - `POST /webhooks/{provider}` — Unified inbound webhook endpoint for SNS, PagerDuty, Alertmanager,
    CloudEvents, Jenkins, GitHub, Datadog, ServiceNow, Azure Monitor, and Generic JSON payloads.
- Remote Client & Multi-User Session Routes (`/api/v1/*`):
  - `GET /api/v1/server/info` — Central server capabilities, auth mode, storage backend, and caller identity.
  - `GET /api/v1/threads` — List threads scoped to the authenticated operator (plus shared webhook threads).
  - `GET /api/v1/threads/recent` — Most recently updated thread for `-r` / `--resume`.
  - `GET /api/v1/threads/similar` — Prefix search for thread IDs.
  - `GET /api/v1/threads/{thread_id}` — Thread existence and title lookup.
  - `PATCH /api/v1/threads/{thread_id}/title` — Persist conversation title & preview.
  - `POST /api/v1/threads/{thread_id}/fork` — Fork thread checkpoints and summary on the server.
  - `POST /api/v1/threads/{thread_id}/prune` — Compact thread checkpoints on the server.
  - `DELETE /api/v1/threads/{thread_id}` — Delete thread and checkpoints for the authenticated operator.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from talkops.server.auth import get_auth_mode, resolve_user_from_headers
from talkops.tasks.manager import get_background_task_manager
from talkops.state.postgres_session import get_postgres_session_uri
from talkops.state.session import (
     bind_user_context,
    delete_thread,
    find_similar_threads,
    fork_thread,
    get_most_recent,
    get_thread_title,
    list_threads,
    set_thread_title,
    thread_exists,
    SessionManager,
)
from talkops.addons.builtin.webhooks import (
    get_webhook_dispatcher as _addon_get_webhook_dispatcher,
    set_webhook_dispatcher as _addon_set_webhook_dispatcher,
)
from talkops.webhooks.dispatcher import WebhookDispatcher


def get_webhook_dispatcher() -> WebhookDispatcher:
    """Return or initialize the singleton WebhookDispatcher for the server process."""
    return _addon_get_webhook_dispatcher()


def set_webhook_dispatcher(dispatcher: WebhookDispatcher | None) -> None:
    """Override the active WebhookDispatcher (used for testing and custom injection)."""
    _addon_set_webhook_dispatcher(dispatcher)


async def _authenticate_client_request(request: Request) -> tuple[str | None, JSONResponse | None]:
    """Authenticate an `/api/v1/*` request and return `(user_id, error_response)`."""
    try:
        user_dict = await resolve_user_from_headers(request.headers, path=request.url.path)
        user_id = str(user_dict.get("identity") or "default")
        return user_id, None
    except Exception as exc:
        status_code = getattr(exc, "status_code", 401)
        detail = getattr(exc, "detail", str(exc))
        return None, JSONResponse({"error": detail}, status_code=status_code)


# ── Webhook Routes ──────────────────────────────────────────────────────


async def list_webhooks_endpoint(request: Request) -> JSONResponse:
    """Return supported webhook providers, aliases, and configuration summary."""
    dispatcher = get_webhook_dispatcher()
    providers = dispatcher.registry.list_providers()
    return JSONResponse(
        {
            "status": "ok",
            "service": "talkops-webhook-server",
            "providers": providers,
            "recent_delivery_count": len(dispatcher.recent_deliveries),
        },
        status_code=200,
    )


async def list_deliveries_endpoint(request: Request) -> JSONResponse:
    """Return recent accepted webhook deliveries and their LangGraph thread IDs."""
    dispatcher = get_webhook_dispatcher()
    return JSONResponse(
        {
            "status": "ok",
            "deliveries": dispatcher.recent_deliveries,
        },
        status_code=200,
    )


async def receive_webhook_endpoint(request: Request) -> JSONResponse:
    """Ingest, verify, normalize, and dispatch an incoming third-party webhook."""
    provider = str(request.path_params.get("provider") or "generic")
    raw_body = await request.body()
    headers = dict(request.headers)
    query_params = dict(request.query_params)

    dispatcher = get_webhook_dispatcher()
    status_code, payload = await dispatcher.handle_webhook(
        provider=provider,
        headers=headers,
        raw_body=raw_body,
        query_params=query_params,
    )
    return JSONResponse(payload, status_code=status_code)


# ── Client Session, Auth Token Exchange & Server Info Routes (`/api/v1/*`) ───


async def auth_token_exchange_endpoint(request: Request) -> JSONResponse:
    """Exchange a long-lived API key for a short-lived signed JWT (`POST /api/v1/auth/token`)."""
    from talkops.server.auth import exchange_api_key_for_temp_token

    headers = dict(request.headers)
    ttl_seconds: int | None = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            if body.get("api_key") and "x-api-key" not in {k.lower() for k in headers}:
                headers["x-api-key"] = str(body["api_key"]).strip()
            if body.get("user_id") and "x-talkops-user" not in {k.lower() for k in headers}:
                headers["x-talkops-user"] = str(body["user_id"]).strip()
            if isinstance(body.get("ttl_seconds"), int) and body["ttl_seconds"] > 0:
                ttl_seconds = min(86400, int(body["ttl_seconds"]))
    except Exception:
        pass

    try:
        token_payload = await exchange_api_key_for_temp_token(headers, ttl_seconds=ttl_seconds)
        return JSONResponse(token_payload, status_code=200)
    except Exception as exc:
        status_code = getattr(exc, "status_code", 401)
        detail = getattr(exc, "detail", str(exc))
        return JSONResponse(
            {"error": "unauthorized", "detail": detail},
            status_code=int(status_code) if isinstance(status_code, int) else 401,
        )


async def server_info_endpoint(request: Request) -> JSONResponse:
    """Return server mode metadata, storage backend, auth mode, and authenticated user."""
    user_id, err_resp = await _authenticate_client_request(request)
    if err_resp is not None:
        return err_resp

    storage_backend = "postgres" if get_postgres_session_uri() else "sqlite"
    return JSONResponse(
        {
            "status": "ok",
            "service": "talkops-central-server",
            "auth_mode": get_auth_mode(),
            "storage_backend": storage_backend,
            "user_id": user_id,
        },
        status_code=200,
    )


async def api_list_threads_endpoint(request: Request) -> JSONResponse:
    """List threads for the authenticated user (plus shared webhook threads)."""
    user_id, err_resp = await _authenticate_client_request(request)
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
    user_id, err_resp = await _authenticate_client_request(request)
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
    user_id, err_resp = await _authenticate_client_request(request)
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
    user_id, err_resp = await _authenticate_client_request(request)
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
    user_id, err_resp = await _authenticate_client_request(request)
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
    user_id, err_resp = await _authenticate_client_request(request)
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
    user_id, err_resp = await _authenticate_client_request(request)
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
    user_id, err_resp = await _authenticate_client_request(request)
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
    _, err_resp = await _authenticate_client_request(request)
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
    _, err_resp = await _authenticate_client_request(request)
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
    _, err_resp = await _authenticate_client_request(request)
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
    _, err_resp = await _authenticate_client_request(request)
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


async def webhook_health_endpoint(request: Request) -> JSONResponse:
    """Health check endpoint for webhook listeners (`/webhooks/health`)."""
    del request
    return JSONResponse({"status": "ok"}, status_code=200)


async def oidc_configuration_endpoint(request: Request) -> JSONResponse:
    """OpenID Connect Discovery 1.0 endpoint (`/.well-known/openid-configuration`)."""
    from talkops.server.oidc_provider import get_oidc_provider

    base_url = f"{request.url.scheme}://{request.url.netloc}" if request.url.netloc else None
    config = get_oidc_provider().get_openid_configuration(base_url=base_url)
    return JSONResponse(config, status_code=200)


async def oidc_jwks_endpoint(request: Request) -> JSONResponse:
    """JSON Web Key Set endpoint (`/.well-known/jwks.json`)."""
    del request
    from talkops.server.oidc_provider import get_oidc_provider

    return JSONResponse(get_oidc_provider().get_jwks(), status_code=200)


def create_webhooks_only_app(dispatcher: WebhookDispatcher | None = None) -> Starlette:
    """Create a standalone Starlette app exposing ONLY `/webhooks/*` routes (for `--webhook-port`)."""
    if dispatcher is not None:
        set_webhook_dispatcher(dispatcher)

    routes = [
        Route("/webhooks", endpoint=list_webhooks_endpoint, methods=["GET"]),
        Route("/webhooks/health", endpoint=webhook_health_endpoint, methods=["GET"]),
        Route("/webhooks/deliveries", endpoint=list_deliveries_endpoint, methods=["GET"]),
        Route("/webhooks/{provider}", endpoint=receive_webhook_endpoint, methods=["POST"]),
        Route("/.well-known/openid-configuration", endpoint=oidc_configuration_endpoint, methods=["GET"]),
        Route("/.well-known/jwks.json", endpoint=oidc_jwks_endpoint, methods=["GET"]),
    ]
    return Starlette(routes=routes)


def create_webhooks_app(
    dispatcher: WebhookDispatcher | None = None,
    *,
    profile: str | None = None,
    addons: str | list[str] | tuple[str, ...] | None = None,
    enabled_addons: str | list[str] | tuple[str, ...] | None = None,
    disable_addons: str | list[str] | tuple[str, ...] | None = None,
    disabled_addons: str | list[str] | tuple[str, ...] | None = None,
    custom_addons: list[str] | tuple[str, ...] | None = None,
    custom_addon_specs: list[str] | tuple[str, ...] | None = None,
    addons_config: Any | None = None,
    addons_config_path: Any | None = None,
    extra_addons: list[Any] | None = None,
    gateway: Any | None = None,
) -> Starlette:
    """Create the combined Starlette custom routes app via `AddonManager` for `langgraph.json` (`http.app`)."""
    from talkops.server.asgi import create_server_app

    return create_server_app(
        dispatcher=dispatcher,
        profile=profile,
        addons=addons,
        enabled_addons=enabled_addons,
        disable_addons=disable_addons,
        disabled_addons=disabled_addons,
        custom_addons=custom_addons,
        custom_addon_specs=custom_addon_specs,
        addons_config=addons_config,
        addons_config_path=addons_config_path,
        extra_addons=extra_addons,
        gateway=gateway,
    )


app = create_webhooks_app()
webhooks_only_app = create_webhooks_only_app()

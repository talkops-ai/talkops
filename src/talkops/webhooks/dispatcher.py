"""Central webhook dispatcher coordinating verification, normalization, thread mapping, and background runs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import contextlib
import hmac
import time
from typing import Any
import uuid

from talkops.integrations.hooks import dispatch_hook_fire_and_forget
from talkops.integrations.identity import IdentityMapper
from talkops.integrations.thread_store import ThreadStore
from talkops.security.approval_mode import (
    APPROVAL_MODE_NAMESPACE,
    approval_mode_key,
    approval_mode_payload,
    coerce_approval_mode,
)
from talkops.server._server_config import ServerConfig
from talkops.utils.logger import bind_logging_to_thread, get_logger
from talkops.webhooks.models import (
    NormalizedWebhookEvent,
    WebhookConfig,
    WebhookEventStatus,
)
from talkops.webhooks.providers import (
    WebhookParseError,
    WebhookProviderRegistry,
    create_default_registry,
)
from talkops.webhooks.security import DeduplicationCache, verify_request_signature
from talkops.webhooks.sinks import (
    WebhookRunResult,
    confirm_sns_subscription,
    dispatch_completion_sinks,
)

logger = get_logger(__name__)

RunExecutor = Callable[[NormalizedWebhookEvent, str, str, WebhookConfig], Awaitable[WebhookRunResult]]


def _extract_last_assistant_text(state_values: dict[str, Any] | None) -> str:
    """Extract the final assistant text message from a LangGraph thread state."""
    if not isinstance(state_values, dict):
        return ""
    messages = state_values.get("messages")
    if not isinstance(messages, list):
        return ""

    for msg in reversed(messages):
        if isinstance(msg, dict):
            role = str(msg.get("type") or msg.get("role") or "").lower()
            if role in {"ai", "assistant"}:
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    return content.strip()
                if isinstance(content, list):
                    parts = [
                        b.get("text", "") if isinstance(b, dict) else str(b)
                        for b in content
                        if not isinstance(b, dict) or b.get("type") not in {"thinking", "thought", "reasoning"}
                    ]
                    joined = "\n".join(p for p in parts if p).strip()
                    if joined:
                        return joined
        elif hasattr(msg, "content"):
            msg_type = getattr(msg, "type", "").lower()
            if msg_type in {"ai", "assistant"}:
                content = getattr(msg, "content", "")
                if isinstance(content, str) and content.strip():
                    return content.strip()
    return ""


def _log_messages_tool_activity(
    messages: list[Any],
    *,
    run_id: str,
    thread_id: str,
    seen_tool_calls: set[str],
    seen_tool_results: set[str],
) -> None:
    """Log tool calls and tool completion messages from a list of state messages."""
    for idx, msg in enumerate(messages):
        if isinstance(msg, Mapping):
            msg_type = str(msg.get("type") or msg.get("role") or "").lower()
            tool_calls = msg.get("tool_calls") or []
            msg_id = str(msg.get("id") or f"idx-{idx}")
        else:
            msg_type = str(getattr(msg, "type", "") or getattr(msg, "role", "")).lower()
            tool_calls = getattr(msg, "tool_calls", None) or []
            msg_id = str(getattr(msg, "id", None) or f"idx-{idx}")

        if isinstance(tool_calls, list):
            for tc_idx, tc in enumerate(tool_calls):
                if isinstance(tc, Mapping):
                    tc_id = str(tc.get("id") or f"{msg_id}-tc-{tc_idx}")
                    tc_name = str(tc.get("name") or "tool")
                    tc_args = tc.get("args") or {}
                else:
                    tc_id = str(getattr(tc, "id", None) or f"{msg_id}-tc-{tc_idx}")
                    tc_name = str(getattr(tc, "name", None) or "tool")
                    tc_args = getattr(tc, "args", None) or {}

                if tc_id in seen_tool_calls:
                    continue
                seen_tool_calls.add(tc_id)
                args_preview = str(tc_args)
                if len(args_preview) > 160:
                    args_preview = args_preview[:157] + "..."
                logger.info(
                    "Webhook agent tool call | run_id=%s thread_id=%s tool=%s args=%s",
                    run_id,
                    thread_id,
                    tc_name,
                    args_preview,
                )

        if msg_type == "tool":
            if isinstance(msg, Mapping):
                tc_id = str(msg.get("tool_call_id") or msg.get("id") or f"res-{idx}")
                t_name = str(msg.get("name") or "tool")
                t_status = str(msg.get("status") or "success")
            else:
                tc_id = str(getattr(msg, "tool_call_id", None) or getattr(msg, "id", None) or f"res-{idx}")
                t_name = str(getattr(msg, "name", None) or "tool")
                t_status = str(getattr(msg, "status", None) or "success")

            if tc_id in seen_tool_results:
                continue
            seen_tool_results.add(tc_id)
            logger.info(
                "Webhook agent tool completed | run_id=%s thread_id=%s tool=%s status=%s",
                run_id,
                thread_id,
                t_name,
                t_status,
            )


def _log_stream_update_part(
    part: Any,
    *,
    run_id: str,
    thread_id: str,
    seen_tool_calls: set[str],
    seen_tool_results: set[str],
) -> None:
    """Process a single streaming part from `client.runs.join_stream` and emit live logs."""
    event_name = getattr(part, "event", None) if not isinstance(part, Mapping) else part.get("event")
    data = getattr(part, "data", None) if not isinstance(part, Mapping) else part.get("data")
    ev = str(event_name or "")

    if ev == "error" and data:
        logger.error("Webhook agent stream error | run_id=%s thread_id=%s error=%s", run_id, thread_id, data)
        return

    if ev.startswith("updates") and isinstance(data, Mapping):
        for _node_name, node_payload in data.items():
            if not isinstance(node_payload, Mapping):
                continue
            raw_msgs = node_payload.get("messages")
            if isinstance(raw_msgs, Mapping) and "value" in raw_msgs:
                raw_msgs = raw_msgs.get("value")
            if isinstance(raw_msgs, list):
                _log_messages_tool_activity(
                    raw_msgs,
                    run_id=run_id,
                    thread_id=thread_id,
                    seen_tool_calls=seen_tool_calls,
                    seen_tool_results=seen_tool_results,
                )
    elif ev.startswith("values") and isinstance(data, Mapping):
        raw_msgs = data.get("messages")
        if isinstance(raw_msgs, Mapping) and "value" in raw_msgs:
            raw_msgs = raw_msgs.get("value")
        if isinstance(raw_msgs, list):
            _log_messages_tool_activity(
                raw_msgs,
                run_id=run_id,
                thread_id=thread_id,
                seen_tool_calls=seen_tool_calls,
                seen_tool_results=seen_tool_results,
            )


async def default_langgraph_run_executor(
    event: NormalizedWebhookEvent,
    lg_thread_id: str,
    delivery_id: str,
    config: WebhookConfig,
) -> WebhookRunResult:
    """Execute an autonomous background agent run via the LangGraph SDK (in-process or loopback)."""
    import os
    from langgraph_sdk import get_client
    from talkops.server.auth import get_internal_webhook_token
    from talkops.state.session import set_thread_title

    start_time = time.monotonic()
    bind_logging_to_thread(lg_thread_id)

    server_cfg = ServerConfig.from_env()
    resolved_mode = coerce_approval_mode(server_cfg.approval_mode, allow_yolo=True)
    assistant_id = config.assistant_id or server_cfg.assistant_id
    prompt = event.format_agent_prompt(custom_instructions=config.custom_prompt_instructions)

    logger.info(
        "Starting webhook agent run | delivery_id=%s provider=%s incident_id=%s thread_id=%s approval_mode=%s read_only=%s model=%s",
        delivery_id,
        event.provider,
        event.incident_id,
        lg_thread_id,
        resolved_mode.value,
        server_cfg.read_only,
        server_cfg.model or "default",
    )

    internal_url = os.environ.get("TALKOPS_INTERNAL_LANGGRAPH_URL", "").strip() or None
    internal_headers = {
        "X-TalkOps-Internal-Webhook": get_internal_webhook_token(),
        "X-TalkOps-User": "webhook",
    }
    client = get_client(url=internal_url, headers=internal_headers)
    webhook_title = f"[{event.provider.upper()}] {event.title}" if event.title else f"[{event.provider.upper()}] Incident {event.incident_id}"
    await client.threads.create(
        thread_id=lg_thread_id,
        if_exists="do_nothing",
        metadata={
            "owner": "webhook",
            "source": "webhook",
            "provider": event.provider,
            "incident_id": event.incident_id,
            "service_name": event.service_name,
            "title": webhook_title,
        },
    )
    with contextlib.suppress(Exception):
        await set_thread_title(
            lg_thread_id,
            webhook_title,
            preview=event.summary or prompt[:200],
            only_if_empty=True,
            user_id="webhook",
        )

    store_key = approval_mode_key(lg_thread_id)
    with contextlib.suppress(Exception):
        await client.store.put_item(
            list(APPROVAL_MODE_NAMESPACE),
            key=store_key,
            value=dict(approval_mode_payload(mode=resolved_mode)),
            index=False,
        )

    context: dict[str, Any] = {
        "thread_id": lg_thread_id,
        "approval_mode": resolved_mode.value,
        "approval_mode_key": store_key,
        "read_only": server_cfg.read_only,
        "yolo": resolved_mode.value == "yolo",
        "smart": resolved_mode.value == "smart",
        "webhook_provider": event.provider,
        "webhook_event_type": event.event_type,
        "webhook_incident_id": event.incident_id,
        "source_addon": "webhooks",
        "interactive": False,
    }
    if server_cfg.model:
        context["model"] = server_cfg.model
    if server_cfg.shell_allow_list:
        context["shell_allow_list"] = list(server_cfg.shell_allow_list)

    run = await client.runs.create(
        thread_id=lg_thread_id,
        assistant_id=assistant_id,
        input={"messages": [{"role": "user", "content": prompt}]},
        context=context,
        metadata={
            "delivery_id": delivery_id,
            "provider": event.provider,
            "event_type": event.event_type,
            "incident_id": event.incident_id,
            "severity": str(event.severity),
        },
        multitask_strategy="enqueue",
        stream_mode=["values", "updates"],
    )
    run_id = str(run.get("run_id") or delivery_id)
    logger.info(
        "LangGraph run created | run_id=%s delivery_id=%s thread_id=%s assistant_id=%s",
        run_id,
        delivery_id,
        lg_thread_id,
        assistant_id,
    )

    seen_tool_calls: set[str] = set()
    seen_tool_results: set[str] = set()

    # Stream real-time step/tool updates while the background run executes
    if hasattr(client.runs, "join_stream"):
        with contextlib.suppress(Exception):
            stream_iter = client.runs.join_stream(
                lg_thread_id,
                run_id,
                stream_mode=["values", "updates"],
            )
            if hasattr(stream_iter, "__aiter__"):
                async for part in stream_iter:
                    _log_stream_update_part(
                        part,
                        run_id=run_id,
                        thread_id=lg_thread_id,
                        seen_tool_calls=seen_tool_calls,
                        seen_tool_results=seen_tool_results,
                    )

    await client.runs.join(lg_thread_id, run_id)
    run_info = await client.runs.get(lg_thread_id, run_id)
    status = str(run_info.get("status") or "success")
    error_obj = run_info.get("error")
    error_msg = str(error_obj.get("message") or error_obj) if isinstance(error_obj, dict) else (str(error_obj) if error_obj else None)

    state = await client.threads.get_state(lg_thread_id)
    raw_values = state.get("values") if isinstance(state, dict) else None
    values: dict[str, Any] | None = raw_values if isinstance(raw_values, dict) else None
    if values is not None and isinstance(values.get("messages"), list):
        _log_messages_tool_activity(
            values["messages"],
            run_id=run_id,
            thread_id=lg_thread_id,
            seen_tool_calls=seen_tool_calls,
            seen_tool_results=seen_tool_results,
        )
    analysis_text = _extract_last_assistant_text(values)
    elapsed = time.monotonic() - start_time

    if error_msg or status == "error":
        logger.error(
            "Webhook agent run failed | run_id=%s thread_id=%s provider=%s incident_id=%s status=%s duration=%.2fs error=%s",
            run_id,
            lg_thread_id,
            event.provider,
            event.incident_id,
            status,
            elapsed,
            error_msg or "unknown_error",
        )
    else:
        logger.info(
            "Webhook agent run completed | run_id=%s thread_id=%s provider=%s incident_id=%s status=%s duration=%.2fs tools_called=%d rca_chars=%d",
            run_id,
            lg_thread_id,
            event.provider,
            event.incident_id,
            status,
            elapsed,
            len(seen_tool_calls),
            len(analysis_text),
        )
        if analysis_text:
            preview = " ".join(analysis_text.split())[:240]
            logger.info(
                "Webhook RCA summary | run_id=%s incident_id=%s preview=%r",
                run_id,
                event.incident_id,
                preview + ("..." if len(analysis_text) > 240 else ""),
            )

    return WebhookRunResult(
        run_id=run_id,
        thread_id=lg_thread_id,
        status=status,
        analysis_text=analysis_text,
        error=error_msg,
    )


def create_gateway_run_executor(gateway: Any) -> RunExecutor:
    """Create an in-memory `RunExecutor` backed directly by `AgentRunGateway` (no HTTP loopback)."""

    async def _gateway_executor(
        event: NormalizedWebhookEvent,
        lg_thread_id: str,
        delivery_id: str,
        config: WebhookConfig,
    ) -> WebhookRunResult:
        from talkops.state.session import set_thread_title

        bind_logging_to_thread(lg_thread_id)
        server_cfg = ServerConfig.from_env()
        resolved_mode = coerce_approval_mode(server_cfg.approval_mode, allow_yolo=True)
        prompt = event.format_agent_prompt(custom_instructions=config.custom_prompt_instructions)
        webhook_title = (
            f"[{event.provider.upper()}] {event.title}"
            if event.title
            else f"[{event.provider.upper()}] Incident {event.incident_id}"
        )

        with contextlib.suppress(Exception):
            await set_thread_title(
                lg_thread_id,
                webhook_title,
                preview=event.summary or prompt[:200],
                only_if_empty=True,
                user_id="webhook",
            )

        store_key = approval_mode_key(lg_thread_id)
        with contextlib.suppress(Exception):
            await gateway.put_store_item(
                tuple(APPROVAL_MODE_NAMESPACE),
                key=store_key,
                value=dict(approval_mode_payload(mode=resolved_mode)),
            )

        context: dict[str, Any] = {
            "thread_id": lg_thread_id,
            "approval_mode": resolved_mode.value,
            "approval_mode_key": store_key,
            "read_only": server_cfg.read_only,
            "yolo": resolved_mode.value == "yolo",
            "smart": resolved_mode.value == "smart",
            "webhook_provider": event.provider,
            "webhook_event_type": event.event_type,
            "webhook_incident_id": event.incident_id,
        }
        if server_cfg.model:
            context["model"] = server_cfg.model
        if server_cfg.shell_allow_list:
            context["shell_allow_list"] = list(server_cfg.shell_allow_list)

        handle = await gateway.trigger_run(
            prompt,
            thread_id=lg_thread_id,
            source_addon="webhooks",
            context=context,
            metadata={
                "owner": "webhook",
                "source": "webhook",
                "delivery_id": delivery_id,
                "provider": event.provider,
                "event_type": event.event_type,
                "incident_id": event.incident_id,
                "service_name": event.service_name,
                "title": webhook_title,
            },
            wait=True,
        )
        return WebhookRunResult(
            run_id=handle.run_id,
            thread_id=lg_thread_id,
            status=handle.status,
            analysis_text=handle.analysis_text,
            error=handle.error,
        )

    return _gateway_executor


class WebhookDispatcher:
    """Orchestrates webhook verification, provider parsing, deduplication, and async run execution."""

    def __init__(
        self,
        *,
        registry: WebhookProviderRegistry | None = None,
        config: WebhookConfig | None = None,
        thread_store: ThreadStore | None = None,
        identity_mapper: IdentityMapper | None = None,
        dedup_cache: DeduplicationCache | None = None,
        run_executor: RunExecutor | None = None,
        gateway: Any | None = None,
        event_bus: Any | None = None,
    ) -> None:
        self.registry = registry or create_default_registry()
        self.config = config or WebhookConfig.from_settings()
        self.thread_store = thread_store or ThreadStore()
        self.identity_mapper = identity_mapper or IdentityMapper()
        self.dedup_cache = dedup_cache or DeduplicationCache(
            ttl_seconds=self.config.dedup_ttl_seconds
        )
        self._gateway = gateway
        self._event_bus = event_bus or (getattr(gateway, "event_bus", None) if gateway is not None else None)
        self._outbound_sinks: dict[str, Callable[..., Awaitable[bool]]] = {}
        if run_executor is not None:
            self.run_executor: RunExecutor = run_executor
        elif gateway is not None and getattr(gateway, "graph", None) is not None:
            self.run_executor = create_gateway_run_executor(gateway)
        else:
            self.run_executor = default_langgraph_run_executor
        self._concurrency_semaphore = asyncio.Semaphore(max(1, self.config.max_concurrent_runs))
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._recent_deliveries: list[dict[str, Any]] = []

    def bind_event_bus(self, event_bus: Any | None) -> None:
        """Bind the server's `HookEventBus` so marketplace addons receive `webhook.received` and `webhook.completed`."""
        self._event_bus = event_bus

    def register_outbound_sink(
        self,
        name: str,
        callback: Callable[..., Awaitable[bool]],
    ) -> None:
        """Register a marketplace addon outbound notification sink (e.g., `channels`)."""
        self._outbound_sinks[name] = callback

    def unregister_outbound_sink(self, name: str) -> None:
        """Unregister a previously registered outbound notification sink."""
        self._outbound_sinks.pop(name, None)

    async def handle_webhook(
        self,
        provider: str,
        headers: Mapping[str, str],
        raw_body: bytes,
        query_params: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Process an incoming HTTP webhook request and return `(status_code, response_dict)`."""
        query = query_params or {}
        adapter = self.registry.get(provider)
        if adapter is None:
            logger.warning("Rejected webhook for unknown provider %r", provider)
            return 404, {
                "error": f"Unknown webhook provider: {provider!r}",
                "supported_providers": [p["provider"] for p in self.registry.list_providers()],
            }

        canonical_provider = adapter.name
        secret = self.config.get_provider_secret(canonical_provider)

        # Verify HMAC signature, header token, or ?token= query parameter
        if secret:
            query_token = query.get("token") or query.get("secret")
            token_matched = bool(query_token and hmac.compare_digest(secret, query_token))
            if not token_matched and not verify_request_signature(canonical_provider, headers, raw_body, secret):
                logger.warning("Rejected webhook for provider %r due to invalid signature/token", canonical_provider)
                return 401, {"error": "invalid webhook signature"}
            logger.debug("Verified webhook signature/token for provider=%s", canonical_provider)

        try:
            event = adapter.parse(headers, raw_body, self.config)
        except WebhookParseError as exc:
            logger.warning("Failed to parse %s webhook payload: %s", canonical_provider, exc)
            return 400, {"error": str(exc)}
        except Exception as exc:
            logger.exception("Unexpected error parsing %s webhook", canonical_provider)
            return 400, {"error": f"malformed webhook payload: {exc}"}

        if query.get("callback_url") and not event.callback_url:
            event.callback_url = query["callback_url"]

        logger.info(
            "Webhook received | provider=%s event_type=%s incident_id=%s severity=%s status=%s service=%s title=%r",
            event.provider,
            event.event_type,
            event.incident_id,
            event.severity.value,
            event.status.value,
            event.service_name or "-",
            event.title,
        )

        dispatch_hook_fire_and_forget(
            "webhook.received",
            {
                "provider": event.provider,
                "event_type": event.event_type,
                "incident_id": event.incident_id,
                "title": event.title,
                "severity": str(event.severity),
                "status": str(event.status),
                "should_trigger_run": event.should_trigger_run,
            },
        )

        # 1. Handle provider verification / handshake challenges (e.g. AWS SNS SubscriptionConfirmation, GitHub Ping)
        if event.status == WebhookEventStatus.CHALLENGE:
            logger.info(
                "Webhook challenge accepted | provider=%s event_type=%s auto_confirm_sns=%s",
                event.provider,
                event.event_type,
                self.config.auto_confirm_sns,
            )
            if (
                event.event_type == "sns.subscription_confirmation"
                and self.config.auto_confirm_sns
                and event.metadata.get("subscribe_url")
            ):
                sub_url = str(event.metadata["subscribe_url"])
                task = asyncio.create_task(confirm_sns_subscription(sub_url))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            return 200, event.challenge_response or {"status": "challenge_accepted"}

        # 2. Ignore non-actionable / resolved events
        if not event.should_trigger_run:
            logger.info(
                "Webhook ignored (non-actionable) | provider=%s event_type=%s incident_id=%s reason=%s",
                event.provider,
                event.event_type,
                event.incident_id,
                event.ignore_reason or "non_actionable_event",
            )
            return 202, {
                "status": "ignored",
                "provider": event.provider,
                "event_type": event.event_type,
                "incident_id": event.incident_id,
                "reason": event.ignore_reason or "non_actionable_event",
            }

        # 3. Alert storm & retry deduplication
        if self.dedup_cache.check_and_record(event.effective_dedup_key):
            logger.info("Deduplicated repeated webhook event %s", event.effective_dedup_key)
            return 202, {
                "status": "deduplicated",
                "provider": event.provider,
                "event_type": event.event_type,
                "incident_id": event.incident_id,
                "dedup_key": event.effective_dedup_key,
            }

        # 4. Resolve deterministic LangGraph thread_id and caller RBAC identity
        lg_thread_id = self.thread_store.resolve(event.provider, event.incident_id)
        identity = self.identity_mapper.resolve(event.provider, event.caller_id)
        delivery_id = str(uuid.uuid4())

        logger.info(
            "Webhook accepted & queued | delivery_id=%s provider=%s incident_id=%s thread_id=%s caller=%s role=%s",
            delivery_id,
            event.provider,
            event.incident_id,
            lg_thread_id,
            identity.internal_user_id,
            identity.rbac_role,
        )

        delivery_record: dict[str, Any] = {
            "delivery_id": delivery_id,
            "provider": event.provider,
            "event_type": event.event_type,
            "incident_id": event.incident_id,
            "thread_id": lg_thread_id,
            "title": event.title,
            "severity": str(event.severity),
            "caller": identity.internal_user_id,
            "rbac_role": identity.rbac_role,
            "run_status": "running",
        }
        self._recent_deliveries.append(delivery_record)
        if len(self._recent_deliveries) > 100:
            self._recent_deliveries.pop(0)

        # 5. Publish `webhook.received` on HookEventBus for marketplace channel addons
        if self._event_bus is not None and hasattr(self._event_bus, "publish"):
            matching_received_routes = [
                r.to_dict()
                for r in self.config.outbound_channels
                if r.matches(event, stage="received")
            ]
            with contextlib.suppress(Exception):
                await self._event_bus.publish(
                    "webhook.received",
                    {
                        "provider": event.provider,
                        "event_type": event.event_type,
                        "incident_id": event.incident_id,
                        "thread_id": lg_thread_id,
                        "delivery_id": delivery_id,
                        "title": event.title,
                        "summary": event.summary,
                        "severity": event.severity.value,
                        "status": event.status.value,
                        "service_name": event.service_name,
                        "source_url": event.source_url,
                        "outbound_channels": matching_received_routes,
                    },
                    thread_id=lg_thread_id,
                    run_id=delivery_id,
                    source_addon="webhooks",
                )

        # 6. Schedule background run and return 202 Accepted immediately
        bg_task = asyncio.create_task(
            self._execute_and_notify(event, lg_thread_id, delivery_id, delivery_record)
        )
        self._background_tasks.add(bg_task)
        bg_task.add_done_callback(self._background_tasks.discard)

        return 202, {
            "status": "accepted",
            "delivery_id": delivery_id,
            "provider": event.provider,
            "event_type": event.event_type,
            "incident_id": event.incident_id,
            "thread_id": lg_thread_id,
            "severity": str(event.severity),
            "title": event.title,
        }

    async def _execute_and_notify(
        self,
        event: NormalizedWebhookEvent,
        lg_thread_id: str,
        delivery_id: str,
        delivery_record: dict[str, Any] | None = None,
    ) -> None:
        """Run the agent investigation in the background and trigger outbound write-back sinks."""
        try:
            async with self._concurrency_semaphore:
                logger.info(
                    "Executing background webhook investigation | delivery_id=%s provider=%s incident_id=%s thread_id=%s",
                    delivery_id,
                    event.provider,
                    event.incident_id,
                    lg_thread_id,
                )
                result = await self.run_executor(event, lg_thread_id, delivery_id, self.config)
        except Exception as exc:
            logger.exception(
                "Background webhook run failed for provider=%s incident=%s",
                event.provider,
                event.incident_id,
            )
            result = WebhookRunResult(
                run_id=delivery_id,
                thread_id=lg_thread_id,
                status="error",
                analysis_text="",
                error=str(exc),
            )

        if delivery_record is not None:
            delivery_record["run_id"] = result.run_id
            delivery_record["run_status"] = result.status
            if result.analysis_text:
                delivery_record["analysis_preview"] = result.analysis_text[:1000]
            if result.error:
                delivery_record["error"] = result.error

        await dispatch_completion_sinks(event, result, self.config)

        # Publish `webhook.completed` on HookEventBus and invoke registered marketplace channel sinks
        matching_completed_routes = [
            r.to_dict()
            for r in self.config.outbound_channels
            if r.matches(event, stage="completed")
        ]
        if self._event_bus is not None and hasattr(self._event_bus, "publish"):
            with contextlib.suppress(Exception):
                await self._event_bus.publish(
                    "webhook.completed",
                    {
                        "provider": event.provider,
                        "event_type": event.event_type,
                        "incident_id": event.incident_id,
                        "thread_id": result.thread_id,
                        "run_id": result.run_id,
                        "status": result.status,
                        "title": event.title,
                        "summary": event.summary,
                        "severity": event.severity.value,
                        "service_name": event.service_name,
                        "source_url": event.source_url,
                        "analysis": result.analysis_text,
                        "error": result.error,
                        "outbound_channels": matching_completed_routes,
                    },
                    thread_id=result.thread_id,
                    run_id=result.run_id,
                    source_addon="webhooks",
                )

        for sink_name, sink_cb in list(self._outbound_sinks.items()):
            try:
                await sink_cb(event, result, matching_completed_routes)
            except Exception as exc:
                logger.warning("Registered outbound webhook sink %r failed: %s", sink_name, exc)


    async def drain_background_tasks(self) -> None:
        """Await all in-flight background webhook runs (used in tests and graceful shutdown)."""
        if self._background_tasks:
            await asyncio.gather(*list(self._background_tasks), return_exceptions=True)

    @property
    def recent_deliveries(self) -> list[dict[str, Any]]:
        """Return recent accepted webhook deliveries for status inspection."""
        return list(self._recent_deliveries)

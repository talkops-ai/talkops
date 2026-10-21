"""Built-in WebhookAddon (`webhooks`) for TalkOps Server Mode.

Wraps the 9+ built-in webhook provider adapters (AWS SNS/CloudWatch/EventBridge,
PagerDuty v3, Alertmanager/Prometheus/Grafana, CloudEvents/Argo/Flux/Tekton,
Jenkins, GitHub, Datadog, ServiceNow, Azure Monitor, Generic JSON), HMAC
signature verification, deduplication cache, and outbound completion sinks into
a first-class `ServerAddon` enabled by default.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from talkops.addons.config import AddonSettings
from talkops.addons.protocol import (
    AddonCapabilities,
    AddonContext,
    AddonHealthStatus,
    AddonTier,
    BaseServerAddon,
)
from talkops.webhooks.dispatcher import (
    WebhookDispatcher,
    create_gateway_run_executor,
    default_langgraph_run_executor,
)

_dispatcher: WebhookDispatcher | None = None


def get_webhook_dispatcher() -> WebhookDispatcher:
    """Return or initialize the singleton `WebhookDispatcher` for the server process."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = WebhookDispatcher()
    return _dispatcher


def set_webhook_dispatcher(dispatcher: WebhookDispatcher | None) -> None:
    """Override the active `WebhookDispatcher` (used for testing and custom injection)."""
    global _dispatcher
    _dispatcher = dispatcher


class WebhookAddonSettings(AddonSettings):
    """Configuration schema for `WebhookAddon` (`webhooks`)."""

    enabled: bool = True
    max_concurrent_runs: int | None = Field(default=None, ge=1)
    dedup_ttl_seconds: float | None = Field(default=None, gt=0)
    default_secret: str | None = None
    auto_confirm_sns: bool | None = None
    outbound_channels: list[dict[str, Any]] | str | None = None


async def list_webhooks_endpoint(_request: Request) -> JSONResponse:
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


async def webhook_health_endpoint(_request: Request) -> JSONResponse:
    """Health check endpoint for webhook listeners (`/webhooks/health`)."""
    return JSONResponse({"status": "ok"}, status_code=200)


async def list_deliveries_endpoint(_request: Request) -> JSONResponse:
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


class WebhookAddon(BaseServerAddon):
    """Built-in Open-Source webhook ingestion & autonomous RCA addon (`webhooks`)."""

    name = "webhooks"
    tier: AddonTier = "oss"
    version = "1.0.0"
    description = "Inbound DevOps/Cloud webhook ingestion, HMAC verification, deduplication, and RCA sinks"
    dependencies = ()
    capabilities = AddonCapabilities(
        routes=True,
        background_workers=True,
        event_sinks=True,
    )
    settings_schema = WebhookAddonSettings
    enabled_by_default = True

    def __init__(self, dispatcher: WebhookDispatcher | None = None) -> None:
        super().__init__()
        if dispatcher is not None:
            set_webhook_dispatcher(dispatcher)

    @property
    def dispatcher(self) -> WebhookDispatcher:
        return get_webhook_dispatcher()

    async def on_startup(self, ctx: AddonContext) -> None:
        await super().on_startup(ctx)
        dispatcher = get_webhook_dispatcher()
        dispatcher.bind_event_bus(ctx.event_bus)

        # Apply optional settings overrides if configured in `addons.webhooks`
        cfg = self._config
        if isinstance(cfg, WebhookAddonSettings):
            if cfg.default_secret:
                dispatcher.config.default_secret = cfg.default_secret
            if cfg.dedup_ttl_seconds is not None:
                dispatcher.config.dedup_ttl_seconds = cfg.dedup_ttl_seconds
            if cfg.max_concurrent_runs is not None:
                dispatcher.config.max_concurrent_runs = cfg.max_concurrent_runs
            if cfg.auto_confirm_sns is not None:
                dispatcher.config.auto_confirm_sns = cfg.auto_confirm_sns
            raw_outbound = cfg.outbound_channels or cfg.config.get("outbound_channels")
            if raw_outbound:
                from talkops.webhooks.models import parse_outbound_channel_routes

                dispatcher.config.outbound_channels = parse_outbound_channel_routes(raw_outbound)

        # If AgentRunGateway has a bound in-memory graph and dispatcher uses the default executor,
        # upgrade to zero-loopback in-memory execution automatically.
        if (
            ctx.gateway.graph is not None
            and dispatcher.run_executor is default_langgraph_run_executor
        ):
            dispatcher.run_executor = create_gateway_run_executor(ctx.gateway)

    async def on_shutdown(self, ctx: AddonContext) -> None:
        dispatcher = get_webhook_dispatcher()
        await dispatcher.drain_background_tasks()
        await super().on_shutdown(ctx)

    def get_routes(self) -> list[BaseRoute]:
        return [
            Route("/webhooks", endpoint=list_webhooks_endpoint, methods=["GET"]),
            Route("/webhooks/health", endpoint=webhook_health_endpoint, methods=["GET"]),
            Route("/webhooks/deliveries", endpoint=list_deliveries_endpoint, methods=["GET"]),
            Route("/webhooks/{provider}", endpoint=receive_webhook_endpoint, methods=["POST"]),
        ]

    async def health_check(self) -> AddonHealthStatus:
        dispatcher = get_webhook_dispatcher()
        port = getattr(self._config, "port", None) if self._config is not None else None
        return AddonHealthStatus(
            name=self.name,
            status="ok",
            tier=self.tier,
            version=self.version,
            port=port,
            source=self.source,
            details={
                "providers_count": len(dispatcher.registry.list_providers()),
                "recent_deliveries": len(dispatcher.recent_deliveries),
            },
        )

"""Built-in CoreAuthAddon (`core-auth`) for TalkOps Server Mode.

Wraps `src/talkops/server/auth.py` into a `ServerAddon` and `IdentityMapper`:
- Supports mandatory token mode (`TALKOPS_SERVER_API_KEYS`, Control Plane introspection, and `HS256` / RS256 OIDC JWT verification)
- Mounts `POST /api/v1/auth/token` for short-lived token exchange
- Mounts `GET /.well-known/openid-configuration` and `GET /.well-known/jwks.json`
- Automatically superseded when `EnterpriseAuthAddon` (`enterprise-auth`) is enabled
"""

from __future__ import annotations

from typing import Any, Literal

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
from talkops.server.auth import (
    exchange_api_key_for_temp_token,
    get_auth_mode,
    resolve_user_from_headers,
)


class CoreAuthSettings(AddonSettings):
    """Configuration schema for `CoreAuthAddon`."""

    enabled: bool = True
    default_token_ttl_seconds: int = 3600


async def auth_token_exchange_endpoint(request: Request) -> JSONResponse:
    """Exchange a long-lived API key for a short-lived signed JWT (`POST /api/v1/auth/token`)."""
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


class CoreAuthAddon(BaseServerAddon):
    """Built-in Open-Source authentication and thread-isolation addon."""

    name = "core-auth"
    tier: AddonTier = "oss"
    version = "1.0.0"
    description = "Core API key, temporary JWT exchange, and per-user thread owner isolation"
    dependencies = ()
    capabilities = AddonCapabilities(routes=True, auth_provider=True)
    settings_schema = CoreAuthSettings
    enabled_by_default = True

    async def on_startup(self, ctx: AddonContext) -> None:
        await super().on_startup(ctx)
        if ctx.identity_mapper is None:
            ctx.identity_mapper = self

    async def authenticate_request(self, headers: dict[str, str]) -> dict[str, Any]:
        """Validate request headers using `talkops.server.auth.resolve_user_from_headers`."""
        return await resolve_user_from_headers(headers)

    def authorize_action(
        self,
        identity: dict[str, Any],
        resource: Literal["thread", "run", "store", "mcp", "webhook", "admin"],
        action: Literal["create", "read", "update", "delete", "execute"],
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Enforce basic owner isolation in OSS CoreAuthAddon."""
        user_id = str(identity.get("identity") or "")
        if not user_id:
            return False
        if user_id in ("internal:webhook-dispatcher", "admin"):
            return True
        if resource == "thread" and metadata and "owner" in metadata:
            owner = str(metadata.get("owner") or "")
            if owner and owner not in (user_id, "webhook"):
                return False
        return True

    def get_routes(self) -> list[BaseRoute]:
        return [
            Route("/.well-known/openid-configuration", endpoint=oidc_configuration_endpoint, methods=["GET"]),
            Route("/.well-known/jwks.json", endpoint=oidc_jwks_endpoint, methods=["GET"]),
            Route("/api/v1/auth/token", endpoint=auth_token_exchange_endpoint, methods=["POST"]),
        ]

    async def health_check(self) -> AddonHealthStatus:
        return AddonHealthStatus(
            name=self.name,
            status="ok",
            tier=self.tier,
            version=self.version,
            source=self.source,
            details={"auth_mode": get_auth_mode()},
        )

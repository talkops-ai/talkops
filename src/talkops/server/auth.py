"""LangGraph Custom Authentication & Multi-User Resource Isolation for TalkOps Server Mode.

Implements the official LangGraph `langgraph_sdk.Auth` specification:
- `@auth.authenticate`: Middleware inspecting `Authorization: Bearer <token>`, `X-API-Key`,
  and `X-TalkOps-User` headers. Includes a modular `AuthProviderProtocol` and a
  `PlaceholderTokenAuthProvider` ready to be swapped with a production DB/OIDC/IAM verifier.
- `/webhooks/*` passthrough: Allows third-party webhook deliveries to reach `WebhookDispatcher`
  for provider-specific HMAC signature verification (`X-Hub-Signature-256`, `X-PagerDuty-Signature`, etc.).
- `@auth.on.threads`: Stamps `metadata["owner"] = ctx.user.identity` on thread creation and
  scopes thread visibility per user.
- `@auth.on.store`: Scopes long-term memory namespaces (`/memories/`, `/learnings/`) per user
  while preserving shared system namespaces such as `("talkops", "approval_mode")`.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Any, Protocol

import httpx
import jwt
from jwt import PyJWKClient
from langgraph_sdk import Auth

from talkops.utils.logger import get_logger

logger = get_logger(__name__)

auth = Auth()

_SAFE_USER_RE = re.compile(r"^[a-zA-Z0-9_.@-]{1,64}$")
_INTERNAL_WEBHOOK_TOKEN = os.environ.get("TALKOPS_INTERNAL_WEBHOOK_TOKEN") or secrets.token_hex(24)
os.environ.setdefault("TALKOPS_INTERNAL_WEBHOOK_TOKEN", _INTERNAL_WEBHOOK_TOKEN)

_DEFAULT_JWT_SECRET = os.environ.get("TALKOPS_JWT_SECRET") or secrets.token_hex(32)
os.environ.setdefault("TALKOPS_JWT_SECRET", _DEFAULT_JWT_SECRET)

_SYSTEM_STORE_PREFIXES = frozenset({"talkops", "org_shared", "__system__"})

# Cached PyJWKClient instances keyed by JWKS URL
_jwks_clients: dict[str, PyJWKClient] = {}

# In-memory TTL cache for Control Plane token introspection:
# sha256(token) -> (expires_at_monotonic, user_dict)
_control_plane_token_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_MAX_TOKEN_CACHE_SIZE = 1024


def clear_auth_caches() -> None:
    """Clear in-memory JWKS and Control Plane token validation caches."""
    _jwks_clients.clear()
    _control_plane_token_cache.clear()


def get_internal_webhook_token() -> str:
    """Return the process-scoped internal webhook authentication token."""
    return os.environ.get("TALKOPS_INTERNAL_WEBHOOK_TOKEN") or _INTERNAL_WEBHOOK_TOKEN


def get_jwt_secret() -> str:
    """Return the active JWT signing/verification secret."""
    return os.environ.get("TALKOPS_JWT_SECRET") or _DEFAULT_JWT_SECRET


def get_auth_mode() -> str:
    """Return the active server authentication mode (always `'token'`)."""
    return "token"


def _looks_like_jwt(token: str) -> bool:
    """Return True if `token` has the 3-segment structure of a compact JWS/JWT (`eyJ...`)."""
    if not token or not token.startswith("eyJ"):
        return False
    parts = token.split(".")
    return len(parts) == 3 and all(bool(p) for p in parts)


def issue_temporary_jwt(
    user_id: str,
    *,
    org_id: str | None = None,
    email: str | None = None,
    role: str = "operator",
    permissions: list[str] | None = None,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Mint a short-lived signed JWT access token for an authenticated operator.

    Used by `POST /api/v1/auth/token` (or local Control Plane exchange) so CLI/TUI
    clients exchange their long-lived API key once and send a short-lived JWT on
    subsequent requests.
    """
    if ttl_seconds is None:
        raw_ttl = os.environ.get("TALKOPS_TEMP_TOKEN_TTL_SECONDS", "900").strip()
        ttl_seconds = int(raw_ttl) if raw_ttl.isdigit() and int(raw_ttl) > 0 else 900

    now = int(time.time())
    exp = now + ttl_seconds
    issuer = os.environ.get("TALKOPS_JWT_ISSUER", "").strip() or "talkops-control-plane"
    audience = os.environ.get("TALKOPS_JWT_AUDIENCE", "").strip() or "talkops-agent-server"

    payload: dict[str, Any] = {
        "sub": user_id,
        "user_id": user_id,
        "iat": now,
        "exp": exp,
        "iss": issuer,
        "aud": audience,
        "role": role,
        "scopes": permissions or ["*"],
    }
    if org_id:
        payload["org_id"] = org_id
    if email:
        payload["email"] = email

    private_key = os.environ.get("TALKOPS_JWT_PRIVATE_KEY", "").strip()
    if private_key:
        alg = os.environ.get("TALKOPS_JWT_ALGORITHM", "RS256").strip() or "RS256"
        encoded = jwt.encode(payload, private_key, algorithm=alg)
    else:
        encoded = jwt.encode(payload, get_jwt_secret(), algorithm="HS256")

    return {
        "access_token": encoded,
        "token_type": "Bearer",
        "expires_in": ttl_seconds,
        "expires_at": exp,
        "user_id": user_id,
        "org_id": org_id,
    }


def verify_jwt_token(token: str) -> dict[str, Any]:
    """Verify a signed JWT via Control Plane JWKS, Public Key, or Shared Secret (<1ms).

    Supports:
    1. `TALKOPS_CONTROL_PLANE_JWKS_URL`: Asymmetric verification (`RS256`, `ES256`, `EdDSA`)
       against the Control Plane's `/.well-known/jwks.json` endpoint with key caching.
    2. `TALKOPS_JWT_PUBLIC_KEY`: Asymmetric verification using a static PEM public key.
    3. `TALKOPS_JWT_SECRET`: Symmetric `HS256` verification.
    """
    jwks_url = os.environ.get("TALKOPS_CONTROL_PLANE_JWKS_URL", "").strip()
    public_key = os.environ.get("TALKOPS_JWT_PUBLIC_KEY", "").strip()
    expected_issuer = os.environ.get("TALKOPS_JWT_ISSUER", "").strip() or None
    expected_audience = os.environ.get("TALKOPS_JWT_AUDIENCE", "").strip() or None

    decode_options: Any = {
        "verify_signature": True,
        "verify_exp": True,
        "verify_aud": bool(expected_audience),
        "verify_iss": bool(expected_issuer),
    }

    try:
        unverified_header = jwt.get_unverified_header(token)
        alg = str(unverified_header.get("alg") or "HS256")

        if alg != "HS256" and not jwks_url and not public_key:
            cp_base = os.environ.get("TALKOPS_CONTROL_PLANE_URL", "").strip().rstrip("/")
            if not cp_base:
                try:
                    from talkops.auth.storage import get_credential_storage

                    stored = get_credential_storage().load_session()
                    if stored and stored.control_plane_url:
                        cp_base = stored.control_plane_url.strip().rstrip("/")
                except Exception:
                    pass
            if not cp_base:
                from talkops._constants import DEFAULT_CONTROL_PLANE_URL

                cp_base = DEFAULT_CONTROL_PLANE_URL
            jwks_url = f"{cp_base}/.well-known/jwks.json"

        if jwks_url and alg != "HS256":
            jwk_client = _jwks_clients.get(jwks_url)
            if jwk_client is None:
                jwk_client = PyJWKClient(jwks_url, cache_keys=True, lifespan=300)
                _jwks_clients[jwks_url] = jwk_client
            signing_key = jwk_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=[alg, "RS256", "ES256", "EdDSA"],
                issuer=expected_issuer,
                audience=expected_audience,
                options=decode_options,
            )
        elif public_key and alg != "HS256":
            claims = jwt.decode(
                token,
                public_key,
                algorithms=[alg, "RS256", "ES256", "EdDSA"],
                issuer=expected_issuer,
                audience=expected_audience,
                options=decode_options,
            )
        else:
            claims = jwt.decode(
                token,
                get_jwt_secret(),
                algorithms=["HS256"],
                issuer=expected_issuer,
                audience=expected_audience,
                options=decode_options,
            )
    except jwt.ExpiredSignatureError as exc:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Temporary access token has expired; please refresh token",
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail=f"Invalid JWT access token: {exc}",
        ) from exc

    user_id = str(
        claims.get("ls_user_id")
        or claims.get("user_id")
        or claims.get("sub")
        or claims.get("identity")
        or ""
    ).strip()
    if not user_id:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="JWT token is missing required 'sub' or 'user_id' claim",
        )

    raw_scopes = claims.get("scopes") or claims.get("permissions") or ["*"]
    if isinstance(raw_scopes, str):
        scopes = [s.strip() for s in raw_scopes.split() if s.strip()]
    elif isinstance(raw_scopes, list):
        scopes = [str(s) for s in raw_scopes]
    else:
        scopes = ["*"]

    result: dict[str, Any] = {
        "identity": user_id,
        "is_authenticated": True,
        "permissions": scopes,
        "role": str(claims.get("role") or "operator"),
        "is_webhook": False,
    }
    org_id = claims.get("org_id") or claims.get("organization_id") or claims.get("tenant_id")
    if org_id:
        result["org_id"] = str(org_id)
    if claims.get("email"):
        result["email"] = str(claims["email"])
    for extra_key in (
        "tier",
        "account_type",
        "capabilities",
        "allowed_agents",
        "execution_limits",
        "enforced_approval_mode",
        "allowed_mcp_servers",
    ):
        if claims.get(extra_key) is not None:
            result[extra_key] = claims[extra_key]
    return result


async def verify_token_with_control_plane(
    token: str,
    *,
    control_plane_url: str | None = None,
) -> dict[str, Any]:
    """Verify an API key or opaque token with the external SaaS Control Plane.

    Caches positive verifications in memory for `TALKOPS_AUTH_CACHE_TTL_SECONDS`
    (default 60s), keyed by `SHA-256(token)`.
    """
    cp_url = (
        control_plane_url
        or os.environ.get("TALKOPS_CONTROL_PLANE_URL")
        or ""
    ).strip().rstrip("/")
    if not cp_url:
        raise ValueError("TALKOPS_CONTROL_PLANE_URL is not configured")

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now_mono = time.monotonic()

    cached = _control_plane_token_cache.get(token_hash)
    if cached is not None:
        exp_mono, cached_user = cached
        if now_mono < exp_mono:
            return dict(cached_user)
        _control_plane_token_cache.pop(token_hash, None)

    verify_path = (
        os.environ.get("TALKOPS_CONTROL_PLANE_VERIFY_PATH")
        or "/api/v1/auth/verify"
    ).strip()
    if not verify_path.startswith("/"):
        verify_path = f"/{verify_path}"

    headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "X-API-Key": token,
        "Accept": "application/json",
    }
    service_key = os.environ.get("TALKOPS_CONTROL_PLANE_SERVICE_KEY", "").strip()
    if service_key:
        headers["X-Control-Plane-Key"] = service_key

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{cp_url}{verify_path}",
                json={"token": token},
                headers=headers,
            )
            if resp.status_code == 401 and not service_key:
                try:
                    err_payload = resp.json()
                except Exception:
                    err_payload = {}
                if isinstance(err_payload, dict) and err_payload.get("error") == "invalid_service_key":
                    resp = await client.post(
                        f"{cp_url}/api/v1/auth/token",
                        json={"token": token},
                        headers=headers,
                    )
    except Exception as exc:
        logger.warning("Control Plane token verification request failed: %s", exc)
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Could not verify token with Control Plane",
        ) from exc

    if resp.status_code != 200:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Invalid or revoked API token (rejected by Control Plane)",
        )

    try:
        data = resp.json()
    except Exception as exc:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Invalid response from Control Plane auth endpoint",
        ) from exc

    if not isinstance(data, dict):
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Malformed Control Plane verification payload",
        )

    # Support RFC 7662 (`active: bool`) and common SaaS (`valid: bool`) contracts
    if data.get("active") is False or data.get("valid") is False:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Token is inactive or revoked in Control Plane",
        )

    user_id = str(
        data.get("user_id")
        or data.get("sub")
        or data.get("identity")
        or data.get("id")
        or ""
    ).strip()
    if not user_id:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Control Plane response did not include a user_id",
        )

    raw_scopes = data.get("scopes") or data.get("permissions") or ["*"]
    if isinstance(raw_scopes, str):
        scopes = [s.strip() for s in raw_scopes.split() if s.strip()]
    elif isinstance(raw_scopes, list):
        scopes = [str(s) for s in raw_scopes]
    else:
        scopes = ["*"]

    user_dict: dict[str, Any] = {
        "identity": user_id,
        "is_authenticated": True,
        "permissions": scopes,
        "role": str(data.get("role") or "operator"),
        "is_webhook": False,
    }
    org_id = data.get("org_id") or data.get("organization_id") or data.get("tenant_id")
    if org_id:
        user_dict["org_id"] = str(org_id)
    if data.get("email"):
        user_dict["email"] = str(data["email"])
    for extra_key in (
        "tier",
        "account_type",
        "capabilities",
        "allowed_agents",
        "execution_limits",
        "enforced_approval_mode",
        "allowed_mcp_servers",
    ):
        if data.get(extra_key) is not None:
            user_dict[extra_key] = data[extra_key]

    raw_cache_ttl = os.environ.get("TALKOPS_AUTH_CACHE_TTL_SECONDS", "60").strip()
    cache_ttl = float(raw_cache_ttl) if raw_cache_ttl.isdigit() else 60.0
    if cache_ttl > 0:
        if len(_control_plane_token_cache) >= _MAX_TOKEN_CACHE_SIZE:
            oldest_key = next(iter(_control_plane_token_cache))
            _control_plane_token_cache.pop(oldest_key, None)
        _control_plane_token_cache[token_hash] = (now_mono + cache_ttl, dict(user_dict))

    return user_dict


def _parse_configured_api_keys() -> dict[str, str]:
    """Parse `TALKOPS_SERVER_API_KEYS` into a `{token: user_id}` mapping.

    Supports either:
    - Comma-separated `<user_id>:<token>` pairs (e.g. `alice:oc_123,bob:oc_456`)
    - Comma-separated raw tokens (e.g. `oc_123,oc_456`)
    - JSON object `{"alice": "oc_123"}` or `{"oc_123": "alice"}`
    """
    raw = os.environ.get("TALKOPS_SERVER_API_KEYS", "").strip()
    if not raw:
        return {}

    mapping: dict[str, str] = {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    k_str, v_str = str(k).strip(), str(v).strip()
                    if k_str and v_str:
                        mapping[v_str] = k_str
                        mapping[k_str] = v_str
                return mapping
        except Exception:
            pass

    for entry in raw.split(","):
        item = entry.strip()
        if not item:
            continue
        if ":" in item:
            user_part, token_part = item.split(":", 1)
            user_clean = user_part.strip()
            token_clean = token_part.strip()
            if user_clean and token_clean:
                mapping[token_clean] = user_clean
                # Also allow `<user_id>:<token>` full string as the bearer token
                mapping[item] = user_clean
        else:
            digest = hashlib.sha256(item.encode("utf-8")).hexdigest()[:8]
            mapping[item] = f"user_{digest}"
    return mapping


def _normalize_headers(headers: Mapping[str, Any] | Mapping[bytes, Any] | None) -> dict[str, str]:
    """Normalize HTTP headers (bytes or str keys) to lowercase string keys."""
    if not headers:
        return {}
    normalized: dict[str, str] = {}
    for k, v in headers.items():
        key_str = k.decode("latin-1") if isinstance(k, bytes) else str(k)
        if isinstance(v, bytes):
            val_str = v.decode("latin-1")
        elif isinstance(v, list):
            val_str = v[0].decode("latin-1") if v and isinstance(v[0], bytes) else (str(v[0]) if v else "")
        else:
            val_str = str(v)
        normalized[key_str.lower().strip()] = val_str.strip()
    return normalized


class AuthProviderProtocol(Protocol):
    """Extensible interface for TalkOps server authentication providers."""

    async def authenticate(
        self,
        headers: Mapping[str, str],
        path: str | None = None,
    ) -> dict[str, Any]:
        """Validate request headers and return a LangGraph user identity dict."""
        ...


class PlaceholderTokenAuthProvider:
    """Multi-tiered SaaS Control Plane + JWT + Bearer Token authentication provider.

    Verification priority order:
    1. `/webhooks/*` passthrough (verified by `WebhookDispatcher` HMAC signatures).
    2. `/api/v1/auth/token` passthrough (validates API key inside endpoint to mint short-lived JWT).
    3. `X-TalkOps-Internal-Webhook` secret header for internal webhook run dispatch.
    4. **Short-lived JWT (`eyJ...`)**: Verified cryptographically via Control Plane JWKS
       (`TALKOPS_CONTROL_PLANE_JWKS_URL`), `TALKOPS_JWT_PUBLIC_KEY`, or `TALKOPS_JWT_SECRET`.
    5. **Control Plane Introspection (`TALKOPS_CONTROL_PLANE_URL`)**: Verifies API keys or
       tokens against `{TALKOPS_CONTROL_PLANE_URL}/api/v1/auth/verify` with TTL caching.
    6. **Static API Keys (`TALKOPS_SERVER_API_KEYS`)** or local placeholder token fallback.
    """

    async def authenticate(
        self,
        headers: Mapping[str, str],
        path: str | None = None,
    ) -> dict[str, Any]:
        norm = _normalize_headers(headers)
        req_path = (path or "").strip()

        # 1. Third-party webhook & channel routes authenticate via their own HMAC/Ed25519/JWT verification
        if (
            req_path in {"/webhooks", "/channels"}
            or req_path.startswith("/webhooks/")
            or req_path.startswith("/channels/")
        ):
            return {
                "identity": "webhook",
                "permissions": ["webhook", "*"],
                "role": "webhook",
                "is_webhook": True,
            }

        # 2. Public health, OIDC discovery, and token exchange endpoints
        if (
            req_path in {"/ok", "/api/v1/health", "/api/v1/auth/token"}
            or req_path.startswith("/.well-known/")
        ):
            return {
                "identity": "token_exchange",
                "permissions": ["auth:exchange"],
                "role": "anonymous",
                "is_webhook": False,
            }

        # 3. Internal webhook background run executor call
        internal_hdr = norm.get("x-talkops-internal-webhook", "")
        expected_internal = get_internal_webhook_token()
        if internal_hdr and expected_internal and hmac.compare_digest(internal_hdr, expected_internal):
            caller_user = norm.get("x-talkops-user") or "webhook"
            return {
                "identity": caller_user,
                "permissions": ["webhook", "*"],
                "role": "webhook",
                "is_webhook": True,
            }

        # 4. Extract Bearer token or X-API-Key
        auth_header = norm.get("authorization", "")
        raw_token = ""
        if auth_header.lower().startswith("bearer "):
            raw_token = auth_header[7:].strip()
        elif auth_header:
            raw_token = auth_header.strip()

        if not raw_token:
            raw_token = norm.get("x-api-key", "").strip()

        explicit_user = norm.get("x-talkops-user", "").strip()

        # 5. If token is a signed JWT (e.g. temporary token from Control Plane or /api/v1/auth/token),
        #    verify its cryptographic signature and expiry in <1ms.
        if raw_token and _looks_like_jwt(raw_token):
            jwt_user = verify_jwt_token(raw_token)
            if explicit_user and _SAFE_USER_RE.match(explicit_user) and jwt_user["identity"].startswith("user_"):
                jwt_user["identity"] = explicit_user
            return jwt_user

        # 6. If external Control Plane URL is configured, verify the API key / token with Control Plane
        cp_url = os.environ.get("TALKOPS_CONTROL_PLANE_URL", "").strip()
        if cp_url:
            if not raw_token:
                raise Auth.exceptions.HTTPException(
                    status_code=401,
                    detail="Missing Authorization Bearer token or X-API-Key header",
                )
            return await verify_token_with_control_plane(raw_token, control_plane_url=cp_url)

        configured_keys = _parse_configured_api_keys()

        # 7. If static API keys are configured in TALKOPS_SERVER_API_KEYS, enforce them
        if configured_keys:
            if not raw_token:
                raise Auth.exceptions.HTTPException(
                    status_code=401,
                    detail="Missing Authorization Bearer token or X-API-Key header",
                )
            matched_user: str | None = None
            for candidate_token, mapped_user in configured_keys.items():
                if hmac.compare_digest(raw_token, candidate_token):
                    matched_user = mapped_user
                    break
            if not matched_user:
                raise Auth.exceptions.HTTPException(
                    status_code=401,
                    detail="Invalid API key or Bearer token",
                )
            user_id = explicit_user or matched_user
            return {
                "identity": user_id,
                "permissions": ["*"],
                "role": "operator",
                "is_webhook": False,
            }

        # 8. Token mode (when neither Control Plane nor static key list is set yet):
        #    a non-empty Bearer token or X-API-Key is still strictly mandatory
        #    except for public discovery endpoints (/api/v1/server/info, /api/v1/addons).
        if raw_token:
            if ":" in raw_token:
                prefix_user, _ = raw_token.split(":", 1)
                prefix_user = prefix_user.strip()
                if prefix_user and _SAFE_USER_RE.match(prefix_user):
                    user_id = explicit_user or prefix_user
                else:
                    digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()[:8]
                    user_id = explicit_user or f"user_{digest}"
            elif explicit_user and _SAFE_USER_RE.match(explicit_user):
                user_id = explicit_user
            elif _SAFE_USER_RE.match(raw_token) and len(raw_token) <= 32:
                user_id = raw_token
            else:
                digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()[:8]
                user_id = f"user_{digest}"

            return {
                "identity": user_id,
                "permissions": ["*"],
                "role": "operator",
                "is_webhook": False,
            }

        if req_path in {"/api/v1/server/info", "/api/v1/addons"}:
            return {
                "identity": explicit_user if (explicit_user and _SAFE_USER_RE.match(explicit_user)) else "default",
                "permissions": ["server:info"],
                "role": "anonymous",
                "is_webhook": False,
            }

        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Authentication required: provide Authorization: Bearer <token> or X-API-Key",
        )


ControlPlaneAuthProvider = PlaceholderTokenAuthProvider
_active_auth_provider: AuthProviderProtocol = ControlPlaneAuthProvider()


def get_auth_provider() -> AuthProviderProtocol:
    """Return the active server `AuthProviderProtocol` instance."""
    return _active_auth_provider


def set_auth_provider(provider: AuthProviderProtocol) -> None:
    """Register a custom `AuthProviderProtocol` implementation."""
    global _active_auth_provider
    _active_auth_provider = provider


async def resolve_user_from_headers(
    headers: Mapping[str, Any] | Mapping[bytes, Any] | None,
    *,
    path: str | None = None,
) -> dict[str, Any]:
    """Resolve user identity dictionary from request headers using the active auth provider."""
    norm = _normalize_headers(headers)
    return await _active_auth_provider.authenticate(norm, path=path)


async def exchange_api_key_for_temp_token(
    headers: Mapping[str, Any] | Mapping[bytes, Any] | None,
    *,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Validate an operator's API key (via Control Plane or configured keys) and mint a short-lived JWT."""
    norm = _normalize_headers(headers)
    auth_header = norm.get("authorization", "")
    raw_key = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else auth_header.strip()
    if not raw_key:
        raw_key = norm.get("x-api-key", "").strip()

    if not raw_key:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="API key required in X-API-Key or Authorization: Bearer header to issue temporary token",
        )

    # Validate the API key using the active provider (targeting /threads path so it runs full auth)
    user_info = await _active_auth_provider.authenticate(norm, path="/threads")
    user_id = str(user_info.get("identity") or "default")
    return issue_temporary_jwt(
        user_id,
        org_id=user_info.get("org_id"),
        email=user_info.get("email"),
        role=str(user_info.get("role") or "operator"),
        permissions=user_info.get("permissions"),
        ttl_seconds=ttl_seconds,
    )


async def authenticate_request(
    headers: dict[bytes, bytes] | Mapping[str, Any],
    path: str | None = None,
) -> dict[str, Any]:
    """LangGraph Agent Server `@auth.authenticate` entrypoint."""
    user_dict = await resolve_user_from_headers(headers, path=path)
    from talkops.state.session import set_current_user_id

    set_current_user_id(str(user_dict.get("identity") or "default"))
    return user_dict


auth.authenticate(authenticate_request)  # type: ignore[arg-type]


async def on_thread_create(
    ctx: Auth.types.AuthContext,
    value: Any,
) -> dict[str, str] | None:
    """Stamp `owner` metadata on newly created threads."""
    user_id = ctx.user.identity if ctx.user else "default"
    metadata = value.setdefault("metadata", {})
    if isinstance(metadata, dict):
        if metadata.get("source") == "webhook":
            metadata.setdefault("owner", "webhook")
        else:
            metadata.setdefault("owner", user_id)
    if user_id in ("default", "local", "webhook"):
        return None
    return {"owner": metadata.get("owner", user_id)}


auth.on.threads.create(on_thread_create)


async def on_thread_access(
    ctx: Auth.types.AuthContext,
    value: Any,
) -> dict[str, str] | None:
    """Allow operators to access their own threads (and shared webhook threads)."""
    user_id = ctx.user.identity if ctx.user else "default"
    if user_id in ("default", "local", "webhook"):
        return None
    # Note: LangGraph thread metadata filter matches exact key-value pairs;
    # for strict per-user isolation on thread mutations, filter by owner=user_id
    # unless the user is accessing a webhook thread.
    return None


for _thread_access_decorator in (
    auth.on.threads.read,
    auth.on.threads.update,
    auth.on.threads.delete,
    auth.on.threads.create_run,
):
    _thread_access_decorator(on_thread_access)


async def on_thread_search(
    ctx: Auth.types.AuthContext,
    value: Any,
) -> dict[str, str] | None:
    """Filter `client.threads.search()` results to the authenticated user's threads."""
    user_id = ctx.user.identity if ctx.user else "default"
    if user_id in ("default", "local", "webhook"):
        return None
    return {"owner": user_id}


auth.on.threads.search(on_thread_search)


async def on_store_access(
    ctx: Auth.types.AuthContext,
    value: Any,
) -> None:
    """Isolate `BaseStore` namespaces per user while preserving shared system namespaces."""
    user_id = ctx.user.identity if ctx.user else "default"
    if user_id in ("default", "local", "webhook"):
        return

    namespace: tuple[str, ...] = tuple(value.get("namespace") or ())
    if not namespace:
        value["namespace"] = (user_id,)
        return

    # Preserve system namespaces like ("talkops", "approval_mode") so graph nodes
    # and TUI clients read/write the same thread approval key.
    if namespace[0] in _SYSTEM_STORE_PREFIXES:
        return

    if namespace[0] != user_id:
        value["namespace"] = (user_id, *namespace)


auth.on.store(on_store_access)


@auth.on.assistants
async def on_assistants_access(
    ctx: Auth.types.AuthContext,
    value: dict[str, Any],
) -> None:
    """Allow authenticated operators to inspect and invoke shared server assistants."""
    del ctx, value
    return None


@auth.on.crons
async def on_crons_access(
    ctx: Auth.types.AuthContext,
    value: dict[str, Any],
) -> None:
    """Allow authenticated operators to manage server cron schedules."""
    del ctx, value
    return None


__all__ = [
    "AuthProviderProtocol",
    "ControlPlaneAuthProvider",
    "PlaceholderTokenAuthProvider",
    "auth",
    "authenticate_request",
    "clear_auth_caches",
    "exchange_api_key_for_temp_token",
    "get_auth_mode",
    "get_auth_provider",
    "get_internal_webhook_token",
    "get_jwt_secret",
    "issue_temporary_jwt",
    "resolve_user_from_headers",
    "set_auth_provider",
    "verify_jwt_token",
    "verify_token_with_control_plane",
]

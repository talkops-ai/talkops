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

"""Remote HTTP session client backend for TalkOps TUI and CLI in Central Server Mode.

When an operator connects to a central TalkOps server (`talkops --server-url https://...`),
the local process does not have direct filesystem access to the server's SQLite or PostgreSQL
database. `RemoteSessionBackend` routes thread listing, title management, forking, pruning,
and deletion over HTTP to the central server's `/api/v1/threads/*` endpoints while forwarding
the operator's Bearer token / API key and user identity headers.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from talkops.state.session import ThreadInfo

logger = get_logger(__name__)

_remote_server_url: str | None = None
_remote_api_key: str | None = None
_remote_user_id: str | None = None

# Client-side cached short-lived temporary token (JWT) exchanged from the API key
_cached_temp_token: str | None = None
_cached_temp_token_expires_at: float = 0.0
_cached_temp_token_source_key: str | None = None
_TOKEN_EXPIRY_BUFFER_SECONDS = 30.0
_bg_refresh_task: asyncio.Task[None] | None = None


def _looks_like_jwt(token: str | None) -> bool:
    """Return True if `token` has the 3-part `eyJ...` structure of a compact JWT."""
    if not token:
        return False
    cleaned = token.strip()
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip()
    return cleaned.startswith("eyJ") and len(cleaned.split(".")) == 3


def _is_jwt_expired(token: str | None, *, buffer_seconds: float = _TOKEN_EXPIRY_BUFFER_SECONDS) -> bool:
    """Return True if a JWT `token` is expired or expiring within `buffer_seconds`."""
    if not token:
        return True
    cleaned = token.strip()
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip()
    if not _looks_like_jwt(cleaned):
        return False

    try:
        payload_b64 = cleaned.split(".")[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload_raw = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(payload_raw.decode("utf-8"))
        if isinstance(payload, dict) and "exp" in payload and payload["exp"] is not None:
            return (time.time() + buffer_seconds) >= float(payload["exp"])
    except Exception:
        pass

    if cleaned == _cached_temp_token and _cached_temp_token_expires_at > 0:
        return (time.time() + buffer_seconds) >= _cached_temp_token_expires_at

    if _can_use_disk_auth_session():
        try:
            from talkops.auth.storage import get_credential_storage

            stored = get_credential_storage().load_session()
            if stored and stored.access_token == cleaned and stored.expires_at > 0:
                return (time.time() + buffer_seconds) >= stored.expires_at
        except Exception:
            pass

    return False


def _resolve_long_lived_api_key(
    candidate_key: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve `(long_lived_api_key, control_plane_url, user_id)` from args, env, or `auth.json`."""
    long_key: str | None = None
    if candidate_key:
        cleaned = candidate_key[7:].strip() if candidate_key.lower().startswith("bearer ") else candidate_key.strip()
        if cleaned and not _looks_like_jwt(cleaned):
            long_key = cleaned

    if not long_key:
        for env_name in ("TALKOPS_API_KEY", "TALKOPS_SERVER_API_KEY"):
            env_val = os.environ.get(env_name, "").strip()
            if env_val.lower().startswith("bearer "):
                env_val = env_val[7:].strip()
            if env_val and not _looks_like_jwt(env_val):
                long_key = env_val
                break

    stored_cp: str | None = None
    stored_user: str | None = None
    if _can_use_disk_auth_session():
        try:
            from talkops.auth.storage import get_credential_storage

            stored = get_credential_storage().load_session()
            if stored is not None:
                if not long_key and stored.api_key and not _looks_like_jwt(stored.api_key):
                    long_key = stored.api_key.strip()
                if stored.control_plane_url:
                    stored_cp = stored.control_plane_url.strip().rstrip("/") or None
                if stored.user_id:
                    stored_user = stored.user_id.strip() or None
        except Exception:
            pass

    return long_key, stored_cp, stored_user


def set_remote_client_context(
    server_url: str | None,
    *,
    api_key: str | None = None,
    user_id: str | None = None,
) -> None:
    """Configure the active remote server URL and credentials for the client process."""
    global _remote_server_url, _remote_api_key, _remote_user_id
    global _cached_temp_token, _cached_temp_token_expires_at, _cached_temp_token_source_key
    cleaned_url = (server_url or "").strip().rstrip("/") or None
    if cleaned_url and cleaned_url.startswith("inprocess://"):
        cleaned_url = None
    new_key = (api_key or "").strip() or None
    if new_key != _remote_api_key or cleaned_url != _remote_server_url:
        _cached_temp_token = None
        _cached_temp_token_expires_at = 0.0
        _cached_temp_token_source_key = None
    _remote_server_url = cleaned_url
    _remote_api_key = new_key
    _remote_user_id = (user_id or "").strip() or None


def clear_remote_client_context() -> None:
    """Clear the configured remote server client context and cached temporary token."""
    global _remote_server_url, _remote_api_key, _remote_user_id
    global _cached_temp_token, _cached_temp_token_expires_at, _cached_temp_token_source_key
    _remote_server_url = None
    _remote_api_key = None
    _remote_user_id = None
    _cached_temp_token = None
    _cached_temp_token_expires_at = 0.0
    _cached_temp_token_source_key = None


def _can_use_disk_auth_session() -> bool:
    """Return True if reading/writing `~/.talkops/.state/auth.json` is allowed.

    Prevents a developer's real `~/.talkops/.state/auth.json` from leaking into
    unrelated pytest unit tests, while allowing tests that monkeypatch `paths.AUTH_PATH`
    (and all production CLI/TUI runs) to use disk persistence.
    """
    if "PYTEST_CURRENT_TEST" not in os.environ:
        return True
    from talkops.config import paths as paths_mod

    default_home_auth = Path.home() / ".talkops" / ".state" / "auth.json"
    return paths_mod.AUTH_PATH != default_home_auth


def get_remote_client_config() -> tuple[str | None, str | None, str | None]:
    """Return `(server_url, api_key, user_id)` if remote client mode is active."""
    if os.environ.get("TALKOPS_IN_SERVER_PROCESS") == "1":
        return None, None, None

    url = _remote_server_url or os.environ.get("TALKOPS_SERVER_URL", "").strip().rstrip("/") or None
    if url and url.startswith("inprocess://"):
        url = None
    api_key = (
        _remote_api_key
        or os.environ.get("TALKOPS_API_KEY", "").strip()
        or os.environ.get("TALKOPS_SERVER_API_KEY", "").strip()
        or None
    )
    user_id = _remote_user_id or os.environ.get("TALKOPS_USER_ID", "").strip() or None

    if _can_use_disk_auth_session() and (not url or not api_key or not user_id):
        try:
            from talkops.auth.storage import get_credential_storage

            stored = get_credential_storage().load_session()
            if stored is not None:
                if not url and stored.server_url:
                    url = stored.server_url.strip().rstrip("/") or None
                if not api_key and stored.api_key:
                    api_key = stored.api_key.strip() or None
                if not api_key and stored.access_token and stored.is_access_token_valid():
                    api_key = stored.access_token.strip() or None
                if not user_id and stored.user_id:
                    user_id = stored.user_id.strip() or None
        except Exception:
            pass

    return url, api_key, user_id


def is_remote_client_mode() -> bool:
    """Return True if the current process is acting as a client connected to a remote HTTP server."""
    url, _, _ = get_remote_client_config()
    return bool(url)


def get_cached_temporary_token(source_api_key: str | None = None) -> str | None:
    """Return the cached short-lived JWT if it is still valid (with a 30s safety buffer).

    Checks in-memory process cache first, then falls back to `~/.talkops/.state/auth.json`
    via `CompositeCredentialStorage` so short CLI invocations reuse the valid lease.
    """
    global _cached_temp_token, _cached_temp_token_expires_at, _cached_temp_token_source_key
    effective_source_key = None if _looks_like_jwt(source_api_key) else source_api_key
    if _cached_temp_token:
        if (
            not effective_source_key
            or not _cached_temp_token_source_key
            or effective_source_key == _cached_temp_token_source_key
        ) and (time.time() + _TOKEN_EXPIRY_BUFFER_SECONDS < _cached_temp_token_expires_at):
            return _cached_temp_token

    if _can_use_disk_auth_session():
        try:
            from talkops.auth.models import hash_api_key
            from talkops.auth.storage import get_credential_storage

            stored = get_credential_storage().load_session()
            if stored and stored.is_access_token_valid(buffer_seconds=_TOKEN_EXPIRY_BUFFER_SECONDS):
                if effective_source_key:
                    expected_hash = hash_api_key(effective_source_key)
                    if stored.source_key_hash and stored.source_key_hash != expected_hash:
                        return None
                _cached_temp_token = stored.access_token
                _cached_temp_token_expires_at = stored.expires_at
                if effective_source_key:
                    _cached_temp_token_source_key = effective_source_key
                return stored.access_token
        except Exception:
            pass
    return None


async def exchange_api_key_for_temporary_token(
    server_url: str | None = None,
    *,
    api_key: str | None = None,
    user_id: str | None = None,
    control_plane_url: str | None = None,
    timeout: float = 10.0,
    force_refresh: bool = False,
    raise_on_unauthorized: bool = False,
) -> str | None:
    """Exchange the client's long-lived API key for a short-lived temporary JWT token.

    Contacts either:
    1. `TALKOPS_CONTROL_PLANE_URL` (`POST <control_plane_url>/api/v1/auth/token`) if set, or
    2. `server_url` (`POST <server_url>/api/v1/auth/token`).

    Caches the returned `access_token` in memory and in `~/.talkops/.state/auth.json`
    until `expires_at - 30s`.
    """
    global _cached_temp_token, _cached_temp_token_expires_at, _cached_temp_token_source_key, _remote_user_id

    cfg_url, cfg_key, cfg_user = get_remote_client_config()
    eff_url = (server_url or cfg_url or "").strip().rstrip("/")
    eff_key = (api_key or cfg_key or "").strip()
    eff_user = (user_id or cfg_user or "").strip()

    if not eff_key:
        return None

    clean_key = eff_key[7:].strip() if eff_key.lower().startswith("bearer ") else eff_key
    # If the key is already a JWT, return it if still unexpired (and not force_refresh);
    # otherwise transparently fall back to the stored long-lived API key to refresh it.
    if _looks_like_jwt(clean_key):
        if not force_refresh and not _is_jwt_expired(clean_key, buffer_seconds=_TOKEN_EXPIRY_BUFFER_SECONDS):
            return clean_key
        long_key, stored_cp, stored_user = _resolve_long_lived_api_key(None)
        if long_key:
            clean_key = long_key
            if not control_plane_url and stored_cp:
                control_plane_url = stored_cp
            if not eff_user and stored_user:
                eff_user = stored_user
        else:
            return clean_key if not _is_jwt_expired(clean_key, buffer_seconds=0.0) else None

    if not force_refresh:
        existing = get_cached_temporary_token(clean_key)
        if existing:
            return existing

    cp_base = (
        control_plane_url
        or os.environ.get("TALKOPS_CONTROL_PLANE_URL", "")
    ).strip().rstrip("/")
    if not cp_base and _can_use_disk_auth_session():
        try:
            from talkops.auth.storage import get_credential_storage

            stored_sess = get_credential_storage().load_session()
            if stored_sess and stored_sess.control_plane_url:
                cp_base = stored_sess.control_plane_url.strip().rstrip("/")
        except Exception:
            pass
    token_path = (
        os.environ.get("TALKOPS_CONTROL_PLANE_TOKEN_PATH")
        or "/api/v1/auth/token"
    ).strip()
    if not token_path.startswith("/"):
        token_path = f"/{token_path}"

    target_endpoint = f"{cp_base}{token_path}" if cp_base else (f"{eff_url}/api/v1/auth/token" if eff_url else "")
    if not target_endpoint:
        return None

    req_headers: dict[str, str] = {
        "Authorization": f"Bearer {clean_key}",
        "X-API-Key": clean_key,
        "Accept": "application/json",
    }
    if eff_user:
        req_headers["X-TalkOps-User"] = eff_user
    service_key = os.environ.get("TALKOPS_CONTROL_PLANE_SERVICE_KEY", "").strip()
    if service_key:
        req_headers["X-Control-Plane-Key"] = service_key

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                target_endpoint,
                json={"api_key": clean_key, **({"user_id": eff_user} if eff_user else {})},
                headers=req_headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                access_token = str(data.get("access_token") or data.get("token") or "").strip()
                if access_token:
                    expires_in = int(data.get("expires_in") or 900)
                    expires_at = float(data.get("expires_at") or (time.time() + expires_in))
                    _cached_temp_token = access_token
                    _cached_temp_token_expires_at = expires_at
                    _cached_temp_token_source_key = clean_key
                    returned_user = str(data.get("user_id") or "").strip()
                    if returned_user and not _remote_user_id:
                        _remote_user_id = returned_user

                    # If TALKOPS_API_KEY in the current process was holding a temporary JWT,
                    # transparently update it in-place so in-process components see the fresh token.
                    if _looks_like_jwt(os.environ.get("TALKOPS_API_KEY")):
                        os.environ["TALKOPS_API_KEY"] = access_token

                    # Persist the temporary JWT lease in CompositeCredentialStorage (`auth.json`)
                    if _can_use_disk_auth_session():
                        try:
                            from talkops.auth.models import (
                                StoredSession,
                                hash_api_key,
                                mask_api_key,
                            )
                            from talkops.auth.storage import get_credential_storage

                            storage = get_credential_storage()
                            prev = storage.load_session()
                            session = StoredSession(
                                access_token=access_token,
                                api_key=prev.api_key if (prev and prev.api_key) else clean_key,
                                api_key_prefix=mask_api_key(clean_key),
                                server_url=(prev.server_url if prev and prev.server_url else (_remote_server_url or "")),
                                control_plane_url=cp_base or (prev.control_plane_url if prev else ""),
                                user_id=returned_user or eff_user or (prev.user_id if prev else ""),
                                email=str(data.get("email") or (prev.email if prev else "")),
                                org_id=str(data.get("org_id") or (prev.org_id if prev else "")),
                                role=str(data.get("role") or (prev.role if prev else "operator")),
                                user_tier=str(data.get("tier") or (prev.user_tier if prev else "pro")),
                                expires_at=expires_at,
                                offline_grace_until=float(
                                    data.get("offline_grace_until")
                                    or (time.time() + 7 * 86400)
                                ),
                                source_key_hash=hash_api_key(clean_key),
                            )
                            storage.save_session(session, sync_dotenv=False)
                        except Exception as store_exc:
                            logger.debug("Could not persist temporary JWT lease to auth.json: %s", store_exc)

                    return access_token
            elif resp.status_code in (401, 403) and raise_on_unauthorized:
                err_detail = "Invalid or revoked API token (rejected by Control Plane)"
                try:
                    err_json = resp.json()
                    if isinstance(err_json, dict) and err_json.get("error"):
                        err_detail = f"{err_detail} [{err_json['error']}]"
                except Exception:
                    pass
                raise PermissionError(err_detail)
    except PermissionError:
        raise
    except Exception as exc:
        logger.debug("Token exchange against %s skipped/failed: %s", target_endpoint, exc)

    return None


async def refresh_temporary_token_if_needed(
    *,
    buffer_seconds: float = 120.0,
) -> str | None:
    """Proactively refresh the short-lived JWT if it is missing or expiring within `buffer_seconds`.

    Uses the operator's stored long-lived `api_key` from `CompositeCredentialStorage`
    (`~/.talkops/.state/auth.json`) so long-running CLI/TUI sessions never hit an
    expired temporary token mid-session.
    """
    long_key, stored_cp, stored_user = _resolve_long_lived_api_key(None)
    if not long_key:
        return None

    # Check if the currently cached or persisted token is still valid with `buffer_seconds`
    if _cached_temp_token and (time.time() + buffer_seconds < _cached_temp_token_expires_at):
        return _cached_temp_token

    if _can_use_disk_auth_session():
        try:
            from talkops.auth.storage import get_credential_storage

            stored = get_credential_storage().load_session()
            if stored and stored.is_access_token_valid(buffer_seconds=buffer_seconds):
                return stored.access_token
        except Exception:
            pass

    return await exchange_api_key_for_temporary_token(
        None,
        api_key=long_key,
        user_id=stored_user,
        control_plane_url=stored_cp,
        force_refresh=True,
    )


def start_background_token_refresh_task(
    *,
    check_interval_seconds: float = 60.0,
    buffer_seconds: float = 120.0,
) -> asyncio.Task[None] | None:
    """Start a background asyncio task that silently refreshes temporary JWTs before expiry."""
    global _bg_refresh_task
    if "PYTEST_CURRENT_TEST" in os.environ and os.environ.get("TALKOPS_ENFORCE_CLI_AUTH_IN_TESTS") != "1":
        return None

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None

    if _bg_refresh_task is not None and not _bg_refresh_task.done():
        return _bg_refresh_task

    async def _refresh_loop() -> None:
        while True:
            try:
                await asyncio.sleep(check_interval_seconds)
                await refresh_temporary_token_if_needed(buffer_seconds=buffer_seconds)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Background token refresh check skipped: %s", exc)

    _bg_refresh_task = loop.create_task(_refresh_loop(), name="talkops-token-refresh")
    return _bg_refresh_task


def stop_background_token_refresh_task() -> None:
    """Cancel the background token refresh task if running."""
    global _bg_refresh_task
    task = _bg_refresh_task
    _bg_refresh_task = None
    if task is not None and not task.done():
        task.cancel()


class TalkOpsClientAuth(httpx.Auth):
    """Industry-standard `httpx.Auth` middleware for TalkOps clients.

    Automatically exchanges the operator's long-lived `api_key` for a short-lived
    temporary JWT before requests and transparently refreshes it on `401 Unauthorized`.
    """

    def __init__(
        self,
        *,
        server_url: str | None = None,
        api_key: str | None = None,
        user_id: str | None = None,
        control_plane_url: str | None = None,
    ) -> None:
        """Initialize TalkOpsClientAuth."""
        self.server_url = server_url
        self.api_key = api_key
        self.user_id = user_id
        self.control_plane_url = control_plane_url

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        """Execute auth flow on TalkOpsClientAuth."""
        headers = build_remote_auth_headers(api_key=self.api_key, user_id=self.user_id)
        for k, v in headers.items():
            request.headers[k] = v
        yield request

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """Execute async auth flow on TalkOpsClientAuth."""
        temp_token = await exchange_api_key_for_temporary_token(
            self.server_url,
            api_key=self.api_key,
            user_id=self.user_id,
            control_plane_url=self.control_plane_url,
        )
        headers = build_remote_auth_headers(api_key=self.api_key, user_id=self.user_id)
        if temp_token:
            headers["Authorization"] = f"Bearer {temp_token}"
            headers.pop("X-API-Key", None)
        for k, v in headers.items():
            request.headers[k] = v

        response = yield request
        if response.status_code == 401 and self.api_key:
            refreshed = await exchange_api_key_for_temporary_token(
                self.server_url,
                api_key=self.api_key,
                user_id=self.user_id,
                control_plane_url=self.control_plane_url,
                force_refresh=True,
            )
            if refreshed:
                request.headers["Authorization"] = f"Bearer {refreshed}"
                request.headers.pop("X-API-Key", None)
                yield request


def build_remote_auth_headers(
    api_key: str | None = None,
    user_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Construct authentication and user-identity headers for remote server requests.

    If a short-lived temporary token (JWT) has been exchanged and cached, sends ONLY
    `Authorization: Bearer <temporary_jwt>` so the long-lived API key is not exposed
    on every request.
    """
    _, cfg_key, cfg_user = get_remote_client_config()
    effective_key = (api_key if api_key is not None else cfg_key) or ""
    effective_user = (user_id if user_id is not None else cfg_user) or ""

    headers: dict[str, str] = {}
    if extra_headers:
        headers.update(extra_headers)

    if effective_key:
        if effective_key.lower().startswith("bearer "):
            token = effective_key[7:].strip()
        else:
            token = effective_key.strip()
        if token:
            # Prefer the cached short-lived temporary JWT if available
            temp_jwt = get_cached_temporary_token(token)
            if temp_jwt:
                headers.setdefault("Authorization", f"Bearer {temp_jwt}")
            else:
                if _looks_like_jwt(token) and _is_jwt_expired(token):
                    fallback_key, _, _ = _resolve_long_lived_api_key(None)
                    if fallback_key:
                        token = fallback_key
                headers.setdefault("Authorization", f"Bearer {token}")
                if not _looks_like_jwt(token):
                    headers.setdefault("X-API-Key", token)
            # If placeholder format `<user_id>:<token>` is used and no explicit user_id is set
            if not effective_user and ":" in token:
                candidate_user = token.split(":", 1)[0].strip()
                if candidate_user:
                    effective_user = candidate_user

    if effective_user:
        headers.setdefault("X-TalkOps-User", effective_user)

    return headers


class RemoteSessionBackend:
    """HTTP session backend communicating with `/api/v1/threads` on a central TalkOps server."""

    def __init__(
        self,
        server_url: str | None = None,
        *,
        api_key: str | None = None,
        user_id: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        """Initialize RemoteSessionBackend."""
        cfg_url, cfg_key, cfg_user = get_remote_client_config()
        self.server_url = (server_url or cfg_url or "").rstrip("/")
        if not self.server_url:
            raise ValueError("Remote server_url is required for RemoteSessionBackend")
        self.api_key = api_key if api_key is not None else cfg_key
        self.user_id = user_id if user_id is not None else cfg_user
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return build_remote_auth_headers(api_key=self.api_key, user_id=self.user_id)

    async def _async_headers(self) -> dict[str, str]:
        if self.api_key:
            await exchange_api_key_for_temporary_token(
                self.server_url,
                api_key=self.api_key,
                user_id=self.user_id,
                timeout=self.timeout,
            )
        return build_remote_auth_headers(api_key=self.api_key, user_id=self.user_id)

    async def list_threads(
        self,
        *,
        agent_name: str | None = None,
        limit: int = 20,
        include_message_count: bool = True,
        sort_by: str = "updated",
        branch: str | None = None,
        cwd: str | None = None,
    ) -> list[ThreadInfo]:
        """List threads."""
        params: dict[str, Any] = {
            "limit": limit,
            "include_message_count": str(include_message_count).lower(),
            "sort_by": sort_by,
        }
        if agent_name:
            params["agent_name"] = agent_name
        if branch:
            params["branch"] = branch
        if cwd:
            params["cwd"] = cwd

        try:
            headers = await self._async_headers()
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.server_url}/api/v1/threads",
                    params=params,
                    headers=headers,
                )
                resp.raise_for_status()
                payload = resp.json()
                threads = payload.get("threads", [])
                if isinstance(threads, list):
                    return threads
        except Exception as exc:
            logger.debug("RemoteSessionBackend.list_threads failed: %s", exc)
        return []

    async def get_most_recent(
        self,
        *,
        agent_name: str | None = None,
        branch: str | None = None,
        cwd: str | None = None,
    ) -> ThreadInfo | None:
        """Retrieve most recent."""
        threads = await self.list_threads(
            agent_name=agent_name,
            limit=1,
            include_message_count=True,
            branch=branch,
            cwd=cwd,
        )
        return threads[0] if threads else None

    async def find_similar_threads(self, prefix: str, limit: int = 3) -> list[str]:
        """Find and return similar threads."""
        try:
            headers = await self._async_headers()
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.server_url}/api/v1/threads/similar",
                    params={"prefix": prefix, "limit": limit},
                    headers=headers,
                )
                resp.raise_for_status()
                payload = resp.json()
                items = payload.get("thread_ids", [])
                if isinstance(items, list):
                    return [str(x) for x in items]
        except Exception as exc:
            logger.debug("RemoteSessionBackend.find_similar_threads failed: %s", exc)
        return []

    async def thread_exists(self, thread_id: str) -> bool:
        """Execute thread exists on RemoteSessionBackend."""
        if not thread_id:
            return False
        safe_tid = quote(thread_id, safe="")
        try:
            headers = await self._async_headers()
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.server_url}/api/v1/threads/{safe_tid}",
                    headers=headers,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    return bool(payload.get("exists", True))
        except Exception as exc:
            logger.debug("RemoteSessionBackend.thread_exists failed for %s: %s", thread_id, exc)
        return False

    async def get_thread_title(self, thread_id: str) -> str | None:
        """Retrieve thread title."""
        if not thread_id:
            return None
        safe_tid = quote(thread_id, safe="")
        try:
            headers = await self._async_headers()
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.server_url}/api/v1/threads/{safe_tid}",
                    headers=headers,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    title = payload.get("title")
                    if isinstance(title, str) and title.strip():
                        return title.strip()
        except Exception as exc:
            logger.debug("RemoteSessionBackend.get_thread_title failed for %s: %s", thread_id, exc)
        return None

    async def set_thread_title(
        self,
        thread_id: str,
        title: str,
        *,
        preview: str | None = None,
        only_if_empty: bool = True,
    ) -> bool:
        """Set thread title."""
        if not thread_id or not title:
            return False
        safe_tid = quote(thread_id, safe="")
        try:
            headers = await self._async_headers()
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.patch(
                    f"{self.server_url}/api/v1/threads/{safe_tid}/title",
                    json={
                        "title": title,
                        "preview": preview,
                        "only_if_empty": only_if_empty,
                    },
                    headers=headers,
                )
                if resp.status_code == 200:
                    return bool(resp.json().get("updated", False))
        except Exception as exc:
            logger.debug("RemoteSessionBackend.set_thread_title failed for %s: %s", thread_id, exc)
        return False

    async def fork_thread(
        self,
        source_thread_id: str,
        target_thread_id: str | None = None,
    ) -> str:
        """Execute fork thread on RemoteSessionBackend."""
        from talkops.state.session import generate_thread_id

        fallback_id = target_thread_id or generate_thread_id()
        safe_tid = quote(source_thread_id, safe="")
        try:
            headers = await self._async_headers()
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.server_url}/api/v1/threads/{safe_tid}/fork",
                    json={"target_thread_id": fallback_id},
                    headers=headers,
                )
                if resp.status_code == 200:
                    new_id = resp.json().get("thread_id")
                    if isinstance(new_id, str) and new_id.strip():
                        return new_id.strip()
        except Exception as exc:
            logger.debug("RemoteSessionBackend.fork_thread failed for %s: %s", source_thread_id, exc)
        return fallback_id

    async def prune_thread(
        self,
        thread_id: str,
        *,
        strategy: str = "keep_latest",
        turn_completed: bool = False,
    ) -> None:
        """Execute prune thread on RemoteSessionBackend."""
        if not thread_id:
            return
        safe_tid = quote(thread_id, safe="")
        try:
            headers = await self._async_headers()
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                await client.post(
                    f"{self.server_url}/api/v1/threads/{safe_tid}/prune",
                    json={"strategy": strategy, "turn_completed": turn_completed},
                    headers=headers,
                )
        except Exception as exc:
            logger.debug("RemoteSessionBackend.prune_thread failed for %s: %s", thread_id, exc)

    async def delete_thread(self, thread_id: str) -> bool:
        """Delete thread."""
        if not thread_id:
            return False
        safe_tid = quote(thread_id, safe="")
        try:
            headers = await self._async_headers()
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.delete(
                    f"{self.server_url}/api/v1/threads/{safe_tid}",
                    headers=headers,
                )
                if resp.status_code == 200:
                    return bool(resp.json().get("deleted", False))
        except Exception as exc:
            logger.debug("RemoteSessionBackend.delete_thread failed for %s: %s", thread_id, exc)
        return False


__all__ = [
    "TalkOpsClientAuth",
    "RemoteSessionBackend",
    "_is_jwt_expired",
    "_looks_like_jwt",
    "_resolve_long_lived_api_key",
    "build_remote_auth_headers",
    "clear_remote_client_context",
    "exchange_api_key_for_temporary_token",
    "get_cached_temporary_token",
    "get_remote_client_config",
    "is_remote_client_mode",
    "refresh_temporary_token_if_needed",
    "set_remote_client_context",
    "start_background_token_refresh_task",
    "stop_background_token_refresh_task",
]

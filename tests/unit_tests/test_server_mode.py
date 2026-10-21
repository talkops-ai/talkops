"""Unit and integration tests for TalkOps Central Server Mode.

Covers:
1. LangGraph Custom Auth (`talkops.server.auth`) — Bearer token / API key,
   user isolation on threads & store namespaces, webhook passthrough, and
   internal webhook secret verification.
2. Multi-user session isolation (`talkops.state.session`) — per-user thread
   creation, listing, webhook thread visibility for operators, title updates,
   forking, and deletion.
3. Server REST API (`talkops.server.webhooks_app`) & `RemoteSessionBackend`
   (`talkops.state.remote_session`) end-to-end via ASGI transport.
4. Dedicated `--webhook-port` app (`create_webhooks_only_app`) & dual-port
   `ServerProcess` lifecycle.
5. `start_server_and_get_agent` remote connection mode & CLI flags
   (`--server-url`, `--api-key`, `--user-id`, `--webhook-port`, `--postgres-uri`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from langgraph_sdk import Auth

from talkops.cli.main import build_parser, main
from talkops.server._server_config import ServerConfig
from talkops.server.server import generate_langgraph_json
from talkops.server.auth import (
    PlaceholderTokenAuthProvider,
    authenticate_request,
    auth,
    clear_auth_caches,
    exchange_api_key_for_temp_token,
    issue_temporary_jwt,
    on_store_access,
    on_thread_access,
    on_thread_create,
    on_thread_search,
    verify_jwt_token,
    verify_token_with_control_plane,
)
from talkops.server.webhooks_app import app as full_server_app, create_webhooks_only_app
from talkops.state import session as session_mod
from talkops.state.postgres_session import get_postgres_session_uri
from talkops.state.remote_session import (
    RemoteSessionBackend,
    clear_remote_client_context,
    get_cached_temporary_token,
    set_remote_client_context,
)


# ── 1. LangGraph Custom Auth & Multi-User Isolation Tests ─────────────────


class TestLangGraphCustomAuth:
    """Verify LangGraph custom auth handlers and PlaceholderTokenAuthProvider."""

    def test_auth_covers_all_langgraph_handler_paths(self) -> None:
        assert auth._authenticate_handler is not None
        expected_resources = {
            ("threads", "create"),
            ("threads", "read"),
            ("threads", "update"),
            ("threads", "delete"),
            ("threads", "search"),
            ("threads", "create_run"),
            ("store", "*"),
            ("assistants", "*"),
            ("crons", "*"),
        }
        assert expected_resources.issubset(set(auth._handlers.keys()))

    @pytest.mark.asyncio
    async def test_placeholder_auth_bearer_token_and_explicit_user(self) -> None:
        provider = PlaceholderTokenAuthProvider()
        headers = {
            "authorization": "Bearer sk-ops-secret-token-12345",
            "x-talkops-user": "alice@company.com",
        }
        user = await provider.authenticate(headers=headers, path="/threads")
        assert user["identity"] == "alice@company.com"
        assert user["role"] == "operator"
        assert user["is_webhook"] is False

    @pytest.mark.asyncio
    async def test_placeholder_auth_derives_deterministic_user_from_token(self) -> None:
        provider = PlaceholderTokenAuthProvider()
        u1 = await provider.authenticate(
            headers={"authorization": "Bearer token-alpha-long-secret-string-1234567890"},
            path="/threads",
        )
        u2 = await provider.authenticate(
            headers={"authorization": "Bearer token-alpha-long-secret-string-1234567890"},
            path="/threads",
        )
        u3 = await provider.authenticate(
            headers={"authorization": "Bearer token-beta-long-secret-string-1234567890"},
            path="/threads",
        )
        assert u1["identity"] == u2["identity"]
        assert u1["identity"].startswith("user_")
        assert u1["identity"] != u3["identity"]

    @pytest.mark.asyncio
    async def test_placeholder_auth_static_api_keys_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "TALKOPS_SERVER_API_KEYS",
            json.dumps({"alice": "sk-alice", "bob": "sk-bob"}),
        )
        provider = PlaceholderTokenAuthProvider()
        alice = await provider.authenticate(
            headers={"x-api-key": "sk-alice"},
            path="/threads",
        )
        assert alice["identity"] == "alice"

        with pytest.raises(Auth.exceptions.HTTPException) as exc_info:
            await provider.authenticate(
                headers={"x-api-key": "sk-invalid"},
                path="/threads",
            )
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_placeholder_auth_token_mode_rejects_anonymous(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TALKOPS_AUTH_MODE", raising=False)
        provider = PlaceholderTokenAuthProvider()
        with pytest.raises(Auth.exceptions.HTTPException) as exc_info:
            await provider.authenticate(headers={}, path="/threads")
        assert exc_info.value.status_code == 401

        with pytest.raises(Auth.exceptions.HTTPException) as exc_info_user:
            await provider.authenticate(headers={"x-talkops-user": "alice"}, path="/threads")
        assert exc_info_user.value.status_code == 401

    @pytest.mark.asyncio
    async def test_webhooks_endpoint_bypasses_operator_bearer_auth(self) -> None:
        user = await authenticate_request(
            path="/webhooks/pagerduty",
            headers={"x-pagerduty-signature": "v1=deadbeef"},
        )
        assert user["identity"] == "webhook"
        assert user["role"] == "webhook"

    @pytest.mark.asyncio
    async def test_internal_webhook_header_authenticates_with_matching_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TALKOPS_INTERNAL_WEBHOOK_TOKEN", "top-secret-internal")
        monkeypatch.setenv("TALKOPS_AUTH_MODE", "token")
        user = await authenticate_request(
            path="/threads",
            headers={"x-talkops-internal-webhook": "top-secret-internal"},
        )
        assert user["identity"] == "webhook"
        assert user["role"] == "webhook"

        with pytest.raises(Auth.exceptions.HTTPException) as exc_info:
            await authenticate_request(
                path="/threads",
                headers={"x-talkops-internal-webhook": "wrong-secret"},
            )
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_thread_handlers_stamp_owner_and_search_filter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TALKOPS_AUTH_MODE", "token")
        mock_ctx = MagicMock()
        mock_ctx.user.identity = "alice"

        create_value: dict[str, Any] = {"metadata": {"topic": "eks"}}
        create_filter = await on_thread_create(mock_ctx, create_value)
        assert create_value["metadata"]["owner"] == "alice"
        assert create_filter == {"owner": "alice"}

        # Webhook source stamps owner="webhook"
        wh_value: dict[str, Any] = {"metadata": {"source": "webhook"}}
        await on_thread_create(mock_ctx, wh_value)
        assert wh_value["metadata"]["owner"] == "webhook"

        search_filter = await on_thread_search(mock_ctx, {})
        assert search_filter == {"owner": "alice"}

        access_filter = await on_thread_access(mock_ctx, {})
        assert access_filter is None

    @pytest.mark.asyncio
    async def test_store_handler_isolates_user_namespaces_and_preserves_system_namespaces(self) -> None:
        mock_ctx = MagicMock()
        mock_ctx.user.identity = "alice"

        # Unprefixed user namespace gets prefixed with ("alice", ...)
        val1: dict[str, Any] = {"namespace": ("notes", "k8s")}
        await on_store_access(mock_ctx, val1)
        assert val1["namespace"] == ("alice", "notes", "k8s")

        # Already prefixed with ("alice", ...) is unchanged
        val2: dict[str, Any] = {"namespace": ("alice", "notes")}
        await on_store_access(mock_ctx, val2)
        assert val2["namespace"] == ("alice", "notes")

        # System namespace ("talkops", "approval_mode") is preserved
        val3: dict[str, Any] = {"namespace": ("talkops", "approval_mode")}
        await on_store_access(mock_ctx, val3)
        assert val3["namespace"] == ("talkops", "approval_mode")

    @pytest.mark.asyncio
    async def test_temporary_jwt_issuance_and_verification(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TALKOPS_JWT_SECRET", "test-super-secret-hmac-key-for-talkops-32bytes")
        token_resp = issue_temporary_jwt(
            user_id="alice@company.com",
            role="operator",
            org_id="org_acme",
            permissions=["threads:read", "threads:write"],
            ttl_seconds=300,
        )
        assert token_resp["token_type"] == "Bearer"
        assert token_resp["user_id"] == "alice@company.com"
        assert token_resp["expires_in"] == 300
        raw_jwt = token_resp["access_token"]
        assert raw_jwt.startswith("eyJ")

        # Verify via verify_jwt_token
        claims = verify_jwt_token(raw_jwt)
        assert claims["identity"] == "alice@company.com"
        assert claims["org_id"] == "org_acme"
        assert claims["role"] == "operator"

        # Verify via PlaceholderTokenAuthProvider
        provider = PlaceholderTokenAuthProvider()
        user = await provider.authenticate(
            headers={"authorization": f"Bearer {raw_jwt}"},
            path="/threads",
        )
        assert user["identity"] == "alice@company.com"
        assert user["org_id"] == "org_acme"
        assert "threads:write" in user["permissions"]

        # Expired JWT is rejected with 401
        expired_resp = issue_temporary_jwt(user_id="alice@company.com", ttl_seconds=-10)
        with pytest.raises(Auth.exceptions.HTTPException) as exc_info:
            verify_jwt_token(expired_resp["access_token"])
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_control_plane_token_introspection_with_ttl_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clear_auth_caches()
        monkeypatch.setenv("TALKOPS_CONTROL_PLANE_URL", "https://cp.talkops.internal")
        monkeypatch.setenv("TALKOPS_CONTROL_PLANE_SERVICE_KEY", "cp-svc-secret")

        call_count = 0

        async def _mock_handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            assert request.url == httpx.URL("https://cp.talkops.internal/api/v1/auth/verify")
            assert request.headers.get("X-Control-Plane-Key") == "cp-svc-secret"
            body = json.loads(request.content.decode("utf-8"))
            if body["token"] == "oc_live_valid_token":
                return httpx.Response(
                    200,
                    json={
                        "active": True,
                        "user_id": "charlie@acme.io",
                        "org_id": "org_acme",
                        "role": "admin",
                        "scopes": ["threads:read", "threads:write", "runs:create"],
                    },
                )
            return httpx.Response(200, json={"active": False})

        mock_transport = httpx.MockTransport(_mock_handler)
        orig_client = httpx.AsyncClient

        def _patched_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = mock_transport
            return orig_client(*args, **kwargs)

        with patch("talkops.server.auth.httpx.AsyncClient", side_effect=_patched_client):
            # 1st call hits the Control Plane HTTP endpoint
            u1 = await verify_token_with_control_plane("oc_live_valid_token")
            assert u1 is not None
            assert u1["identity"] == "charlie@acme.io"
            assert u1["org_id"] == "org_acme"
            assert u1["role"] == "admin"
            assert call_count == 1

            # 2nd call with same token is served from in-memory SHA-256 TTL cache (0 HTTP calls)
            u2 = await verify_token_with_control_plane("oc_live_valid_token")
            assert u2 is not None
            assert u2["identity"] == "charlie@acme.io"
            assert call_count == 1

            # Invalid/revoked token raises 401
            with pytest.raises(Auth.exceptions.HTTPException) as exc_info:
                await verify_token_with_control_plane("oc_live_revoked_token")
            assert exc_info.value.status_code == 401
            assert call_count == 2
        clear_auth_caches()


# ── 2. Multi-User Session Isolation Tests (SQLite & REST API) ─────────────


@pytest.fixture
def isolated_sqlite_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point session persistence at an isolated temporary SQLite database."""
    db_file = tmp_path / "sessions.db"
    monkeypatch.setattr(session_mod, " get_db_path", lambda: db_file, raising=False)
    monkeypatch.setattr(session_mod, "get_db_path", lambda: db_file)
    monkeypatch.delenv("TALKOPS_POSTGRES_URI", raising=False)
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TALKOPS_SERVER_URL", raising=False)
    monkeypatch.delenv("TALKOPS_CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("TALKOPS_CONTROL_PLANE_JWKS_URL", raising=False)
    clear_remote_client_context()
    session_mod.set_current_user_id(None)
    return db_file


async def _seed_checkpoint_thread(
    thread_id: str,
    *,
    title: str,
    user_id: str,
    git_branch: str = "main",
    cwd: str = "/srv/ops",
) -> None:
    """Seed a realistic LangGraph checkpoint + thread summary row in SQLite."""
    from datetime import UTC, datetime
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.base import empty_checkpoint

    now_iso = datetime.now(UTC).isoformat()
    async with session_mod.get_checkpointer() as cp:
        chk = empty_checkpoint()
        chk["channel_values"] = {"messages": [HumanMessage(content=title)]}
        await cp.aput(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
            chk,
            {
                "source": "input",
                "step": 1,
                "agent_name": "talkops",
                "updated_at": now_iso,
                "git_branch": git_branch,
                "cwd": cwd,
                "owner": user_id,
                "title": title,
            },
            {},
        )
    await session_mod.set_thread_title(
        thread_id,
        title,
        only_if_empty=False,
        user_id=user_id,
    )


class TestMultiUserSessionIsolation:
    """Verify per-user thread isolation and webhook thread visibility."""

    @pytest.mark.asyncio
    async def test_multi_user_threads_isolated_and_webhook_threads_shared(
        self, isolated_sqlite_db: Path
    ) -> None:
        # Create threads for alice, bob, and webhook
        await _seed_checkpoint_thread(
            "thread-alice-1",
            title="Alice EKS Investigation",
            user_id="alice",
        )
        await _seed_checkpoint_thread(
            "thread-bob-1",
            title="Bob RDS Migration",
            user_id="bob",
        )
        await _seed_checkpoint_thread(
            "thread-webhook-1",
            title="[Alert] PagerDuty High CPU",
            user_id="webhook",
        )

        # Alice sees her thread + webhook thread, NOT Bob's thread
        alice_threads = await session_mod.list_threads(user_id="alice")
        alice_ids = {t["thread_id"] for t in alice_threads}
        assert "thread-alice-1" in alice_ids
        assert "thread-webhook-1" in alice_ids
        assert "thread-bob-1" not in alice_ids

        # Bob sees his thread + webhook thread, NOT Alice's thread
        bob_threads = await session_mod.list_threads(user_id="bob")
        bob_ids = {t["thread_id"] for t in bob_threads}
        assert "thread-bob-1" in bob_ids
        assert "thread-webhook-1" in bob_ids
        assert "thread-alice-1" not in bob_ids

        # Alice cannot delete Bob's thread
        deleted_bob_by_alice = await session_mod.delete_thread("thread-bob-1", user_id="alice")
        assert deleted_bob_by_alice is False
        assert await session_mod.thread_exists("thread-bob-1") is True

        # Bob can delete his own thread
        deleted_bob_by_bob = await session_mod.delete_thread("thread-bob-1", user_id="bob")
        assert deleted_bob_by_bob is True
        assert await session_mod.thread_exists("thread-bob-1") is False


# ── 3. Server REST API & RemoteSessionBackend End-to-End Tests ────────────


class TestServerRestApiAndRemoteBackend:
    """Test `/api/v1/threads` endpoints and `RemoteSessionBackend` client integration."""

    @pytest.mark.asyncio
    async def test_server_threads_rest_api_and_remote_session_backend(
        self, isolated_sqlite_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Seed threads in the server's DB
        await _seed_checkpoint_thread(
            "srv-alice-100",
            title="Alice Prod Outage",
            git_branch="main",
            cwd="/srv/ops",
            user_id="alice",
        )
        await _seed_checkpoint_thread(
            "srv-bob-200",
            title="Bob Staging Test",
            git_branch="dev",
            cwd="/srv/bob",
            user_id="bob",
        )
        await _seed_checkpoint_thread(
            "srv-webhook-300",
            title="[Alert] GuardDuty Finding",
            user_id="webhook",
        )

        transport = httpx.ASGITransport(app=full_server_app)

        # Create a RemoteSessionBackend that routes HTTP requests through ASGITransport
        backend = RemoteSessionBackend(
            server_url="http://central-talkops:2024",
            api_key="sk-alice-token",
            user_id="alice",
        )

        orig_async_client = httpx.AsyncClient

        def _mock_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            return orig_async_client(*args, **kwargs)

        with patch("talkops.state.remote_session.httpx.AsyncClient", side_effect=_mock_client):
            # 1. List threads as Alice via RemoteSessionBackend
            threads = await backend.list_threads()
            t_ids = {t["thread_id"] for t in threads}
            assert "srv-alice-100" in t_ids
            assert "srv-webhook-300" in t_ids
            assert "srv-bob-200" not in t_ids

            # Verify the client automatically exchanged "sk-alice-token" for a temporary JWT via /api/v1/auth/token
            cached_jwt = get_cached_temporary_token("sk-alice-token")
            assert cached_jwt is not None
            assert cached_jwt.startswith("eyJ")
            decoded = verify_jwt_token(cached_jwt)
            assert decoded["identity"] == "alice"

            # 2. Update thread title via RemoteSessionBackend
            updated = await backend.set_thread_title(
                "srv-alice-100",
                "Alice Resolved Outage",
                only_if_empty=False,
            )
            assert updated is True
            updated_threads = await backend.list_threads()
            alice_row = next(t for t in updated_threads if t["thread_id"] == "srv-alice-100")
            assert alice_row["title"] == "Alice Resolved Outage"

            # 3. Get most recent thread for Alice
            recent = await backend.get_most_recent()
            assert recent is not None
            assert recent["thread_id"] in {"srv-alice-100", "srv-webhook-300"}

            # 4. Check thread_exists
            assert await backend.thread_exists("srv-alice-100") is True
            assert await backend.thread_exists("non-existent-id") is False

            # 5. Delete Alice's thread via RemoteSessionBackend
            deleted = await backend.delete_thread("srv-alice-100")
            assert deleted is True
            assert await backend.thread_exists("srv-alice-100") is False

    @pytest.mark.asyncio
    async def test_webhooks_only_app_isolates_webhook_routes(self) -> None:
        webhook_app = create_webhooks_only_app()
        transport = httpx.ASGITransport(app=webhook_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://webhook-listener") as client:
            health_resp = await client.get("/webhooks/health")
            assert health_resp.status_code == 200
            assert health_resp.json()["status"] == "ok"

            # Operator thread management API must NOT be exposed on the dedicated webhook port
            threads_resp = await client.get("/api/v1/threads")
            assert threads_resp.status_code == 404


# ── 4. Server Config, Scaffolding & Dual-Port Webhook Listener Tests ──────


class TestServerConfigAndLifecycle:
    """Verify langgraph.json generation, server scaffolding, and dual-port support."""

    def test_generate_langgraph_json_includes_store_and_auth(self, tmp_path: Path) -> None:
        lg_path = generate_langgraph_json(
            tmp_path,
            enable_store=True,
            enable_auth=True,
        )
        lg_json = json.loads(lg_path.read_text(encoding="utf-8"))
        assert lg_json["store"] == {"path": "./store.py:create_store"}
        assert lg_json["auth"] == {"path": "./auth.py:auth"}
        assert lg_json["http"]["app"] == "./webhooks_app.py:app"

    def test_postgres_uri_resolved_from_discrete_env_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TALKOPS_POSTGRES_URI", raising=False)
        monkeypatch.delenv("POSTGRES_URI", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("TALKOPS_POSTGRES_HOST", "pg.talkops.internal")
        monkeypatch.setenv("TALKOPS_POSTGRES_PORT", "5432")
        monkeypatch.setenv("TALKOPS_POSTGRES_USER", "ops_admin")
        monkeypatch.setenv("TALKOPS_POSTGRES_PASSWORD", "s3cr#t!")
        monkeypatch.setenv("TALKOPS_POSTGRES_DB", "talkops_prod")
        monkeypatch.setenv("TALKOPS_POSTGRES_SSLMODE", "require")

        uri = get_postgres_session_uri()
        assert uri == "postgresql://ops_admin:s3cr%23t%21@pg.talkops.internal:5432/talkops_prod?sslmode=require"

        cfg = ServerConfig.from_env()
        assert cfg.postgres_uri == uri

    @pytest.mark.asyncio
    async def test_start_server_and_get_agent_remote_mode_skips_local_subprocess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from talkops.cli.server_manager import start_server_and_get_agent

        clear_remote_client_context()
        try:
            remote_agent, handle = await start_server_and_get_agent(
                model="anthropic:claude-sonnet-4-5",
                server_url="http://central.talkops.internal:2024",
                api_key="sk-central-123",
                user_id="alice",
            )
            assert handle is None
            assert remote_agent._url == "http://central.talkops.internal:2024"
            assert remote_agent._user_id == "alice"
            assert remote_agent._headers["Authorization"] == "Bearer sk-central-123"
            assert remote_agent._headers["X-TalkOps-User"] == "alice"
        finally:
            clear_remote_client_context()


# ── 5. CLI Flags (`serve` & Client `--server-url` / `--api-key`) Tests ────


class TestCliServerAndClientFlags:
    """Verify CLI parser and command routing for central server and remote client."""

    def test_serve_parser_accepts_webhook_port_postgres_and_auth_flags(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "serve",
                "--host",
                "0.0.0.0",
                "--port",
                "2024",
                "--webhook-port",
                "8080",
                "--postgres-uri",
                "postgresql://ops:secret@db:5432/talkops",
                "--auth-mode",
                "token",
            ]
        )
        assert args.subcommand == "serve"
        assert args.host == "0.0.0.0"
        assert args.port == 2024
        assert args.webhook_port == 8080
        assert args.postgres_uri == "postgresql://ops:secret@db:5432/talkops"
        assert args.auth_mode == "token"

    def test_client_flags_forwarded_to_non_interactive_without_local_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_run = MagicMock(return_value=0)
        monkeypatch.setattr("talkops.cli.main.run_non_interactive", mock_run)
        # Simulate missing local model credentials — remote server owns the model
        monkeypatch.setattr(
            "talkops.model.factory._get_default_model_spec",
            MagicMock(side_effect=RuntimeError("No local model configured")),
        )

        rc = main(
            [
                "--server-url",
                "http://central:2024",
                "--api-key",
                "sk-remote-key",
                "--user-id",
                "alice",
                "-p",
                "check cluster health",
            ]
        )
        assert rc == 0
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["server_url"] == "http://central:2024"
        assert kwargs["api_key"] == "sk-remote-key"
        assert kwargs["user_id"] == "alice"
        assert kwargs["model"] is None

    def test_client_flags_forwarded_to_interactive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_interactive = MagicMock(return_value=0)
        monkeypatch.setattr("talkops.cli.main.run_interactive", mock_interactive)

        rc = main(
            [
                "--server-url",
                "http://central:2024",
                "--api-key",
                "sk-remote-key",
                "--user-id",
                "alice",
            ]
        )
        assert rc == 0
        mock_interactive.assert_called_once()
        _, kwargs = mock_interactive.call_args
        assert kwargs["server_url"] == "http://central:2024"
        assert kwargs["api_key"] == "sk-remote-key"
        assert kwargs["user_id"] == "alice"


# ── 6. Client CompositeCredentialStorage & `talkops auth` CLI Suite Tests ─


class TestClientAuthStorageAndCliCommands:
    """Verify `talkops.auth.storage` and `talkops auth` CLI subcommands end-to-end."""

    def test_keyring_bypass_detectors_and_timeout_memoization(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from talkops.auth.storage import (
            memoize_keyring_timeout,
            should_bypass_keyring,
            ssh_detector,
            wsl_detector,
        )
        from talkops.config import paths as paths_mod

        auth_file = tmp_path / "auth.json"
        monkeypatch.setattr(paths_mod, "AUTH_PATH", auth_file)

        monkeypatch.setenv("SSH_CLIENT", "192.168.1.10 54321 22")
        assert ssh_detector() is True

        monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
        assert wsl_detector() is True

        marker = memoize_keyring_timeout("unit-test")
        assert marker.exists()
        assert "unit-test" in marker.read_text(encoding="utf-8")
        assert should_bypass_keyring() is True

    def test_composite_storage_session_and_provider_keys_0600(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from talkops.auth.models import StoredSession, mask_api_key
        from talkops.auth.storage import CompositeCredentialStorage

        auth_file = tmp_path / "auth.json"
        env_file = tmp_path / ".env"
        storage = CompositeCredentialStorage(auth_file_path=auth_file, env_file_path=env_file)

        assert mask_api_key("oc_live_k9x2m4p1_supersecretvalue123") == "oc_live_k9x2m4p1_***"

        # 1. Save and load Control Plane session
        sess = StoredSession(
            access_token="eyJ.fake.jwt",
            api_key="oc_live_k9x2m4p1_supersecretvalue123",
            server_url="http://central:2024",
            control_plane_url="https://cp.talkops.ai",
            user_id="alice@company.com",
            org_id="org_acme",
            expires_at=9999999999.0,
        )
        assert storage.save_session(sess, sync_dotenv=True) is True
        assert auth_file.exists()
        assert (auth_file.stat().st_mode & 0o777) == 0o600

        loaded = storage.load_session()
        assert loaded is not None
        assert loaded.user_id == "alice@company.com"
        assert loaded.org_id == "org_acme"
        assert loaded.api_key_prefix == "oc_live_k9x2m4p1_***"
        assert loaded.is_access_token_valid() is True

        # 2. Save, read, and delete LLM provider key
        assert storage.set_provider_key(
            "anthropic",
            "sk-ant-api03-testkey123456",
            base_url="https://gateway.acme.internal/v1",
            sync_dotenv=True,
        ) is True
        assert storage.get_provider_key("anthropic") == "sk-ant-api03-testkey123456"
        assert "ANTHROPIC_API_KEY=sk-ant-api03-testkey123456" in env_file.read_text(encoding="utf-8")

        assert storage.delete_provider_key("anthropic", sync_dotenv=False) is True
        assert storage.get_provider_key("anthropic") is None

        # 3. Clear session
        assert storage.clear_session(clear_dotenv=True) is True
        assert storage.load_session() is None

    def test_cli_auth_login_status_token_set_list_remove_logout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from talkops.auth import storage as storage_mod
        from talkops.config import paths as paths_mod

        auth_file = tmp_path / "auth.json"
        env_file = tmp_path / ".env"
        monkeypatch.setattr(paths_mod, "AUTH_PATH", auth_file)
        monkeypatch.setattr(paths_mod, "GLOBAL_ENV_PATH", env_file)
        monkeypatch.setattr(
            storage_mod,
            "_default_storage",
            storage_mod.CompositeCredentialStorage(auth_file_path=auth_file, env_file_path=env_file),
        )
        clear_remote_client_context()

        transport = httpx.ASGITransport(app=full_server_app)
        orig_async_client = httpx.AsyncClient

        def _mock_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            return orig_async_client(*args, **kwargs)

        with patch("talkops.state.remote_session.httpx.AsyncClient", side_effect=_mock_client):
            # 1. `talkops auth login` exchanges key for JWT and saves to auth.json
            rc_login = main(
                [
                    "auth",
                    "login",
                    "--server-url",
                    "http://central-talkops:2024",
                    "--api-key",
                    "oc_live_k9x2m4p1_secret987654",
                    "--user-id",
                    "alice",
                    "--json",
                ]
            )
            assert rc_login == 0
            login_payload = json.loads(capsys.readouterr().out)
            assert login_payload["command"] == "auth login"
            assert login_payload["data"]["authenticated"] is True
            assert login_payload["data"]["verified_temp_token"] is True
            assert login_payload["data"]["api_key_prefix"] == "oc_live_k9x2m4p1_***"

            # 2. `talkops auth token --json` returns the cached JWT
            rc_tok = main(["auth", "token", "--json"])
            assert rc_tok == 0
            tok_payload = json.loads(capsys.readouterr().out)
            assert tok_payload["data"]["access_token"].startswith("eyJ")

            # 3. `talkops auth set openai sk-proj-test123456 --json`
            rc_set = main(["auth", "set", "openai", "sk-proj-test123456", "--json"])
            assert rc_set == 0
            set_payload = json.loads(capsys.readouterr().out)
            assert set_payload["data"]["saved"] is True

            # 4. `talkops auth status --json`
            fake_aws = MagicMock(
                is_authenticated=True,
                account_id="111122223333",
                arn="arn:aws:iam::111122223333:user/alice",
                user_id="AIDA123",
                region="us-east-1",
                profile="default",
                error=None,
            )
            monkeypatch.setattr("talkops.cli.commands.auth.get_aws_context", lambda: fake_aws)
            rc_status = main(["auth", "status", "--json"])
            assert rc_status == 0
            status_payload = json.loads(capsys.readouterr().out)
            assert status_payload["data"]["control_plane"]["authenticated"] is True
            assert status_payload["data"]["control_plane"]["temp_token_active"] is True
            assert status_payload["data"]["aws_sts"]["account_id"] == "111122223333"

            # 5. `talkops auth list --json`
            rc_list = main(["auth", "list", "--json"])
            assert rc_list == 0
            list_payload = json.loads(capsys.readouterr().out)
            assert list_payload["data"]["control_plane"]["authenticated"] is True

            # 6. `talkops auth remove openai --json`
            rc_rem = main(["auth", "remove", "openai", "--json"])
            assert rc_rem == 0
            rem_payload = json.loads(capsys.readouterr().out)
            assert rem_payload["data"]["removed"] is True

            # 7. `talkops auth logout --all --json`
            rc_logout = main(["auth", "logout", "--all", "--json"])
            assert rc_logout == 0
            logout_payload = json.loads(capsys.readouterr().out)
            assert logout_payload["data"]["session_cleared"] is True

    @pytest.mark.asyncio
    async def test_browser_and_paste_token_acquirer_loopback_and_paste_race(self) -> None:
        from urllib.parse import parse_qs, urlparse
        from talkops.auth.oauth_acquirer import (
            BrowserAndPasteTokenAcquirer,
            extract_code_from_paste,
            generate_pkce_challenge,
        )

        pkce = generate_pkce_challenge()
        assert len(pkce.code_verifier) >= 43
        assert pkce.code_challenge_method == "S256"
        assert extract_code_from_paste(
            f"http://127.0.0.1:51234/auth/callback?code=oc_code_abc123&state={pkce.state}",
            expected_state=pkce.state,
        ) == "oc_code_abc123"

        def _mock_cp_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/auth/token":
                body = json.loads(request.content.decode("utf-8"))
                assert body["grant_type"] == "authorization_code"
                assert body["code"].startswith("oc_code_")
                assert body["code_verifier"]
                return httpx.Response(
                    200,
                    json={
                        "access_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c3JfMSJ9.sig",
                        "token_type": "Bearer",
                        "expires_in": 900,
                        "api_key": "oc_live_sso99999_secretkey1234567890",
                        "api_key_prefix": "oc_live_sso99999",
                        "user_id": "usr_sso_1",
                        "email": "sandeep@acmecorp.com",
                        "org_id": "org_ent_acmecorp_com",
                        "role": "operator",
                        "tier": "enterprise",
                        "account_type": "enterprise",
                        "auth_method": "enterprise",
                        "capabilities": ["k8s.read", "agent.spawn.multi"],
                        "allowed_agents": ["k8s-agent", "cloud-agent"],
                    },
                )
            return httpx.Response(404)

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_cp_handler))
        try:
            # 1. Test Loopback Browser Callback Winner
            acquirer_loopback: BrowserAndPasteTokenAcquirer

            def _simulate_browser_callback(authorize_url: str) -> bool:
                parsed = urlparse(authorize_url)
                qs = parse_qs(parsed.query)
                redirect_uri = qs["redirect_uri"][0]
                state = qs["state"][0]
                callback_target = f"{redirect_uri}?code=oc_code_loopback_win&state={state}"
                assert acquirer_loopback.active_loopback is not None
                html_out = acquirer_loopback.active_loopback.deliver_callback_url(callback_target)
                assert "SSO Login Complete" in html_out
                return True

            acquirer_loopback = BrowserAndPasteTokenAcquirer(
                control_plane_url="http://cp.talkops.local",
                server_url="http://srv.talkops.local:2024",
                sso_provider="enterprise",
                login_hint="sandeep@acmecorp.com",
                timeout_seconds=2.0,
                browser_opener=_simulate_browser_callback,
                http_client=mock_client,
                output_fn=lambda _msg: None,
            )
            session1, data1 = await acquirer_loopback.acquire_session()
            assert session1.auth_method == "wif_sso"
            assert session1.email == "sandeep@acmecorp.com"
            assert session1.org_id == "org_ent_acmecorp_com"
            assert session1.user_tier == "enterprise"
            assert session1.api_key == "oc_live_sso99999_secretkey1234567890"
            assert data1["account_type"] == "enterprise"

            # 2. Test Terminal Paste Code Winner (SSH / Headless fallback)
            acquirer_paste = BrowserAndPasteTokenAcquirer(
                control_plane_url="http://cp.talkops.local",
                server_url="http://srv.talkops.local:2024",
                sso_provider="google",
                no_browser=True,
                timeout_seconds=2.0,
                paste_reader=lambda: "oc_code_pasted_from_ssh_terminal",
                http_client=mock_client,
                output_fn=lambda _msg: None,
            )
            session2, _ = await acquirer_paste.acquire_session()
            assert session2.access_token.startswith("eyJ")
            assert session2.api_key_prefix == "oc_live_sso99999"
        finally:
            await mock_client.aclose()





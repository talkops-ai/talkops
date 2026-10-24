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

"""Comprehensive unit tests for the TalkOps Server Addon Framework.

Covers:
1. Default flow & built-in OSS addons (`client-gateway`, `core-auth`, `webhooks`) with zero disturbance
2. External repository addon discovery via `importlib.metadata.entry_points(group="talkops.addons")`
3. Industry-standard runtime hooking of external repositories via `addons.json` / `--addons-config`
   (`package` + `python_path`, or direct `path` to an external repository)
4. Custom user addons via `.talkops/addons/*.py`, `settings.json`, `--custom-addon`, and `@register_addon`
5. 4-Tier configuration precedence, Server Profiles, and `.talkops/.env` / environment variable secret resolution
6. In-memory `AgentRunGateway` & `HookEventBus` (zero HTTP loopback + HITL interrupt & resume from external addon)
7. CLI `talkops serve addons` / `--addons-config` / `--profile` / `--addons` / `--disable-addons` / `--custom-addon`
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from talkops.addons import (
    ADDON_ENTRYPOINT_GROUP,
    AddonCapabilities,
    AddonContext,
    AddonManager,
    AddonSettings,
    AgentRunGateway,
    BaseServerAddon,
    HookEvent,
    HookEventBus,
    clear_registered_addons,
    register_addon,
    register_server_profile,
    resolve_addon_configs,
    resolve_secret_value,
    resolve_secrets_in_mapping,
)
from talkops.cli.main import main as cli_main
from talkops.server import ServerConfig, create_webhooks_app
from talkops.webhooks import WebhookConfig, WebhookDispatcher


@pytest.fixture(autouse=True)
def _clean_programmatic_addons(monkeypatch: pytest.MonkeyPatch) -> Any:
    from talkops.config.settings import _ensure_bootstrap

    _ensure_bootstrap()
    monkeypatch.delenv("TALKOPS_SERVER_API_KEY", raising=False)
    monkeypatch.delenv("TALKOPS_SERVER_API_KEYS", raising=False)
    monkeypatch.delenv("TALKOPS_API_KEY", raising=False)
    monkeypatch.delenv("TALKOPS_CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("TALKOPS_CONTROL_PLANE_JWKS_URL", raising=False)
    monkeypatch.delenv("TALKOPS_CONTROL_PLANE_SERVICE_KEY", raising=False)
    clear_registered_addons()
    yield
    clear_registered_addons()


# ── 1. Default Flow & Built-In Addons (Zero Disturbance to Current Flow) ─────


@pytest.mark.asyncio
async def test_default_profile_enables_builtin_oss_addons_and_preserves_routes() -> None:
    """Verify default profile enables builtin oss addons and preserves routes."""
    mgr = AddonManager()
    active = mgr.resolve_and_validate()
    active_names = [a.name for a in active]

    assert mgr.profile == "default"
    assert active_names == ["core-auth", "client-gateway", "webhooks"]
    # Ensure no hardcoded enterprise addons are bundled inside core
    assert set(mgr.discovered_addons.keys()) == {"core-auth", "client-gateway", "webhooks"}

    app = mgr.create_asgi_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # 1. Server info includes profile and active_addons while keeping existing fields
        info_resp = await client.get("/api/v1/server/info")
        assert info_resp.status_code == 200
        info = info_resp.json()
        assert info["status"] == "ok"
        assert info["service"] == "talkops-central-server"
        assert info["profile"] == "default"
        assert info["active_addons"] == ["core-auth", "client-gateway", "webhooks"]

        # 2. Addons discovery endpoint lists enabled OSS addons
        addons_resp = await client.get("/api/v1/addons")
        assert addons_resp.status_code == 200
        addons_data = addons_resp.json()
        assert addons_data["profile"] == "default"
        assert addons_data["active_addons"] == ["core-auth", "client-gateway", "webhooks"]
        by_name = {a["name"]: a for a in addons_data["addons"]}
        assert by_name["webhooks"]["status"] == "ok"
        assert by_name["webhooks"]["tier"] == "oss"

        # 3. Built-in webhook routes are mounted and healthy
        wh_resp = await client.get("/webhooks/health")
        assert wh_resp.status_code == 200
        assert wh_resp.json() == {"status": "ok"}


# ── 2. External Repository Addon Discovery (`entry_points`) ──────────────────


@pytest.mark.asyncio
async def test_external_repository_addon_discovery_via_entry_points() -> None:
    """Verify addons from a separate repository / wheel load via `entry_points(group='talkops.addons')`."""

    class ExternalRepoPagerAddon(BaseServerAddon):
        """Test suite or helper class for external repo pager addon."""
        name = "external-pager"
        tier = "enterprise"
        version = "2.1.0"
        description = "External repository incident correlation addon"
        capabilities = AddonCapabilities(routes=True)
        enabled_by_default = False

        def get_routes(self) -> list[BaseRoute]:
            """Provide get routes test helper or fixture."""
            async def _ping(_req: Request) -> JSONResponse:
                return JSONResponse({"addon": "external-pager", "from": "external-repo"})

            return [Route("/addons/external-pager/ping", _ping, methods=["GET"])]

    fake_ep = MagicMock()
    fake_ep.name = "external-pager"
    fake_ep.value = "talkops_enterprise_repo.pager:ExternalRepoPagerAddon"
    fake_ep.load.return_value = ExternalRepoPagerAddon

    with patch("importlib.metadata.entry_points", return_value=[fake_ep]) as mock_eps:
        mgr = AddonManager(enabled_addons=["external-pager"])
        active = mgr.resolve_and_validate()
        mock_eps.assert_called_once_with(group=ADDON_ENTRYPOINT_GROUP)

        active_names = [a.name for a in active]
        assert "external-pager" in active_names
        ext_addon = mgr.discovered_addons["external-pager"]
        assert ext_addon.source == "entrypoint:talkops_enterprise_repo.pager:ExternalRepoPagerAddon"

        app = mgr.create_asgi_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            resp = await client.get("/addons/external-pager/ping")
            assert resp.status_code == 200
            assert resp.json() == {"addon": "external-pager", "from": "external-repo"}


# ── 3. Industry-Standard Runtime Hooking of Separate Repositories (`addons.json` + `.env`) ──


@pytest.mark.asyncio
async def test_external_repository_hooked_at_runtime_via_addons_json_and_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate a totally separate repository containing TalkOps & custom addons hooked at runtime via `addons.json` and `.talkops/.env`."""
    # 1. Create a simulated external repository outside the project root
    external_repo_root = tmp_path / "talkops-enterprise-addons-repo"
    ext_pkg_dir = external_repo_root / "src" / "talkops_addons"
    ext_pkg_dir.mkdir(parents=True)
    (ext_pkg_dir / "__init__.py").write_text("", encoding="utf-8")

    # Write a TalkOps-supported addon in the external repository
    (ext_pkg_dir / "slack_bot.py").write_text(
        """
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from talkops.addons import AddonCapabilities, AddonSettings, BaseServerAddon

class SlackBotSettings(AddonSettings):
    bot_token: str = ""
    signing_secret: str = ""
    default_channel: str = "#ops"

class TalkOpsSlackAddon(BaseServerAddon):
    name = "talkops-slack"
    tier = "enterprise"
    version = "1.2.0"
    description = "TalkOps Slack Bot from separate repository"
    capabilities = AddonCapabilities(routes=True, gateway_runner=True)
    settings_schema = SlackBotSettings
    enabled_by_default = False

    def get_routes(self):
        async def _status(_req: Request) -> JSONResponse:
            return JSONResponse({
                "addon": self.name,
                "tier": self.tier,
                "has_bot_token": bool(self._config.bot_token),
                "bot_token_prefix": self._config.bot_token[:8] if self._config.bot_token else "",
                "has_signing_secret": bool(self._config.signing_secret),
                "default_channel": self._config.default_channel,
            })
        return [Route("/addons/talkops-slack/status", _status, methods=["GET"])]
""",
        encoding="utf-8",
    )

    # Also write a standalone custom addon in another separate directory loaded via `path`
    custom_repo_dir = tmp_path / "my-company-custom-addons"
    custom_repo_dir.mkdir(parents=True)
    (custom_repo_dir / "servicenow_addon.py").write_text(
        """
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from talkops.addons import AddonCapabilities, AddonSettings, BaseServerAddon

class ServiceNowSettings(AddonSettings):
    instance_url: str = ""
    api_key: str = ""

class ServiceNowAddon(BaseServerAddon):
    name = "servicenow-bridge"
    tier = "custom"
    description = "Custom ServiceNow bridge from external repo path"
    capabilities = AddonCapabilities(routes=True)
    settings_schema = ServiceNowSettings
    enabled_by_default = False

    def get_routes(self):
        async def _info(_req: Request) -> JSONResponse:
            return JSONResponse({
                "addon": self.name,
                "instance_url": self._config.instance_url,
                "api_key_loaded": self._config.api_key == "snow-secret-from-dotenv",
            })
        return [Route("/addons/servicenow/info", _info, methods=["GET"])]
""",
        encoding="utf-8",
    )

    # 2. Create project workspace with `.talkops/.env` (secrets) and `.talkops/addons.json` (runtime addon manifest)
    project_root = tmp_path / "talkops-workspace"
    talkops_dir = project_root / ".talkops"
    talkops_dir.mkdir(parents=True)

    # User puts secrets in `.talkops/.env`
    dotenv_file = talkops_dir / ".env"
    dotenv_file.write_text(
        "TALKOPS_SLACK_BOT_TOKEN=xoxb-dotenv-token-999\n"
        "TALKOPS_SLACK_SIGNING_SECRET=sig-dotenv-secret-888\n"
        "SERVICENOW_API_KEY=snow-secret-from-dotenv\n",
        encoding="utf-8",
    )

    # Ensure env vars aren't pre-set before `_load_project_dotenv` loads `.talkops/.env`
    monkeypatch.delenv("TALKOPS_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TALKOPS_SLACK_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("SERVICENOW_API_KEY", raising=False)
    monkeypatch.chdir(project_root)

    # Declarative `addons.json` hooking both external repositories and referencing secrets via `${ENV_VAR}` and `*_env`
    addons_json = talkops_dir / "addons.json"
    addons_json.write_text(
        json.dumps(
            {
                "profile": "default",
                "addons": {
                    "talkops-slack": {
                        "enabled": True,
                        "package": "talkops_addons.slack_bot:TalkOpsSlackAddon",
                        "python_path": [str(external_repo_root / "src")],
                        "config": {
                            "bot_token": "${TALKOPS_SLACK_BOT_TOKEN}",
                            "signing_secret_env": "TALKOPS_SLACK_SIGNING_SECRET",
                            "default_channel": "#sre-war-room",
                        },
                    },
                    "servicenow-bridge": {
                        "enabled": True,
                        "path": str(custom_repo_dir / "servicenow_addon.py") + ":ServiceNowAddon",
                        "config": {
                            "instance_url": "https://acme.service-now.com",
                            "api_key": "${SERVICENOW_API_KEY}",
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    mgr = AddonManager(project_root=project_root)
    active = mgr.resolve_and_validate()
    active_names = [a.name for a in active]

    # Built-in default flow is preserved AND external repo addons are hooked in!
    assert "core-auth" in active_names
    assert "client-gateway" in active_names
    assert "webhooks" in active_names
    assert "talkops-slack" in active_names
    assert "servicenow-bridge" in active_names

    app = mgr.create_asgi_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        slack_resp = await client.get("/addons/talkops-slack/status")
        assert slack_resp.status_code == 200
        assert slack_resp.json() == {
            "addon": "talkops-slack",
            "tier": "enterprise",
            "has_bot_token": True,
            "bot_token_prefix": "xoxb-dot",
            "has_signing_secret": True,
            "default_channel": "#sre-war-room",
        }

        snow_resp = await client.get("/addons/servicenow/info")
        assert snow_resp.status_code == 200
        assert snow_resp.json() == {
            "addon": "servicenow-bridge",
            "instance_url": "https://acme.service-now.com",
            "api_key_loaded": True,
        }


# ── 4. Custom User Addons (`.talkops/addons/`, `settings.json`, `@register_addon`) ──


@pytest.mark.asyncio
async def test_custom_user_addons_via_file_and_project_addons_dir(tmp_path: Path) -> None:
    """Verify users can drop custom addons into `.talkops/addons/*.py` or pass `--custom-addon`."""
    addons_dir = tmp_path / ".talkops" / "addons"
    addons_dir.mkdir(parents=True)

    custom_file = addons_dir / "jira_sync.py"
    custom_file.write_text(
        """
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from talkops.addons import AddonCapabilities, AddonSettings, BaseServerAddon

class JiraSyncSettings(AddonSettings):
    project_key: str = "OPS"

class JiraSyncAddon(BaseServerAddon):
    name = "jira-sync"
    tier = "custom"
    description = "Custom Jira ticket synchronization addon"
    capabilities = AddonCapabilities(routes=True)
    settings_schema = JiraSyncSettings
    enabled_by_default = True

    def get_routes(self):
        async def _jira_info(_req: Request) -> JSONResponse:
            return JSONResponse({
                "addon": self.name,
                "project_key": self._config.project_key,
            })
        return [Route("/addons/jira/info", _jira_info, methods=["GET"])]
""",
        encoding="utf-8",
    )

    # Also configure custom field via `.talkops/settings.json`
    settings_file = tmp_path / ".talkops" / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "addons": {
                    "jira-sync": {
                        "enabled": True,
                        "project_key": "CLOUD-SRE",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    mgr = AddonManager(project_root=tmp_path)
    active = mgr.resolve_and_validate()
    assert "jira-sync" in [a.name for a in active]

    app = mgr.create_asgi_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        resp = await client.get("/addons/jira/info")
        assert resp.status_code == 200
        assert resp.json() == {"addon": "jira-sync", "project_key": "CLOUD-SRE"}


@pytest.mark.asyncio
async def test_programmatic_register_addon_decorator_and_mount_prefix() -> None:
    """Verify programmatic register addon decorator and mount prefix."""
    @register_addon
    class CustomEchoAddon(BaseServerAddon):
        """Test suite or helper class for custom echo addon."""
        name = "custom-echo"
        tier = "custom"
        description = "Programmatically registered echo addon"
        capabilities = AddonCapabilities(routes=True)

        def get_routes(self) -> list[BaseRoute]:
            """Provide get routes test helper or fixture."""
            async def _echo(_req: Request) -> JSONResponse:
                return JSONResponse({"echo": "ok"})

            return [Route("/echo", _echo, methods=["GET"])]

    with patch.dict(
        "os.environ",
        {
            "TALKOPS_ADDONS": "custom-echo",
            "TALKOPS_ADDON_CUSTOM_ECHO_MOUNT_PREFIX": "/custom/v1",
        },
    ):
        app = create_webhooks_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            resp = await client.get("/custom/v1/echo")
            assert resp.status_code == 200
            assert resp.json() == {"echo": "ok"}


# ── 5. 4-Tier Configuration Precedence, Profiles & Env Secret Resolution ─────


def test_four_tier_configuration_precedence_and_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify four tier configuration precedence and profiles."""
    mgr = AddonManager(project_root=tmp_path)
    discovered = mgr.discover_addons()

    # 1. Profile 'api-only' enables client-gateway & core-auth, disables webhooks
    prof, cfgs = resolve_addon_configs(discovered, profile="api-only", project_root=tmp_path, env={})
    assert prof == "api-only"
    assert cfgs["client-gateway"]["enabled"] is True
    assert cfgs["core-auth"]["enabled"] is True
    assert cfgs["webhooks"]["enabled"] is False

    # 2. Custom profile registration (for external enterprise packages)
    register_server_profile("webhook-only", ("core-auth", "webhooks"))
    prof_wh, cfgs_wh = resolve_addon_configs(
        discovered,
        profile="webhook-only",
        project_root=tmp_path,
        env={},
    )
    assert prof_wh == "webhook-only"
    assert cfgs_wh["webhooks"]["enabled"] is True
    assert cfgs_wh["core-auth"]["enabled"] is True
    assert cfgs_wh["client-gateway"]["enabled"] is False

    # 3. Environment variable secret resolution (${ENV_VAR}, env://, and *_env)
    monkeypatch.setenv("MY_SLACK_SECRET", "xoxb-secret-token-123")
    assert resolve_secret_value("${MY_SLACK_SECRET}") == "xoxb-secret-token-123"
    assert resolve_secret_value("env://MY_SLACK_SECRET") == "xoxb-secret-token-123"

    resolved_map = resolve_secrets_in_mapping(
        {
            "slack_bot_token_env": "MY_SLACK_SECRET",
            "direct_ref": "${MY_SLACK_SECRET}",
        }
    )
    assert resolved_map["slack_bot_token"] == "xoxb-secret-token-123"
    assert resolved_map["direct_ref"] == "xoxb-secret-token-123"


# ── 6. In-Memory `AgentRunGateway` & `HookEventBus` (Zero Loopback & HITL) ───


class _FakePregelGraph:
    """Fake in-memory compiled LangGraph `Pregel` instance for testing `AgentRunGateway`."""

    def __init__(self) -> None:
        """Initialize _FakePregelGraph."""
        self.states: dict[str, dict[str, Any]] = {}
        self.should_interrupt: bool = False

    async def astream(
        self,
        input_payload: Any,
        *,
        stream_mode: Any = None,
        subgraphs: bool = True,
        config: dict[str, Any] | None = None,
        context: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Provide astream test helper or fixture."""
        del stream_mode, subgraphs, context, kwargs
        tid = (config or {}).get("configurable", {}).get("thread_id", "default")
        resume_val = getattr(input_payload, "resume", None)
        if resume_val is not None:
            decision = (
                resume_val.get("decisions", [{}])[0].get("type", "approve")
                if isinstance(resume_val, dict)
                else "approve"
            )
            msg = {"role": "assistant", "content": f"Resumed after HITL decision: {decision}"}
            self.states[tid] = {"values": {"messages": [msg]}}
            yield ((), "updates", {"agent": {"messages": [msg]}})
            return

        if self.should_interrupt:
            yield (
                (),
                "updates",
                {
                    "__interrupt__": [
                        {
                            "id": "intr-1",
                            "value": {
                                "action_requests": [
                                    {"name": "execute", "args": {"command": "terraform apply"}}
                                ]
                            },
                        }
                    ]
                },
            )
            return

        msg = {"role": "assistant", "content": "In-memory RCA: Pod memory limit reached."}
        self.states[tid] = {"values": {"messages": [msg]}}
        yield ((), "updates", {"agent": {"messages": [msg]}})

    async def aget_state(self, config: dict[str, Any]) -> dict[str, Any]:
        """Provide aget state test helper or fixture."""
        tid = config.get("configurable", {}).get("thread_id", "default")
        return self.states.get(tid, {"values": {"messages": []}})

    async def aupdate_state(
        self,
        config: dict[str, Any],
        values: dict[str, Any],
        *,
        as_node: str | None = None,
    ) -> dict[str, Any]:
        """Provide aupdate state test helper or fixture."""
        del as_node
        tid = config.get("configurable", {}).get("thread_id", "default")
        self.states[tid] = {"values": dict(values)}
        return self.states[tid]


@pytest.mark.asyncio
async def test_agent_run_gateway_zero_loopback_webhook_and_external_hitl_resume() -> None:
    """Verify built-in WebhookAddon and an external ChatOps addon share AgentRunGateway & HookEventBus."""
    fake_graph = _FakePregelGraph()
    bus = HookEventBus()
    gateway = AgentRunGateway(graph=fake_graph, event_bus=bus)

    observed_events: list[str] = []
    bus.subscribe("*", lambda ev: observed_events.append(ev.event_type))

    # 1. WebhookDispatcher bound to AgentRunGateway executes in-memory with zero HTTP loopback
    cfg = WebhookConfig(default_secret="wh-secret")
    dispatcher = WebhookDispatcher(config=cfg, gateway=gateway)

    pd_payload = {
        "event": {
            "id": "evt-gw-1",
            "event_type": "incident.triggered",
            "data": {"id": "INC-GW-100", "title": "EKS OOM", "urgency": "high"},
        }
    }
    raw_pd = json.dumps(pd_payload).encode()
    sig = "v1=" + hmac.new(b"wh-secret", raw_pd, hashlib.sha256).hexdigest()

    status_code, _ = await dispatcher.handle_webhook(
        provider="pagerduty",
        headers={"X-PagerDuty-Signature": sig},
        raw_body=raw_pd,
    )
    assert status_code == 202
    await dispatcher.drain_background_tasks()

    assert len(dispatcher.recent_deliveries) == 1
    deliv = dispatcher.recent_deliveries[0]
    assert deliv["run_status"] == "success"
    assert "In-memory RCA: Pod memory limit reached." in deliv["analysis_preview"]
    assert "addon.run.started" in observed_events
    assert "addon.run.completed" in observed_events

    # 2. Simulate an external ChatOps addon submitting a run that hits HITL interrupt, then resuming it
    @register_addon
    class ExternalChatOpsAddon(BaseServerAddon):
        """Test suite or helper class for external chat ops addon."""
        name = "external-chatops"
        tier = "enterprise"
        capabilities = AddonCapabilities(routes=True, gateway_runner=True, event_subscriber=True)

        def __init__(self) -> None:
            """Initialize ExternalChatOpsAddon."""
            super().__init__()
            self.interrupted_threads: list[str] = []

        async def on_startup(self, ctx: AddonContext) -> None:
            """Provide on startup test helper or fixture."""
            await super().on_startup(ctx)

            async def _on_interrupt(ev: HookEvent) -> None:
                if ev.thread_id:
                    self.interrupted_threads.append(ev.thread_id)

            ctx.event_bus.subscribe("addon.run.interrupted", _on_interrupt)

        def get_routes(self) -> list[BaseRoute]:
            """Provide get routes test helper or fixture."""
            async def _trigger(req: Request) -> JSONResponse:
                body = await req.json()
                assert self._ctx is not None
                handle = await self._ctx.gateway.submit_run(
                    prompt=body["prompt"],
                    addon_name=self.name,
                    source_key=body["thread_key"],
                )
                return JSONResponse({"run_id": handle.run_id, "thread_id": handle.thread_id}, status_code=202)

            async def _approve(req: Request) -> JSONResponse:
                body = await req.json()
                assert self._ctx is not None
                handle = await self._ctx.gateway.resume_run(
                    thread_id=body["thread_id"],
                    decision=body["decision"],
                    addon_name=self.name,
                )
                return JSONResponse({"run_id": handle.run_id, "status": "resumed"})

            return [
                Route("/addons/chatops/trigger", _trigger, methods=["POST"]),
                Route("/addons/chatops/approve", _approve, methods=["POST"]),
            ]

    fake_graph.should_interrupt = True
    app = create_webhooks_app(addons=["external-chatops"], gateway=gateway)
    mgr: AddonManager = app.state.addon_manager
    await mgr.startup()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            trig = await client.post(
                "/addons/chatops/trigger",
                json={"prompt": "Apply terraform fix", "thread_key": "slack:C1:100"},
            )
            assert trig.status_code == 202
            tid = trig.json()["thread_id"]
            run_handle = gateway.get_run(trig.json()["run_id"])
            if run_handle and run_handle._task:
                await run_handle._task

            ext_chatops: Any = mgr.discovered_addons["external-chatops"]
            assert tid in ext_chatops.interrupted_threads

            # Now resume via HITL approval
            fake_graph.should_interrupt = False
            appr = await client.post(
                "/addons/chatops/approve",
                json={"thread_id": tid, "decision": "approve"},
            )
            assert appr.status_code == 200
            resumed = gateway.get_run(appr.json()["run_id"])
            if resumed and resumed._task:
                await resumed._task
            assert resumed is not None
            assert resumed.status == "success"
            assert "Resumed after HITL decision: approve" in resumed.analysis_text
    finally:
        await mgr.shutdown()


# ── 7. CLI `talkops serve addons` & `--addons-config` Flags ─────────────────


def test_cli_serve_addons_json_and_addons_config_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 1. Default profile addons listing
    """Verify cli serve addons json and addons config flag."""
    code1 = cli_main(["serve", "addons", "--output-format", "json"])
    assert code1 == 0
    out1 = json.loads(capsys.readouterr().out)
    assert out1["command"] == "serve addons"
    assert out1["data"]["profile"] == "default"
    assert out1["data"]["active_addons"] == ["core-auth", "client-gateway", "webhooks"]

    # 2. Create an external addon and an explicit `--addons-config` file
    ext_dir = tmp_path / "external_addon_repo"
    ext_dir.mkdir(parents=True)
    (ext_dir / "pager_addon.py").write_text(
        """
from talkops.addons import AddonCapabilities, BaseServerAddon

class ExternalPagerAddon(BaseServerAddon):
    name = "external-pager"
    tier = "enterprise"
    description = "External Pager Addon"
    capabilities = AddonCapabilities(routes=True)
""",
        encoding="utf-8",
    )
    addons_cfg_file = tmp_path / "my-addons.json"
    addons_cfg_file.write_text(
        json.dumps(
            {
                "addons": {
                    "external-pager": {
                        "enabled": True,
                        "path": str(ext_dir / "pager_addon.py") + ":ExternalPagerAddon",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    code2 = cli_main(
        [
            "serve",
            "--list-addons",
            "--addons-config",
            str(addons_cfg_file),
            "--disable-addons",
            "webhooks",
            "--json",
        ]
    )
    assert code2 == 0
    out2 = json.loads(capsys.readouterr().out)
    assert "external-pager" in out2["data"]["active_addons"]
    assert "webhooks" not in out2["data"]["active_addons"]

    # 3. Verify ServerConfig round-trips addon flags (including addons_config) via to_env() / from_env()
    cfg = ServerConfig.from_cli_args(
        profile="api-only",
        addons=["external-pager"],
        disable_addons=["webhooks"],
        custom_addons=["my_pkg.addons:CustomAddon"],
        addons_config=str(addons_cfg_file),
    )
    env_map = cfg.to_env()
    assert env_map["TALKOPS_SERVER_PROFILE"] == "api-only"
    assert env_map["TALKOPS_ADDONS"] == "external-pager"
    assert env_map["TALKOPS_DISABLE_ADDONS"] == "webhooks"
    assert env_map["TALKOPS_CUSTOM_ADDONS"] == "my_pkg.addons:CustomAddon"
    assert env_map["TALKOPS_ADDONS_CONFIG"] == str(addons_cfg_file)

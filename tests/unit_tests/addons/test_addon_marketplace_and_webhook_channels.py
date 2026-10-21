"""Unit and integration tests for TalkOps Addon Marketplace (`.talkops/`), Python dependency auto-installer, and `webhooks` <-> `channels` outbound notification & thread binding."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator
import json
from pathlib import Path
import subprocess
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from talkops.addons import (
    AddonManager,
    HookEventBus,
    add_addon_marketplace_source,
    discover_marketplace_addons,
    ensure_addon_python_dependencies,
    find_addon_manifest_path,
    find_missing_python_dependencies,
    install_addon,
    list_available_addons,
    load_addon_manifest,
    set_installed_addon_enabled,
    uninstall_addon,
)
from talkops.addons.builtin.webhooks import (
    WebhookAddon,
    set_webhook_dispatcher,
)
from talkops.addons.commands_cli import execute_addon_command, setup_addon_parser
from talkops.config.addons import (
    DefaultAddonMarketplaceConfig,
    get_effective_default_addon_marketplace_configs,
)
from talkops.integrations.thread_store import ThreadStore
from talkops.webhooks.dispatcher import WebhookDispatcher
from talkops.webhooks.models import (
    WebhookConfig,
    parse_outbound_channel_routes,
)
from talkops.webhooks.sinks import WebhookRunResult
from talkops_channels import (  # type: ignore[import-not-found]
    ChannelExposure,
    ChannelHub,
    ChannelsAddon,
    ExposureMode,
    PairingStore,
    SlackChannelConfig,
    SlackWebhookChannel,
)


def test_addon_manifest_and_marketplace_in_libs_directory() -> None:
    """Verify `.talkops/addon.json` and `.talkops/marketplace.json` in `libs/`."""
    repo_root = Path(__file__).resolve().parents[3]
    channels_dir = repo_root / "libs" / "channels"
    manifest_path = find_addon_manifest_path(channels_dir)
    assert manifest_path is not None
    assert manifest_path == channels_dir / ".talkops" / "addon.json"

    manifest = load_addon_manifest(channels_dir)
    assert manifest.name == "channels"
    assert manifest.entry_point == "talkops_channels.addon:ChannelsAddon"
    assert "cryptography>=42.0.0" in manifest.python_dependencies
    assert manifest.capabilities["event_sinks"] is True
    assert manifest.capabilities["event_subscriber"] is True
    assert manifest.webhook_sink["bindsIncidentThread"] is True


def test_python_dependency_detection_and_auto_installer() -> None:
    """Verify `ensure_addon_python_dependencies` detects missing packages and invokes the active env installer."""
    # Satisfied packages in current venv return 0 missing without spawning a subprocess
    installed, missing = ensure_addon_python_dependencies(
        ["pytest>=7.0.0", "pydantic>=2.0.0", "httpx>=0.24.0"],
        addon_name="channels",
    )
    assert installed == []
    assert missing == []

    # Missing package triggers internal installer command targeting `sys.executable`
    fake_pkg = "nonexistent-talkops-test-dep-xyz>=1.2.3"
    assert find_missing_python_dependencies([fake_pkg]) == [fake_pkg]

    executed_cmds: list[list[str]] = []

    def _fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        executed_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="Successfully installed", stderr="")

    installed2, missing2 = ensure_addon_python_dependencies(
        ["pytest>=7.0.0", fake_pkg],
        addon_name="custom-org-addon",
        runner=_fake_runner,
    )
    assert len(executed_cmds) == 1
    assert fake_pkg in executed_cmds[0]
    assert "pytest>=7.0.0" not in executed_cmds[0]
    assert installed2 == [fake_pkg]
    assert missing2 == []


def test_organization_marketplace_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify organizations can override the default central addon marketplace via env vars."""
    monkeypatch.setenv("TALKOPS_ADDON_MARKETPLACE_REPO", "acme-corp/internal-talkops-addons")
    monkeypatch.setenv("TALKOPS_ADDON_MARKETPLACE_REF", "release-2026")
    monkeypatch.setenv("TALKOPS_ADDON_MARKETPLACE_NAME", "acme-addons")

    configs = get_effective_default_addon_marketplace_configs()
    assert configs[0].name == "acme-addons"
    assert configs[0].repository_url == "https://github.com/acme-corp/internal-talkops-addons.git"
    assert (
        configs[0].manifest_url
        == "https://raw.githubusercontent.com/acme-corp/internal-talkops-addons/release-2026/.talkops/marketplace.json"
    )


def test_addon_marketplace_lifecycle_and_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test registering a `.talkops/marketplace.json` repo, installing `channels`, CLI commands, and `AddonManager`."""
    monkeypatch.setenv("TALKOPS_ADDON_DIR", str(tmp_path / "talkops_addons_home"))
    repo_root = Path(__file__).resolve().parents[3]
    libs_marketplace_dir = repo_root / "libs"

    # 1. Register `libs/` (which has `libs/.talkops/marketplace.json`) via CLI
    parser = argparse.ArgumentParser(prog="talkops")
    subparsers = parser.add_subparsers(dest="subcommand")
    setup_addon_parser(subparsers)

    add_args = parser.parse_args(["addon", "marketplace", "add", str(libs_marketplace_dir)])
    out_add = execute_addon_command(add_args)
    assert out_add is not None
    assert "talkops-addons" in out_add

    # 2. Search and list available addons
    search_args = parser.parse_args(["addon", "search", "slack"])
    out_search = execute_addon_command(search_args)
    assert out_search is not None
    assert "channels@talkops-addons" in out_search

    # 3. Install `channels` addon from marketplace
    install_args = parser.parse_args(["addon", "install", "channels@talkops-addons"])
    out_install = execute_addon_command(install_args)
    assert out_install is not None
    assert "Installed and enabled addon 'channels@talkops-addons'" in out_install

    # 4. Verify `discover_marketplace_addons` and `AddonManager` automatically load `channels` alongside built-in `webhooks`
    discovered_mp = discover_marketplace_addons(project_root=tmp_path)
    assert len(discovered_mp.addons) == 1
    assert discovered_mp.addons[0].name == "channels"

    manager = AddonManager(project_root=tmp_path)
    discovered_all = manager.discover_addons()
    assert "webhooks" in discovered_all
    assert "channels" in discovered_all
    assert discovered_all["webhooks"].source == "builtin"
    assert discovered_all["channels"].source == "marketplace:channels@talkops-addons"

    active = manager.resolve_and_validate()
    active_names = [a.name for a in active]
    assert "webhooks" in active_names
    assert "channels" in active_names

    # 5. Disable, re-enable, and uninstall via CLI
    disable_args = parser.parse_args(["addon", "disable", "channels"])
    execute_addon_command(disable_args)
    rows_after_disable = list_available_addons()
    assert any(aid == "channels@talkops-addons" and not is_en for aid, _, _, _, is_en in rows_after_disable)

    enable_args = parser.parse_args(["addon", "enable", "channels"])
    execute_addon_command(enable_args)
    rows_after_enable = list_available_addons()
    assert any(aid == "channels@talkops-addons" and is_en for aid, _, _, _, is_en in rows_after_enable)

    uninstall_args = parser.parse_args(["addon", "uninstall", "channels"])
    out_uninstall = execute_addon_command(uninstall_args)
    assert out_uninstall is not None
    assert "Uninstalled addon 'channels@talkops-addons'" in out_uninstall


@pytest.mark.asyncio
async def test_webhook_to_channel_outbound_notification_and_thread_binding(
    tmp_path: Path,
) -> None:
    """Verify end-to-end flow: Inbound PagerDuty Webhook -> Outbound Slack Thread Notification -> Interactive Slack Follow-Up on same `lg_thread_id`."""
    slack_api_calls: list[tuple[str, dict[str, Any]]] = []

    def _mock_slack_transport(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/conversations.replies"):
            return httpx.Response(200, json={"ok": True, "messages": []})
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        slack_api_calls.append((path, body))
        return httpx.Response(
            200,
            json={"ok": True, "ts": f"1710000100.000{len(slack_api_calls)}"},
        )

    slack_http = httpx.AsyncClient(transport=httpx.MockTransport(_mock_slack_transport))
    shared_thread_store = ThreadStore(backend="memory")
    event_bus = HookEventBus()

    # Fake Gateway for interactive follow-up in Slack
    gateway_runs: list[dict[str, Any]] = []

    class FakeGateway:
        def __init__(self) -> None:
            self.event_bus = event_bus
            self.graph = None

        async def stream_run(
            self,
            input_payload: Any,
            *,
            thread_id: str,
            source_addon: str = "channels",
            context: Any = None,
            **kwargs: Any,
        ) -> AsyncIterator[tuple[tuple[str, ...], str, Any]]:
            gateway_runs.append(
                {
                    "input": input_payload,
                    "thread_id": thread_id,
                    "source_addon": source_addon,
                }
            )
            yield ((), "messages", ({"type": "ai", "content": "Scaled prod-api deployment to 6 replicas."}, {}))

    fake_gateway = FakeGateway()

    # 1. Configure `ChannelHub` & `ChannelsAddon` with Slack adapter
    hub = ChannelHub(
        gateway=fake_gateway,
        thread_store=shared_thread_store,
        pairing_store=PairingStore(tmp_path / "pairing.json"),
        pairing_enabled=False,
    )
    slack_adapter = SlackWebhookChannel(
        SlackChannelConfig(
            bot_token="xoxb-test",
            signing_secret="sec",
            bot_user_id="U_BOT",
            verify_signatures=False,
            exposure=ChannelExposure(
                mode=ExposureMode.ALLOWLIST,
                allowed_users=frozenset({"*"}),
            ),
        ),
        http_client=slack_http,
    )
    hub.register_adapter(slack_adapter)
    channels_addon = ChannelsAddon(hub=hub)

    # 2. Configure built-in `WebhookDispatcher` & `WebhookAddon` with outbound channel routing to Slack `C_INCIDENTS`
    outbound_routes = parse_outbound_channel_routes(
        [
            {
                "channel": "slack",
                "conversation_id": "C_INCIDENTS",
                "providers": ["pagerduty"],
                "severities": ["critical", "high"],
                "notify_on": ["received", "completed"],
                "bind_thread": True,
            }
        ]
    )
    wh_config = WebhookConfig(
        outbound_channels=outbound_routes,
    )

    async def _fake_webhook_rca_executor(
        event: Any,
        lg_thread_id: str,
        delivery_id: str,
        _cfg: WebhookConfig,
    ) -> WebhookRunResult:
        return WebhookRunResult(
            run_id=f"run-{delivery_id[:8]}",
            thread_id=lg_thread_id,
            status="success",
            analysis_text="Root Cause: OOMKilled on pod `prod-api-7f8b9` due to memory spike.",
        )

    dispatcher = WebhookDispatcher(
        config=wh_config,
        thread_store=shared_thread_store,
        run_executor=_fake_webhook_rca_executor,
        event_bus=event_bus,
    )
    webhook_addon = WebhookAddon(dispatcher=dispatcher)

    # 3. Start both `WebhookAddon` (built-in) and `ChannelsAddon` (marketplace) via `AddonManager`
    manager = AddonManager(
        project_root=tmp_path,
        enabled_addons=["webhooks", "channels"],
        disabled_addons=["core-auth", "client-gateway"],
        gateway=fake_gateway,  # type: ignore[arg-type]
        event_bus=event_bus,
        extra_addons=[webhook_addon, channels_addon],
    )
    app = manager.create_asgi_app()
    await manager.startup()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            # 4. Send an inbound PagerDuty v3 webhook to `POST /webhooks/pagerduty`
            pd_payload = {
                "event": {
                    "id": "evt_pd_999",
                    "event_type": "incident.triggered",
                    "data": {
                        "id": "INC-9001",
                        "title": "High Memory Usage on prod-api",
                        "urgency": "high",
                        "status": "triggered",
                        "html_url": "https://acme.pagerduty.com/incidents/INC-9001",
                        "service": {"summary": "prod-api"},
                    },
                }
            }
            resp = await client.post("/webhooks/pagerduty", json=pd_payload)
            assert resp.status_code == 202
            resp_data = resp.json()
            webhook_lg_thread_id = resp_data["thread_id"]

            # Drain background webhook investigation
            await dispatcher.drain_background_tasks()

            # Verify Slack received BOTH:
            # (a) The initial `webhook.received` incident thread creation in `C_INCIDENTS`
            # (b) The follow-up `webhook.completed` RCA report inside that exact Slack thread (`1710000100.0001`)!
            assert len(slack_api_calls) >= 2
            first_post = slack_api_calls[0][1]
            assert first_post["channel"] == "C_INCIDENTS"
            assert "PAGERDUTY" in first_post["text"]
            assert "High Memory Usage on prod-api" in first_post["text"]

            second_post = slack_api_calls[1][1]
            assert second_post["channel"] == "C_INCIDENTS"
            assert second_post.get("thread_ts") == "1710000100.0001"
            assert "OOMKilled on pod" in second_post["text"]

            # 5. Now an engineer replies in that Slack thread (`C_INCIDENTS:1710000100.0001`)!
            # Verify `ChannelHub` resolves the bound `webhook_lg_thread_id` and continues the exact same thread!
            slack_reply_event = {
                "type": "event_callback",
                "event_id": "Ev_FOLLOWUP_1",
                "event": {
                    "type": "app_mention",
                    "user": "U_SRE_ONCALL",
                    "channel": "C_INCIDENTS",
                    "thread_ts": "1710000100.0001",
                    "ts": "1710000200.0001",
                    "text": "<@U_BOT> please scale prod-api to 6 replicas",
                },
            }
            ch_resp = await client.post("/channels/slack/events", json=slack_reply_event)
            assert ch_resp.status_code == 200
            await asyncio.sleep(0.05)

            assert len(gateway_runs) == 1
            assert gateway_runs[0]["thread_id"] == webhook_lg_thread_id
    finally:
        await manager.shutdown()

    set_webhook_dispatcher(None)
    await slack_http.aclose()

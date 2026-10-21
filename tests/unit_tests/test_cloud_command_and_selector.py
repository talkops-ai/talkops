"""Unit tests for multi-cloud /cloud command, provider aliases, agent cloud tools, and CloudProfileSelectorScreen."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from talkops.commands._base import CommandContext
from talkops.commands.core.cloud import CloudHandler
from talkops.config.cloud_discovery import CloudTargetContext
from talkops.config.cloud_profiles import (
    get_active_cloud_provider,
    get_active_cloud_target,
    get_cloud_execution_mode,
    invalidate_cloud_profile_cache,
     set_active_cloud_target,
    set_cloud_execution_mode,
)
from talkops.tools.cloud_tools import (
    cloud_get_status,
    cloud_list_contexts,
    cloud_switch_context,
)


@pytest.fixture(autouse=True)
def _isolate_cloud_state(monkeypatch, tmp_path: Path):
    import talkops.config.cloud_profiles as cp_mod

    test_config = tmp_path / "config.toml"
    monkeypatch.setattr("talkops.config.paths.CONFIG_PATH", test_config)
    monkeypatch.setattr("talkops.config.toml_config.CONFIG_PATH", test_config)
    monkeypatch.setattr(cp_mod, "_runtime_active_provider", None)
    monkeypatch.setattr(cp_mod, "_runtime_active_targets", {})
    monkeypatch.setenv("TALKOPS_CLOUD_PROVIDER", "aws")
    set_cloud_execution_mode("elevated", persist=False)
    invalidate_cloud_profile_cache()
    yield
    monkeypatch.setattr(cp_mod, "_runtime_active_provider", None)
    monkeypatch.setattr(cp_mod, "_runtime_active_targets", {})
    invalidate_cloud_profile_cache()


@pytest.mark.asyncio
async def test_cloud_handler_aliases_and_use_subcommand():
    handler = CloudHandler()
    for alias in ("/aws", "/azure", "/gcp", "/k8s"):
        assert alias in handler.aliases

    # Switch to GCP via `/cloud use gcp analytics-prod --region us-central1`
    ctx = CommandContext(
        app=None,
        raw_command="/cloud use gcp analytics-prod --region us-central1",
        args="use gcp analytics-prod --region us-central1",
    )
    res = await handler.execute(ctx)
    assert res.success is True
    assert res.message is not None
    assert "GCP" in res.message
    assert "analytics-prod" in res.message

    prov, target_id, region = get_active_cloud_target()
    assert prov == "gcp"
    assert target_id == "analytics-prod"
    assert region == "us-central1"


@pytest.mark.asyncio
async def test_cloud_handler_provider_alias_shortcut():
    handler = CloudHandler()

    # `/k8s eks-prod-cluster --region kube-system`
    ctx = CommandContext(
        app=None,
        raw_command="/k8s eks-prod-cluster --region kube-system",
        args="eks-prod-cluster --region kube-system",
    )
    res = await handler.execute(ctx)
    assert res.success is True
    assert get_active_cloud_provider() == "k8s"
    prov, target_id, region = get_active_cloud_target()
    assert prov == "k8s"
    assert target_id == "eks-prod-cluster"
    assert region == "kube-system"


@pytest.mark.asyncio
async def test_cloud_handler_mode_and_auth_subcommands(monkeypatch):
    from talkops.config.credential_broker import CloudIdentityStatus, get_credential_broker

    handler = CloudHandler()

    # Switch to read-only mode
    ctx_ro = CommandContext(
        app=None,
        raw_command="/cloud mode read-only",
        args="mode read-only",
    )
    res_ro = await handler.execute(ctx_ro)
    assert res_ro.success is True
    assert get_cloud_execution_mode() == "read_only"

    # Switch back to elevated mode
    ctx_rw = CommandContext(
        app=None,
        raw_command="/cloud mode elevated",
        args="mode elevated",
    )
    res_rw = await handler.execute(ctx_rw)
    assert res_rw.success is True
    assert get_cloud_execution_mode() == "elevated"

    # Inspect `/cloud auth` with mocked live verification
    monkeypatch.setattr(
        get_credential_broker(),
        "verify_cloud_identity",
        lambda **kwargs: CloudIdentityStatus(
            provider="aws",
            target_id="@ambient",
            display_name="EKS IRSA (talkops-role)",
            is_authenticated=True,
            identity_principal="arn:aws:sts::123456789012:assumed-role/talkops-role/session",
            account_or_tenant_id="123456789012",
            region_or_location="us-east-1",
            auth_source="ambient_service_role",
            execution_mode="elevated",
        ),
    )
    ctx_auth = CommandContext(
        app=None,
        raw_command="/cloud auth",
        args="auth",
    )
    res_auth = await handler.execute(ctx_auth)
    assert res_auth.success is True
    assert res_auth.message is not None
    assert "Authentication" in res_auth.message
    assert "arn:aws:sts::123456789012:assumed-role/talkops-role/session" in res_auth.message


def test_agent_cloud_tools_list_switch_and_status():
    # 1. Switch to Azure subscription via tool
    switch_raw = cloud_switch_context.invoke(
        {
            "provider": "azure",
            "target_id": "sub-prod-001",
            "region": "eastus",
        }
    )
    switch_data = json.loads(switch_raw)
    assert switch_data["status"] == "ok"
    assert switch_data["provider"] == "azure"
    assert switch_data["target_id"] == "sub-prod-001"
    assert switch_data["region_or_location"] == "eastus"
    assert "AZURE_SUBSCRIPTION_ID" in switch_data["synchronized_env_keys"]

    # 2. Check status tool
    status_raw = cloud_get_status.invoke({"verify_live": False})
    status_data = json.loads(status_raw)
    assert status_data["provider"] == "azure"
    assert status_data["target_id"] == "sub-prod-001"
    assert status_data["region_or_location"] == "eastus"

    # 3. List contexts tool
    list_raw = cloud_list_contexts.invoke({})
    list_data = json.loads(list_raw)
    assert list_data["active_context"]["provider"] == "azure"
    assert list_data["active_context"]["target_id"] == "sub-prod-001"
    assert isinstance(list_data["targets"], list)


@pytest.mark.asyncio
async def test_cloud_selector_multicloud_tabs_and_local_profiles():
    from talkops.ui.app import TalkOpsApp
    from talkops.ui.widgets.cloud_selector import (
        CloudProfileOption,
        CloudProfileSelectorScreen,
    )
    from talkops.ui.widgets.modal_base import PillTabs

    app = TalkOpsApp()
    async with app.run_test(headless=True) as pilot:
        screen = CloudProfileSelectorScreen(current_profile="default", current_provider="aws")
        screen._load_profile_data = lambda: [
            CloudTargetContext(
                provider="aws",
                target_id="@ambient",
                display_name="EKS IRSA (talkops-role)",
                account_or_project="123456789012",
                region="us-east-1",
                auth_source="ambient_service_role",
                auth_method="irsa",
            ),
            CloudTargetContext(
                provider="aws",
                target_id="prod-aws",
                display_name="prod-aws",
                account_or_project="123456789012",
                region="us-east-1",
            ),
            CloudTargetContext(
                provider="azure",
                target_id="sub-azure-1",
                display_name="Azure-Core (sub-azur…)",
                account_or_project="tenant-1",
                region="eastus",
            ),
            CloudTargetContext(
                provider="gcp",
                target_id="default",
                display_name="default (otel-demo-499213)",
                account_or_project="otel-demo-499213",
                region=None,
            ),
            CloudTargetContext(
                provider="k8s",
                target_id="eks-prod",
                display_name="eks-prod",
                account_or_project="eks-cluster",
                region="default",
            ),
        ]
        app.push_screen(screen)
        await pilot.pause()

        # Verify PillTabs mounted with the 5 cloud tabs: all, aws, azure, gcp, k8s
        pill_tabs = screen.query_one("#cloud-tabs", PillTabs)
        assert [t.tab_id for t in pill_tabs.tabs] == [
            "all",
            "aws",
            "azure",
            "gcp",
            "k8s",
        ]
        assert pill_tabs.active_tab == "all"
        assert len(screen._option_widgets) == 5
        assert screen._provider_filter is None

        # Cycle tab: All -> aws
        screen.action_cycle_provider()
        await pilot.pause()
        assert screen._active_tab == "aws"
        assert screen._provider_filter == "aws"
        assert all(w.provider == "aws" for w in screen._option_widgets)
        prof_rows = [w for w in screen._option_widgets if isinstance(w, CloudProfileOption)]
        assert len(prof_rows) == 2

        # Next tab: Azure (shows discovered Azure subscription only, no inline setting keys)
        screen.action_cycle_provider()
        await pilot.pause()
        assert screen._active_tab == "azure"
        assert screen._provider_filter == "azure"
        az_profs = [w for w in screen._option_widgets if isinstance(w, CloudProfileOption)]
        assert len(az_profs) == 1
        assert az_profs[0].profile_name == "sub-azure-1"

        # Next tab: GCP (verify clean label without duplicate 'default — default (project)')
        screen.action_cycle_provider()
        await pilot.pause()
        assert screen._active_tab == "gcp"
        gcp_profs = [w for w in screen._option_widgets if isinstance(w, CloudProfileOption)]
        assert len(gcp_profs) == 1
        label_str = str(gcp_profs[0].render_label_text())
        assert "project: otel-demo-499213" in label_str
        assert "[GCP]" not in label_str



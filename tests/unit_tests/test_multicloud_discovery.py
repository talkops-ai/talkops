"""Unit tests for multi-cloud discovery, ambient cloud service role detection, and AWS ambient session safety."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from talkops.config.cloud_discovery import (
    AmbientCloudDetector,
    CloudTargetContext,
    LocalCloudDiscoveryService,
    clear_ambient_probe_cache,
)
from talkops.config.cloud_profiles import (
    get_active_cloud_provider,
    get_active_cloud_target,
    has_local_aws_profile_in_files,
    invalidate_cloud_profile_cache,
    set_active_cloud_target,
)


@pytest.fixture(autouse=True)
def _clean_discovery_caches(monkeypatch):
    clear_ambient_probe_cache()
    invalidate_cloud_profile_cache()
    yield
    clear_ambient_probe_cache()
    invalidate_cloud_profile_cache()


# ── Ambient Cloud Service Role Detection Tests ──────────────────────────────


def test_detect_aws_eks_irsa():
    env = {
        "AWS_WEB_IDENTITY_TOKEN_FILE": "/var/run/secrets/eks.amazonaws.com/serviceaccount/token",
        "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/talkops-eks-agent-role",
        "AWS_REGION": "eu-west-1",
    }
    ctx = AmbientCloudDetector.detect_aws(env, allow_imds_probe=False)
    assert ctx is not None
    assert ctx.provider == "aws"
    assert ctx.target_id == "@ambient"
    assert ctx.is_ambient is True
    assert ctx.auth_source == "ambient_service_role"
    assert ctx.auth_method == "irsa"
    assert ctx.account_or_project == "123456789012"
    assert ctx.region == "eu-west-1"
    assert "talkops-eks-agent-role" in ctx.display_name


def test_detect_aws_ecs_task_role_and_pod_identity():
    ecs_env = {
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/uuid-1234",
        "AWS_DEFAULT_REGION": "us-west-2",
    }
    ecs_ctx = AmbientCloudDetector.detect_aws(ecs_env, allow_imds_probe=False)
    assert ecs_ctx is not None
    assert ecs_ctx.auth_method == "ecs_task_role"
    assert ecs_ctx.region == "us-west-2"

    pod_id_env = {
        "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.23/v1/credentials",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE": "/var/run/secrets/pods.eks.amazonaws.com/serviceaccount/eks-pod-identity-token",
        "AWS_REGION": "ap-south-1",
    }
    pod_ctx = AmbientCloudDetector.detect_aws(pod_id_env, allow_imds_probe=False)
    assert pod_ctx is not None
    assert pod_ctx.auth_method == "eks_pod_identity"
    assert pod_ctx.region == "ap-south-1"


def test_detect_aws_ec2_imdsv2_probe(monkeypatch):
    monkeypatch.setattr("talkops.config.cloud_discovery._read_dmi_hint", lambda: "amazon ec2")

    def fake_put(url: str, headers=None, timeout=0.15):
        assert "latest/api/token" in url
        return "imdsv2-token-xyz"

    def fake_get(url: str, headers=None, timeout=0.15):
        assert headers and headers.get("X-aws-ec2-metadata-token") == "imdsv2-token-xyz"
        if url.endswith("/security-credentials/"):
            return "talkops-ec2-prod-role\n"
        if url.endswith("/instance-identity/document"):
            return json.dumps({"accountId": "999888777666", "region": "us-east-2"})
        return None

    monkeypatch.setattr("talkops.config.cloud_discovery._http_put_metadata", fake_put)
    monkeypatch.setattr("talkops.config.cloud_discovery._http_get_metadata", fake_get)

    ctx = AmbientCloudDetector.detect_aws({}, allow_imds_probe=True)
    assert ctx is not None
    assert ctx.target_id == "@ambient"
    assert ctx.auth_method == "imds_role"
    assert ctx.account_or_project == "999888777666"
    assert ctx.region == "us-east-2"
    assert "talkops-ec2-prod-role" in ctx.display_name


def test_detect_azure_aks_workload_identity_and_msi():
    aks_env = {
        "AZURE_FEDERATED_TOKEN_FILE": "/var/run/secrets/azure/tokens/azure-identity-token",
        "AZURE_CLIENT_ID": "11112222-3333-4444-5555-666677778888",
        "AZURE_TENANT_ID": "aaaa-bbbb-cccc-dddd",
        "AZURE_SUBSCRIPTION_ID": "sub-aks-001",
    }
    ctx = AmbientCloudDetector.detect_azure(aks_env, allow_imds_probe=False)
    assert ctx is not None
    assert ctx.provider == "azure"
    assert ctx.target_id == "sub-aks-001"
    assert ctx.is_ambient is True
    assert ctx.auth_method == "aks_workload_identity"
    assert ctx.account_or_project == "aaaa-bbbb-cccc-dddd"

    msi_env = {
        "IDENTITY_ENDPOINT": "http://169.254.169.254/metadata/identity/oauth2/token",
        "IDENTITY_HEADER": "secret-header",
    }
    msi_ctx = AmbientCloudDetector.detect_azure(msi_env, allow_imds_probe=False)
    assert msi_ctx is not None
    assert msi_ctx.target_id == "@ambient"
    assert msi_ctx.auth_method == "managed_identity"


def test_detect_gcp_cloud_run_and_metadata_server(monkeypatch):
    cr_env = {
        "K_SERVICE": "talkops-worker",
        "GOOGLE_CLOUD_PROJECT": "talkops-gcp-prod",
        "GOOGLE_CLOUD_REGION": "us-central1",
    }
    cr_ctx = AmbientCloudDetector.detect_gcp(cr_env, allow_metadata_probe=False)
    assert cr_ctx is not None
    assert cr_ctx.provider == "gcp"
    assert cr_ctx.target_id == "@ambient"
    assert cr_ctx.auth_method == "cloud_run_sa"
    assert cr_ctx.account_or_project == "talkops-gcp-prod"
    assert cr_ctx.region == "us-central1"

    # Metadata server probe (GCE / GKE Workload Identity)
    monkeypatch.setattr("talkops.config.cloud_discovery._read_dmi_hint", lambda: "google compute engine")

    def fake_gcp_get(url: str, headers=None, timeout=0.15):
        assert headers and headers.get("Metadata-Flavor") == "Google"
        if url.endswith("/project/project-id"):
            return "gke-prod-project-123"
        if url.endswith("/service-accounts/default/email"):
            return "talkops-sa@gke-prod-project-123.iam.gserviceaccount.com"
        if url.endswith("/instance/zone"):
            return "projects/12345/zones/europe-west1-b"
        return None

    monkeypatch.setattr("talkops.config.cloud_discovery._http_get_metadata", fake_gcp_get)
    gce_ctx = AmbientCloudDetector.detect_gcp({}, allow_metadata_probe=True)
    assert gce_ctx is not None
    assert gce_ctx.auth_method == "gce_metadata_sa"
    assert gce_ctx.account_or_project == "gke-prod-project-123"
    assert gce_ctx.region == "europe-west1"


def test_detect_k8s_in_cluster_service_account(tmp_path: Path):
    sa_dir = tmp_path / "serviceaccount"
    sa_dir.mkdir(parents=True)
    (sa_dir / "token").write_text("eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...", encoding="utf-8")
    (sa_dir / "namespace").write_text("talkops-system\n", encoding="utf-8")

    env = {"KUBERNETES_SERVICE_HOST": "10.96.0.1", "KUBERNETES_SERVICE_PORT": "443"}
    ctx = AmbientCloudDetector.detect_k8s(env, sa_dir=sa_dir)
    assert ctx is not None
    assert ctx.provider == "k8s"
    assert ctx.target_id == "@in-cluster"
    assert ctx.is_ambient is True
    assert ctx.auth_method == "in_cluster_sa"
    assert ctx.region == "talkops-system"
    assert ctx.account_or_project == "10.96.0.1:443"


# ── Zero-Latency Local File Discovery Tests ─────────────────────────────────


def test_discover_azure_with_utf8_bom(tmp_path: Path):
    """Verify ~/.azure/azureProfile.json with UTF-8 BOM (\\ufeff) is parsed cleanly."""
    az_profile = tmp_path / "azureProfile.json"
    az_config = tmp_path / "config"

    payload = {
        "installationId": "inst-123",
        "subscriptions": [
            {
                "id": "sub-1111-2222",
                "name": "Production-EA",
                "state": "Enabled",
                "tenantId": "tenant-aaaa-bbbb",
                "isDefault": True,
                "environmentName": "AzureCloud",
                "user": {"name": "sre@company.io", "type": "user"},
            },
            {
                "id": "sub-disabled-9999",
                "name": "Old-Disabled-Sub",
                "state": "Disabled",
                "tenantId": "tenant-aaaa-bbbb",
                "isDefault": False,
            },
        ],
    }
    # Write with explicit UTF-8 BOM (\ufeff)
    az_profile.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8"))
    az_config.write_text("[defaults]\nlocation = eastus2\ngroup = rg-prod-core\n", encoding="utf-8")

    targets = LocalCloudDiscoveryService.discover_azure(
        profile_path=az_profile,
        config_path=az_config,
        include_ambient=False,
        allow_imds_probe=False,
    )
    assert len(targets) == 1
    t = targets[0]
    assert t.provider == "azure"
    assert t.target_id == "sub-1111-2222"
    assert "Production-EA" in t.display_name
    assert t.account_or_project == "tenant-aaaa-bbbb"
    assert t.region == "eastus2"
    assert t.is_default is True
    assert t.extra_metadata["resource_group"] == "rg-prod-core"


def test_discover_gcp_configurations(tmp_path: Path):
    gcloud_dir = tmp_path / "gcloud"
    configs_dir = gcloud_dir / "configurations"
    configs_dir.mkdir(parents=True)

    (gcloud_dir / "active_config").write_text("prod-eu\n", encoding="utf-8")
    (gcloud_dir / "application_default_credentials.json").write_text(
        json.dumps({"type": "authorized_user", "client_id": "cid"}),
        encoding="utf-8",
    )

    (configs_dir / "config_default").write_text(
        "[core]\nproject = dev-sandbox-101\naccount = dev@company.io\n[compute]\nregion = us-central1\n",
        encoding="utf-8",
    )
    (configs_dir / "config_prod-eu").write_text(
        "[core]\nproject = prod-payments-999\naccount = sre@company.io\n[compute]\nzone = europe-west3-a\n",
        encoding="utf-8",
    )

    targets = LocalCloudDiscoveryService.discover_gcp(
        gcloud_dir=gcloud_dir,
        include_ambient=False,
        allow_metadata_probe=False,
    )
    assert len(targets) == 2
    by_id = {t.target_id: t for t in targets}

    assert by_id["default"].account_or_project == "dev-sandbox-101"
    assert by_id["default"].region == "us-central1"
    assert by_id["default"].is_default is False

    assert by_id["prod-eu"].account_or_project == "prod-payments-999"
    assert by_id["prod-eu"].region == "europe-west3"
    assert by_id["prod-eu"].is_default is True
    assert by_id["prod-eu"].auth_method == "gcp_adc"


def test_discover_k8s_kubeconfig(tmp_path: Path):
    kc = tmp_path / "config"
    kc.write_text(
        "apiVersion: v1\n"
        "kind: Config\n"
        "current-context: eks-prod-us-east-1\n"
        "contexts:\n"
        "- name: eks-prod-us-east-1\n"
        "  context:\n"
        "    cluster: arn:aws:eks:us-east-1:123456789012:cluster/prod\n"
        "    namespace: payments\n"
        "    user: eks-admin\n"
        "- name: gke-staging\n"
        "  context:\n"
        "    cluster: gke_staging_us-central1_main\n"
        "    user: gke-user\n",
        encoding="utf-8",
    )

    targets = LocalCloudDiscoveryService.discover_k8s(kubeconfig_path=kc, include_ambient=False)
    assert len(targets) == 2
    by_id = {t.target_id: t for t in targets}
    assert by_id["eks-prod-us-east-1"].is_default is True
    assert by_id["eks-prod-us-east-1"].region == "payments"
    assert by_id["gke-staging"].is_default is False
    assert by_id["gke-staging"].region == "default"


def test_aws_ambient_role_never_clobbered_by_default_profile(monkeypatch, tmp_path: Path):
    """When deployed on EC2/ECS/EKS without ~/.aws/config, get_boto3_session() must NOT pass profile_name='default'."""
    from talkops.config.aws import get_boto3_session
    from talkops.config.settings import get_settings

    monkeypatch.setattr("talkops.config.cloud_profiles.Path.home", lambda: tmp_path)
    monkeypatch.setattr("talkops.config.cloud_profiles.load_aws_profile", lambda: None)
    monkeypatch.setattr(get_settings(), "aws_profile", "default")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/eks.amazonaws.com/serviceaccount/token")
    monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/eks-irsa-role")
    monkeypatch.setenv("AWS_REGION", "us-west-2")

    assert has_local_aws_profile_in_files("default") is False

    # Tuple unpacking and attribute access on get_active_cloud_target()
    prov, target_id, region = get_active_cloud_target("aws")
    assert prov == "aws"
    assert target_id == "@ambient"
    assert region == "us-west-2"

    with patch("boto3.Session") as mock_boto_session:
        get_boto3_session()
        assert mock_boto_session.called
        call_kwargs = mock_boto_session.call_args.kwargs
        # Crucial: profile_name must NOT be passed so botocore uses IRSA / IMDS / ECS Task Role!
        assert "profile_name" not in call_kwargs
        assert call_kwargs.get("region_name") == "us-west-2"


def test_auto_detect_primary_provider_on_gcp_or_azure_compute(monkeypatch, tmp_path: Path):
    """When running on Cloud Run or Azure MSI without ~/.aws/config, get_active_cloud_provider() auto-selects that cloud."""
    monkeypatch.setattr("talkops.config.cloud_profiles.Path.home", lambda: tmp_path)
    monkeypatch.setattr("talkops.config.cloud_profiles.load_active_cloud_context", lambda: None)
    for k in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "TALKOPS_CLOUD_PROVIDER",
        "TALKOPS_SERVER_CLOUD_PROVIDER",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("K_SERVICE", "talkops-cloud-run-agent")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "gcp-native-project")

    import talkops.config.cloud_profiles as cp_mod
    monkeypatch.setattr(cp_mod, "_runtime_active_provider", None)

    assert get_active_cloud_provider() == "gcp"

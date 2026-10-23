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

"""Unit tests for Tri-Mode Credential Broker, OIDC Provider, and Process-Scoped Environment Isolation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from talkops.config.credential_broker import (
    DualModeCredentialBroker,
    detect_runtime_auth_mode,
)
from talkops.server.oidc_provider import OIDCProvider


def test_detect_runtime_auth_mode(monkeypatch):
    """Verify detect runtime auth mode."""
    monkeypatch.delenv("TALKOPS_DEPLOYMENT_MODE", raising=False)
    monkeypatch.delenv("TALKOPS_SERVER_MODE", raising=False)
    assert detect_runtime_auth_mode() == "local"

    monkeypatch.setenv("TALKOPS_DEPLOYMENT_MODE", "central_server")
    assert detect_runtime_auth_mode() == "central_server"


def test_broker_process_env_isolation_across_providers(monkeypatch, tmp_path: Path):
    """Verify get_process_env() injects provider-specific env vars without mutating disk or clobbering ambient roles."""
    monkeypatch.setattr("talkops.config.cloud_profiles.Path.home", lambda: tmp_path)
    broker = DualModeCredentialBroker(runtime_mode="local")

    # 1. AWS Ambient role: must NOT inject AWS_PROFILE="default" when ~/.aws/config does not exist
    aws_amb_env = broker.get_process_env(
        provider="aws",
        target_id="@ambient",
        region="eu-central-1",
        base_env={"AWS_PROFILE": "default", "AWS_WEB_IDENTITY_TOKEN_FILE": "/token"},
    )
    assert "AWS_PROFILE" not in aws_amb_env
    assert aws_amb_env["AWS_REGION"] == "eu-central-1"
    assert aws_amb_env["AWS_WEB_IDENTITY_TOKEN_FILE"] == "/token"
    assert aws_amb_env["TALKOPS_CLOUD_PROVIDER"] == "aws"
    assert aws_amb_env["TALKOPS_CLOUD_TARGET"] == "@ambient"

    # 2. AWS Named profile: injects AWS_PROFILE and strips conflicting static keys
    aws_named_env = broker.get_process_env(
        provider="aws",
        target_id="prod-account",
        region="us-east-1",
        base_env={"AWS_ACCESS_KEY_ID": "AKIAOLD", "AWS_SECRET_ACCESS_KEY": "SECRETOLD"},
    )
    assert aws_named_env["AWS_PROFILE"] == "prod-account"
    assert aws_named_env["AWS_DEFAULT_PROFILE"] == "prod-account"
    assert "AWS_ACCESS_KEY_ID" not in aws_named_env
    assert "AWS_SECRET_ACCESS_KEY" not in aws_named_env

    # 3. Azure Subscription: injects AZURE_SUBSCRIPTION_ID and ARM_SUBSCRIPTION_ID
    az_env = broker.get_process_env(
        provider="azure",
        target_id="sub-9876-5432",
        region="westus2",
        execution_mode="read_only",
    )
    assert az_env["TALKOPS_CLOUD_PROVIDER"] == "azure"
    assert az_env["AZURE_SUBSCRIPTION_ID"] == "sub-9876-5432"
    assert az_env["ARM_SUBSCRIPTION_ID"] == "sub-9876-5432"
    assert az_env["AZURE_LOCATION"] == "westus2"
    assert az_env["TALKOPS_CLOUD_EXECUTION_MODE"] == "read_only"

    # 4. GCP Configuration: injects CLOUDSDK_ACTIVE_CONFIG_NAME and region
    gcp_env = broker.get_process_env(
        provider="gcp",
        target_id="analytics-prod",
        region="asia-east1",
    )
    assert gcp_env["TALKOPS_CLOUD_PROVIDER"] == "gcp"
    assert gcp_env["CLOUDSDK_ACTIVE_CONFIG_NAME"] == "analytics-prod"
    assert gcp_env["CLOUDSDK_COMPUTE_REGION"] == "asia-east1"

    # 5. Kubernetes Context: injects KUBE_CONTEXT, KUBECONFIG_CONTEXT, HELM_KUBECONTEXT, and KUBE_NAMESPACE
    k8s_env = broker.get_process_env(
        provider="k8s",
        target_id="gke-prod-cluster",
        region="monitoring",
    )
    assert k8s_env["TALKOPS_CLOUD_PROVIDER"] == "k8s"
    assert k8s_env["KUBE_CONTEXT"] == "gke-prod-cluster"
    assert k8s_env["KUBECONFIG_CONTEXT"] == "gke-prod-cluster"
    assert k8s_env["HELM_KUBECONTEXT"] == "gke-prod-cluster"
    assert k8s_env["KUBE_NAMESPACE"] == "monitoring"


def test_broker_central_server_aws_sts_assume_role(monkeypatch):
    """Verify Central Server mode assumes customer cross-account IAM Role with ExternalId."""
    monkeypatch.setenv("TALKOPS_AWS_READONLY_ROLE_ARN", "arn:aws:iam::555566667777:role/TalkOpsReadOnlyRole")
    monkeypatch.setenv("TALKOPS_AWS_EXTERNAL_ID", "tenant-ext-id-999")

    broker = DualModeCredentialBroker(runtime_mode="central_server")

    mock_sts = MagicMock()
    mock_sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "ASIAFEDERATEDKEY",
            "SecretAccessKey": "FederatedSecret",
            "SessionToken": "FederatedToken",
        }
    }
    mock_base_session = MagicMock()
    mock_base_session.client.return_value = mock_sts

    with (
        patch.object(broker, "_create_base_boto3_session", return_value=mock_base_session),
        patch("boto3.Session") as mock_assumed_session,
    ):
        broker.get_aws_session(target_id="@ambient", region="us-east-1", execution_mode="read_only")
        mock_sts.assume_role.assert_called_once()
        assume_kwargs = mock_sts.assume_role.call_args.kwargs
        assert assume_kwargs["RoleArn"] == "arn:aws:iam::555566667777:role/TalkOpsReadOnlyRole"
        assert assume_kwargs["ExternalId"] == "tenant-ext-id-999"
        mock_assumed_session.assert_called_once_with(
            aws_access_key_id="ASIAFEDERATEDKEY",
            aws_secret_access_key="FederatedSecret",
            aws_session_token="FederatedToken",
            region_name="us-east-1",
        )


def test_oidc_provider_discovery_and_jwks_endpoints():
    """Verify OIDCProvider mints valid JWTs and webhooks_app serves /.well-known/openid-configuration and jwks.json."""
    provider = OIDCProvider(issuer_url="https://auth.talkops.example.com")
    disco = provider.get_openid_configuration()
    assert disco["issuer"] == "https://auth.talkops.example.com"
    assert disco["jwks_uri"] == "https://auth.talkops.example.com/.well-known/jwks.json"

    jwks = provider.get_jwks()
    assert "keys" in jwks
    assert len(jwks["keys"]) == 1
    assert jwks["keys"][0]["kid"] == provider.kid

    token = provider.mint_workload_jwt(
        audience="sts.amazonaws.com",
        subject="talkops:agent",
        extra_claims={"execution_mode": "read_only"},
    )
    parts = token.split(".")
    assert len(parts) == 3

    # Test HTTP endpoints on webhooks_app
    from talkops.server.webhooks_app import app

    client = TestClient(app)
    resp_disco = client.get("/.well-known/openid-configuration")
    assert resp_disco.status_code == 200
    assert "jwks_uri" in resp_disco.json()

    resp_jwks = client.get("/.well-known/jwks.json")
    assert resp_jwks.status_code == 200
    assert "keys" in resp_jwks.json()

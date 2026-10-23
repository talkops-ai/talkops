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

"""Tri-Mode Cloud Credential Broker for TalkOps.

Supports three unified authentication paradigms across AWS, Microsoft Azure,
Google Cloud Platform (GCP), and Kubernetes (K8s):

1. **Ambient Cloud Service Role (`@ambient` / `@in-cluster`)**:
   Zero-config workload identity when TalkOps runs on cloud compute
   (AWS EC2 IMDSv2, ECS Task Role, EKS IRSA / Pod Identity, Lambda;
   GCP GCE / Cloud Run / GKE Workload Identity Metadata Server;
   Azure VM IMDS / App Service / Container Apps / AKS Workload Identity;
   Kubernetes In-Cluster ServiceAccount).
2. **Local Workstation Profile (`local_profile`)**:
   Reads developer CLI configurations (`~/.aws`, `~/.azure`, `~/.config/gcloud`,
   `~/.kube/config`) and injects process-scoped environment variables into tool
   subprocesses without mutating global CLI state on disk.
3. **Central Server Cross-Account Federation (`federated_role`)**:
   Exchanges short-lived OIDC / STS tokens (AWS STS `AssumeRole` with `ExternalId`,
   GCP Workload Identity Federation + Service Account Impersonation, Azure
   Multi-Tenant Entra ID Federation) with tiered privileges (`read_only` vs `elevated`).
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from talkops.config.cloud_discovery import (
    AmbientCloudDetector,
    CloudExecutionMode,
    CloudProviderType,
    CloudTargetContext,
    LocalCloudDiscoveryService,
)
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

RuntimeAuthMode = Literal["local", "central_server"]


@dataclass
class CloudIdentityStatus:
    """Unified cloud authentication & live identity verification status."""

    provider: CloudProviderType
    target_id: str
    display_name: str = ""
    is_authenticated: bool = False
    identity_principal: str | None = None
    account_or_tenant_id: str | None = None
    region_or_location: str | None = None
    auth_source: str = "unknown"
    execution_mode: CloudExecutionMode = "read_only"
    remediation_hint: str | None = None
    error: str | None = None
    expires_at: str | None = None


def detect_runtime_auth_mode() -> RuntimeAuthMode:
    """Detect whether TalkOps is running in local workstation or central server mode."""
    mode = (
        os.environ.get("TALKOPS_DEPLOYMENT_MODE")
        or os.environ.get("TALKOPS_SERVER_MODE")
        or ""
    ).strip().lower()
    if mode in ("central", "central_server", "server", "enterprise", "true", "1"):
        return "central_server"
    return "local"


class DualModeCredentialBroker:
    """Tri-Mode (Ambient + Local Workstation + Central Server) Cloud Credential Broker."""

    def __init__(self, runtime_mode: RuntimeAuthMode | None = None) -> None:
        """Initialize DualModeCredentialBroker."""
        self.runtime_mode: RuntimeAuthMode = runtime_mode or detect_runtime_auth_mode()

    # ── Target Resolution Helpers ──────────────────────────────────────────

    @staticmethod
    def _resolve_target_context(
        provider: CloudProviderType,
        target_id: str | None = None,
    ) -> CloudTargetContext | None:
        """Look up discovered CloudTargetContext for a provider and target_id."""
        from talkops.config.cloud_profiles import (
            get_active_cloud_target,
            get_cloud_target_info,
        )

        effective_id = target_id
        if not effective_id:
            active_prov, active_id, _ = get_active_cloud_target()
            if active_prov == provider:
                effective_id = active_id

        if effective_id:
            info = get_cloud_target_info(provider, effective_id)
            if info is not None:
                return info

        # Fallback: check ambient detector directly
        if effective_id in ("@ambient", "@in-cluster", None):
            if provider == "aws":
                return AmbientCloudDetector.detect_aws()
            if provider == "azure":
                return AmbientCloudDetector.detect_azure()
            if provider == "gcp":
                return AmbientCloudDetector.detect_gcp()
            if provider == "k8s":
                return AmbientCloudDetector.detect_k8s()
        return None

    # ── AWS Session & STS Federation ───────────────────────────────────────

    def get_aws_session(
        self,
        *,
        target_id: str | None = None,
        region: str | None = None,
        execution_mode: CloudExecutionMode | None = None,
        role_arn: str | None = None,
        external_id: str | None = None,
    ) -> Any:
        """Create a boto3.Session supporting Ambient IAM Roles, Local Profiles, and STS AssumeRole."""
        import boto3

        from talkops.config.cloud_profiles import (
            get_active_aws_profile,
            get_active_aws_region,
            get_cloud_execution_mode,
            has_local_aws_profile_in_files,
        )
        from talkops.config.settings import sync_aws_env_aliases

        sync_aws_env_aliases()

        effective_target = target_id or get_active_aws_profile()
        effective_region = region or get_active_aws_region() or "us-east-1"
        effective_mode = execution_mode or get_cloud_execution_mode()

        # Determine if Central Server STS AssumeRole is requested or configured
        configured_role_arn = role_arn
        if not configured_role_arn and self.runtime_mode == "central_server":
            if effective_mode == "elevated":
                configured_role_arn = (
                    os.environ.get("TALKOPS_AWS_DIRECTED_ACTION_ROLE_ARN")
                    or os.environ.get("TALKOPS_AWS_ELEVATED_ROLE_ARN")
                    or os.environ.get("TALKOPS_AWS_ASSUME_ROLE_ARN")
                )
            else:
                configured_role_arn = (
                    os.environ.get("TALKOPS_AWS_READONLY_ROLE_ARN")
                    or os.environ.get("TALKOPS_AWS_ASSUME_ROLE_ARN")
                )

        # Build base session (either ambient or local profile)
        base_session = self._create_base_boto3_session(
            target_id=effective_target,
            region=effective_region,
            has_local_profile_fn=has_local_aws_profile_in_files,
        )

        if configured_role_arn:
            eff_external_id = external_id or os.environ.get("TALKOPS_AWS_EXTERNAL_ID")
            sts_client = base_session.client("sts", region_name=effective_region)
            assume_kwargs: dict[str, Any] = {
                "RoleArn": configured_role_arn,
                "RoleSessionName": f"talkops-{effective_mode}-session",
                "DurationSeconds": int(os.environ.get("TALKOPS_AWS_SESSION_DURATION", "3600")),
            }
            if eff_external_id:
                assume_kwargs["ExternalId"] = eff_external_id
            response = sts_client.assume_role(**assume_kwargs)
            creds = response["Credentials"]
            return boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name=effective_region,
            )

        return base_session

    @staticmethod
    def _create_base_boto3_session(
        *,
        target_id: str | None,
        region: str,
        has_local_profile_fn: Any,
    ) -> Any:
        """Create a base boto3.Session without clobbering ambient service roles."""
        import boto3

        is_ambient = (
            target_id == "@ambient"
            or not target_id
            or (target_id == "default" and not has_local_profile_fn("default"))
        )

        if is_ambient:
            # Ensure stale AWS_PROFILE="default" does not force botocore to look for a missing profile
            saved_prof = os.environ.get("AWS_PROFILE")
            saved_def = os.environ.get("AWS_DEFAULT_PROFILE")
            try:
                if saved_prof == "default" and not has_local_profile_fn("default"):
                    os.environ.pop("AWS_PROFILE", None)
                if saved_def == "default" and not has_local_profile_fn("default"):
                    os.environ.pop("AWS_DEFAULT_PROFILE", None)
                return boto3.Session(region_name=region)
            finally:
                if saved_prof is not None and "AWS_PROFILE" not in os.environ and has_local_profile_fn(saved_prof):
                    os.environ["AWS_PROFILE"] = saved_prof
                if saved_def is not None and "AWS_DEFAULT_PROFILE" not in os.environ and has_local_profile_fn(saved_def):
                    os.environ["AWS_DEFAULT_PROFILE"] = saved_def

        session_kwargs: dict[str, Any] = {"region_name": region, "profile_name": target_id}
        try:
            return boto3.Session(**session_kwargs)
        except Exception:
            if target_id and target_id not in ("default", "@ambient"):
                raise
            return boto3.Session(region_name=region)

    # ── GCP Credentials & Workload Identity Federation ─────────────────────

    def get_gcp_credentials(
        self,
        *,
        target_id: str | None = None,
        project_id: str | None = None,
        service_account_email: str | None = None,
        execution_mode: CloudExecutionMode | None = None,
    ) -> dict[str, Any]:
        """Resolve GCP credentials or exchange OIDC JWT via Workload Identity Federation."""
        from talkops.config.cloud_profiles import get_cloud_execution_mode

        effective_mode = execution_mode or get_cloud_execution_mode()
        ctx = self._resolve_target_context("gcp", target_id)
        eff_project = (
            project_id
            or (ctx.account_or_tenant_id if ctx else None)
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("CLOUDSDK_CORE_PROJECT")
        )

        # Central server WIF token exchange when configured
        wif_audience = os.environ.get("TALKOPS_GCP_WIF_AUDIENCE")
        target_sa = service_account_email or (
            os.environ.get("TALKOPS_GCP_ELEVATED_SA_EMAIL")
            if effective_mode == "elevated"
            else os.environ.get("TALKOPS_GCP_READONLY_SA_EMAIL")
            or os.environ.get("TALKOPS_GCP_SERVICE_ACCOUNT_EMAIL")
        )

        if self.runtime_mode == "central_server" and wif_audience:
            try:
                from talkops.server.oidc_provider import get_oidc_provider

                subject_token = get_oidc_provider().mint_workload_jwt(
                    audience=wif_audience,
                    subject=f"talkops:{effective_mode}",
                    extra_claims={"execution_mode": effective_mode, "project_id": eff_project or ""},
                )
                return {
                    "mode": "workload_identity_federation",
                    "project_id": eff_project,
                    "wif_audience": wif_audience,
                    "service_account_email": target_sa,
                    "subject_token": subject_token,
                }
            except Exception as exc:
                logger.debug("GCP WIF token exchange preparation failed: %s", exc)

        return {
            "mode": "ambient" if (ctx and ctx.is_ambient) or target_id == "@ambient" else "local_profile",
            "target_id": target_id or (ctx.target_id if ctx else "default"),
            "project_id": eff_project,
            "account": ctx.identity_hint if ctx else None,
        }

    # ── Azure Credential & Multi-Tenant Federation ─────────────────────────

    def get_azure_credential(
        self,
        *,
        target_id: str | None = None,
        tenant_id: str | None = None,
        client_id: str | None = None,
        execution_mode: CloudExecutionMode | None = None,
    ) -> dict[str, Any]:
        """Resolve Azure subscription/tenant credential metadata or federated OIDC assertion."""
        from talkops.config.cloud_profiles import get_cloud_execution_mode

        effective_mode = execution_mode or get_cloud_execution_mode()
        ctx = self._resolve_target_context("azure", target_id)
        eff_tenant = tenant_id or os.environ.get("AZURE_TENANT_ID") or os.environ.get("ARM_TENANT_ID")
        if not eff_tenant and ctx and ctx.account_or_tenant_id and ":" in ctx.account_or_tenant_id:
            eff_tenant = ctx.account_or_tenant_id.split(":", 1)[1]

        eff_client_id = client_id or (
            os.environ.get("TALKOPS_AZURE_ELEVATED_CLIENT_ID")
            if effective_mode == "elevated"
            else os.environ.get("TALKOPS_AZURE_READONLY_CLIENT_ID")
            or os.environ.get("AZURE_CLIENT_ID")
        )

        if self.runtime_mode == "central_server" and eff_tenant and eff_client_id:
            try:
                from talkops.server.oidc_provider import get_oidc_provider

                assertion = get_oidc_provider().mint_workload_jwt(
                    audience="api://AzureADTokenExchange",
                    subject=f"talkops:{effective_mode}",
                    extra_claims={"tenant_id": eff_tenant, "execution_mode": effective_mode},
                )
                return {
                    "mode": "federated_client_assertion",
                    "tenant_id": eff_tenant,
                    "client_id": eff_client_id,
                    "subscription_id": target_id or (ctx.target_id if ctx else None),
                    "client_assertion": assertion,
                }
            except Exception as exc:
                logger.debug("Azure OIDC client assertion generation failed: %s", exc)

        return {
            "mode": "ambient" if (ctx and ctx.is_ambient) or target_id == "@ambient" else "local_profile",
            "subscription_id": target_id or (ctx.target_id if ctx else None),
            "tenant_id": eff_tenant,
            "client_id": eff_client_id,
        }

    # ── Process-Scoped Environment Injection ───────────────────────────────

    def get_process_env(
        self,
        *,
        provider: CloudProviderType | None = None,
        target_id: str | None = None,
        region: str | None = None,
        base_env: dict[str, str] | None = None,
        execution_mode: CloudExecutionMode | None = None,
    ) -> dict[str, str]:
        """Compute process-scoped cloud environment variables without mutating disk configs.

        Crucially protects ambient service roles (`@ambient` / `@in-cluster`) by never
        setting `AWS_PROFILE="default"` when no local `[default]` profile exists and
        never stripping ambient STS / Managed Identity / Workload Identity env vars.
        """
        from talkops.config.cloud_profiles import (
            get_active_aws_profile,
            get_active_aws_region,
            get_active_cloud_target,
            get_cloud_execution_mode,
            has_local_aws_profile_in_files,
        )

        env: dict[str, str] = dict(base_env) if base_env is not None else {}

        active_prov, active_target, active_region = get_active_cloud_target()
        eff_provider: CloudProviderType = provider or active_prov
        eff_target = target_id or active_target
        eff_region = region if region is not None else active_region
        eff_mode = execution_mode or get_cloud_execution_mode()

        env["TALKOPS_CLOUD_PROVIDER"] = eff_provider
        env["TALKOPS_CLOUD_TARGET"] = eff_target
        env["TALKOPS_CLOUD_EXECUTION_MODE"] = eff_mode

        # Always synchronize AWS profile safely (since AWS tools may be invoked even when k8s/etc is primary)
        aws_prof = eff_target if eff_provider == "aws" else get_active_aws_profile()
        aws_reg = eff_region if eff_provider == "aws" else get_active_aws_region()

        is_aws_ambient = (
            aws_prof == "@ambient"
            or not aws_prof
            or (aws_prof == "default" and not has_local_aws_profile_in_files("default"))
        )

        if is_aws_ambient:
            # Do NOT set AWS_PROFILE="default" when running under an ambient role without ~/.aws/config
            if env.get("AWS_PROFILE") in ("default", "@ambient"):
                env.pop("AWS_PROFILE", None)
            if env.get("AWS_DEFAULT_PROFILE") in ("default", "@ambient"):
                env.pop("AWS_DEFAULT_PROFILE", None)
        else:
            env["AWS_PROFILE"] = aws_prof
            env["AWS_DEFAULT_PROFILE"] = aws_prof
            # Only strip static env keys when an explicit non-default named profile is active
            if aws_prof != "default":
                env.pop("AWS_ACCESS_KEY_ID", None)
                env.pop("AWS_SECRET_ACCESS_KEY", None)
                env.pop("AWS_SESSION_TOKEN", None)

        if aws_reg:
            env["AWS_REGION"] = aws_reg
            env["AWS_DEFAULT_REGION"] = aws_reg
        elif eff_provider == "aws" and region is None and not active_region:
            # Preserve ambient AWS_REGION if not explicitly cleared
            pass

        # Synchronize Azure environment variables
        if eff_provider == "azure":
            env["AZURE_CORE_NO_COLOR"] = "true"
            if eff_target and eff_target != "@ambient":
                ctx = self._resolve_target_context("azure", eff_target)
                sub_id = ctx.target_id if ctx else eff_target
                env["AZURE_SUBSCRIPTION_ID"] = sub_id
                env["ARM_SUBSCRIPTION_ID"] = sub_id
                if ctx and ctx.account_or_tenant_id and ":" in ctx.account_or_tenant_id:
                    _, tenant_part = ctx.account_or_tenant_id.split(":", 1)
                    if tenant_part:
                        env["AZURE_TENANT_ID"] = tenant_part
                        env["ARM_TENANT_ID"] = tenant_part
            if eff_region:
                env["AZURE_LOCATION"] = eff_region
                env["AZURE_DEFAULTS_LOCATION"] = eff_region

        # Synchronize GCP environment variables
        elif eff_provider == "gcp":
            env["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
            ctx = self._resolve_target_context("gcp", eff_target)
            if eff_target and eff_target != "@ambient":
                env["CLOUDSDK_ACTIVE_CONFIG_NAME"] = eff_target
            project_id = (ctx.account_or_tenant_id if ctx else None) or os.environ.get("GOOGLE_CLOUD_PROJECT")
            if project_id:
                env["CLOUDSDK_CORE_PROJECT"] = project_id
                env["GOOGLE_CLOUD_PROJECT"] = project_id
                env["GCLOUD_PROJECT"] = project_id
            if eff_region:
                env["CLOUDSDK_COMPUTE_REGION"] = eff_region
                env["GOOGLE_CLOUD_REGION"] = eff_region

        # Synchronize Kubernetes environment variables
        elif eff_provider == "k8s":
            if eff_target and eff_target not in ("@in-cluster", "@ambient"):
                env["KUBE_CONTEXT"] = eff_target
                env["KUBECONFIG_CONTEXT"] = eff_target
                env["HELM_KUBECONTEXT"] = eff_target
            if eff_region:
                env["KUBE_NAMESPACE"] = eff_region

        return env

    def get_sandbox_env_vars(
        self,
        *,
        provider: CloudProviderType | None = None,
        target_id: str | None = None,
        region: str | None = None,
        execution_mode: CloudExecutionMode | None = None,
    ) -> dict[str, str]:
        """Return isolated environment dictionary suitable for subprocesses or container sandboxes."""
        return self.get_process_env(
            provider=provider,
            target_id=target_id,
            region=region,
            execution_mode=execution_mode,
        )

    # ── Live Multi-Cloud Identity Verification ─────────────────────────────

    def verify_cloud_identity(
        self,
        *,
        provider: CloudProviderType | None = None,
        target_id: str | None = None,
        region: str | None = None,
    ) -> CloudIdentityStatus:
        """Perform a fast live identity check for the specified cloud provider and target."""
        from talkops.config.cloud_profiles import (
            get_active_cloud_target,
            get_cloud_execution_mode,
        )

        active_prov, active_target, active_region = get_active_cloud_target()
        eff_provider: CloudProviderType = provider or active_prov
        eff_target = target_id or active_target
        eff_region = region or active_region
        eff_mode = get_cloud_execution_mode()

        if eff_provider == "aws":
            return self._verify_aws_identity(eff_target, eff_region, eff_mode)
        if eff_provider == "azure":
            return self._verify_azure_identity(eff_target, eff_region, eff_mode)
        if eff_provider == "gcp":
            return self._verify_gcp_identity(eff_target, eff_region, eff_mode)
        if eff_provider == "k8s":
            return self._verify_k8s_identity(eff_target, eff_region, eff_mode)

        return CloudIdentityStatus(
            provider=eff_provider,
            target_id=eff_target,
            is_authenticated=False,
            execution_mode=eff_mode,
            error=f"Unsupported provider: {eff_provider}",
        )

    def _verify_aws_identity(
        self,
        target_id: str,
        region: str | None,
        execution_mode: CloudExecutionMode,
    ) -> CloudIdentityStatus:
        ctx = self._resolve_target_context("aws", target_id)
        try:
            session = self.get_aws_session(
                target_id=target_id,
                region=region,
                execution_mode=execution_mode,
            )
            sts = session.client("sts")
            identity = sts.get_caller_identity()
            arn = identity.get("Arn")
            account = identity.get("Account")
            eff_reg = region or session.region_name or "us-east-1"
            return CloudIdentityStatus(
                provider="aws",
                target_id=target_id,
                display_name=ctx.display_name if ctx else target_id,
                is_authenticated=True,
                identity_principal=arn,
                account_or_tenant_id=account,
                region_or_location=eff_reg,
                auth_source=ctx.auth_source if ctx else ("ambient_service_role" if target_id == "@ambient" else "local_profile"),
                execution_mode=execution_mode,
            )
        except Exception as exc:
            is_sso = bool(ctx and ctx.is_sso)
            if target_id == "@ambient":
                hint = "Verify attached IAM Role (EC2 Instance Profile, ECS Task Role, or EKS IRSA/Pod Identity)."
            elif is_sso:
                hint = f"Run `aws sso login --profile {target_id}` to refresh your AWS SSO session."
            else:
                hint = f"Run `aws configure --profile {target_id}` or `aws sso login --profile {target_id}`."
            return CloudIdentityStatus(
                provider="aws",
                target_id=target_id,
                display_name=ctx.display_name if ctx else target_id,
                is_authenticated=False,
                account_or_tenant_id=ctx.account_or_tenant_id if ctx else None,
                region_or_location=region or (ctx.region_or_location if ctx else "us-east-1"),
                auth_source=ctx.auth_source if ctx else "local_profile",
                execution_mode=execution_mode,
                remediation_hint=hint,
                error=str(exc),
            )

    def _verify_azure_identity(
        self,
        target_id: str,
        region: str | None,
        execution_mode: CloudExecutionMode,
    ) -> CloudIdentityStatus:
        ctx = self._resolve_target_context("azure", target_id)
        if target_id == "@ambient" or (ctx and ctx.is_ambient):
            ambient = AmbientCloudDetector.detect_azure()
            if ambient is not None:
                return CloudIdentityStatus(
                    provider="azure",
                    target_id="@ambient",
                    display_name=ambient.display_name,
                    is_authenticated=True,
                    identity_principal=ambient.identity_hint or "ManagedIdentity",
                    account_or_tenant_id=ambient.account_or_tenant_id,
                    region_or_location=region or ambient.region_or_location,
                    auth_source="ambient_service_role",
                    execution_mode=execution_mode,
                )

        # Try fast az account show if az CLI is available, else inspect local discovery + MSAL cache
        try:
            cmd = ["az", "account", "show", "-o", "json"]
            if target_id and target_id not in ("default", "@ambient"):
                cmd.extend(["--subscription", target_id])
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout)
                sub_id = str(data.get("id") or target_id)
                tenant_id = str(data.get("tenantId") or "")
                user_name = str((data.get("user") or {}).get("name") or "")
                return CloudIdentityStatus(
                    provider="azure",
                    target_id=sub_id,
                    display_name=str(data.get("name") or (ctx.display_name if ctx else sub_id)),
                    is_authenticated=True,
                    identity_principal=user_name or "AzureCLI",
                    account_or_tenant_id=f"{sub_id}:{tenant_id}" if tenant_id else sub_id,
                    region_or_location=region,
                    auth_source="local_profile",
                    execution_mode=execution_mode,
                )
            err_msg = proc.stderr.strip() or "Azure CLI session not authenticated."
        except FileNotFoundError:
            # If az binary is not installed, check if subscription was discovered with a valid token cache
            if ctx is not None and ctx.has_valid_Hint:
                return CloudIdentityStatus(
                    provider="azure",
                    target_id=ctx.target_id,
                    display_name=ctx.display_name,
                    is_authenticated=True,
                    identity_principal=ctx.identity_hint,
                    account_or_tenant_id=ctx.account_or_tenant_id,
                    region_or_location=region or ctx.region_or_location,
                    auth_source=ctx.auth_source,
                    execution_mode=execution_mode,
                )
            err_msg = "Azure CLI (`az`) is not installed or no active token cache was found."
        except Exception as exc:
            err_msg = str(exc)

        return CloudIdentityStatus(
            provider="azure",
            target_id=target_id,
            display_name=ctx.display_name if ctx else target_id,
            is_authenticated=False,
            account_or_tenant_id=ctx.account_or_tenant_id if ctx else None,
            region_or_location=region,
            auth_source=ctx.auth_source if ctx else "local_profile",
            execution_mode=execution_mode,
            remediation_hint="Run `az login` (or `az login --tenant <tenant-id>`) to authenticate with Microsoft Entra ID.",
            error=err_msg,
        )

    def _verify_gcp_identity(
        self,
        target_id: str,
        region: str | None,
        execution_mode: CloudExecutionMode,
    ) -> CloudIdentityStatus:
        ctx = self._resolve_target_context("gcp", target_id)
        if target_id == "@ambient" or (ctx and ctx.is_ambient):
            ambient = AmbientCloudDetector.detect_gcp()
            if ambient is not None:
                return CloudIdentityStatus(
                    provider="gcp",
                    target_id="@ambient",
                    display_name=ambient.display_name,
                    is_authenticated=True,
                    identity_principal=ambient.identity_hint or "ComputeEngineServiceAccount",
                    account_or_tenant_id=ambient.account_or_tenant_id,
                    region_or_location=region or ambient.region_or_location,
                    auth_source="ambient_service_role",
                    execution_mode=execution_mode,
                )

        # Check ADC or gcloud configuration credentials
        if ctx is not None and ctx.has_valid_Hint:
            return CloudIdentityStatus(
                provider="gcp",
                target_id=ctx.target_id,
                display_name=ctx.display_name,
                is_authenticated=True,
                identity_principal=ctx.identity_hint or "ADC",
                account_or_tenant_id=ctx.account_or_tenant_id,
                region_or_location=region or ctx.region_or_location,
                auth_source=ctx.auth_source,
                execution_mode=execution_mode,
            )

        return CloudIdentityStatus(
            provider="gcp",
            target_id=target_id,
            display_name=ctx.display_name if ctx else target_id,
            is_authenticated=False,
            account_or_tenant_id=ctx.account_or_tenant_id if ctx else None,
            region_or_location=region or (ctx.region_or_location if ctx else None),
            auth_source=ctx.auth_source if ctx else "local_profile",
            execution_mode=execution_mode,
            remediation_hint=(
                f"Run `gcloud auth login --configuration={target_id}` and "
                "`gcloud auth application-default login`."
            ),
            error="No active GCP credentials or Application Default Credentials found.",
        )

    def _verify_k8s_identity(
        self,
        target_id: str,
        region: str | None,
        execution_mode: CloudExecutionMode,
    ) -> CloudIdentityStatus:
        ctx = self._resolve_target_context("k8s", target_id)
        if target_id in ("@in-cluster", "@ambient") or (ctx and ctx.is_ambient):
            ambient = AmbientCloudDetector.detect_k8s()
            if ambient is not None:
                return CloudIdentityStatus(
                    provider="k8s",
                    target_id="@in-cluster",
                    display_name=ambient.display_name,
                    is_authenticated=True,
                    identity_principal=ambient.identity_hint,
                    account_or_tenant_id=ambient.account_or_tenant_id,
                    region_or_location=region or ambient.region_or_location,
                    auth_source="ambient_service_role",
                    execution_mode=execution_mode,
                )

        if ctx is not None and ctx.has_valid_Hint:
            return CloudIdentityStatus(
                provider="k8s",
                target_id=ctx.target_id,
                display_name=ctx.display_name,
                is_authenticated=True,
                identity_principal=ctx.identity_hint,
                account_or_tenant_id=ctx.account_or_tenant_id,
                region_or_location=region or ctx.region_or_location,
                auth_source=ctx.auth_source,
                execution_mode=execution_mode,
            )

        return CloudIdentityStatus(
            provider="k8s",
            target_id=target_id,
            display_name=ctx.display_name if ctx else target_id,
            is_authenticated=False,
            execution_mode=execution_mode,
            remediation_hint=f"Verify context `{target_id}` exists in `~/.kube/config` or `$KUBECONFIG`.",
            error=f"Kubernetes context '{target_id}' was not found in kubeconfig.",
        )


_broker_instance: DualModeCredentialBroker | None = None


def get_credential_broker() -> DualModeCredentialBroker:
    """Return the singleton DualModeCredentialBroker."""
    global _broker_instance
    if _broker_instance is None:
        _broker_instance = DualModeCredentialBroker()
    return _broker_instance


__all__ = [
    "CloudIdentityStatus",
    "DualModeCredentialBroker",
    "RuntimeAuthMode",
    "detect_runtime_auth_mode",
    "get_credential_broker",
]

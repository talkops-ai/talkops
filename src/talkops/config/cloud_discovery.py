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

"""Zero-latency multi-cloud discovery and ambient service role detection for TalkOps.

Discovers cloud contexts across AWS, Microsoft Azure, Google Cloud Platform (GCP),
and Kubernetes from three sources without spawning CLI subprocesses:
1. Ambient Attached Service Roles / Workload Identities (EC2 IMDSv2, ECS Task Role,
   EKS IRSA / Pod Identity, Lambda, GCP Metadata Server / GKE Workload Identity /
   Cloud Run, Azure VM IMDS / App Service MSI / AKS Workload Identity, K8s In-Cluster SA).
2. Local Workstation Configuration Files (~/.aws/config & credentials,
   ~/.azure/azureProfile.json with utf-8-sig BOM handling, ~/.config/gcloud/configurations/*,
   and ~/.kube/config).
3. Registered Cross-Account / Multi-Tenant Federated Targets in config.toml or environment.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from talkops.utils.logger import get_logger

logger = get_logger(__name__)

CloudProviderType = Literal["aws", "azure", "gcp", "k8s"]
CloudExecutionMode = Literal["read_only", "elevated"]
CloudAuthSource = Literal[
    "ambient_service_role",
    "local_profile",
    "env_credentials",
    "federated_role",
]

_SENSITIVE_METADATA_SUBSTRINGS = (
    "secret",
    "token",
    "password",
    "private_key",
    "client_secret",
    "credential",
    "access_key",
    "session_key",
)

_ROLE_ARN_ACCOUNT_RE = re.compile(r"arn:aws(?:-[a-z0-9-]+)?:(?:iam|sts)::(\d{12}):")


def _sanitize_metadata(raw: dict[str, Any]) -> dict[str, str]:
    """Strip sensitive keys and coerce values to strings."""
    cleaned: dict[str, str] = {}
    for k, v in raw.items():
        if v is None:
            continue
        k_low = str(k).lower()
        if any(s in k_low for s in _SENSITIVE_METADATA_SUBSTRINGS):
            continue
        val_str = str(v).strip()
        if val_str:
            cleaned[str(k)] = val_str
    return cleaned


@dataclass(frozen=True)
class CloudTargetContext:
    """Unified cloud context target across AWS, Azure, GCP, and Kubernetes."""

    provider: CloudProviderType
    target_id: str                  # AWS profile, Azure subscription ID, GCP config name, K8s context, or "@ambient"
    display_name: str               # Human-readable alias or subscription/project label
    account_or_project: str         # AWS Account ID, Azure Tenant ID, GCP Project ID, or K8s Cluster
    region: str | None = None
    is_default: bool = False
    auth_source: CloudAuthSource = "local_profile"
    auth_method: str = "profile"    # "irsa", "ecs_task_role", "imds_role", "sso", "iam_keys", "azure_cli", "azure_msi", "gcp_adc", "gce_metadata", "in_cluster_sa", etc.
    execution_mode: CloudExecutionMode = "elevated"
    extra_metadata: dict[str, str] = field(default_factory=dict)

    @property
    def qualified_id(self) -> str:
        """Return canonical `<provider>:<target_id>` identifier."""
        return f"{self.provider}:{self.target_id}"

    @property
    def is_ambient(self) -> bool:
        """Whether this context represents an attached service role or ambient env identity."""
        return self.auth_source in ("ambient_service_role", "env_credentials") or self.target_id.startswith("@")

    # ── Backward-compatibility properties with AWSProfileInfo ──

    @property
    def name(self) -> str:
        """Alias for target_id (AWSProfileInfo compatibility)."""
        return self.target_id

    @property
    def account_id(self) -> str | None:
        """Return account/tenant/project identifier if known."""
        if self.account_or_project and self.account_or_project not in ("unassigned", "local-credentials", "unknown"):
            return self.account_or_project
        return self.extra_metadata.get("account_id") or None

    @property
    def role_arn(self) -> str | None:
        """Return the role arn property."""
        return self.extra_metadata.get("role_arn") or None

    @property
    def source_profile(self) -> str | None:
        """Return the source profile property."""
        return self.extra_metadata.get("source_profile") or None

    @property
    def is_sso(self) -> bool:
        """Return the is sso property."""
        return self.auth_method == "sso" or "sso" in self.auth_method

    @property
    def has_keys(self) -> bool:
        """Return the has keys property."""
        return self.auth_method in ("iam_keys", "env_keys")

    @property
    def region_or_location(self) -> str | None:
        """Alias for region (compatibility with multi-cloud callers)."""
        return self.region

    @property
    def account_or_tenant_id(self) -> str | None:
        """Alias for account_id / account_or_project."""
        return self.account_id

    @property
    def identity_hint(self) -> str | None:
        """Return principal/role/user hint if available."""
        return (
            self.extra_metadata.get("role_arn")
            or self.extra_metadata.get("user")
            or self.extra_metadata.get("account")
            or self.account_id
        )

    @property
    def has_valid_Hint(self) -> bool:
        """Return True if this target has a valid local or ambient credential hint."""
        return bool(
            self.is_ambient
            or self.has_keys
            or self.is_sso
            or self.role_arn
            or (self.account_or_project and self.account_or_project not in ("unconfigured", "unassigned"))
        )

    def __iter__(self) -> Iterator[Any]:
        """Support 3-tuple unpacking: `provider, target_id, region = get_active_cloud_target()`."""
        yield self.provider
        yield self.target_id
        yield self.region

    def __getitem__(self, index: int) -> Any:
        """Return the item for the given key or index."""
        return (self.provider, self.target_id, self.region)[index]

    @property
    def display_details(self) -> str:
        """Formatted summary string for UI cards, selectors, and CLI output."""
        parts: list[str] = []
        if self.is_ambient:
            parts.append(f"ambient ({self.auth_method})")
        if self.region:
            label = "ns" if self.provider == "k8s" else "region"
            parts.append(f"{label}: {self.region}")
        if self.account_or_project and self.account_or_project not in ("unassigned", "local-credentials", "unknown", ""):
            if self.provider == "aws":
                parts.append(f"acct: {self.account_or_project}")
            elif self.provider == "azure":
                parts.append(f"tenant: {self.account_or_project[:12]}…")
            elif self.provider == "gcp":
                parts.append(f"project: {self.account_or_project}")
            elif self.provider == "k8s":
                parts.append(f"cluster: {self.account_or_project}")
        elif self.role_arn:
            parts.append("role assumed")
        elif self.is_sso:
            parts.append("SSO")
        elif self.has_keys:
            parts.append("IAM keys")
        return ", ".join(parts) if parts else "configured"


# ── Ambient Service Role / Cloud Runtime Detector ──────────────────────────

_ambient_probe_cache: dict[str, CloudTargetContext | None] = {}


def clear_ambient_probe_cache() -> None:
    """Reset cached ambient cloud metadata probes."""
    _ambient_probe_cache.clear()


def _read_dmi_hint() -> str:
    """Read Linux DMI / hypervisor strings in <0.1ms to avoid unnecessary metadata HTTP probes on laptops."""
    if os.environ.get("TALKOPS_PROBE_CLOUD_METADATA", "").strip().lower() in ("1", "true", "yes"):
        return "force_probe"
    if sys.platform != "linux":
        return ""
    candidates = (
        "/sys/devices/virtual/dmi/id/sys_vendor",
        "/sys/devices/virtual/dmi/id/product_name",
        "/sys/devices/virtual/dmi/id/bios_vendor",
        "/sys/devices/virtual/dmi/id/board_vendor",
        "/sys/devices/virtual/dmi/id/board_asset_tag",
        "/sys/hypervisor/uuid",
    )
    parts: list[str] = []
    for path_str in candidates:
        p = Path(path_str)
        if p.exists():
            try:
                parts.append(p.read_text(encoding="utf-8", errors="ignore").strip().lower())
            except OSError:
                pass
    return " ".join(parts)


def _http_get_metadata(url: str, headers: dict[str, str] | None = None, timeout: float = 0.15) -> str | None:
    """Perform a fast, non-blocking HTTP GET against a link-local metadata endpoint."""
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return None
    return None


def _http_put_metadata(url: str, headers: dict[str, str] | None = None, timeout: float = 0.15) -> str | None:
    """Perform a fast, non-blocking HTTP PUT (e.g. AWS IMDSv2 token acquisition)."""
    req = urllib.request.Request(url, headers=headers or {}, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return None
    return None


class AmbientCloudDetector:
    """Detects attached cloud service roles, workload identities, and container credentials."""

    @staticmethod
    def detect_aws(
        env: dict[str, str] | None = None,
        *,
        allow_imds_probe: bool = True,
    ) -> CloudTargetContext | None:
        """Detect AWS ambient service role (EKS IRSA, Pod Identity, ECS Task Role, Lambda, Env, or EC2 IMDSv2)."""
        e = env if env is not None else dict(os.environ)
        region = e.get("AWS_REGION") or e.get("AWS_DEFAULT_REGION") or "us-east-1"

        # 1. EKS IRSA (IAM Roles for Service Accounts)
        web_id_token = e.get("AWS_WEB_IDENTITY_TOKEN_FILE", "").strip()
        role_arn = e.get("AWS_ROLE_ARN", "").strip()
        if web_id_token and role_arn:
            m = _ROLE_ARN_ACCOUNT_RE.search(role_arn)
            acct = m.group(1) if m else "irsa-account"
            role_short = role_arn.rsplit("/", 1)[-1]
            return CloudTargetContext(
                provider="aws",
                target_id="@ambient",
                display_name=f"EKS IRSA ({role_short})",
                account_or_project=acct,
                region=region,
                is_default=True,
                auth_source="ambient_service_role",
                auth_method="irsa",
                extra_metadata={"role_arn": role_arn, "token_file": web_id_token},
            )

        # 2. EKS Pod Identity / ECS Task Role
        ecs_rel = e.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "").strip()
        ecs_full = e.get("AWS_CONTAINER_CREDENTIALS_FULL_URI", "").strip()
        if ecs_rel or ecs_full:
            is_pod_id = "169.254.170.23" in ecs_full or bool(e.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"))
            method = "eks_pod_identity" if is_pod_id else "ecs_task_role"
            label = "EKS Pod Identity Role" if is_pod_id else "ECS Task Service Role"
            return CloudTargetContext(
                provider="aws",
                target_id="@ambient",
                display_name=label,
                account_or_project="container-role",
                region=region,
                is_default=True,
                auth_source="ambient_service_role",
                auth_method=method,
                extra_metadata={"credentials_uri": ecs_full or ecs_rel},
            )

        # 3. AWS Lambda / CodeBuild Execution Role
        exec_env = e.get("AWS_EXECUTION_ENV", "").strip()
        lambda_fn = e.get("AWS_LAMBDA_FUNCTION_NAME", "").strip()
        if lambda_fn or "AWS_Lambda" in exec_env or e.get("CODEBUILD_BUILD_ID"):
            label = f"Lambda Role ({lambda_fn})" if lambda_fn else f"AWS Execution Role ({exec_env or 'CodeBuild'})"
            return CloudTargetContext(
                provider="aws",
                target_id="@ambient",
                display_name=label,
                account_or_project="execution-role",
                region=region,
                is_default=True,
                auth_source="ambient_service_role",
                auth_method="execution_role",
                extra_metadata={"execution_env": exec_env or "codebuild"},
            )

        # 4. Explicit AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY in environment
        # Only treat env keys as an ambient identity when explicitly probing a custom env dict
        # or when no local ~/.aws/config profiles exist (e.g. container/CI runtime).
        ak = e.get("AWS_ACCESS_KEY_ID", "").strip()
        sk = e.get("AWS_SECRET_ACCESS_KEY", "").strip()
        if ak and sk and (ak.startswith(("AKIA", "ASIA")) or env is not None):
            has_local_aws_cfg = (Path.home() / ".aws" / "config").is_file() or (
                Path.home() / ".aws" / "credentials"
            ).is_file()
            if env is not None or not has_local_aws_cfg:
                st = e.get("AWS_SESSION_TOKEN", "").strip()
                is_sts = ak.startswith("ASIA") or bool(st)
                method = "sts_env_token" if is_sts else "env_keys"
                label = f"AWS STS Session ({ak[:8]}…)" if is_sts else f"AWS Env Keys ({ak[:8]}…)"
                return CloudTargetContext(
                    provider="aws",
                    target_id="@ambient",
                    display_name=label,
                    account_or_project="env-credentials",
                    region=region,
                    is_default=True,
                    auth_source="ambient_service_role" if is_sts else "env_credentials",
                    auth_method=method,
                    extra_metadata={"key_prefix": ak[:8]},
                )

        # 5. EC2 Instance Profile (IMDSv2)
        if not allow_imds_probe or e.get("AWS_EC2_METADATA_DISABLED", "").lower() == "true":
            return None

        if env is None and "aws_imds" in _ambient_probe_cache:
            return _ambient_probe_cache["aws_imds"]

        dmi = _read_dmi_hint()
        if not any(k in dmi for k in ("amazon", "ec2", "xen", "force_probe")):
            if env is None:
                _ambient_probe_cache["aws_imds"] = None
            return None

        token = _http_put_metadata(
            "http://169.254.169.254/latest/api/token",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        headers = {"X-aws-ec2-metadata-token": token} if token else {}
        role_name = _http_get_metadata(
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
            headers=headers,
        )
        if not role_name:
            if env is None:
                _ambient_probe_cache["aws_imds"] = None
            return None

        first_role = role_name.splitlines()[0].strip()
        doc_raw = _http_get_metadata(
            "http://169.254.169.254/latest/dynamic/instance-identity/document",
            headers=headers,
        )
        acct_id = "ec2-instance-role"
        imds_region = region
        if doc_raw:
            try:
                doc = json.loads(doc_raw)
                acct_id = doc.get("accountId", acct_id)
                imds_region = doc.get("region", imds_region)
            except Exception:
                pass

        ctx = CloudTargetContext(
            provider="aws",
            target_id="@ambient",
            display_name=f"EC2 Instance Role ({first_role})",
            account_or_project=acct_id,
            region=imds_region,
            is_default=True,
            auth_source="ambient_service_role",
            auth_method="imds_role",
            extra_metadata={"instance_role": first_role},
        )
        if env is None:
            _ambient_probe_cache["aws_imds"] = ctx
        return ctx

    @staticmethod
    def detect_azure(
        env: dict[str, str] | None = None,
        *,
        allow_imds_probe: bool = True,
    ) -> CloudTargetContext | None:
        """Detect Azure Managed Identity, AKS Workload Identity, or Service Principal env."""
        e = env if env is not None else dict(os.environ)
        sub_id = (
            e.get("AZURE_SUBSCRIPTION_ID")
            or e.get("ARM_SUBSCRIPTION_ID")
            or ""
        ).strip()
        tenant_id = (
            e.get("AZURE_TENANT_ID")
            or e.get("ARM_TENANT_ID")
            or ""
        ).strip()
        client_id = (
            e.get("AZURE_CLIENT_ID")
            or e.get("ARM_CLIENT_ID")
            or ""
        ).strip()
        location = e.get("AZURE_DEFAULTS_LOCATION") or e.get("ARM_LOCATION") or None
        rg = e.get("AZURE_DEFAULTS_GROUP") or None

        # 1. AKS Workload Identity (Federated Token File)
        fed_token_file = e.get("AZURE_FEDERATED_TOKEN_FILE", "").strip()
        if fed_token_file and client_id and tenant_id:
            target = sub_id or "@ambient"
            return CloudTargetContext(
                provider="azure",
                target_id=target,
                display_name=f"AKS Workload Identity ({client_id[:8]}…)",
                account_or_project=tenant_id,
                region=location,
                is_default=True,
                auth_source="ambient_service_role",
                auth_method="aks_workload_identity",
                extra_metadata=_sanitize_metadata(
                    {
                        "client_id": client_id,
                        "tenant_id": tenant_id,
                        "subscription_id": sub_id,
                        "resource_group": rg or "",
                        "federated_token_file": fed_token_file,
                    }
                ),
            )

        # 2. App Service / Container Apps / Functions Managed Identity
        identity_endpoint = (e.get("IDENTITY_ENDPOINT") or e.get("MSI_ENDPOINT") or "").strip()
        if identity_endpoint:
            target = sub_id or "@ambient"
            label = f"Azure Managed Identity ({client_id[:8]}…)" if client_id else "Azure System Managed Identity"
            return CloudTargetContext(
                provider="azure",
                target_id=target,
                display_name=label,
                account_or_project=tenant_id or "managed-identity",
                region=location,
                is_default=True,
                auth_source="ambient_service_role",
                auth_method="managed_identity",
                extra_metadata=_sanitize_metadata(
                    {
                        "client_id": client_id,
                        "subscription_id": sub_id,
                        "resource_group": rg or "",
                        "identity_endpoint": identity_endpoint,
                    }
                ),
            )

        # 3. Environment Service Principal
        has_secret = bool(e.get("AZURE_CLIENT_SECRET") or e.get("ARM_CLIENT_SECRET") or e.get("AZURE_CLIENT_CERTIFICATE_PATH"))
        if client_id and tenant_id and has_secret:
            target = sub_id or "@ambient"
            return CloudTargetContext(
                provider="azure",
                target_id=target,
                display_name=f"Azure Service Principal ({client_id[:8]}…)",
                account_or_project=tenant_id,
                region=location,
                is_default=True,
                auth_source="env_credentials",
                auth_method="service_principal",
                extra_metadata=_sanitize_metadata(
                    {
                        "client_id": client_id,
                        "tenant_id": tenant_id,
                        "subscription_id": sub_id,
                        "resource_group": rg or "",
                    }
                ),
            )

        # 4. Azure VM IMDS Probe
        if not allow_imds_probe:
            return None

        if env is None and "azure_imds" in _ambient_probe_cache:
            return _ambient_probe_cache["azure_imds"]

        dmi = _read_dmi_hint()
        if not any(k in dmi for k in ("microsoft", "azure", "force_probe")):
            if env is None:
                _ambient_probe_cache["azure_imds"] = None
            return None

        imds_raw = _http_get_metadata(
            "http://169.254.169.254/metadata/instance?api-version=2021-02-01",
            headers={"Metadata": "true"},
        )
        if not imds_raw:
            if env is None:
                _ambient_probe_cache["azure_imds"] = None
            return None

        try:
            imds = json.loads(imds_raw)
            comp = imds.get("compute", {})
            vm_sub = comp.get("subscriptionId") or sub_id or "@ambient"
            vm_loc = comp.get("location") or location
            vm_rg = comp.get("resourceGroupName") or rg or ""
            vm_name = comp.get("name") or "azure-vm"
            az_env = comp.get("azEnvironment") or "AzurePublicCloud"
            ctx = CloudTargetContext(
                provider="azure",
                target_id=vm_sub,
                display_name=f"Azure VM Managed Identity ({vm_name})",
                account_or_project=tenant_id or "azure-imds",
                region=vm_loc,
                is_default=True,
                auth_source="ambient_service_role",
                auth_method="vm_managed_identity",
                extra_metadata=_sanitize_metadata(
                    {
                        "subscription_id": vm_sub,
                        "resource_group": vm_rg,
                        "environmentName": az_env,
                        "vm_name": vm_name,
                    }
                ),
            )
            if env is None:
                _ambient_probe_cache["azure_imds"] = ctx
            return ctx
        except Exception:
            if env is None:
                _ambient_probe_cache["azure_imds"] = None
            return None

    @staticmethod
    def detect_gcp(
        env: dict[str, str] | None = None,
        *,
        allow_metadata_probe: bool = True,
    ) -> CloudTargetContext | None:
        """Detect GCP attached Service Account (Cloud Run, GKE Workload Identity, GCE Metadata, or GOOGLE_APPLICATION_CREDENTIALS)."""
        e = env if env is not None else dict(os.environ)
        env_project = (
            e.get("GOOGLE_CLOUD_PROJECT")
            or e.get("CLOUDSDK_CORE_PROJECT")
            or e.get("GCLOUD_PROJECT")
            or e.get("GCP_PROJECT")
            or ""
        ).strip()
        env_region = (
            e.get("CLOUDSDK_COMPUTE_REGION")
            or e.get("GOOGLE_CLOUD_REGION")
            or None
        )

        # 1. Explicit GOOGLE_APPLICATION_CREDENTIALS file (Service Account or WIF External Account)
        gac_path_str = e.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        if gac_path_str:
            gac_path = Path(gac_path_str).expanduser()
            if gac_path.is_file():
                try:
                    data = json.loads(gac_path.read_text(encoding="utf-8"))
                    cred_type = data.get("type", "service_account")
                    proj = data.get("project_id") or data.get("quota_project_id") or env_project or "unassigned"
                    email = data.get("client_email") or data.get("service_account_impersonation_url") or cred_type
                    is_wif = cred_type == "external_account"
                    return CloudTargetContext(
                        provider="gcp",
                        target_id="@ambient",
                        display_name=f"GCP {'WIF' if is_wif else 'Service Account'} ({proj})",
                        account_or_project=proj,
                        region=env_region,
                        is_default=True,
                        auth_source="ambient_service_role" if is_wif else "env_credentials",
                        auth_method="workload_identity_federation" if is_wif else "service_account_json",
                        extra_metadata=_sanitize_metadata(
                            {
                                "account": str(email),
                                "credentials_type": str(cred_type),
                                "credentials_file": str(gac_path),
                            }
                        ),
                    )
                except Exception:
                    pass

        # 2. Cloud Run / Cloud Functions / App Engine environment indicators
        k_service = e.get("K_SERVICE") or e.get("FUNCTION_TARGET") or e.get("GAE_SERVICE")
        metadata_host = e.get("GCE_METADATA_HOST", "").strip()
        if k_service:
            proj = env_project or "cloud-run-project"
            return CloudTargetContext(
                provider="gcp",
                target_id="@ambient",
                display_name=f"GCP Cloud Run SA ({k_service})",
                account_or_project=proj,
                region=env_region,
                is_default=True,
                auth_source="ambient_service_role",
                auth_method="cloud_run_sa",
                extra_metadata={"service": k_service},
            )

        # 3. GCE / GKE Workload Identity Metadata Server Probe
        if not allow_metadata_probe:
            return None

        if env is None and "gcp_metadata" in _ambient_probe_cache:
            return _ambient_probe_cache["gcp_metadata"]

        dmi = _read_dmi_hint()
        if not metadata_host and not any(k in dmi for k in ("google", "gce", "force_probe")):
            if env is None:
                _ambient_probe_cache["gcp_metadata"] = None
            return None

        host = metadata_host or "169.254.169.254"
        headers = {"Metadata-Flavor": "Google"}
        proj_id = _http_get_metadata(
            f"http://{host}/computeMetadata/v1/project/project-id",
            headers=headers,
        )
        if not proj_id:
            if env is None:
                _ambient_probe_cache["gcp_metadata"] = None
            return None

        sa_email = _http_get_metadata(
            f"http://{host}/computeMetadata/v1/instance/service-accounts/default/email",
            headers=headers,
        ) or "default"
        zone_raw = _http_get_metadata(
            f"http://{host}/computeMetadata/v1/instance/zone",
            headers=headers,
        ) or ""
        zone = zone_raw.rsplit("/", 1)[-1] if zone_raw else ""
        region = "-".join(zone.split("-")[:2]) if zone and "-" in zone else env_region

        ctx = CloudTargetContext(
            provider="gcp",
            target_id="@ambient",
            display_name=f"GCP Attached SA ({proj_id})",
            account_or_project=proj_id,
            region=region,
            is_default=True,
            auth_source="ambient_service_role",
            auth_method="gce_metadata_sa",
            extra_metadata=_sanitize_metadata(
                {
                    "account": sa_email,
                    "zone": zone,
                }
            ),
        )
        if env is None:
            _ambient_probe_cache["gcp_metadata"] = ctx
        return ctx

    @staticmethod
    def detect_k8s(
        env: dict[str, str] | None = None,
        sa_dir: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount"),
    ) -> CloudTargetContext | None:
        """Detect Kubernetes In-Cluster ServiceAccount when running inside a Pod."""
        e = env if env is not None else dict(os.environ)
        k8s_host = e.get("KUBERNETES_SERVICE_HOST", "").strip()
        token_file = sa_dir / "token"
        if not k8s_host or not token_file.is_file():
            return None

        ns_file = sa_dir / "namespace"
        namespace = "default"
        if ns_file.is_file():
            try:
                namespace = ns_file.read_text(encoding="utf-8").strip() or "default"
            except OSError:
                pass

        port = e.get("KUBERNETES_SERVICE_PORT", "443")
        cluster_endpoint = f"{k8s_host}:{port}"
        return CloudTargetContext(
            provider="k8s",
            target_id="@in-cluster",
            display_name=f"In-Cluster ServiceAccount ({namespace})",
            account_or_project=cluster_endpoint,
            region=namespace,
            is_default=True,
            auth_source="ambient_service_role",
            auth_method="in_cluster_sa",
            extra_metadata={"namespace": namespace, "service_host": k8s_host},
        )

    @classmethod
    def detect_all(
        cls,
        env: dict[str, str] | None = None,
        *,
        allow_network_probes: bool = True,
    ) -> list[CloudTargetContext]:
        """Detect all active ambient cloud identities across AWS, Azure, GCP, and K8s."""
        results: list[CloudTargetContext] = []
        aws_ctx = cls.detect_aws(env, allow_imds_probe=allow_network_probes)
        if aws_ctx:
            results.append(aws_ctx)
        az_ctx = cls.detect_azure(env, allow_imds_probe=allow_network_probes)
        if az_ctx:
            results.append(az_ctx)
        gcp_ctx = cls.detect_gcp(env, allow_metadata_probe=allow_network_probes)
        if gcp_ctx:
            results.append(gcp_ctx)
        k8s_ctx = cls.detect_k8s(env)
        if k8s_ctx:
            results.append(k8s_ctx)
        return results


# ── Zero-Latency Local Cloud Discovery Service ─────────────────────────────


class LocalCloudDiscoveryService:
    """Discovers local AWS, Azure, GCP, and Kubernetes contexts directly from disk with zero CLI overhead."""

    @staticmethod
    def discover_aws(
        config_path: Path | None = None,
        creds_path: Path | None = None,
        *,
        include_ambient: bool = True,
        allow_imds_probe: bool = True,
    ) -> list[CloudTargetContext]:
        """Parse ~/.aws/config and ~/.aws/credentials in memory (no subprocesses)."""
        aws_dir = Path.home() / ".aws"
        cfg_file = config_path or Path(os.environ.get("AWS_CONFIG_FILE") or (aws_dir / "config")).expanduser()
        crd_file = creds_path or Path(
            os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or (aws_dir / "credentials")
        ).expanduser()

        cfg_parser = configparser.RawConfigParser()
        discovered_names: list[str] = []

        if cfg_file.exists():
            try:
                cfg_parser.read(cfg_file, encoding="utf-8")
                for section in cfg_parser.sections():
                    sec_clean = section.strip()
                    if sec_clean == "default":
                        if "default" not in discovered_names:
                            discovered_names.append("default")
                    elif sec_clean.startswith("profile "):
                        pname = sec_clean[len("profile ") :].strip()
                        if pname and pname not in discovered_names:
                            discovered_names.append(pname)
                    elif not sec_clean.startswith(("sso-session ", "services ")):
                        if sec_clean and sec_clean not in discovered_names:
                            discovered_names.append(sec_clean)
            except Exception as exc:
                logger.debug("Error reading %s: %s", cfg_file, exc)

        cred_parser = configparser.RawConfigParser()
        if crd_file.exists():
            try:
                cred_parser.read(crd_file, encoding="utf-8")
                for section in cred_parser.sections():
                    pname = section.strip()
                    if pname and pname not in discovered_names:
                        discovered_names.append(pname)
            except Exception as exc:
                logger.debug("Error reading %s: %s", crd_file, exc)

        # Put 'default' first if present in files
        if "default" in discovered_names and discovered_names[0] != "default":
            discovered_names.remove("default")
            discovered_names.insert(0, "default")

        results: list[CloudTargetContext] = []

        # Include ambient service role if detected
        if include_ambient:
            ambient = AmbientCloudDetector.detect_aws(allow_imds_probe=allow_imds_probe)
            if ambient is not None:
                results.append(ambient)

        for name in discovered_names:
            ctx = LocalCloudDiscoveryService._inspect_aws_profile(name, cfg_parser, cred_parser)
            results.append(ctx)

        return results

    @staticmethod
    def _inspect_aws_profile(
        name: str,
        cfg_parser: configparser.RawConfigParser,
        cred_parser: configparser.RawConfigParser,
    ) -> CloudTargetContext:
        """Extract rich metadata for a single AWS profile including source_profile chain."""
        sec_candidates = [f"profile {name}", name] if name != "default" else ["default"]
        sec_name = next((s for s in sec_candidates if cfg_parser.has_section(s)), sec_candidates[0])

        raw_items: dict[str, str] = {}
        region: str | None = None
        role_arn: str | None = None
        source_profile: str | None = None
        account_id: str | None = None
        is_sso = False
        has_keys = False
        has_cred_process = False

        if cfg_parser.has_section(sec_name):
            raw_items.update(dict(cfg_parser.items(sec_name)))
            region = cfg_parser.get(sec_name, "region", fallback=None)
            role_arn = cfg_parser.get(sec_name, "role_arn", fallback=None)
            source_profile = cfg_parser.get(sec_name, "source_profile", fallback=None)
            if cfg_parser.has_option(sec_name, "sso_session") or cfg_parser.has_option(sec_name, "sso_start_url"):
                is_sso = True
            if cfg_parser.has_option(sec_name, "credential_process"):
                has_cred_process = True
            sso_acct = cfg_parser.get(sec_name, "sso_account_id", fallback=None)
            if sso_acct:
                account_id = sso_acct.strip()

        # Traverse source_profile chain up to 5 hops for region inheritance
        if not region and source_profile:
            curr = source_profile
            visited = {name, curr}
            for _ in range(5):
                src_cands = [f"profile {curr}", curr] if curr != "default" else ["default"]
                src_sec = next((s for s in src_cands if cfg_parser.has_section(s)), None)
                if not src_sec:
                    break
                src_reg = cfg_parser.get(src_sec, "region", fallback=None)
                if src_reg and src_reg.strip():
                    region = src_reg.strip()
                    break
                nxt = cfg_parser.get(src_sec, "source_profile", fallback=None)
                if nxt and nxt not in visited:
                    visited.add(nxt)
                    curr = nxt
                else:
                    break

        if cred_parser.has_section(name):
            if cred_parser.has_option(name, "aws_access_key_id"):
                has_keys = True
            if cred_parser.has_option(name, "credential_process"):
                has_cred_process = True
            if not region and cred_parser.has_option(name, "region"):
                region = cred_parser.get(name, "region", fallback=None)

        if role_arn and not account_id:
            m = _ROLE_ARN_ACCOUNT_RE.search(role_arn)
            if m:
                account_id = m.group(1)

        if is_sso:
            auth_method = "sso"
        elif role_arn:
            auth_method = "iam_role"
        elif has_cred_process:
            auth_method = "credential_process"
        elif has_keys:
            auth_method = "iam_keys"
        else:
            auth_method = "profile"

        meta = _sanitize_metadata(raw_items)
        if account_id:
            meta["account_id"] = account_id
        if role_arn:
            meta["role_arn"] = role_arn
        if source_profile:
            meta["source_profile"] = source_profile

        return CloudTargetContext(
            provider="aws",
            target_id=name,
            display_name=name,
            account_or_project=account_id or (role_arn if role_arn else "local-credentials"),
            region=region.strip() if region else None,
            is_default=(name == "default"),
            auth_source="local_profile",
            auth_method=auth_method,
            extra_metadata=meta,
        )

    @staticmethod
    def discover_azure(
        profile_path: Path | None = None,
        config_path: Path | None = None,
        *,
        include_ambient: bool = True,
        allow_imds_probe: bool = True,
    ) -> list[CloudTargetContext]:
        """Parse ~/.azure/azureProfile.json (handling UTF-8 BOM utf-8-sig) and ~/.azure/config."""
        az_dir = Path(os.environ.get("AZURE_CONFIG_DIR") or (Path.home() / ".azure")).expanduser()
        prof_file = profile_path or (az_dir / "azureProfile.json")
        cfg_file = config_path or (az_dir / "config")

        default_location: str | None = None
        default_group: str | None = None
        if cfg_file.exists():
            try:
                cp = configparser.RawConfigParser()
                cp.read(cfg_file, encoding="utf-8")
                default_location = cp.get("defaults", "location", fallback=None)
                default_group = cp.get("defaults", "group", fallback=None)
            except Exception:
                pass

        results: list[CloudTargetContext] = []
        if include_ambient:
            ambient = AmbientCloudDetector.detect_azure(allow_imds_probe=allow_imds_probe)
            if ambient is not None:
                results.append(ambient)

        if not prof_file.exists():
            return results

        try:
            # Crucial: Azure CLI writes a UTF-8 BOM (\ufeff) at the start of azureProfile.json
            raw_content = prof_file.read_text(encoding="utf-8-sig")
            data = json.loads(raw_content)
        except Exception as exc:
            logger.debug("Error reading Azure profile %s: %s", prof_file, exc)
            return results

        seen_ids = {r.target_id for r in results}
        for sub in data.get("subscriptions", []):
            if not isinstance(sub, dict):
                continue
            state = str(sub.get("state", "Enabled"))
            if state != "Enabled":
                continue
            sub_id = str(sub.get("id", "")).strip()
            if not sub_id or sub_id in seen_ids:
                continue
            seen_ids.add(sub_id)
            sub_name = str(sub.get("name") or sub_id).strip()
            tenant_id = str(sub.get("tenantId") or "").strip()
            raw_user = sub.get("user")
            user_obj: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
            user_name = str(user_obj.get("name") or "")
            user_type = str(user_obj.get("type") or "user")
            env_name = str(sub.get("environmentName") or "AzureCloud")

            meta = _sanitize_metadata(
                {
                    "subscription_name": sub_name,
                    "tenant_id": tenant_id,
                    "environmentName": env_name,
                    "user": user_name,
                    "user_type": user_type,
                    "resource_group": default_group or "",
                }
            )
            results.append(
                CloudTargetContext(
                    provider="azure",
                    target_id=sub_id,
                    display_name=f"{sub_name} ({sub_id[:8]}…)" if sub_name != sub_id else sub_id,
                    account_or_project=tenant_id or "azure-tenant",
                    region=default_location,
                    is_default=bool(sub.get("isDefault", False)),
                    auth_source="local_profile",
                    auth_method="azure_cli",
                    extra_metadata=meta,
                )
            )
        return results

    @staticmethod
    def discover_gcp(
        gcloud_dir: Path | None = None,
        *,
        include_ambient: bool = True,
        allow_metadata_probe: bool = True,
    ) -> list[CloudTargetContext]:
        """Parse ~/.config/gcloud/configurations/config_* and active_config in memory."""
        base_dir = gcloud_dir or Path(
            os.environ.get("CLOUDSDK_CONFIG") or (Path.home() / ".config" / "gcloud")
        ).expanduser()
        configs_dir = base_dir / "configurations"
        active_file = base_dir / "active_config"
        adc_file = base_dir / "application_default_credentials.json"

        active_name = "default"
        if active_file.exists():
            try:
                active_name = active_file.read_text(encoding="utf-8").strip() or "default"
            except OSError:
                pass

        has_adc = adc_file.is_file()

        results: list[CloudTargetContext] = []
        if include_ambient:
            ambient = AmbientCloudDetector.detect_gcp(allow_metadata_probe=allow_metadata_probe)
            if ambient is not None:
                results.append(ambient)

        if not configs_dir.exists():
            return results

        for cfg_file in sorted(configs_dir.glob("config_*")):
            if not cfg_file.is_file():
                continue
            cfg_name = cfg_file.name[len("config_") :]
            if not cfg_name:
                continue
            cp = configparser.RawConfigParser()
            try:
                cp.read(cfg_file, encoding="utf-8")
            except Exception as exc:
                logger.debug("Error reading GCP config %s: %s", cfg_file, exc)
                continue

            project = cp.get("core", "project", fallback="unassigned").strip()
            account = cp.get("core", "account", fallback="unassigned").strip()
            region = cp.get("compute", "region", fallback=None)
            zone = cp.get("compute", "zone", fallback=None)
            effective_region = (region.strip() if region else None) or (
                "-".join(zone.strip().split("-")[:2]) if zone and "-" in zone else None
            )

            display = f"{cfg_name} ({project})" if project and project != "unassigned" else cfg_name
            results.append(
                CloudTargetContext(
                    provider="gcp",
                    target_id=cfg_name,
                    display_name=display,
                    account_or_project=project,
                    region=effective_region,
                    is_default=(cfg_name == active_name),
                    auth_source="local_profile",
                    auth_method="gcp_adc" if has_adc else "gcloud_cli",
                    extra_metadata=_sanitize_metadata(
                        {
                            "account": account,
                            "project": project,
                            "zone": zone.strip() if zone else "",
                            "has_adc": "true" if has_adc else "false",
                        }
                    ),
                )
            )
        return results

    @staticmethod
    def discover_k8s(
        kubeconfig_path: Path | None = None,
        *,
        include_ambient: bool = True,
    ) -> list[CloudTargetContext]:
        """Parse ~/.kube/config (or $KUBECONFIG) in memory without spawning kubectl."""
        results: list[CloudTargetContext] = []
        if include_ambient:
            ambient = AmbientCloudDetector.detect_k8s()
            if ambient is not None:
                results.append(ambient)

        paths_to_read: list[Path] = []
        if kubeconfig_path is not None:
            paths_to_read.append(kubeconfig_path)
        else:
            env_kc = os.environ.get("KUBECONFIG", "").strip()
            if env_kc:
                for part in env_kc.split(os.pathsep):
                    if part.strip():
                        paths_to_read.append(Path(part.strip()).expanduser())
            else:
                paths_to_read.append(Path.home() / ".kube" / "config")

        seen_names: set[str] = {r.target_id for r in results}
        for kc_file in paths_to_read:
            if not kc_file.is_file():
                continue
            try:
                raw_text = kc_file.read_text(encoding="utf-8", errors="replace")
                parsed = LocalCloudDiscoveryService._parse_kubeconfig_text(raw_text)
                current_ctx = parsed.get("current_context", "")
                for item in parsed.get("contexts", []):
                    ctx_name = item.get("name", "").strip()
                    if not ctx_name or ctx_name in seen_names:
                        continue
                    seen_names.add(ctx_name)
                    cluster = item.get("cluster", "") or "cluster"
                    namespace = item.get("namespace") or "default"
                    user = item.get("user", "")
                    results.append(
                        CloudTargetContext(
                            provider="k8s",
                            target_id=ctx_name,
                            display_name=ctx_name,
                            account_or_project=cluster,
                            region=namespace,
                            is_default=(ctx_name == current_ctx),
                            auth_source="local_profile",
                            auth_method="kubeconfig",
                            extra_metadata=_sanitize_metadata(
                                {
                                    "cluster": cluster,
                                    "namespace": namespace,
                                    "user": user,
                                    "kubeconfig": str(kc_file),
                                }
                            ),
                        )
                    )
            except Exception as exc:
                logger.debug("Error parsing kubeconfig %s: %s", kc_file, exc)

        return results

    @staticmethod
    def _parse_kubeconfig_text(raw_text: str) -> dict[str, Any]:
        """Parse kubeconfig YAML using PyYAML if available, falling back to a lightweight parser."""
        try:
            import yaml  # type: ignore[import-untyped]

            data = yaml.safe_load(raw_text)
            if isinstance(data, dict):
                current_ctx = str(data.get("current-context") or "").strip()
                contexts_out: list[dict[str, str]] = []
                for entry in data.get("contexts") or []:
                    if not isinstance(entry, dict):
                        continue
                    name = str(entry.get("name") or "").strip()
                    raw_ctx = entry.get("context")
                    ctx_block: dict[str, Any] = raw_ctx if isinstance(raw_ctx, dict) else {}
                    contexts_out.append(
                        {
                            "name": name,
                            "cluster": str(ctx_block.get("cluster") or ""),
                            "namespace": str(ctx_block.get("namespace") or "default"),
                            "user": str(ctx_block.get("user") or ""),
                        }
                    )
                return {"current_context": current_ctx, "contexts": contexts_out}
        except Exception:
            pass

        # Fallback regex parser when PyYAML is not installed
        curr_m = re.search(r"^current-context:\s*['\"]?([^'\"\n]+)['\"]?", raw_text, re.MULTILINE)
        current_ctx = curr_m.group(1).strip() if curr_m else ""
        contexts_out = []
        in_contexts = False
        curr_item: dict[str, str] = {}
        for line in raw_text.splitlines():
            if re.match(r"^[a-zA-Z0-9_-]+:", line):
                if in_contexts and curr_item.get("name"):
                    contexts_out.append(curr_item)
                    curr_item = {}
                in_contexts = line.startswith("contexts:")
                continue
            if not in_contexts:
                continue
            stripped = line.strip()
            if stripped.startswith("- "):
                if curr_item.get("name"):
                    contexts_out.append(curr_item)
                curr_item = {"namespace": "default"}
                stripped = stripped[2:].strip()
            if ":" in stripped:
                k, v = stripped.split(":", 1)
                k_clean = k.strip()
                v_clean = v.strip().strip("'\"")
                if k_clean in ("name", "cluster", "namespace", "user") and v_clean:
                    curr_item[k_clean] = v_clean
        if in_contexts and curr_item.get("name"):
            contexts_out.append(curr_item)
        return {"current_context": current_ctx, "contexts": contexts_out}


__all__ = [
    "AmbientCloudDetector",
    "CloudAuthSource",
    "CloudExecutionMode",
    "CloudProviderType",
    "CloudTargetContext",
    "LocalCloudDiscoveryService",
    "clear_ambient_probe_cache",
]

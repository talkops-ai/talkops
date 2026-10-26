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

"""Multi-Cloud context discovery, status, and switching tools for TalkOps agents."""

from __future__ import annotations

import json
from typing import Any, Literal, cast

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from talkops.config.cloud_discovery import CloudProviderType
from talkops.config.cloud_profiles import (
    get_active_aws_profile,
    get_active_cloud_target,
    get_cloud_execution_mode,
    get_cloud_target_info,
    list_cloud_targets,
    set_active_cloud_target,
)
from talkops.config.credential_broker import get_credential_broker

_VALID_PROVIDERS: tuple[CloudProviderType, ...] = ("aws", "azure", "gcp", "k8s")


@tool
def cloud_list_contexts(
    provider: Literal["aws", "azure", "gcp", "k8s"] | None = None,
    config: RunnableConfig | None = None,
) -> str:
    """List discovered cloud profiles, subscriptions, projects, Kubernetes contexts, and ambient service roles.

    Args:
        provider: Optional filter ('aws', 'azure', 'gcp', 'k8s'). Omit to list all providers.
        config: Optional LangChain RunnableConfig injected at runtime.
    """
    del config
    prov_filter: CloudProviderType | None = (
        cast("CloudProviderType", provider.lower())
        if provider and provider.lower() in _VALID_PROVIDERS
        else None
    )
    active_prov, active_target, active_region = get_active_cloud_target()
    targets = list_cloud_targets(prov_filter)

    items: list[dict[str, Any]] = []
    for t in targets:
        items.append({
            "provider": t.provider,
            "target_id": t.target_id,
            "display_name": t.display_name,
            "region_or_location": t.region_or_location,
            "account_or_tenant_id": t.account_or_tenant_id,
            "identity_hint": t.identity_hint,
            "auth_source": t.auth_source,
            "is_ambient": t.is_ambient,
            "is_active": t.provider == active_prov and t.target_id == active_target,
        })

    return json.dumps(
        {
            "active_context": {
                "provider": active_prov,
                "target_id": active_target,
                "region": active_region,
                "execution_mode": get_cloud_execution_mode(),
            },
            "targets": items,
        },
        indent=2,
    )


@tool
def cloud_switch_context(
    provider: Literal["aws", "azure", "gcp", "k8s"],
    target_id: str,
    region: str | None = None,
    config: RunnableConfig | None = None,
) -> str:
    """Switch the active cloud provider and target context (AWS profile, Azure subscription, GCP config, K8s context, or '@ambient').

    Args:
        provider: Cloud provider ('aws', 'azure', 'gcp', 'k8s').
        target_id: Profile name, subscription ID/name, gcloud config name, kubeconfig context, or '@ambient' / '@in-cluster'.
        region: Optional region, location, or Kubernetes namespace override.
        config: Optional LangChain RunnableConfig injected at runtime.
    """
    del config
    prov_norm = provider.lower().strip()
    if prov_norm not in _VALID_PROVIDERS:
        return json.dumps(
            {
                "status": "error",
                "error": f"Unsupported provider '{provider}'. Expected one of: aws, azure, gcp, k8s.",
            }
        )

    prov = cast("CloudProviderType", prov_norm)
    info = get_cloud_target_info(prov, target_id)
    eff_target = info.target_id if info else target_id
    eff_region = region or (info.region_or_location if info else None)

    set_active_cloud_target(prov, eff_target, region=eff_region, persist=True)
    env_vars = get_credential_broker().get_process_env(
        provider=prov,
        target_id=eff_target,
        region=eff_region,
    )

    return json.dumps(
        {
            "status": "ok",
            "provider": prov,
            "target_id": eff_target,
            "display_name": info.display_name if info else eff_target,
            "region_or_location": eff_region,
            "is_ambient": info.is_ambient if info else (eff_target in ("@ambient", "@in-cluster")),
            "execution_mode": get_cloud_execution_mode(),
            "synchronized_env_keys": sorted(env_vars.keys()),
        },
        indent=2,
    )


@tool
def cloud_get_status(
    verify_live: bool = False,
    config: RunnableConfig | None = None,
) -> str:
    """Get the active multi-cloud provider, target, ambient service role status, and execution mode.

    Args:
        verify_live: When True, performs a live STS / CLI / metadata identity check.
        config: Optional LangChain RunnableConfig injected at runtime.
    """
    del config
    active_prov, active_target, active_region = get_active_cloud_target()
    exec_mode = get_cloud_execution_mode()
    info = get_cloud_target_info(active_prov, active_target)

    payload: dict[str, Any] = {
        "provider": active_prov,
        "target_id": active_target,
        "display_name": info.display_name if info else active_target,
        "region_or_location": active_region or (info.region_or_location if info else None),
        "account_or_tenant_id": info.account_or_tenant_id if info else None,
        "auth_source": info.auth_source if info else ("ambient_service_role" if active_target in ("@ambient", "@in-cluster") else "local_profile"),
        "is_ambient": info.is_ambient if info else (active_target in ("@ambient", "@in-cluster")),
        "execution_mode": exec_mode,
        "aws_profile": get_active_aws_profile(),
    }

    if verify_live:
        live = get_credential_broker().verify_cloud_identity(
            provider=active_prov,
            target_id=active_target,
            region=active_region,
        )
        payload["live_verification"] = {
            "is_authenticated": live.is_authenticated,
            "identity_principal": live.identity_principal,
            "account_or_tenant_id": live.account_or_tenant_id,
            "remediation_hint": live.remediation_hint,
            "error": live.error,
        }

    return json.dumps(payload, indent=2)


__all__ = [
    "cloud_get_status",
    "cloud_list_contexts",
    "cloud_switch_context",
]

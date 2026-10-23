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

"""Unified Multi-Cloud (AWS, Azure, GCP, Kubernetes) context & authentication command handler."""

from __future__ import annotations

import inspect
from typing import cast

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.config.cloud_discovery import CloudExecutionMode, CloudProviderType
from talkops.config.cloud_profiles import (
    get_active_aws_profile,
    get_active_cloud_target,
    get_aws_profile_info,
    get_cloud_execution_mode,
    get_cloud_target_info,
    list_aws_profiles,
    list_cloud_targets,
    set_active_aws_profile,
    set_active_cloud_target,
    set_cloud_execution_mode,
)
from talkops.config.credential_broker import get_credential_broker
from talkops.config.toml_config import (
    clear_active_cloud_context,
    clear_aws_profile,
    load_active_cloud_context,
    load_aws_profile,
)
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

_VALID_PROVIDERS: tuple[CloudProviderType, ...] = ("aws", "azure", "gcp", "k8s")
_PROVIDER_LABELS: dict[str, str] = {
    "aws": "AWS",
    "azure": "Microsoft Azure",
    "gcp": "Google Cloud (GCP)",
    "k8s": "Kubernetes",
}


class CloudHandler(BaseCommandHandler):
    """Handler for /cloud (and /aws, /azure, /gcp, /k8s) to manage multi-cloud contexts."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/cloud"

    @property
    def aliases(self) -> tuple[str, ...]:
        """Return alternative slash command aliases."""
        return ("/aws", "/azure", "/gcp", "/k8s")

    @property
    def category(self) -> CommandCategory:
        """Return the command category classification."""
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        """Return the safety level required for this command."""
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        """Return the execution queue bypass tier for this command."""
        return BypassTier.IMMEDIATE_UI

    @property
    def model_facing_description(self) -> str | None:
        """Return the description exposed to the LLM for command recommendations."""
        return (
            "Recommend this when the user wants to inspect or switch active "
            "AWS, Azure, GCP, or Kubernetes profiles and execution modes."
        )

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute the command handler with the provided command context."""
        args = ctx.args.strip()
        raw_cmd = (ctx.raw_command or "").strip().split()[0].lower() if ctx.raw_command else "/cloud"
        alias_provider: CloudProviderType | None = None
        initial_tab: str | None = None
        if raw_cmd in ("/aws", "/azure", "/gcp", "/k8s"):
            alias_provider = cast("CloudProviderType", raw_cmd.lstrip("/"))
            initial_tab = alias_provider

        # 1. Interactive picker when no args provided
        if not args:
            if ctx.app is not None:
                show_cloud = getattr(ctx.app, "_show_cloud_selector", None)
                if callable(show_cloud):
                    if initial_tab is not None:
                        try:
                            res = show_cloud(initial_tab=initial_tab)
                        except TypeError:
                            res = show_cloud()
                    else:
                        res = show_cloud()
                    if inspect.isawaitable(res):
                        await res
                    return CommandResult(success=True, mount_as_app_message=False)
            return self._handle_status()

        tokens = args.split()
        sub = tokens[0].lower()

        # 2. Subcommands: list, status, auth, mode, use, default
        if sub in ("list", "--list", "-l"):
            prov_filter: CloudProviderType | None = alias_provider
            if "--provider" in tokens:
                idx = tokens.index("--provider")
                if idx + 1 < len(tokens) and tokens[idx + 1].lower() in _VALID_PROVIDERS:
                    prov_filter = cast("CloudProviderType", tokens[idx + 1].lower())
            elif len(tokens) > 1 and tokens[1].lower() in _VALID_PROVIDERS:
                prov_filter = cast("CloudProviderType", tokens[1].lower())
            return self._handle_list(provider=prov_filter)

        if sub in ("status", "--status", "info", "--info"):
            verify = "--verify" in tokens or "-v" in tokens
            return self._handle_status(verify=verify)

        if sub == "auth":
            return self._handle_auth(tokens[1:], default_provider=alias_provider)

        if sub == "mode":
            return self._handle_mode(tokens[1:])

        if args.startswith("--default"):
            return self._handle_default(ctx, args)

        # 3. `/cloud use <provider> <target_id> [--region <region>]`
        if sub == "use":
            return await self._handle_use(ctx, tokens[1:], default_provider=alias_provider)

        # 4. Direct switch:
        #    - `/cloud <provider> <target_id> [--region <region>]`
        #    - `/cloud <provider>:<target_id>`
        #    - `/aws <profile>` / `/azure <sub_id>` / `/gcp <config>` / `/k8s <context>`
        #    - `/cloud <aws_profile>` or `/cloud set <aws_profile>`
        if sub == "set" and len(tokens) > 1:
            tokens = tokens[1:]
            sub = tokens[0].lower()

        if sub in _VALID_PROVIDERS and len(tokens) >= 2:
            return await self._handle_use(
                ctx,
                tokens[1:],
                default_provider=cast("CloudProviderType", sub),
            )

        if ":" in tokens[0]:
            maybe_prov, maybe_target = tokens[0].split(":", 1)
            if maybe_prov.lower() in _VALID_PROVIDERS and maybe_target:
                rest_tokens = [maybe_target, *tokens[1:]]
                return await self._handle_use(
                    ctx,
                    rest_tokens,
                    default_provider=cast("CloudProviderType", maybe_prov.lower()),
                )

        if alias_provider and alias_provider != "aws":
            return await self._handle_use(ctx, tokens, default_provider=alias_provider)

        # Backward-compatible AWS profile switch: `/cloud <profile_name>`
        profile_name = " ".join(tokens).strip()
        if not profile_name:
            return CommandResult(success=False, message="Please specify a cloud target or AWS profile name.")

        info = get_aws_profile_info(profile_name)
        target_region = info.region if info else None

        set_active_aws_profile(profile_name, region=target_region, persist=True)

        if ctx.settings is not None:
            ctx.settings.cloud_provider = "aws"
            ctx.settings.aws_profile = profile_name
            if target_region:
                ctx.settings.aws_region = target_region

        if ctx.app is not None:
            switch_fn = getattr(ctx.app, "_switch_cloud_profile", None)
            if callable(switch_fn):
                res = switch_fn(profile_name, region=target_region)
                if inspect.isawaitable(res):
                    await res

        region_str = f" ({target_region})" if target_region else ""
        return CommandResult(
            success=True,
            message=(
                f"**Switched Cloud Profile:** Active AWS profile set to `{profile_name}`{region_str} "
                "(saved to `config.toml` — active across all sessions)."
            ),
        )

    async def _handle_use(
        self,
        ctx: CommandContext,
        tokens: list[str],
        *,
        default_provider: CloudProviderType | None = None,
    ) -> CommandResult:
        """Handle `/cloud use <provider> <target_id> [--region <region>]`."""
        if not tokens:
            return CommandResult(
                success=False,
                message="Usage: `/cloud use <aws|azure|gcp|k8s> <target_id> [--region <region>]`",
            )

        region_override: str | None = None
        cleaned: list[str] = []
        i = 0
        while i < len(tokens):
            if tokens[i] in ("--region", "-r") and i + 1 < len(tokens):
                region_override = tokens[i + 1]
                i += 2
            else:
                cleaned.append(tokens[i])
                i += 1

        if not cleaned:
            return CommandResult(
                success=False,
                message="Usage: `/cloud use <aws|azure|gcp|k8s> <target_id> [--region <region>]`",
            )

        if cleaned[0].lower() in _VALID_PROVIDERS and len(cleaned) >= 2:
            provider = cast("CloudProviderType", cleaned[0].lower())
            target_id = cleaned[1]
        elif default_provider is not None:
            provider = default_provider
            target_id = cleaned[0]
        else:
            provider = "aws"
            target_id = cleaned[0]

        target_info = get_cloud_target_info(provider, target_id)
        eff_target_id = target_info.target_id if target_info else target_id
        eff_region = region_override or (target_info.region_or_location if target_info else None)

        set_active_cloud_target(provider, eff_target_id, region=eff_region, persist=True)

        if ctx.settings is not None:
            ctx.settings.cloud_provider = provider
            if provider == "aws":
                ctx.settings.aws_profile = eff_target_id
                if eff_region:
                    ctx.settings.aws_region = eff_region

        if ctx.app is not None:
            switch_target_fn = getattr(ctx.app, "_switch_cloud_target", None)
            if callable(switch_target_fn):
                res = switch_target_fn(provider, eff_target_id, region=eff_region)
                if inspect.isawaitable(res):
                    await res
            elif provider == "aws":
                switch_fn = getattr(ctx.app, "_switch_cloud_profile", None)
                if callable(switch_fn):
                    res = switch_fn(eff_target_id, region=eff_region)
                    if inspect.isawaitable(res):
                        await res

        label = _PROVIDER_LABELS.get(provider, provider.upper())
        region_str = f" ({eff_region})" if eff_region else ""
        return CommandResult(
            success=True,
            message=(
                f"**Switched Cloud Context:** Active {label} target set to `{eff_target_id}`{region_str} "
                "(saved to `config.toml` — active across all sessions)."
            ),
        )

    def _handle_list(self, provider: CloudProviderType | None = None) -> CommandResult:
        """List discovered multi-cloud targets (including ambient roles and AWS profiles)."""
        active_prov, active_target, _ = get_active_cloud_target()
        active_aws = get_active_aws_profile()
        default_aws = load_aws_profile()

        lines: list[str] = []

        # Always include Configured AWS Profiles section when unfiltered or provider == "aws"
        if provider in (None, "aws"):
            profiles = list_aws_profiles()
            lines.append("**Configured AWS Profiles:**\n")
            for p in profiles:
                flags = []
                if p.name == active_aws and active_prov == "aws":
                    flags.append("**active**")
                if p.name == default_aws:
                    flags.append("default")
                if getattr(p, "is_ambient", False):
                    flags.append("ambient service role")
                flag_str = f" *({', '.join(flags)})*" if flags else ""

                details = []
                if p.region:
                    details.append(f"region: `{p.region}`")
                if p.account_id:
                    details.append(f"account: `{p.account_id}`")
                elif p.role_arn:
                    details.append("role assumed")
                detail_str = f" — {', '.join(details)}" if details else ""

                lines.append(f"- `{p.name}`{flag_str}{detail_str}")

        # Additional multi-cloud providers (Azure, GCP, K8s)
        other_providers: list[CloudProviderType] = (
            [provider] if provider and provider != "aws" else (["azure", "gcp", "k8s"] if provider is None else [])
        )
        for prov in other_providers:
            targets = list_cloud_targets(prov)
            if not targets and provider is None:
                continue
            header = _PROVIDER_LABELS.get(prov, prov.upper())
            lines.append(f"\n**{header} Targets:**\n")
            if not targets:
                lines.append(f"- *(none discovered)*")
                continue
            for t in targets:
                flags = []
                if active_prov == prov and t.target_id == active_target:
                    flags.append("**active**")
                if t.is_ambient:
                    flags.append("ambient service role")
                flag_str = f" *({', '.join(flags)})*" if flags else ""
                detail_str = f" — {t.display_details}" if t.display_details else ""
                lines.append(f"- `{t.target_id}` ({t.display_name}){flag_str}{detail_str}")

        lines.append("\n*Use `/cloud`, `/cloud use <provider> <target>`, or `/cloud <profile>` to switch.*")
        return CommandResult(success=True, message="\n".join(lines))

    def _handle_status(self, *, verify: bool = False) -> CommandResult:
        """Inspect current multi-cloud provider, target, ambient role, and execution mode."""
        active_prov, active_target, active_region = get_active_cloud_target()
        exec_mode = get_cloud_execution_mode()
        persisted_ctx = load_active_cloud_context()
        persisted_aws = load_aws_profile()

        if active_prov == "aws":
            active = get_active_aws_profile()
            from talkops.config.aws import resolve_aws_identity

            identity = resolve_aws_identity(profile=active, region=active_region)
            lines = [
                "**Current Cloud Context:**",
                "- **Provider:** AWS",
                f"- **Active Profile:** `{active}`",
                f"- **Execution Mode:** `{exec_mode}`",
                f"- **Persisted in config.toml:** `{persisted_aws or 'none (using env/default)'}`",
                f"- **Region:** `{identity.region}`",
            ]
            if active == "@ambient":
                lines.append("- **Credential Source:** `Ambient Attached Service Role (IMDS / ECS / IRSA)`")
            if identity.is_authenticated:
                if identity.account_id:
                    lines.append(f"- **Account ID:** `{identity.account_id}`")
                if identity.arn:
                    lines.append(f"- **Caller ARN:** `{identity.arn}`")
                lines.append("- **Authentication:** [Validated via STS]")
            else:
                lines.append("- **Authentication:** [Not validated / credentials local]")
            return CommandResult(success=True, message="\n".join(lines))

        # Non-AWS active provider (Azure, GCP, K8s)
        status = get_credential_broker().verify_cloud_identity(
            provider=active_prov,
            target_id=active_target,
            region=active_region,
        ) if verify else None

        target_info = get_cloud_target_info(active_prov, active_target)
        label = _PROVIDER_LABELS.get(active_prov, str(active_prov).upper())
        persisted_str = (
            f"{persisted_ctx.get('provider')}:{persisted_ctx.get('target_id')}"
            if persisted_ctx
            else "none (using env/ambient)"
        )
        lines = [
            "**Current Cloud Context:**",
            f"- **Provider:** {label} (`{active_prov}`)",
            f"- **Active Profile:** `{active_target}`",
            f"- **Execution Mode:** `{exec_mode}`",
            f"- **Persisted in config.toml:** `{persisted_str}`",
            f"- **Region / Location:** `{active_region or (target_info.region_or_location if target_info else 'default')}`",
        ]
        if target_info and target_info.account_or_tenant_id:
            lines.append(f"- **Account / Project / Cluster:** `{target_info.account_or_tenant_id}`")
        if target_info and target_info.is_ambient:
            lines.append("- **Credential Source:** `Ambient Attached Service Role / Workload Identity`")
        if status is not None:
            if status.is_authenticated:
                lines.append(f"- **Authentication:** [Validated — `{status.identity_principal or 'OK'}`]")
            else:
                lines.append(f"- **Authentication:** [Failed — {status.error or 'unauthenticated'}]")
                if status.remediation_hint:
                    lines.append(f"- **Remediation:** {status.remediation_hint}")
        return CommandResult(success=True, message="\n".join(lines))

    def _handle_auth(
        self,
        tokens: list[str],
        *,
        default_provider: CloudProviderType | None = None,
    ) -> CommandResult:
        """Verify live authentication for a cloud provider/target and return remediation hints."""
        active_prov, active_target, active_region = get_active_cloud_target()
        provider: CloudProviderType = default_provider or active_prov
        target_id: str | None = None

        if tokens:
            if tokens[0].lower() in _VALID_PROVIDERS:
                provider = cast("CloudProviderType", tokens[0].lower())
                if len(tokens) > 1:
                    target_id = tokens[1]
            else:
                target_id = tokens[0]

        eff_target = target_id or (active_target if provider == active_prov else None)
        status = get_credential_broker().verify_cloud_identity(
            provider=provider,
            target_id=eff_target,
            region=active_region if provider == active_prov else None,
        )
        label = _PROVIDER_LABELS.get(provider, provider.upper())
        lines = [
            f"**Cloud Authentication Check ({label}):**",
            f"- **Target:** `{status.target_id}` ({status.display_name})",
            f"- **Source:** `{status.auth_source}`",
            f"- **Authenticated:** `{'Yes' if status.is_authenticated else 'No'}`",
        ]
        if status.identity_principal:
            lines.append(f"- **Principal:** `{status.identity_principal}`")
        if status.account_or_tenant_id:
            lines.append(f"- **Account / Project / Tenant:** `{status.account_or_tenant_id}`")
        if not status.is_authenticated:
            if status.error:
                lines.append(f"- **Error:** {status.error}")
            if status.remediation_hint:
                lines.append(f"- **Remediation:** {status.remediation_hint}")
        return CommandResult(success=status.is_authenticated, message="\n".join(lines))

    def _handle_mode(self, tokens: list[str]) -> CommandResult:
        """Switch or inspect cloud execution mode (`read_only` vs `elevated`)."""
        if not tokens:
            cur = get_cloud_execution_mode()
            return CommandResult(
                success=True,
                message=(
                    f"**Current Cloud Execution Mode:** `{cur}`\n"
                    "*Use `/cloud mode read-only` or `/cloud mode elevated` to switch.*"
                ),
            )
        raw = tokens[0].lower().strip().replace("-", "_")
        if raw in ("read_only", "readonly", "ro"):
            mode: CloudExecutionMode = "read_only"
        elif raw in ("elevated", "write", "rw", "directed"):
            mode = "elevated"
        else:
            return CommandResult(
                success=False,
                message="Usage: `/cloud mode <read-only|elevated>`",
            )
        set_cloud_execution_mode(mode, persist=True)
        desc = (
            "Read-Only Investigation Mode (mutating cloud commands require explicit approval)"
            if mode == "read_only"
            else "Elevated Directed Action Mode (infrastructure mutations permitted per approval policy)"
        )
        return CommandResult(
            success=True,
            message=f"**Switched Cloud Execution Mode:** `{mode}` — {desc}.",
        )

    def _handle_default(self, ctx: CommandContext, args: str) -> CommandResult:
        """Handle setting or clearing default cloud/AWS profile in config.toml."""
        rest = args[len("--default") :].strip()
        if rest == "--clear":
            clear_aws_profile()
            clear_active_cloud_context()
            return CommandResult(success=True, message="Default AWS profile cleared from config.toml.")

        if rest:
            set_active_aws_profile(rest, persist=True)
            if ctx.settings is not None:
                ctx.settings.aws_profile = rest
            return CommandResult(
                success=True,
                message=f"Default AWS profile set to `{rest}` in config.toml.",
            )

        return CommandResult(
            success=False,
            message="Usage: `/cloud --default <profile_name>` or `/cloud --default --clear`",
        )


__all__ = ["CloudHandler"]

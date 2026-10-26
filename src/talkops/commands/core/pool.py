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

"""Agent model pool management command handler for TalkOps."""

from __future__ import annotations

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.config.toml_config import (
    clear_agent_pool,
    load_agent_pool,
    save_agent_pool,
)
from talkops.model.pool import get_model_pool_manager
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


class PoolHandler(BaseCommandHandler):
    """Handler for /pool command to inspect or configure the agent model execution pool."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/pool"

    @property
    def aliases(self) -> tuple[str, ...]:
        """Return alternative slash command aliases."""
        return ("/agent-pool", "/agent_pool")

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

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute the command handler with the provided command context."""
        args = ctx.args.strip()

        # 1. Clear pool configuration
        if args in ("--clear", "clear"):
            clear_agent_pool()
            pool_mgr = get_model_pool_manager()
            with pool_mgr._lock:
                pool_mgr._instances.clear()
            return CommandResult(
                success=True,
                message="**Agent Model Pool Cleared:** Reverted to automatic dynamic discovery from provider capabilities.",
            )

        # 2. Status inspection
        if args in ("--status", "status"):
            pool_mgr = get_model_pool_manager()
            user_pool = load_agent_pool()
            tiers = pool_mgr.discover_tiers()

            is_custom = user_pool is not None
            if is_custom:
                provs = {t[0].split(":", 1)[0] for t in tiers.values() if ":" in t[0]}
                multi_badge = " [Multi-Provider]" if len(provs) > 1 else f" [{next(iter(provs))}]"
                source_label = f"Custom (`config.toml [agent_pool]`){multi_badge}"
            else:
                source_label = "Automatic Dynamic Discovery"

            msg = (
                f"**Agent Model Pool Status** ({source_label}):\n"
                f"• **Fast (Tier 0):** `{tiers[0][0]}` *(effort: {tiers[0][1]})*\n"
                f"• **Standard (Tier 1):** `{tiers[1][0]}` *(effort: {tiers[1][1]})*\n"
                f"• **Powerful (Tier 2):** `{tiers[2][0]}` *(effort: {tiers[2][1]})*\n\n"
                f"In smart mode, Jev routes tasks across these tiers based on complexity."
            )
            from talkops.middleware.jev_model_router import JevDynamicModelRouterMiddleware

            if not JevDynamicModelRouterMiddleware().is_available():
                msg += (
                    "\n\n⚠ **Warning:** `TYPESAFE_API_KEY` is not configured — "
                    "Smart mode dynamic pool routing is currently inactive. "
                    "Configure it via `/auth` (TypeSafe AI) or `export TYPESAFE_API_KEY=...`."
                )
            return CommandResult(success=True, message=msg)

        # 3. Direct setting via CLI key=value pairs (e.g. /pool set fast=... standard=... powerful=...)
        if args.startswith("set "):
            pairs_str = args[4:].strip()
            updates: dict[str, str] = {}
            for token in pairs_str.split():
                if "=" in token:
                    k, v = token.split("=", 1)
                    k = k.lower().strip()
                    if k in ("fast", "standard", "powerful"):
                        updates[k] = v.strip()

            if updates:
                current = load_agent_pool() or {}
                merged = {**current, **updates}

                from talkops.model.config import (
                    apply_stored_credentials,
                    detect_provider,
                )
                provs = set()
                for v in merged.values():
                    if ":" in v:
                        provs.add(v.split(":", 1)[0])
                    else:
                        inferred = detect_provider(v)
                        if inferred:
                            provs.add(inferred)

                provider_to_save = next(iter(provs)) if len(provs) == 1 else ("multi" if len(provs) > 1 else None)
                save_agent_pool(merged, provider=provider_to_save)

                pool_mgr = get_model_pool_manager()
                with pool_mgr._lock:
                    pool_mgr._instances.clear()

                for p in provs:
                    apply_stored_credentials(p)

                from talkops.model.config import has_provider_credentials
                warnings = []
                for p in provs:
                    if has_provider_credentials(p) is False:
                        warnings.append(f"Notice: Credentials for provider '{p}' are not configured.")
                warn_msg = ("\n\n" + "\n".join(warnings)) if warnings else ""

                return CommandResult(
                    success=True,
                    message=f"**Agent Model Pool Updated:** `{merged}` saved to `config.toml [agent_pool]`.{warn_msg}",
                )
            return CommandResult(
                success=False,
                message="Usage: `/pool set fast=<model> standard=<model> powerful=<model>`",
            )

        # 4. Interactive modal UI in Textual
        if not args or args in ("--ui", "ui"):
            if ctx.app is not None and hasattr(ctx.app, "_show_pool_selector"):
                await ctx.app._show_pool_selector()
                return CommandResult(success=True, mount_as_app_message=False)
            return CommandResult(
                success=True,
                push_screen="PoolSelector",
                mount_as_app_message=False,
            )

        return CommandResult(
            success=False,
            message="Usage: `/pool` (open modal), `/pool status` (inspect tiers), `/pool clear` (reset to auto).",
        )


__all__ = ["PoolHandler"]

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

"""LangChain ``AgentMiddleware`` enforcing TalkOps v2 Execution Trust Profiles & Scope Guards."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage

from talkops.middleware.registry import register_middleware
from talkops.plugins.models import ExecutionTrustProfile, normalize_execution_profile
from talkops.security.execution_profiles import (
    evaluate_tool_call_policy,
    register_talkops_execution_harness_profiles,
    resolve_runtime_execution_profile,
)


@register_middleware("execution_profile_guard")
class ExecutionProfileGuardMiddleware(AgentMiddleware):
    """Intercepts tool calls to enforce ``readonly-metadata``, ``gitops-pr``, ``scopeGuards``, and ``egressAllowlist``."""

    def __init__(
        self,
        *,
        default_profile: str | ExecutionTrustProfile | None = None,
        metadata_only_datastores: bool = False,
        egress_allowlist: Sequence[str] = (),
        scope_guards: Mapping[str, Any] | None = None,
    ) -> None:
        """Initialize ExecutionProfileGuardMiddleware."""
        super().__init__()
        register_talkops_execution_harness_profiles()
        self._explicit_profile: ExecutionTrustProfile | None = (
            normalize_execution_profile(default_profile)
            if isinstance(default_profile, str) and default_profile.strip()
            else None
        )
        self._metadata_only_datastores = metadata_only_datastores
        self._egress_allowlist = tuple(egress_allowlist)
        self._scope_guards = dict(scope_guards or {})

    @staticmethod
    def _is_truthy_flag(val: Any) -> bool:
        if val is True:
            return True
        if isinstance(val, str):
            return val.strip().lower() in {"1", "true", "yes", "on"}
        return False

    @classmethod
    def _extract_runtime_bool(cls, runtime: Any, key: str) -> bool:
        if runtime is None:
            return False
        ctx = getattr(runtime, "context", None)
        if isinstance(ctx, Mapping) and cls._is_truthy_flag(ctx.get(key)):
            return True
        if ctx is not None and not isinstance(ctx, Mapping):
            attr_val = getattr(ctx, key, None)
            if cls._is_truthy_flag(attr_val):
                return True
        cfg = getattr(runtime, "config", None)
        if isinstance(cfg, Mapping):
            conf = cfg.get("configurable")
            if isinstance(conf, Mapping) and cls._is_truthy_flag(conf.get(key)):
                return True
        return False

    def _resolve_active_profile(self, state: Any, runtime: Any = None) -> ExecutionTrustProfile | None:
        """Resolve active execution profile; returns ``None`` if unconstrained local interactive session."""
        import os

        # Operator `--read-only` / `TALKOPS_READ_ONLY=true` is a hard safety ceiling
        if (
            self._is_truthy_flag(os.environ.get("TALKOPS_READ_ONLY"))
            or self._is_truthy_flag(os.environ.get("TALKOPS_SERVER_READ_ONLY"))
            or self._extract_runtime_bool(runtime, "read_only")
        ):
            return "readonly-metadata"

        if isinstance(state, Mapping):
            state_prof = state.get("execution_profile")
            if isinstance(state_prof, str) and state_prof.strip():
                return normalize_execution_profile(state_prof)
        if self._explicit_profile is not None:
            return self._explicit_profile

        env_prof = os.environ.get("TALKOPS_EXECUTION_PROFILE", "").strip()
        if env_prof:
            return normalize_execution_profile(env_prof)

        if (
            self._is_truthy_flag(os.environ.get("TALKOPS_SERVER_TNR"))
            or self._extract_runtime_bool(runtime, "tnr")
        ):
            return "autonomous-tnr"
        return None

    def _check_request(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name", ""))
        tool_args = tool_call.get("args") or {}
        tool_call_id = str(tool_call.get("id", ""))
        tool_meta = (
            getattr(request.tool, "metadata", None)
            if request.tool is not None and isinstance(getattr(request.tool, "metadata", None), Mapping)
            else {}
        )

        active_profile = self._resolve_active_profile(
            request.state,
            getattr(request, "runtime", None),
        )
        has_constraints = (
            active_profile is not None
            or self._metadata_only_datastores
            or bool(self._egress_allowlist)
            or bool(self._scope_guards)
        )
        if not has_constraints:
            return None

        effective_profile: ExecutionTrustProfile = active_profile or resolve_runtime_execution_profile(
            request.state if isinstance(request.state, Mapping) else None,
            default="autonomous-tnr",
        )
        decision = evaluate_tool_call_policy(
            profile=effective_profile,
            tool_name=tool_name,
            tool_args=tool_args if isinstance(tool_args, Mapping) else {},
            tool_metadata=tool_meta,
            metadata_only_datastores=self._metadata_only_datastores,
            egress_allowlist=self._egress_allowlist,
            scope_guards=self._scope_guards,
        )
        if decision.allowed:
            return None

        return ToolMessage(
            content=(
                f"[POLICY_BLOCKED:{decision.violation_code}] {decision.reason}"
            ),
            tool_call_id=tool_call_id,
            name=tool_name,
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """Execute `wrap_tool_call`."""
        blocked = self._check_request(request)
        if blocked is not None:
            return blocked
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """Execute `awrap_tool_call`."""
        blocked = self._check_request(request)
        if blocked is not None:
            return blocked
        return await handler(request)

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

"""Official Deep Agents `HarnessProfile` & structured subagent deliverable helpers for TalkOps.

Bridges Google `Harness²` specialization concepts (`HarnessSpec`, structured subagent
handoffs, and model/provider-specific prompt/tool tuning) onto the native
`deepagents.profiles` (`HarnessProfile`, `register_harness_profile`) and
`SubAgent["response_format"]` APIs in `deepagents>=0.7.29`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    HarnessProfileConfig,
    ProviderProfile,
    register_harness_profile,
    register_provider_profile,
)
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from langchain.agents.middleware.types import AgentMiddleware


class SubagentDeliverable(BaseModel):
    """Structured deliverable schema for `SubAgent['response_format']` handoffs."""

    status: str = Field(
        default="completed",
        description="Execution status: 'completed', 'partial', or 'blocked'.",
    )
    executive_summary: str = Field(
        description="Concise technical summary of findings or actions taken.",
    )
    evidence_citations: list[str] = Field(
        default_factory=list,
        description="Verified evidence locators (`file_path:line_number`, cloud resource ARNs/IDs, or command outputs).",
    )
    recommended_actions: list[str] = Field(
        default_factory=list,
        description="Prioritized, copy-pasteable CLI commands or IaC remediations.",
    )
    unverified_risks: list[str] = Field(
        default_factory=list,
        description="Remaining blast-radius risks, missing permissions, or unverified hypotheses.",
    )


_TALKOPS_PROFILES: dict[str, HarnessProfile] = {}


def register_talkops_harness_profile(
    key: str,
    profile: HarnessProfile | HarnessProfileConfig,
    *,
    register_in_sdk: bool = True,
) -> HarnessProfile:
    """Register a model or provider `HarnessProfile` for TalkOps and Deep Agents.

    Args:
        key: Provider or `provider:model` key (e.g. `'anthropic'`, `'openai:gpt-5.6-luna'`).
        profile: A `HarnessProfile` or `HarnessProfileConfig` instance.
        register_in_sdk: When True, also registers the profile in `deepagents.profiles`
            so `create_deep_agent` applies it automatically.

    Returns:
        The resolved `HarnessProfile` stored under `key`.
    """
    resolved = profile.to_harness_profile() if isinstance(profile, HarnessProfileConfig) else profile
    _TALKOPS_PROFILES[key] = resolved
    if register_in_sdk:
        register_harness_profile(key, resolved)
    return resolved


def get_talkops_harness_profile(key: str) -> HarnessProfile | None:
    """Return a registered TalkOps `HarnessProfile` by key, if present."""
    return _TALKOPS_PROFILES.get(key)


def create_talkops_harness_profile(
    *,
    base_system_prompt: str | None = None,
    system_prompt_suffix: str | None = None,
    tool_description_overrides: Mapping[str, str] | None = None,
    excluded_tools: Sequence[str] = (),
    excluded_middleware: Sequence[type[AgentMiddleware] | str] = (),
    extra_middleware: Sequence[AgentMiddleware] | Callable[[], Sequence[AgentMiddleware]] = (),
    general_purpose_subagent: GeneralPurposeSubagentProfile | None = None,
) -> HarnessProfile:
    """Construct a native Deep Agents `HarnessProfile` with TalkOps defaults."""
    return HarnessProfile(
        base_system_prompt=base_system_prompt,
        system_prompt_suffix=system_prompt_suffix,
        tool_description_overrides=dict(tool_description_overrides or {}),
        excluded_tools=frozenset(excluded_tools),
        excluded_middleware=frozenset(excluded_middleware),
        extra_middleware=extra_middleware,
        general_purpose_subagent=general_purpose_subagent,
    )


__all__ = [
    "GeneralPurposeSubagentProfile",
    "HarnessProfile",
    "HarnessProfileConfig",
    "ProviderProfile",
    "SubagentDeliverable",
    "create_talkops_harness_profile",
    "get_talkops_harness_profile",
    "register_harness_profile",
    "register_provider_profile",
    "register_talkops_harness_profile",
]

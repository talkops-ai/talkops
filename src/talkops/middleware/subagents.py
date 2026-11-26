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

"""Middleware that manages subagent metadata, delegation rules, and system-prompt injection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import SystemMessage

from talkops.middleware.registry import register_middleware
from talkops.subagents.types import SubagentMetadata
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from langgraph.runtime import Runtime


@register_middleware(name="subagents")
class SubagentsMiddleware(AgentMiddleware):
    """Middleware that manages subagent metadata and system-prompt injection.

    Features:
    - Dynamic registration and discovery of subagents.
    - Explicit bifurcation between Built-in subagents (direct `task` tool)
      and Plugin/Extension subagents (Code Interpreter `js_eval` primitive).
    - Preserves subagent registry metadata in state for tools.
    """

    def __init__(
        self,
        subagent_metas: Sequence[SubagentMetadata | Mapping[str, Any]] | None = None,
        *,
        planning_mode: bool = False,
    ) -> None:
        """Initialize SubagentsMiddleware."""
        super().__init__()
        self.planning_mode = planning_mode
        self._registry: dict[str, SubagentMetadata] = {}
        if subagent_metas:
            for meta in subagent_metas:
                name = meta.get("name", "")
                if name:
                    self._registry[name] = dict(meta)  # type: ignore[arg-type]

    @property
    def subagent_names(self) -> list[str]:
        """Return sorted list of registered subagent names."""
        return sorted(self._registry.keys())

    def get_subagent(self, name: str) -> SubagentMetadata | None:
        """Look up a subagent by exact name."""
        return self._registry.get(name)

    def register_subagent(self, meta: SubagentMetadata | Mapping[str, Any]) -> None:
        """Add or replace a subagent registration at runtime."""
        name = meta.get("name", "")
        if not name:
            logger.warning("[SubagentsMiddleware] Cannot register subagent without a name")
            return
        self._registry[name] = dict(meta)  # type: ignore[arg-type]
        logger.debug("[SubagentsMiddleware] Registered subagent: %s", name)

    def list_subagents(self) -> list[SubagentMetadata]:
        """Return all registered subagent metadata dicts."""
        return list(self._registry.values())

    def _is_plugin_subagent(self, meta: SubagentMetadata) -> bool:
        """Determine whether a subagent is plugin-provided based on metadata."""
        name = meta.get("name", "")
        source = meta.get("source", "")
        return (
            "plugin" in source.lower()
            or "@" in name
            or bool(meta.get("is_plugin"))
        )

    @staticmethod
    def _extract_mcp_server_names(meta: SubagentMetadata | Mapping[str, Any]) -> list[str]:
        """Return deduplicated plain-text MCP server names declared for a subagent."""
        explicit = meta.get("mcp_servers")
        names: list[str] = []
        if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
            for item in explicit:
                if isinstance(item, str) and item.strip() and item.strip() not in names:
                    names.append(item.strip())
        if names:
            return names

        mcp_cfg = meta.get("mcp_config")
        if isinstance(mcp_cfg, Mapping):
            inner = mcp_cfg.get("mcpServers") if isinstance(mcp_cfg.get("mcpServers"), Mapping) else mcp_cfg
            for srv_name, srv_val in inner.items():
                if not isinstance(srv_name, str) or not srv_name.strip() or srv_name.startswith("$"):
                    continue
                if isinstance(srv_val, Mapping) and srv_val.get("enabled") is False:
                    continue
                if srv_name not in names:
                    names.append(srv_name)
        return names

    def _build_prompt_block(self) -> str:
        """Build the system-prompt fragment dynamically listing available subagents."""
        if not self._registry:
            return ""

        if self.planning_mode:
            lines: list[str] = ["\n\n## Subagent Delegation & Operational Capabilities\n"]
            lines.append(
                "TalkOps operates with specialized autonomous subagents to execute and verify tasks. "
                "You do NOT execute tasks or run subagents yourself. "
                "Use this context solely to understand the operational capabilities and verification boundaries available when drafting acceptance criteria:\n"
            )
            for name in sorted(self._registry):
                meta = self._registry[name]
                desc = meta.get("description", "No description provided.")
                lines.append(f"- **`{name}`**: {desc}")
            return "\n".join(lines)

        builtin_agents: list[SubagentMetadata] = []
        plugin_agents: list[SubagentMetadata] = []

        for name in sorted(self._registry):
            meta = self._registry[name]
            if self._is_plugin_subagent(meta):
                plugin_agents.append(meta)
            else:
                builtin_agents.append(meta)

        lines: list[str] = ["\n\n## Subagent Delegation & Orchestration Architecture\n"]

        # 1. Built-in Subagents
        if builtin_agents:
            lines.append("### 1. Built-in Subagents (Direct `task` Tool)")
            lines.append(
                'Invoke ONLY built-in subagents directly via `task(description="...", subagent_type="<subagent_name>")`:'
            )
            for meta in builtin_agents:
                name = meta.get("name", "")
                desc = meta.get("description", "No description provided.")
                mode_note = " *(mode: `fork` — inherits parent conversation)*" if meta.get("mode") == "fork" else ""
                lines.append(f"- **`{name}`**: {desc}{mode_note}")

        # 2. Plugin & Extension Subagents
        if plugin_agents:
            if builtin_agents:
                lines.append("")
            lines.append("### 2. Plugin & Extension Subagents (Code Interpreter `js_eval` ONLY)")
            lines.append(
                "Plugin subagents hold **isolated domain MCP servers and specialized skill playbooks that are NOT bound to the main orchestrator**. "
                "Whenever a request, alert, or incident touches a domain covered by an installed plugin subagent below, **you MUST dispatch the matching plugin subagent(s) via `js_eval`** "
                "(`await task({ description, subagentType })`, using `await Promise.all([...])` when multiple domains or targets apply) *before* falling back to generic `execute` CLI probes — NEVER pass plugin subagents to the direct `task` tool:"
            )
            for meta in plugin_agents:
                name = meta.get("name", "")
                desc = meta.get("description", "No description provided.")
                mode_note = " *(mode: `fork` — inherits parent conversation)*" if meta.get("mode") == "fork" else ""
                mcp_names = self._extract_mcp_server_names(meta)
                mcp_note = (
                    f" *(Isolated MCP servers — accessible ONLY via this subagent: "
                    f"{', '.join(f'`{s}`' for s in mcp_names)})*"
                    if mcp_names
                    else ""
                )
                lines.append(f"- **`{name}`**: {desc}{mcp_note}{mode_note}")

        lines.append("\n### Subagent Deliverable Presentation Protocol")
        lines.append(
            "- **Self-Contained Delegation & Adversarial Propose-Probe Verification**: Isolated subagents do not share conversation history—include exact file paths, cloud ARNs/IDs, and account/region/namespace context in every brief (for high-blast-radius production mutations, use a 2-pass propose-probe verification in `js_eval`).\n"
            "- **Preserve Full Technical Deliverables**: **DO NOT compress or suppress subagent reports into brief synopsis bullets.** Retain all structured tables, regional breakdowns, resource inventories, and copy-pasteable CLI commands (`aws ...`, `kubectl ...`, `terraform ...`) in the final response."
        )

        return "\n".join(lines)

    def _filter_direct_task_tool_description(self, tools: Sequence[Any] | None) -> list[Any] | None:
        """Keep plugin subagents out of the top-level `task` tool schema description so models use `js_eval`."""
        if not tools or not self._registry:
            return None
        plugin_names = {
            meta.get("name", "")
            for meta in self._registry.values()
            if self._is_plugin_subagent(meta) and meta.get("name")
        }
        if not plugin_names:
            return None

        updated_tools: list[Any] = []
        changed = False
        for t in tools:
            t_name = t.get("name", "") if isinstance(t, Mapping) else getattr(t, "name", "")
            t_desc = t.get("description", "") if isinstance(t, Mapping) else getattr(t, "description", "")
            if t_name == "task" and isinstance(t_desc, str) and any(pn in t_desc for pn in plugin_names):
                filtered_lines = [
                    line
                    for line in t_desc.splitlines()
                    if not any(line.lstrip().startswith(f"- {pn}:") for pn in plugin_names)
                ]
                new_desc = (
                    "\n".join(filtered_lines).rstrip()
                    + "\n- Note: Plugin/extension subagents are NOT invoked via this direct `task` tool — "
                    "dispatch them via `js_eval` using `await task({ description, subagentType })`."
                )
                if isinstance(t, Mapping):
                    t = {**t, "description": new_desc}
                    changed = True
                elif hasattr(t, "model_copy"):
                    t = t.model_copy(update={"description": new_desc})
                    changed = True
            updated_tools.append(t)
        return updated_tools if changed else None

    @staticmethod
    def _is_disabled_in_runtime(runtime: Any) -> bool:
        ctx = getattr(runtime, "context", None)
        if isinstance(ctx, Mapping) and bool(ctx.get("no_subagents")):
            return True
        if ctx is not None and callable(getattr(ctx, "get", None)) and bool(ctx.get("no_subagents")):
            return True
        return False

    def before_agent(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        """Inject subagent registry metadata into state for reference by tools."""
        if not self._registry or self._is_disabled_in_runtime(runtime):
            return None

        return {
            "_subagent_registry": {
                name: {
                    "name": meta.get("name", ""),
                    "description": meta.get("description", ""),
                    "source": meta.get("source", ""),
                    "mode": meta.get("mode", "isolated"),
                    "skills": meta.get("skills"),
                    "tools": meta.get("tools"),
                    "mcp_servers": self._extract_mcp_server_names(meta),
                    "keywords": meta.get("keywords"),
                    "is_plugin": self._is_plugin_subagent(meta),
                }
                for name, meta in self._registry.items()
            },
        }

    async def abefore_agent(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        """Execute asynchronous pre-agent lifecycle hook."""
        return self.before_agent(state, runtime)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        """Intercept and wrap synchronous model invocation."""
        if self._is_disabled_in_runtime(getattr(request, "runtime", None)):
            return handler(request)
        overrides: dict[str, Any] = {}
        block = self._build_prompt_block()
        if block and request.system_message is not None:
            curr_content = request.system_message.content or ""
            if isinstance(curr_content, str):
                new_content = curr_content + block
            elif isinstance(curr_content, list):
                new_content = list(curr_content) + [block]
            else:
                new_content = block
            overrides["system_message"] = SystemMessage(content=new_content)
        filtered_tools = self._filter_direct_task_tool_description(getattr(request, "tools", None))
        if filtered_tools is not None:
            overrides["tools"] = filtered_tools
        if overrides:
            request = request.override(**overrides)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        """Intercept and wrap asynchronous model invocation."""
        if self._is_disabled_in_runtime(getattr(request, "runtime", None)):
            return await handler(request)
        overrides: dict[str, Any] = {}
        block = self._build_prompt_block()
        if block and request.system_message is not None:
            curr_content = request.system_message.content or ""
            if isinstance(curr_content, str):
                new_content = curr_content + block
            elif isinstance(curr_content, list):
                new_content = list(curr_content) + [block]
            else:
                new_content = block
            overrides["system_message"] = SystemMessage(content=new_content)
        filtered_tools = self._filter_direct_task_tool_description(getattr(request, "tools", None))
        if filtered_tools is not None:
            overrides["tools"] = filtered_tools
        if overrides:
            request = request.override(**overrides)
        return await handler(request)


__all__ = ["SubagentsMiddleware"]

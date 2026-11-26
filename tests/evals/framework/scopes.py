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

"""Five-Scope Agent Flow Isolation Primitives (`tests.evals.framework.scopes`).

Implements all 5 architectural scopes from `docs/agent_evals_enhancement.md`:
- Scope 1 (`BindableFakeChatModel`): Function-scoped deterministic router & loop fakes.
- Scope 2 (`invoke_single_node`, `ainvoke_single_node`, `build_langsmith_node_target`):
  Isolated `compiled_graph.nodes[node_name]` & `.bound` single-step evaluation.
- Scope 3 (`run_checkpoint_seeded_subflow`): Mid-graph checkpoint seeding via
  `aupdate_state(..., as_node=...)`, `interrupt_after`, `Command(resume=...)`, and
  `extract_langgraph_trajectory_from_thread`.
- Scope 4 (`create_ephemeral_deep_agent`, `create_standalone_subagent_runnable`):
  Deep Agents `StateBackend()` ephemeral virtual filesystem (`result['files']`,
  `result['todos']`) and standalone subagent runnables bypassing context quarantine.
- Scope 5 (`run_persona_multiturn_simulation`): Multi-turn `openevals` persona simulator
  with seamless handling of conversational turns and `AskUserMiddleware` interrupts.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agentevals.graph_trajectory.utils import extract_langgraph_trajectory_from_thread
from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain.agents import create_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.runnables import Runnable, RunnableLambda
from langgraph.types import Command
from openevals.simulators import (
    create_llm_simulated_user,
    run_multiturn_simulation_async,
)

from tests.evals.framework.graders import (
    extract_final_ai_text,
    extract_tool_calls,
)

# ── Scope 1: Deterministic Router & Loop Fake Model ──────────────────────────


class BindableFakeChatModel(GenericFakeChatModel):
    """`GenericFakeChatModel` subclass implementing `bind_tools` per Scope 1 guidelines.

    Always instantiate inside a function-scoped fixture or test body so its message
    iterator starts fresh for each scenario.
    """

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Any = None, **kwargs: Any) -> Any:
        """Return `self` unchanged so deterministic tool-calling loops bind cleanly."""
        return self


# ── Scope 2: Single-Step Individual Node Evals (`compiled_graph.nodes[...]`) ─


@dataclass(slots=True)
class SingleNodeEvalResult:
    """Structured state-diff & tool-decision output of a single LangGraph node invocation."""

    node_name: str
    state_patch: dict[str, Any]
    tool_calls: list[dict[str, Any]]
    final_text: str
    updated_keys: set[str]


def _build_node_config(
    config: dict[str, Any] | None,
    context: Any,
) -> dict[str, Any]:
    """Ensure `__pregel_runtime` is populated so `AgentMiddleware` nodes run standalone."""
    from types import SimpleNamespace

    from langgraph._internal._constants import CONF, CONFIG_KEY_RUNTIME

    cfg = dict(config or {})
    conf_dict = dict(cfg.get(CONF) or {})
    if CONFIG_KEY_RUNTIME not in conf_dict:
        conf_dict[CONFIG_KEY_RUNTIME] = SimpleNamespace(
            context=context if context is not None else {},
            store=None,
            stream_writer=lambda *_: None,
            previous=None,
        )
    cfg[CONF] = conf_dict
    return cfg


def invoke_single_node(
    compiled_graph: Any,
    node_name: str,
    input_state: Mapping[str, Any],
    *,
    config: dict[str, Any] | None = None,
    context: Any = None,
) -> SingleNodeEvalResult:
    """Invoke `compiled_graph.nodes[node_name]` synchronously (bypassing checkpointers & downstream nodes)."""
    if node_name not in compiled_graph.nodes:
        available = sorted(compiled_graph.nodes.keys())
        raise KeyError(f"Node {node_name!r} not in compiled_graph.nodes ({available})")
    eff_cfg = _build_node_config(config, context)
    raw_patch = compiled_graph.nodes[node_name].invoke(dict(input_state), config=eff_cfg)
    patch = dict(raw_patch) if isinstance(raw_patch, Mapping) else {"output": raw_patch}
    msgs = patch.get("messages") if isinstance(patch.get("messages"), Sequence) else []
    return SingleNodeEvalResult(
        node_name=node_name,
        state_patch=patch,
        tool_calls=extract_tool_calls(msgs),
        final_text=extract_final_ai_text(msgs),
        updated_keys=set(patch.keys()),
    )


async def ainvoke_single_node(
    compiled_graph: Any,
    node_name: str,
    input_state: Mapping[str, Any],
    *,
    config: dict[str, Any] | None = None,
    context: Any = None,
) -> SingleNodeEvalResult:
    """Invoke `compiled_graph.nodes[node_name]` asynchronously in isolation."""
    if node_name not in compiled_graph.nodes:
        available = sorted(compiled_graph.nodes.keys())
        raise KeyError(f"Node {node_name!r} not in compiled_graph.nodes ({available})")
    eff_cfg = _build_node_config(config, context)
    raw_patch = await compiled_graph.nodes[node_name].ainvoke(dict(input_state), config=eff_cfg)
    patch = dict(raw_patch) if isinstance(raw_patch, Mapping) else {"output": raw_patch}
    msgs = patch.get("messages") if isinstance(patch.get("messages"), Sequence) else []
    return SingleNodeEvalResult(
        node_name=node_name,
        state_patch=patch,
        tool_calls=extract_tool_calls(msgs),
        final_text=extract_final_ai_text(msgs),
        updated_keys=set(patch.keys()),
    )


def build_langsmith_node_target(
    compiled_graph: Any,
    node_name: str,
    state_mapper: Callable[[dict[str, Any]], dict[str, Any]],
) -> Runnable[dict[str, Any], Any]:
    """Compose `state_mapper | compiled_graph.nodes[node_name].bound` for `langsmith.aevaluate`."""
    if node_name not in compiled_graph.nodes:
        raise KeyError(f"Node {node_name!r} not in compiled_graph.nodes")
    return RunnableLambda(state_mapper) | compiled_graph.nodes[node_name].bound


# ── Scope 3: Partial Graph Sub-Flow Evals via Checkpoint State Injection ─────


@dataclass(slots=True)
class SubflowEvalResult:
    """Outcome of a checkpoint-seeded partial graph sub-flow execution."""

    thread_id: str
    config: dict[str, Any]
    final_state: dict[str, Any]
    extracted_trajectory: dict[str, Any]
    visited_steps: list[list[str]]


async def run_checkpoint_seeded_subflow(
    compiled_graph_with_checkpointer: Any,
    *,
    seed_state: Mapping[str, Any],
    as_node: str,
    interrupt_after: Sequence[str] | str | None = None,
    resume_commands: Sequence[Any] = (),
    thread_id: str | None = None,
    context: Any = None,
) -> SubflowEvalResult:
    """Seed mid-graph state at `as_node`, execute slice, optionally `Command(resume=...)`, and extract steps."""
    tid = thread_id or f"eval-subflow-{uuid.uuid4().hex[:10]}"
    cfg: dict[str, Any] = {"configurable": {"thread_id": tid}}

    await compiled_graph_with_checkpointer.aupdate_state(
        config=cfg,
        values=dict(seed_state),
        as_node=as_node,
    )

    invoke_kwargs: dict[str, Any] = {"config": cfg}
    if interrupt_after is not None:
        invoke_kwargs["interrupt_after"] = interrupt_after
    if context is not None:
        invoke_kwargs["context"] = context

    await compiled_graph_with_checkpointer.ainvoke(None, **invoke_kwargs)

    for resume_payload in resume_commands:
        cmd = resume_payload if isinstance(resume_payload, Command) else Command(resume=resume_payload)
        await compiled_graph_with_checkpointer.ainvoke(cmd, **invoke_kwargs)

    # `extract_langgraph_trajectory_from_thread` uses sync `graph.get_state_history(cfg)`;
    # fall back to `aget_state_history` automatically for async-only checkpointers.
    try:
        extracted = extract_langgraph_trajectory_from_thread(compiled_graph_with_checkpointer, cfg)
    except Exception:
        steps: list[list[str]] = []
        async for snap in compiled_graph_with_checkpointer.aget_state_history(cfg):
            meta = getattr(snap, "metadata", None) or {}
            writes = meta.get("writes") if isinstance(meta, Mapping) else None
            if isinstance(writes, Mapping) and writes:
                steps.append(list(writes.keys()))
        steps.reverse()
        extracted = {"outputs": {"results": [], "steps": steps}}

    final_snap = await compiled_graph_with_checkpointer.aget_state(cfg)
    final_vals = dict(getattr(final_snap, "values", None) or {})
    visited = list(extracted.get("outputs", {}).get("steps", []))
    return SubflowEvalResult(
        thread_id=tid,
        config=cfg,
        final_state=final_vals,
        extracted_trajectory=extracted,
        visited_steps=visited,
    )


# ── Scope 4: Deep Agents Ephemeral `StateBackend()` & Standalone Subagents ───


def create_ephemeral_deep_agent(
    *,
    model: str | BaseChatModel,
    system_prompt: str,
    tools: Sequence[Any] = (),
    subagents: Sequence[dict[str, Any]] | None = None,
    enable_todos: bool = True,
    checkpointer: Any = None,
) -> Any:
    """Create a Deep Agent wired to an ephemeral in-memory `StateBackend()` (`result['files']`)."""
    mw = [TodoListMiddleware()] if enable_todos else []
    return create_deep_agent(
        model=model,
        tools=list(tools),
        system_prompt=system_prompt,
        middleware=mw,
        subagents=list(subagents) if subagents else None,
        backend=StateBackend(),
        checkpointer=checkpointer,
    )


def create_standalone_subagent_runnable(
    *,
    model: str | BaseChatModel,
    system_prompt: str,
    tools: Sequence[Any],
    checkpointer: Any = None,
) -> Any:
    """Build an isolated subagent runnable so internal tool calls are evaluated without quarantine blindspots."""
    return create_agent(
        model=model,
        tools=list(tools),
        system_prompt=system_prompt,
        checkpointer=checkpointer,
    )


# ── Scope 5: Multi-Turn Persona Simulation (`openevals`) ─────────────────────


async def run_persona_multiturn_simulation(
    *,
    app_turn_handler: Callable[[dict[str, Any]], Awaitable[dict[str, str]]],
    persona_system_prompt: str,
    simulator_model: BaseChatModel,
    max_turns: int = 3,
    fixed_responses: Sequence[str | dict[str, str]] | None = None,
    trajectory_evaluators: Sequence[Callable[..., Any]] = (),
) -> dict[str, Any]:
    """Execute an async multi-turn persona simulation using `openevals.simulators`."""
    sim_kwargs: dict[str, Any] = {
        "system": persona_system_prompt,
        "client": simulator_model,
    }
    if fixed_responses is not None:
        sim_kwargs["fixed_responses"] = list(fixed_responses)

    sync_user = create_llm_simulated_user(**sim_kwargs)

    async def _async_user(*args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(sync_user, *args, **kwargs)

    return await run_multiturn_simulation_async(
        app=app_turn_handler,
        user=_async_user,
        max_turns=max_turns,
        trajectory_evaluators=list(trajectory_evaluators),
    )

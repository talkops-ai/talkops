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

"""``AgentSessionMiddleware`` — run an active plugin subagent on the session.

When an agent persona is activated (e.g. via ``/agent:<name>`` or ``/agent <name>``),
the TUI submits an ``agent_session_request`` through the agent stream.
This middleware handles it in ``before_agent`` and jumps to ``"end"`` so the main
deep agent never runs and its transcript is never touched:

* ``start``    → activates the persona in graph state and optionally executes an
                 initial prompt with the compiled subagent
* ``continue`` → appends the operator message to ``_agent_session_messages`` and runs
                 the compiled subagent
* ``clear``    → resets the active agent persona and returns control to main deep agent

The subagent transcript is persisted in the private ``_agent_session_messages`` channel,
keeping it invisible to the main coordinator agent.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.errors import GraphBubbleUp

from talkops.middleware.compaction import (
    _extract_message_text,
    _extract_message_tool_calls,
    build_tool_call_summary,
    compute_trajectory_file_interactions,
    ensure_not_starting_with_tool_call,
    middle_truncate_bytes,
    remove_unpaired_tool_calls_and_results,
)
from talkops.middleware.registry import register_middleware
from talkops.state.agent_session_channels import AgentSessionChannels
from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from langgraph.runtime import Runtime

logger = get_logger(__name__)

AGENT_SESSION_NODE_PREFIX = "AgentSessionMiddleware."
"""Stream namespace prefix for nested agent session events (used by TextualAgentAdapter)."""

AGENT_HANDOFF_SYSTEM_PROMPT = (
    "You are an expert technical assistant summarizing a completed specialist agent session "
    "for the main coordinator agent. Be concise, factual, and grounded strictly in the session history. "
    "Do NOT call any tools."
)

AGENT_HANDOFF_USER_PROMPT = (
    "Summarize what the specialist agent `{agent_name}` accomplished in this session so the main "
    "coordinator agent has full context without clutter. Provide a concise markdown bullet list covering:\n"
    "- **Requests Handled**: What the user asked the specialist agent to do.\n"
    "- **Actions & Files**: Key tools executed or files inspected/modified (if any).\n"
    "- **Outcome & Findings**: Key conclusions, changes made, or remaining follow-ups.\n"
    "Keep the summary brief (under 200 words) and do NOT call any tools."
)


def generate_agent_handoff_fallback_summary(
    agent_name: str,
    messages: Sequence[Any],
    *,
    max_response_bytes: int = 1500,
) -> str:
    """Build a deterministic, concise handoff summary from specialist session messages."""
    user_requests: list[str] = []
    tool_actions: list[str] = []
    assistant_texts: list[str] = []

    for msg in messages:
        role = (
            str(msg.get("role", msg.get("type", ""))).lower()
            if isinstance(msg, dict)
            else getattr(msg, "type", type(msg).__name__).lower()
        )
        text = _extract_message_text(msg).strip()
        if role in ("human", "user", "humanmessage"):
            if text:
                req_short, _, _, _ = middle_truncate_bytes(
                    text.replace("\n", " "),
                    300,
                    marker_format=" ... ",
                )
                user_requests.append(req_short)
        elif role in ("ai", "assistant", "aimessage"):
            for tc in _extract_message_tool_calls(msg):
                tc_entry, _ = build_tool_call_summary(tc, max_arg_bytes=120, truncate=True)
                tool_actions.append(f"`{tc_entry['name']}({tc_entry['arguments_json']})`")
            if text:
                assistant_texts.append(text)

    file_interactions = compute_trajectory_file_interactions(messages, max_items=10)

    bullets: list[str] = []
    if user_requests:
        reqs_joined = "; ".join(user_requests[-5:])
        bullets.append(f"- **Requests Handled**: {reqs_joined}")
    if tool_actions:
        bullets.append(f"- **Tools Executed**: {', '.join(tool_actions[-10:])}")
    if file_interactions:
        files_inline = ", ".join(item.lstrip("- ").strip() for item in file_interactions[:10])
        bullets.append(f"- **Files Interacted**: {files_inline}")
    if assistant_texts:
        last_reply, _, _, _ = middle_truncate_bytes(
            assistant_texts[-1],
            max_response_bytes,
            marker_format=" ... [truncated] ... ",
        )
        bullets.append(f"- **Outcome & Findings**: {last_reply}")
    elif not bullets:
        bullets.append(f"- Specialist agent `{agent_name}` session exited with {len(messages)} message(s).")

    return "\n".join(bullets)


def generate_agent_handoff_summary(
    model: Any | None,
    agent_name: str,
    messages: Sequence[Any],
) -> str:
    """Synchronously generate a concise specialist agent handoff summary (LLM with deterministic fallback)."""
    fallback = generate_agent_handoff_fallback_summary(agent_name, messages)
    if model is None or not messages or not hasattr(model, "invoke"):
        return fallback

    try:
        cleaned = ensure_not_starting_with_tool_call(remove_unpaired_tool_calls_and_results(messages))
        req_messages: list[BaseMessage] = [
            SystemMessage(content=AGENT_HANDOFF_SYSTEM_PROMPT),
            *[
                m if isinstance(m, BaseMessage) else HumanMessage(content=_extract_message_text(m))
                for m in cleaned
            ],
            HumanMessage(content=AGENT_HANDOFF_USER_PROMPT.format(agent_name=agent_name)),
        ]
        try:
            response = model.invoke(
                req_messages,
                config={"metadata": {"lc_source": "summarization"}},
            )
        except TypeError:
            response = model.invoke(req_messages)
        if _extract_message_tool_calls(response):
            return fallback
        summary_text = _extract_message_text(response).strip()
        return summary_text if summary_text else fallback
    except Exception as exc:
        logger.debug("LLM agent handoff summary failed, using deterministic fallback: %s", exc)
        return fallback


async def agenerate_agent_handoff_summary(
    model: Any | None,
    agent_name: str,
    messages: Sequence[Any],
) -> str:
    """Asynchronously generate a concise specialist agent handoff summary (LLM with deterministic fallback)."""
    fallback = generate_agent_handoff_fallback_summary(agent_name, messages)
    if model is None or not messages or not hasattr(model, "ainvoke"):
        return fallback

    try:
        cleaned = ensure_not_starting_with_tool_call(remove_unpaired_tool_calls_and_results(messages))
        req_messages: list[BaseMessage] = [
            SystemMessage(content=AGENT_HANDOFF_SYSTEM_PROMPT),
            *[
                m if isinstance(m, BaseMessage) else HumanMessage(content=_extract_message_text(m))
                for m in cleaned
            ],
            HumanMessage(content=AGENT_HANDOFF_USER_PROMPT.format(agent_name=agent_name)),
        ]
        try:
            response = await model.ainvoke(
                req_messages,
                config={"metadata": {"lc_source": "summarization"}},
            )
        except TypeError:
            response = await model.ainvoke(req_messages)
        if _extract_message_tool_calls(response):
            return fallback
        summary_text = _extract_message_text(response).strip()
        return summary_text if summary_text else fallback
    except Exception as exc:
        logger.debug("Async LLM agent handoff summary failed, using deterministic fallback: %s", exc)
        return fallback


def build_agent_handoff_message(agent_name: str, summary_body: str) -> AIMessage:
    """Wrap a specialist agent handoff summary in a single coordinator AIMessage."""
    clean_agent = (agent_name or "specialist-agent").strip()
    content = (
        f'<AGENT_HANDOFF_SUMMARY agent="{clean_agent}">\n'
        f"Specialist agent `{clean_agent}` completed its session and returned control to the coordinator.\n\n"
        f"{summary_body.strip()}\n"
        "</AGENT_HANDOFF_SUMMARY>"
    )
    return AIMessage(
        content=content,
        additional_kwargs={
            "lc_source": "summarization",
            "agent_handoff": True,
            "agent_name": clean_agent,
        },
    )


def _cleared_agent_session() -> dict[str, Any]:
    return {
        "_active_agent_id": None,
        "_active_agent_plugin_id": None,
        "_active_agent_description": None,
        "_agent_session_messages": {"reset": True},
    }


@register_middleware(name="agent_session")
class AgentSessionMiddleware(AgentMiddleware[AgentSessionChannels, Any]):
    """Dispatch typed agent session requests to the compiled plugin subagent."""

    state_schema = AgentSessionChannels

    def __init__(
        self,
        agent_runnables: dict[str, Any] | None = None,
        *,
        model: Any | None = None,
    ) -> None:
        """Initialize with mapping of agent names to their compiled subagent graphs.

        Args:
            agent_runnables: Mapping of agent names/short names to compiled runnables.
            model: Optional chat model used to generate concise handoff summaries on exit.
        """
        super().__init__()
        self._agents: dict[str, Any] = dict(agent_runnables or {})
        self._model: Any | None = model

    def _build_handoff_message_if_needed(
        self,
        prev_agent: str | None,
        prev_messages: Sequence[Any] | None,
    ) -> AIMessage | None:
        """Synchronously build a coordinator handoff message if the prior session had messages."""
        msgs = list(prev_messages or [])
        if not msgs:
            return None
        agent_label = (prev_agent or "specialist-agent").strip()
        summary = generate_agent_handoff_summary(self._model, agent_label, msgs)
        return build_agent_handoff_message(agent_label, summary)

    async def _abuild_handoff_message_if_needed(
        self,
        prev_agent: str | None,
        prev_messages: Sequence[Any] | None,
    ) -> AIMessage | None:
        """Asynchronously build a coordinator handoff message if the prior session had messages."""
        msgs = list(prev_messages or [])
        if not msgs:
            return None
        agent_label = (prev_agent or "specialist-agent").strip()
        summary = await agenerate_agent_handoff_summary(self._model, agent_label, msgs)
        return build_agent_handoff_message(agent_label, summary)

    def register_agent(self, name: str, runnable: Any) -> None:
        """Register or override a compiled subagent runnable."""
        self._agents[name] = runnable
        short = name.rsplit(":", 1)[-1]
        if "@" in short:
            short = short.split("@", 1)[0]
        self._agents[short] = runnable
        self._agents[name.lower()] = runnable
        self._agents[short.lower()] = runnable

    def _resolve_agent(self, name: str) -> Any | None:
        """Look up runnable by exact name, short name, or case-insensitive."""
        if not name:
            return None
        candidate = self._agents.get(name)
        if candidate is not None:
            return candidate
        clean = name.strip()
        candidate = self._agents.get(clean.lower())
        if candidate is not None:
            return candidate
        short = clean.rsplit(":", 1)[-1]
        if "@" in short:
            short = short.split("@", 1)[0]
        candidate = self._agents.get(short)
        if candidate is not None:
            return candidate
        return self._agents.get(short.lower())

    @staticmethod
    def _finish(update: dict[str, Any]) -> dict[str, Any]:
        return {
            "_agent_session_error": None,
            **update,
            "agent_session_request": None,
            "_agent_session_turn": True,
            "jump_to": "end",
        }

    @staticmethod
    def _emit_error_event(runtime: Any, error_msg: str, agent_name: str = "") -> None:
        if runtime is None:
            return
        writer = getattr(runtime, "stream_writer", None)
        if not callable(writer) and hasattr(runtime, "config"):
            cfg = getattr(runtime, "config", None)
            if isinstance(cfg, dict):
                pregel_rt = cfg.get("configurable", {}).get("__pregel_runtime")
                writer = getattr(pregel_rt, "stream_writer", None)
        if callable(writer):
            try:
                writer({"type": "agent_session_error", "error": error_msg, "agent_name": agent_name})
            except Exception:
                pass

    # ── operations ───────────────────────────────────────

    def _execute_turn(
        self,
        request: dict[str, Any],
        values: dict[str, Any],
        runtime: Runtime[Any],
        is_async: bool = False,
    ) -> Any:
        kind = request.get("kind")
        prev_active = values.get("_active_agent_id")
        prev_messages = list(values.get("_agent_session_messages") or [])

        if kind == "clear":
            if is_async:
                return self._async_clear_session(prev_active, prev_messages)
            cleared = _cleared_agent_session()
            handoff_msg = self._build_handoff_message_if_needed(prev_active, prev_messages)
            if handoff_msg is not None:
                cleared["messages"] = [handoff_msg]
            return self._finish(cleared)

        agent_name = str(request.get("agent_name") or prev_active or "").strip()
        plugin_id = request.get("plugin_id") or values.get("_active_agent_plugin_id")
        agent_runnable = self._resolve_agent(agent_name)

        excluded_state_keys = {
            "messages",
            "agent_session_request",
            "_agent_session_messages",
            "_agent_session_turn",
            "_agent_session_error",
            "_active_agent_id",
            "_active_agent_plugin_id",
            "_active_agent_description",
            # Exclude goal/rubric and large skill cache channels
            # so they do not bloat or leak into the isolated subagent checkpoint state.
            "goal_criteria_request",
            "_goal_objective",
            "_goal_status",
            "_goal_rubric",
            "_goal_status_note",
            "_pending_goal_objective",
            "_pending_goal_rubric",
            "_pending_goal_kind",
            "_pending_goal_request_id",
            "_pending_goal_completion_note",
            "rubric",
            "_sticky_rubric",
            "_rubric_status",
            "_current_grading_run_id",
            "skills_metadata",
        }
        inherited_state = {
            k: v for k, v in values.items()
            if k not in excluded_state_keys and v is not None
        }

        if kind == "start":
            prompt = request.get("prompt")
            base = {
                "_active_agent_id": agent_name,
                "_active_agent_plugin_id": plugin_id,
            }
            if not prompt:
                # Activation only, no LLM call yet
                if is_async and prev_messages:
                    return self._async_activate_only_with_handoff(base, prev_active, prev_messages)
                update_activate: dict[str, Any] = {
                    **base,
                    "_agent_session_messages": {"reset": True},
                }
                handoff_msg = self._build_handoff_message_if_needed(prev_active, prev_messages)
                if handoff_msg is not None:
                    update_activate["messages"] = [handoff_msg]
                return self._finish(update_activate)

            if agent_runnable is None:
                logger.warning("Plugin agent runnable for '%s' not found", agent_name)
                return self._finish(base)

            input_data = {**inherited_state, "messages": [HumanMessage(content=str(prompt))]}
            context = getattr(runtime, "context", None)
            if is_async:
                # Caller will await
                return self._async_call_subagent(
                    agent_runnable,
                    input_data,
                    context,
                    base,
                    reset=True,
                    prev_active=prev_active,
                    prev_messages=prev_messages,
                )
            handoff_msg = self._build_handoff_message_if_needed(prev_active, prev_messages)
            result = agent_runnable.invoke(input_data, context=context)
            messages = result.get("messages", [HumanMessage(content=str(prompt))])
            update = {
                **base,
                "_agent_session_messages": {"reset": True, "messages": list(messages)},
            }
            if handoff_msg is not None:
                update["messages"] = [handoff_msg]
            transfers = result.get("_session_cost_transfers")
            if isinstance(transfers, dict) and transfers:
                update["_session_cost_transfers"] = transfers
            return self._finish(update)

        if kind == "continue":
            message = str(request.get("message") or "")
            base = {
                "_active_agent_id": agent_name,
                "_active_agent_plugin_id": plugin_id,
            }
            if agent_runnable is None:
                logger.warning("Plugin agent runnable for '%s' not found", agent_name)
                return self._finish(base)

            force_reset = bool(request.get("reset")) or (
                bool(agent_name) and prev_active != agent_name
            )
            history = [] if force_reset else list(prev_messages)
            new_msg = HumanMessage(content=message)
            input_data = {**inherited_state, "messages": [*history, new_msg]}
            context = getattr(runtime, "context", None)
            if is_async:
                return self._async_call_subagent(
                    agent_runnable,
                    input_data,
                    context,
                    base,
                    reset=force_reset,
                    prev_active=prev_active if force_reset else None,
                    prev_messages=prev_messages if force_reset else None,
                )
            handoff_msg = (
                self._build_handoff_message_if_needed(prev_active, prev_messages)
                if force_reset
                else None
            )
            result = agent_runnable.invoke(input_data, context=context)
            messages = list(result.get("messages", [*history, new_msg]))
            update = {
                **base,
                "_agent_session_messages": (
                    {"reset": True, "messages": messages} if force_reset else messages
                ),
            }
            if handoff_msg is not None:
                update["messages"] = [handoff_msg]
            transfers = result.get("_session_cost_transfers")
            if isinstance(transfers, dict) and transfers:
                update["_session_cost_transfers"] = transfers
            return self._finish(update)

        logger.warning("Unknown agent_session_request kind: %r", kind)
        return self._finish({})

    async def _async_clear_session(
        self,
        prev_active: str | None,
        prev_messages: Sequence[Any] | None,
    ) -> dict[str, Any]:
        cleared = _cleared_agent_session()
        handoff_msg = await self._abuild_handoff_message_if_needed(prev_active, prev_messages)
        if handoff_msg is not None:
            cleared["messages"] = [handoff_msg]
        return self._finish(cleared)

    async def _async_activate_only_with_handoff(
        self,
        base: dict[str, Any],
        prev_active: str | None,
        prev_messages: Sequence[Any] | None,
    ) -> dict[str, Any]:
        update_activate: dict[str, Any] = {
            **base,
            "_agent_session_messages": {"reset": True},
        }
        handoff_msg = await self._abuild_handoff_message_if_needed(prev_active, prev_messages)
        if handoff_msg is not None:
            update_activate["messages"] = [handoff_msg]
        return self._finish(update_activate)

    async def _async_call_subagent(
        self,
        agent_runnable: Any,
        input_data: dict[str, Any],
        context: Any,
        base: dict[str, Any],
        reset: bool,
        prev_active: str | None = None,
        prev_messages: Sequence[Any] | None = None,
    ) -> dict[str, Any]:
        handoff_msg = (
            await self._abuild_handoff_message_if_needed(prev_active, prev_messages)
            if reset and prev_messages
            else None
        )
        result = await agent_runnable.ainvoke(input_data, context=context)
        raw_msgs = result.get("messages")
        messages = list(raw_msgs) if isinstance(raw_msgs, list) else list(input_data["messages"])
        update = {
            **base,
            "_agent_session_messages": {"reset": True, "messages": messages} if reset else messages,
        }
        if handoff_msg is not None:
            update["messages"] = [handoff_msg]
        transfers = result.get("_session_cost_transfers")
        if isinstance(transfers, dict) and transfers:
            update["_session_cost_transfers"] = transfers
        return self._finish(update)

    @staticmethod
    def _passthrough_cleanup(values: dict[str, Any]) -> dict[str, Any] | None:
        """Clean up transient flags and any orphaned agent session messages on main agent turns."""
        early: dict[str, Any] = {}
        if values.get("_agent_session_turn"):
            early["_agent_session_turn"] = False
        if not values.get("_active_agent_id") and values.get("_agent_session_messages"):
            early["_agent_session_messages"] = {"reset": True}
        return early or None

    @staticmethod
    async def _close_subagent_mcp_if_needed(
        request: dict[str, Any],
        values: dict[str, Any],
    ) -> None:
        """Close active subagent MCP stdio sessions when clearing or switching agents."""
        kind = request.get("kind")
        prev_active = values.get("_active_agent_id")
        target_agent = request.get("agent_name")
        if kind == "clear":
            to_close: str | None = None
        elif prev_active and target_agent and prev_active != target_agent:
            to_close = str(prev_active)
        elif request.get("reset"):
            to_close = str(prev_active) if prev_active else None
        else:
            return

        try:
            from talkops.mcp.session_manager import MCPSessionManager

            if MCPSessionManager._instance is not None:
                await MCPSessionManager._instance.close_subagent_sessions(to_close)
        except Exception:
            logger.debug("Failed closing subagent MCP sessions", exc_info=True)

    # ── hooks ────────────────────────────────────────────

    @hook_config(can_jump_to=["end"])
    def before_agent(self, state: AgentSessionChannels, runtime: Runtime[Any]) -> dict[str, Any] | None:
        """Handle a pending agent session request synchronously."""
        values: dict[str, Any] = dict(state)
        request = values.get("agent_session_request")
        if not isinstance(request, dict):
            return self._passthrough_cleanup(values)
        try:
            return self._execute_turn(request, values, runtime, is_async=False)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            logger.exception("Agent session request failed: %s", exc)
            agent_name = str(request.get("agent_name") or values.get("_active_agent_id") or "").strip()
            plugin_id = request.get("plugin_id") or values.get("_active_agent_plugin_id")
            self._emit_error_event(runtime, str(exc), agent_name)
            return self._finish({
                "_active_agent_id": agent_name or None,
                "_active_agent_plugin_id": plugin_id,
                "_agent_session_error": str(exc),
            })

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(self, state: AgentSessionChannels, runtime: Runtime[Any]) -> dict[str, Any] | None:
        """Handle a pending agent session request asynchronously."""
        values: dict[str, Any] = dict(state)
        request = values.get("agent_session_request")
        if not isinstance(request, dict):
            return self._passthrough_cleanup(values)
        try:
            await self._close_subagent_mcp_if_needed(request, values)
            res = self._execute_turn(request, values, runtime, is_async=True)
            if hasattr(res, "__await__"):
                return await res
            return res
        except GraphBubbleUp:
            raise
        except Exception as exc:
            logger.exception("Agent session async request failed: %s", exc)
            agent_name = str(request.get("agent_name") or values.get("_active_agent_id") or "").strip()
            plugin_id = request.get("plugin_id") or values.get("_active_agent_plugin_id")
            self._emit_error_event(runtime, str(exc), agent_name)
            return self._finish({
                "_active_agent_id": agent_name or None,
                "_active_agent_plugin_id": plugin_id,
                "_agent_session_error": str(exc),
            })


__all__ = [
    "AGENT_HANDOFF_SYSTEM_PROMPT",
    "AGENT_HANDOFF_USER_PROMPT",
    "AGENT_SESSION_NODE_PREFIX",
    "AgentSessionMiddleware",
    "agenerate_agent_handoff_summary",
    "build_agent_handoff_message",
    "generate_agent_handoff_fallback_summary",
    "generate_agent_handoff_summary",
]

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

"""Compact and offload command handler for TalkOps.

Implements on-demand ``/compact`` and ``/offload`` conversation compaction:
- Parses ``/compact [custom_instructions]`` via ``parse_compact_request``.
- Flushes all prior thread steps to dual line-aligned JSONL transcripts
  (``.system_generated/logs/transcript.jsonl`` and ``transcript_full.jsonl``)
  via ``sync_messages_to_transcript`` before removing any messages from
  LangGraph state, ensuring zero data loss.
- Enforces tool-call pairing and turn invariants on the kept window via
  ``remove_unpaired_tool_calls_and_results`` and
  ``ensure_not_starting_with_tool_call``.
- Generates a structured 6-section continuation ``<summary>...</summary>``
  (with ``ON-DEMAND COMPACTION`` instructions when provided) via the active LLM
  or deterministic ``generate_fallback_summary``, wrapped in the
  ``<CONTEXT_SUMMARY>`` envelope.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
import uuid
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.messages.utils import convert_to_messages

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


class CompactHandler(BaseCommandHandler):
    """Handler for /compact and /offload commands to summarize and free context space."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/compact"

    @property
    def aliases(self) -> tuple[str, ...]:
        """Return alternative slash command aliases."""
        return ("/offload",)

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
        return BypassTier.QUEUED

    @property
    def model_facing_description(self) -> str | None:
        """Return the description exposed to the LLM for command recommendations."""
        return (
            "Recommend this when the conversation is getting long and the user "
            "wants to compact or summarize the context to free up the context window."
        )

    @staticmethod
    def _resolve_custom_instructions(ctx: CommandContext) -> str:
        if ctx.args and ctx.args.strip():
            return ctx.args.strip()
        if ctx.raw_command and ctx.raw_command.strip():
            from talkops.middleware.compaction import parse_compact_request

            _, instructions = parse_compact_request(ctx.raw_command)
            if instructions:
                return instructions
        return ""

    @staticmethod
    def _resolve_agent(ctx: CommandContext) -> Any | None:
        if ctx.agent is not None:
            return ctx.agent
        if ctx.app is not None:
            for attr in ("_client", "_agent", "_agent_graph"):
                candidate = getattr(ctx.app, attr, None)
                if candidate is not None:
                    return candidate
            adapter = getattr(ctx.app, "_adapter", None)
            if adapter is not None:
                client = getattr(adapter, "_client", None)
                if client is not None:
                    return client
        return None

    @staticmethod
    def _resolve_active_model(ctx: CommandContext) -> Any | None:
        if ctx.app is not None:
            for attr in ("_active_model", "_model", "model"):
                candidate = getattr(ctx.app, attr, None)
                if candidate is not None and (
                    hasattr(candidate, "ainvoke") or hasattr(candidate, "invoke")
                ):
                    return candidate
        agent = CompactHandler._resolve_agent(ctx)
        if agent is not None:
            for attr in ("model", "_model"):
                candidate = getattr(agent, attr, None)
                if candidate is not None and (
                    hasattr(candidate, "ainvoke") or hasattr(candidate, "invoke")
                ):
                    return candidate
        return None

    @classmethod
    async def _aresolve_active_model(cls, ctx: CommandContext) -> Any | None:
        model = cls._resolve_active_model(ctx)
        if model is not None:
            return model
        spec = ctx.model_spec
        if not spec and ctx.app is not None:
            raw_app_model = getattr(ctx.app, "_model", None)
            if isinstance(raw_app_model, str) and raw_app_model.strip():
                spec = raw_app_model.strip()
        if isinstance(spec, str) and spec.strip():
            clean_spec = spec.strip()
            try:
                from talkops.model.factory import create_model

                # Prefer low/disabled reasoning effort for fast non-blocking compaction summaries
                try:
                    res = await asyncio.to_thread(
                        create_model,
                        clean_spec,
                        extra_kwargs={"reasoning_effort": "low"},
                    )
                except Exception:
                    res = await asyncio.to_thread(create_model, clean_spec)
                return getattr(res, "model", res)
            except Exception as exc:
                logger.debug("Could not resolve active model for spec %r: %s", spec, exc)
        return None

    @classmethod
    def _prepare_summary_window(cls, to_compact: list[Any], *, max_tokens: int = 120_000) -> list[Any]:
        """Apply Tier-1 tool/read hygiene and bound summary input tokens so LLM compaction stays fast."""
        if not to_compact:
            return []
        from talkops.middleware.compaction import (
            deduplicate_unchanged_file_reads,
            ensure_not_starting_with_tool_call,
            microcompact_tool_messages,
            remove_unpaired_tool_calls_and_results,
        )

        hygiened = microcompact_tool_messages(deduplicate_unchanged_file_reads(to_compact))
        if cls._count_tokens(hygiened) <= max_tokens or len(hygiened) <= 8:
            return list(hygiened)

        # Keep the earliest user objective message plus the most recent messages within max_tokens
        first_msg = hygiened[0]
        budget = max(4_000, max_tokens - cls._count_tokens([first_msg]))
        kept_rev: list[Any] = []
        running = 0
        for msg in reversed(hygiened[1:]):
            msg_tokens = cls._count_tokens([msg])
            if kept_rev and running + msg_tokens > budget:
                break
            kept_rev.append(msg)
            running += msg_tokens
        tail = ensure_not_starting_with_tool_call(
            remove_unpaired_tool_calls_and_results(list(reversed(kept_rev)))
        )
        return [first_msg, *tail] if tail else [first_msg, *hygiened[-4:]]

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute the command handler with the provided command context."""
        if (
            ctx.app is not None
            and hasattr(ctx.app, "_agent_running")
            and ctx.app._agent_running
            and not getattr(ctx.app, "_compact_in_flight", False)
        ):
            return CommandResult(success=False, message="Cannot compact while agent is running.")

        agent = self._resolve_agent(ctx)
        thread_id: str | None = ctx.thread_id
        if ctx.app is not None and not thread_id:
            thread_id = getattr(ctx.app, "_agent_thread_id", None)

        checkpoint_messages: list[Any] = []
        ui_messages: list[Any] = []
        prior_event: Any = None

        if ctx.app is not None:
            get_state_fn = getattr(ctx.app, "_get_thread_state_values", None)
            if inspect.iscoroutinefunction(get_state_fn):
                try:
                    state_values = await get_state_fn(thread_id)
                    if isinstance(state_values, dict):
                        raw_ckpt = state_values.get("messages")
                        if isinstance(raw_ckpt, list):
                            checkpoint_messages = list(raw_ckpt)
                        prior_event = state_values.get("_summarization_event")
                except Exception as exc:
                    logger.debug("Failed fetching checkpoint state values from app: %s", exc)

            if not checkpoint_messages and hasattr(ctx.app, "get_thread_messages"):
                try:
                    raw_ui = ctx.app.get_thread_messages()
                    if isinstance(raw_ui, list):
                        ui_messages = list(raw_ui)
                except Exception as exc:
                    logger.debug("Failed getting thread messages from UI: %s", exc)

        if (
            not checkpoint_messages
            and agent is not None
            and thread_id
            and (not ui_messages or not any(getattr(m, "id", None) for m in ui_messages))
        ):
            try:
                state = await agent.aget_state({"configurable": {"thread_id": thread_id}})
                state_vals = getattr(state, "values", None)
                if isinstance(state_vals, dict):
                    raw_ckpt = state_vals.get("messages")
                    if isinstance(raw_ckpt, list):
                        checkpoint_messages = list(raw_ckpt)
                    prior_event = state_vals.get("_summarization_event")
            except Exception as exc:
                logger.debug("Failed fetching state from agent: %s", exc)

        if checkpoint_messages and len(checkpoint_messages) >= len(ui_messages):
            raw_messages = checkpoint_messages
        else:
            raw_messages = ui_messages or checkpoint_messages
            prior_event = None

        if any(isinstance(m, dict) for m in raw_messages):
            with contextlib.suppress(Exception):
                raw_messages = await asyncio.to_thread(
                    lambda: list(convert_to_messages(raw_messages))
                )

        from talkops.middleware.btw import is_ephemeral_message

        raw_messages = [m for m in raw_messages if not is_ephemeral_message(m)]

        messages: list[Any] = list(raw_messages)
        if isinstance(prior_event, dict):
            old_cutoff = prior_event.get("cutoff_index")
            old_summary = prior_event.get("summary_message")
            if (
                isinstance(old_cutoff, int)
                and 0 < old_cutoff <= len(raw_messages)
                and old_summary is not None
            ):
                if isinstance(old_summary, dict):
                    with contextlib.suppress(Exception):
                        old_summary = convert_to_messages([old_summary])[0]
                messages = [old_summary, *raw_messages[old_cutoff:]]

        if len(messages) < 4:
            return CommandResult(
                success=False,
                message="Not enough messages to compact (minimum 4 messages required).",
            )

        effective_thread_id = thread_id or "default"
        custom_instructions = self._resolve_custom_instructions(ctx)

        from talkops.middleware.compaction import (
            CheckpointConfig,
            CheckpointRetryConfig,
            agenerate_llm_continuation_summary,
            build_context_summary_envelope,
            ensure_not_starting_with_tool_call,
            remove_unpaired_tool_calls_and_results,
            sync_messages_to_transcript,
        )

        app_has_running_flag = ctx.app is not None and hasattr(ctx.app, "_agent_running")
        set_spinner_fn = getattr(ctx.app, "_set_spinner", None) if ctx.app is not None else None
        has_async_spinner = inspect.iscoroutinefunction(set_spinner_fn)

        if app_has_running_flag:
            with contextlib.suppress(Exception):
                setattr(ctx.app, "_agent_running", True)
        if has_async_spinner and set_spinner_fn is not None:
            with contextlib.suppress(Exception):
                await set_spinner_fn("Compacting conversation...")

        try:
            # 1. Persist 100% of current messages to dual line-aligned JSONL transcripts
            #    (transcript.jsonl + transcript_full.jsonl) before compacting.
            compact_transcript_path, full_transcript_path = await asyncio.to_thread(
                sync_messages_to_transcript,
                raw_messages,
                effective_thread_id,
            )

            before_tokens = await asyncio.to_thread(self._count_tokens, messages)
            start_t = time.perf_counter()

            # 2. Separate messages into older history (to compact) and recent tail (to keep),
            #    stepping back across ToolMessages so tool call/result pairs stay intact.
            cutoff_idx = max(1, len(messages) - 2)
            while cutoff_idx > 1 and isinstance(messages[cutoff_idx], ToolMessage):
                cutoff_idx -= 1

            to_compact = messages[:cutoff_idx]
            raw_tail = messages[cutoff_idx:]
            to_keep = ensure_not_starting_with_tool_call(
                remove_unpaired_tool_calls_and_results(raw_tail)
            )
            if not to_keep:
                to_keep = [m for m in raw_tail if not isinstance(m, ToolMessage)] or messages[-2:]

            state_cutoff = max(1, len(raw_messages) - len(raw_tail))

            # 3. Generate structured 6-section <summary> (LLM with deterministic fallback)
            #    and wrap in the full <CONTEXT_SUMMARY> envelope.
            active_model = await self._aresolve_active_model(ctx)
            summary_window = await asyncio.to_thread(self._prepare_summary_window, to_compact)
            interactive_cfg = CheckpointConfig(
                retry_config=CheckpointRetryConfig(max_attempts=2),
            )
            session_summary = await agenerate_llm_continuation_summary(
                active_model,
                summary_window,
                custom_instructions=custom_instructions,
                config=interactive_cfg,
            )
            envelope_content = await asyncio.to_thread(
                build_context_summary_envelope,
                to_compact,
                session_summary,
                thread_id=effective_thread_id,
            )
            duration_seconds = max(0.0, time.perf_counter() - start_t)
            probe_summary_msg = HumanMessage(content=envelope_content)
            summary_tokens = await asyncio.to_thread(self._count_tokens, [probe_summary_msg])
            estimated_after_tokens = await asyncio.to_thread(
                self._count_tokens,
                [probe_summary_msg] + list(to_keep),
            )
            compaction_stats = {
                "steps_summarized": len(to_compact),
                "pre_tokens": before_tokens,
                "post_tokens": estimated_after_tokens,
                "summary_tokens": summary_tokens,
                "duration_seconds": round(duration_seconds, 2),
            }
            summary_msg = HumanMessage(
                content=envelope_content,
                id=str(uuid.uuid4()),
                additional_kwargs={
                    "lc_source": "summarization",
                    "compaction_stats": compaction_stats,
                },
            )

            new_messages: list[Any] = [summary_msg] + list(to_keep)
            after_tokens = estimated_after_tokens or max(50, int(before_tokens * 0.3))
            compaction_stats["post_tokens"] = after_tokens
            freed_tokens = max(0, before_tokens - after_tokens)

            if agent is not None and thread_id:
                try:
                    config = {"configurable": {"thread_id": thread_id}}
                    if inspect.iscoroutinefunction(getattr(agent, "aensure_thread", None)):
                        await agent.aensure_thread(config)
                    summarization_event = {
                        "cutoff_index": state_cutoff,
                        "summary_message": summary_msg,
                        "file_path": str(compact_transcript_path),
                    }
                    await agent.aupdate_state(
                        config,
                        {
                            "_summarization_event": summarization_event,
                            "_context_tokens": after_tokens,
                        },
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed updating LangGraph checkpoint state during compaction: %s",
                        exc,
                    )

            # Update live UI context token counter and status bar without re-mounting history
            if ctx.app is not None:
                with contextlib.suppress(Exception):
                    setattr(ctx.app, "_context_tokens", after_tokens)
                    status_bar = getattr(ctx.app, "_status_bar", None)
                    if status_bar is not None and hasattr(status_bar, "set_tokens"):
                        cumulative = getattr(ctx.app, "_cumulative_session_tokens", 0) or after_tokens
                        status_bar.set_tokens(cumulative, context_tokens=after_tokens)
        finally:
            if has_async_spinner and set_spinner_fn is not None:
                with contextlib.suppress(Exception):
                    await set_spinner_fn(None)
            if app_has_running_flag:
                with contextlib.suppress(Exception):
                    setattr(ctx.app, "_agent_running", False)

        msg = (
            f"**Conversation Compacted**\n"
            f"├ **Before:** ~{before_tokens:,} tokens ({len(messages)} messages)\n"
            f"├ **After:**  ~{after_tokens:,} tokens ({len(new_messages) if new_messages else 'compacted'} messages)\n"
            f"├ **Freed:**  ~{freed_tokens:,} tokens\n"
            f"└ **Transcript:** `{compact_transcript_path}`"
        )
        return CommandResult(
            success=True,
            message=msg,
            data={
                "before_tokens": before_tokens,
                "after_tokens": after_tokens,
                "freed_tokens": freed_tokens,
                "summary_tokens": summary_tokens,
                "steps_summarized": len(to_compact),
                "duration_seconds": round(duration_seconds, 2),
                "transcript_path": str(compact_transcript_path),
                "full_transcript_path": str(full_transcript_path),
                "context_summary": envelope_content,
            },
        )

    @staticmethod
    def _count_tokens(messages: list[Any]) -> int:
        try:
            from langchain_core.messages.utils import count_tokens_approximately

            return count_tokens_approximately(messages)
        except Exception:
            total_chars = sum(len(str(getattr(m, "content", m))) for m in messages)
            return total_chars // 4


__all__ = ["CompactHandler"]

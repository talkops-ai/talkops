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

import contextlib
import time
from typing import Any

from langchain_core.messages import RemoveMessage, SystemMessage

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.middleware.compaction import (
    agenerate_llm_continuation_summary,
    build_context_summary_envelope,
    ensure_not_starting_with_tool_call,
    parse_compact_request,
    remove_unpaired_tool_calls_and_results,
    sync_messages_to_transcript,
)
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
            _, instructions = parse_compact_request(ctx.raw_command)
            if instructions:
                return instructions
        return ""

    @staticmethod
    def _resolve_active_model(ctx: CommandContext) -> Any | None:
        if ctx.app is not None:
            for attr in ("_active_model", "_model", "model"):
                candidate = getattr(ctx.app, attr, None)
                if candidate is not None and hasattr(candidate, "ainvoke"):
                    return candidate
        if ctx.agent is not None:
            for attr in ("model", "_model"):
                candidate = getattr(ctx.agent, attr, None)
                if candidate is not None and hasattr(candidate, "ainvoke"):
                    return candidate
        return None

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute the command handler with the provided command context."""
        if ctx.app is not None and hasattr(ctx.app, "_agent_running") and ctx.app._agent_running:
            return CommandResult(success=False, message="Cannot compact while agent is running.")

        messages: list[Any] = []
        thread_id: str | None = ctx.thread_id

        if ctx.app is not None:
            if not thread_id:
                thread_id = getattr(ctx.app, "_agent_thread_id", None)
            if hasattr(ctx.app, "get_thread_messages"):
                try:
                    messages = ctx.app.get_thread_messages()
                except Exception as exc:
                    logger.debug("Failed getting thread messages: %s", exc)

        if not messages and ctx.agent is not None and thread_id:
            try:
                state = await ctx.agent.aget_state({"configurable": {"thread_id": thread_id}})
                messages = state.values.get("messages", [])
            except Exception as exc:
                logger.debug("Failed fetching state from agent: %s", exc)

        from talkops.middleware.btw import is_ephemeral_message

        messages = [m for m in messages if not is_ephemeral_message(m)]

        if len(messages) < 4:
            return CommandResult(
                success=False,
                message="Not enough messages to compact (minimum 4 messages required).",
            )

        effective_thread_id = thread_id or "default"
        custom_instructions = self._resolve_custom_instructions(ctx)

        # 1. Persist 100% of current messages to dual line-aligned JSONL transcripts
        #    (transcript.jsonl + transcript_full.jsonl) before removing any messages.
        compact_transcript_path, full_transcript_path = sync_messages_to_transcript(
            messages,
            thread_id=effective_thread_id,
        )

        before_tokens = self._count_tokens(messages)
        start_t = time.perf_counter()

        # 2. Separate messages into older history (to compact) and recent 2 turns (to keep),
        #    preserving tool-call pairing and valid turn boundaries on the kept tail.
        to_compact = messages[:-2]
        to_keep = ensure_not_starting_with_tool_call(
            remove_unpaired_tool_calls_and_results(messages[-2:])
        )
        if not to_keep:
            to_keep = messages[-2:]

        # 3. Generate structured 6-section <summary> (LLM with deterministic fallback)
        #    and wrap in the full <CONTEXT_SUMMARY> envelope.
        active_model = self._resolve_active_model(ctx)
        session_summary = await agenerate_llm_continuation_summary(
            active_model,
            to_compact,
            custom_instructions=custom_instructions,
        )
        envelope_content = build_context_summary_envelope(
            to_compact,
            session_summary,
            thread_id=effective_thread_id,
        )
        duration_seconds = max(0.0, time.perf_counter() - start_t)
        summary_tokens = self._count_tokens([SystemMessage(content=envelope_content)])
        estimated_after_tokens = self._count_tokens(
            [SystemMessage(content=envelope_content)] + list(to_keep)
        )
        compaction_stats = {
            "steps_summarized": len(to_compact),
            "pre_tokens": before_tokens,
            "post_tokens": estimated_after_tokens,
            "summary_tokens": summary_tokens,
            "duration_seconds": round(duration_seconds, 2),
        }
        summary_msg = SystemMessage(
            content=envelope_content,
            additional_kwargs={
                "lc_source": "summarization",
                "compaction_stats": compaction_stats,
            },
        )

        new_messages: list[Any] = []
        if ctx.agent is not None and thread_id:
            try:
                removals = [RemoveMessage(id=m.id) for m in to_compact if hasattr(m, "id") and m.id]
                config = {"configurable": {"thread_id": thread_id}}
                await ctx.agent.aupdate_state(config, {"messages": removals + [summary_msg]})

                new_state = await ctx.agent.aget_state(config)
                new_messages = new_state.values.get("messages", [])
            except Exception as exc:
                logger.warning("Failed updating LangGraph checkpoint state during compaction: %s", exc)
                new_messages = [summary_msg] + list(to_keep)
        else:
            new_messages = [summary_msg] + list(to_keep)

        after_tokens = self._count_tokens(new_messages) if new_messages else max(50, int(before_tokens * 0.3))
        compaction_stats["post_tokens"] = after_tokens
        freed_tokens = max(0, before_tokens - after_tokens)

        # Refresh UI messages and context meter if app is available
        if ctx.app is not None:
            with contextlib.suppress(Exception):
                setattr(ctx.app, "_context_tokens", after_tokens)
                status_bar = getattr(ctx.app, "_status_bar", None)
                if status_bar is not None and hasattr(status_bar, "set_tokens"):
                    cumulative = getattr(ctx.app, "_cumulative_session_tokens", 0) or after_tokens
                    status_bar.set_tokens(cumulative, context_tokens=after_tokens)
            if thread_id and hasattr(ctx.app, "_load_thread_history"):
                with contextlib.suppress(Exception):
                    await ctx.app._load_thread_history(thread_id)

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

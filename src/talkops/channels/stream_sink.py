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

"""Live streaming sink (`ChannelStreamSink`) connecting TalkOps `GraphStreamBridge` to channels.

Implements the TalkOps `StreamSink` protocol (`on_stream_start`, `on_token`, `on_thinking`,
`on_tool_call_started`, `on_tool_call_completed`, `on_interrupt`, `on_stream_end`, `on_error`)
and performs debounced live message editing (`update_message`) + chunked final delivery.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any

from langchain_core.messages import AIMessageChunk, BaseMessage

from talkops.channels.approvals import PendingApprovalRegistry, PendingToolApproval
from talkops.channels.formatting import (
    DISCORD_MAX_TEXT_CHARS,
    SLACK_MAX_TEXT_CHARS,
    TEAMS_MAX_TEXT_CHARS,
    chunk_text,
    format_for_channel,
    parse_channel_output,
)
from talkops.channels.interfaces import ChannelMedia, WebhookChannelAdapter
from talkops.channels.media import extract_markdown_media
from talkops.integrations.content_filter import filter_internal_content


def _max_chars_for_channel(channel: str, adapter: Any = None) -> int:
    caps = getattr(adapter, "capabilities", None) if adapter is not None else None
    cap_max = getattr(caps, "max_text_chars", None)
    if isinstance(cap_max, int) and cap_max > 0:
        return cap_max
    norm = channel.strip().lower()
    if norm == "discord":
        return DISCORD_MAX_TEXT_CHARS
    if norm == "teams":
        return TEAMS_MAX_TEXT_CHARS
    return SLACK_MAX_TEXT_CHARS


_SECRET_ARG_RE = re.compile(
    r"(?:Bearer\s+\S+|(?:token|password|secret|api_key|apikey)\s*[=:]\s*\S+)",
    flags=re.IGNORECASE,
)


def _summarize_tool_args(args: Mapping[str, Any] | None, max_len: int = 56) -> str:
    """Return a concise, redacted single-line preview of the primary tool argument."""
    if not isinstance(args, Mapping) or not args:
        return ""
    priority_keys = (
        "command",
        "cmd",
        "query",
        "path",
        "file_path",
        "pattern",
        "url",
        "pod",
        "service",
        "namespace",
        "resource",
    )
    chosen_val: str = ""
    for k in priority_keys:
        v = args.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            chosen_val = str(v).strip()
            break
    if not chosen_val:
        for k, v in args.items():
            if any(s in str(k).lower() for s in ("secret", "token", "password", "key")):
                continue
            if isinstance(v, (str, int, float)) and str(v).strip():
                chosen_val = f"{k}={str(v).strip()}"
                break
    if not chosen_val:
        return ""
    one_line = " ".join(chosen_val.splitlines()).strip()
    redacted = _SECRET_ARG_RE.sub("[REDACTED]", one_line).replace("`", "'")
    if len(redacted) > max_len:
        redacted = redacted[: max_len - 1].rstrip() + "…"
    return f" · `{redacted}`"


class ChannelStreamSink:
    """Bridges LangGraph stream events to a `WebhookChannelAdapter` with debounced live edits.

    Verbosity levels:
    - `1`: Quiet — send only final assistant answer and HITL approval prompts.
    - `2`: Balanced (default) — live-update a status message with active tool calls,
           then replace/follow up with the final assistant response.
    - `3`: Verbose — live-stream thinking snippets, tool calls, and token chunks via
           debounced `update_message` calls.
    """

    def __init__(
        self,
        adapter: WebhookChannelAdapter,
        *,
        conversation_id: str,
        thread_id: str | None = None,
        lg_thread_id: str,
        run_id: str | None = None,
        requester_id: str = "",
        approval_registry: PendingApprovalRegistry | None = None,
        verbosity: int = 2,
        edit_debounce_seconds: float = 1.0,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Initialize `ChannelStreamSink` for a single conversation turn."""
        self.adapter = adapter
        self.conversation_id = conversation_id
        self.thread_id = thread_id
        self.lg_thread_id = lg_thread_id
        self.run_id = run_id
        self.requester_id = requester_id
        self.approval_registry = approval_registry
        self.verbosity = max(1, min(3, verbosity))
        caps = getattr(adapter, "capabilities", None)
        cap_debounce = getattr(caps, "edit_debounce_seconds", None)
        if edit_debounce_seconds > 0.0 and isinstance(cap_debounce, (int, float)) and cap_debounce > 0.0:
            self.edit_debounce_seconds = max(float(edit_debounce_seconds), float(cap_debounce))
        else:
            self.edit_debounce_seconds = edit_debounce_seconds
        self.metadata = dict(metadata or {})

        self._tokens: list[str] = []
        self._thinking: list[str] = []
        self._tool_lines: list[str] = []
        self._used_tools: list[str] = []
        self._active_tools: dict[str, str] = {}
        self._live_message_id: str | None = None
        self._last_edit_ts: float = 0.0
        self._interrupted: bool = False
        self._pending_approval: PendingToolApproval | None = None

    @property
    def accumulated_text(self) -> str:
        """Return filtered assistant text accumulated across streamed token chunks."""
        return filter_internal_content("".join(self._tokens)).strip()

    @property
    def interrupted(self) -> bool:
        """Return whether the stream encountered a HITL interrupt."""
        return self._interrupted

    @property
    def pending_approval(self) -> PendingToolApproval | None:
        """Return the registered `PendingToolApproval` if an interrupt occurred."""
        return self._pending_approval

    def _compose_live_preview(self) -> str:
        sections: list[str] = []
        if self.verbosity >= 3 and self._thinking:
            thought_tail = "".join(self._thinking).strip()[-400:]
            if thought_tail:
                sections.append(f"> 💭 _Thinking:_ {thought_tail}")
        if self.verbosity >= 2 and self._tool_lines:
            sections.append("\n".join(self._tool_lines[-6:]))
        current_text = self.accumulated_text
        if current_text and self.verbosity >= 3:
            sections.append(current_text)
        elif not sections and self.verbosity >= 2:
            sections.append("⏳ _Working on your request..._")
        return "\n\n".join(sections).strip()

    async def _maybe_flush_live_preview(self, *, force: bool = False) -> None:
        if self.verbosity <= 1:
            return
        now = time.monotonic()
        if not force and (now - self._last_edit_ts) < self.edit_debounce_seconds:
            return
        preview = self._compose_live_preview()
        if not preview:
            return
        max_chars = _max_chars_for_channel(self.adapter.name, self.adapter)
        formatted = format_for_channel(self.adapter.name, preview)[:max_chars]
        try:
            if self._live_message_id is None:
                if self.thread_id:
                    sent = await self.adapter.send_in_thread(
                        self.conversation_id,
                        self.thread_id,
                        formatted,
                        metadata=self.metadata,
                    )
                else:
                    sent = await self.adapter.send(
                        self.conversation_id,
                        formatted,
                        metadata=self.metadata,
                    )
                self._live_message_id = sent.message_id
            else:
                await self.adapter.update_message(
                    self.conversation_id,
                    self._live_message_id,
                    formatted,
                    metadata=self.metadata,
                )
            self._last_edit_ts = now
        except Exception:
            pass

    async def on_stream_start(self, channel_id: str, thread_id: str) -> None:
        """Reset per-turn buffers and post initial status preview if verbosity >= 2."""
        del channel_id, thread_id
        self._tokens.clear()
        self._thinking.clear()
        self._tool_lines.clear()
        self._used_tools.clear()
        self._active_tools.clear()
        if self.verbosity >= 2:
            await self._maybe_flush_live_preview(force=True)

    async def on_token(self, token: str) -> None:
        """Append streamed assistant token and debounce-update preview if verbosity >= 3."""
        if token:
            self._tokens.append(token)
            if self.verbosity >= 3:
                await self._maybe_flush_live_preview()

    async def on_thinking(self, text: str) -> None:
        """Append streamed reasoning text and debounce-update preview if verbosity >= 3."""
        if text:
            self._thinking.append(text)
            if self.verbosity >= 3:
                await self._maybe_flush_live_preview()

    async def on_tool_call_started(
        self,
        name: str,
        call_id: str,
        args: dict[str, Any],
    ) -> None:
        """Record active tool invocation and force-flush live preview."""
        key = call_id or name
        if key in self._active_tools:
            return
        arg_hint = _summarize_tool_args(args)
        self._active_tools[key] = f"`{name}`{arg_hint}"
        if name and name not in self._used_tools:
            self._used_tools.append(name)
        self._tool_lines.append(f"🔧 Running `{name}`{arg_hint}...")
        await self._maybe_flush_live_preview(force=True)

    async def on_tool_call_completed(
        self,
        name: str,
        call_id: str,
        result: str,
    ) -> None:
        """Mark active tool invocation complete and flush live preview."""
        del result
        key = call_id or name
        label = self._active_tools.pop(key, None) or f"`{name}`"
        updated = False
        for idx in range(len(self._tool_lines) - 1, -1, -1):
            if f"`{name}`" in self._tool_lines[idx] and "🔧" in self._tool_lines[idx]:
                self._tool_lines[idx] = f"✅ Completed {label}"
                updated = True
                break
        if not updated:
            self._tool_lines.append(f"✅ Completed {label}")
        await self._maybe_flush_live_preview()

    async def on_pre_interrupt(self) -> None:
        """Mark stream as interrupted prior to emitting HITL card."""
        self._interrupted = True

    async def on_interrupt(self, interrupt_value: dict[str, Any]) -> None:
        """Register pending HITL approval and dispatch interactive approval card."""
        self._interrupted = True
        if self.approval_registry is None:
            return
        pending = self.approval_registry.register(
            channel=self.adapter.name,
            conversation_id=self.conversation_id,
            thread_id=self.thread_id,
            lg_thread_id=self.lg_thread_id,
            run_id=self.run_id,
            requester_id=self.requester_id,
            interrupt_value=interrupt_value,
            metadata=self.metadata,
        )
        self._pending_approval = pending
        spec = pending.to_card_spec()
        sent = await self.adapter.send_approval_prompt(spec, metadata=self.metadata)
        self.approval_registry.bind_message_id(pending.approval_id, sent.message_id)

    async def on_subagent_event(self, agent_name: str, status: str) -> None:
        """Record subagent status line in live preview."""
        icon = "🤖" if status == "running" else "✅"
        self._tool_lines.append(f"{icon} Subagent `{agent_name}` ({status})")
        await self._maybe_flush_live_preview()

    async def on_rubric_event(self, data: dict[str, Any]) -> None:
        """No-op hook for rubric events on chat channels."""
        del data

    async def on_auto_mode_event(self, data: dict[str, Any]) -> None:
        """Record smart/auto-mode classification event in live preview."""
        decision = data.get("decision")
        tool_name = data.get("tool_name") or data.get("name")
        if decision and tool_name:
            self._tool_lines.append(f"🛡️ Auto-Mode `{tool_name}`: {decision}")
            await self._maybe_flush_live_preview()

    async def on_stream_end(self) -> None:
        """Deliver final formatted and chunked assistant response (and any extracted media)."""
        raw_final = self.accumulated_text
        if not raw_final:
            if self._live_message_id is not None and not self._interrupted:
                done_preview = "\n".join(self._tool_lines[-6:]) or "✅ _Completed._"
                try:
                    await self.adapter.update_message(
                        self.conversation_id,
                        self._live_message_id,
                        done_preview,
                        metadata=self.metadata,
                    )
                except Exception:
                    pass
            return

        cleaned_final, extracted_media = extract_markdown_media(raw_final)
        final_text = cleaned_final or raw_final
        max_chars = _max_chars_for_channel(self.adapter.name, self.adapter)
        raw_chunks = chunk_text(final_text, max_chars=max_chars)
        if not raw_chunks and not extracted_media:
            return
        if not raw_chunks:
            await self._send_chunk("", media=extracted_media)
            return

        first_raw = raw_chunks[0]
        first_is_last = len(raw_chunks) == 1
        first_parsed = parse_channel_output(
            self.adapter.name,
            first_raw,
            tool_names=self._used_tools if first_is_last else (),
        )
        first_media = extracted_media if first_is_last else ()
        if self._live_message_id is not None and not self._interrupted and not first_media:
            try:
                await self.adapter.update_message(
                    self.conversation_id,
                    self._live_message_id,
                    first_raw,
                    blocks_or_cards=first_parsed.blocks_or_cards,
                    metadata=self.metadata,
                )
            except Exception:
                await self._send_chunk(
                    first_raw,
                    media=first_media,
                    blocks_or_cards=first_parsed.blocks_or_cards,
                )
        else:
            await self._send_chunk(
                first_raw,
                media=first_media,
                blocks_or_cards=first_parsed.blocks_or_cards,
            )

        for idx, extra_raw in enumerate(raw_chunks[1:], start=1):
            is_last = idx == len(raw_chunks) - 1
            extra_parsed = parse_channel_output(
                self.adapter.name,
                extra_raw,
                tool_names=self._used_tools if is_last else (),
            )
            await self._send_chunk(
                extra_raw,
                media=extracted_media if is_last else (),
                blocks_or_cards=extra_parsed.blocks_or_cards,
            )

    async def _send_chunk(
        self,
        text: str,
        *,
        media: tuple[ChannelMedia, ...] = (),
        blocks_or_cards: Any = None,
    ) -> None:
        if self.thread_id:
            await self.adapter.send_in_thread(
                self.conversation_id,
                self.thread_id,
                text,
                media=media,
                blocks_or_cards=blocks_or_cards,
                metadata=self.metadata,
            )
        else:
            await self.adapter.send(
                self.conversation_id,
                text,
                media=media,
                blocks_or_cards=blocks_or_cards,
                metadata=self.metadata,
            )

    async def on_error(self, error: Exception) -> None:
        """Send formatted error alert to the active channel thread."""
        err_msg = f"⚠️ Agent execution error: `{type(error).__name__}: {error}`"
        await self._send_chunk(err_msg)


async def normalize_gateway_stream(
    raw_stream: AsyncIterator[Any],
) -> AsyncIterator[tuple[str, Any]]:
    """Adapt `AgentRunGateway.stream_run` 3-tuples `(ns, mode, data)` to 2-tuples `(mode, data)`."""

    def _coerce_messages_payload(data: Any) -> Any:
        msg = data[0] if isinstance(data, tuple) and data else data
        meta = data[1] if isinstance(data, tuple) and len(data) > 1 else {}
        if isinstance(msg, BaseMessage):
            if not isinstance(msg, AIMessageChunk) and getattr(msg, "type", "") == "ai":
                return (AIMessageChunk(content=str(getattr(msg, "content", ""))), meta)
            return data
        if hasattr(msg, "content") and not isinstance(msg, (dict, str, bytes)):
            return (AIMessageChunk(content=str(getattr(msg, "content", ""))), meta)
        if isinstance(msg, Mapping) and "content" in msg:
            return (AIMessageChunk(content=str(msg["content"])), meta)
        return data

    async for item in raw_stream:
        if isinstance(item, tuple):
            if len(item) == 3:
                _ns, mode, data = item
                mode_str = str(mode)
                yield (
                    mode_str,
                    _coerce_messages_payload(data) if mode_str == "messages" else data,
                )
            elif len(item) == 2:
                mode, data = item
                mode_str = str(mode)
                yield (
                    mode_str,
                    _coerce_messages_payload(data) if mode_str == "messages" else data,
                )
        elif isinstance(item, Mapping):
            mode_str = str(item.get("event") or item.get("mode") or "updates")
            data = item.get("data")
            yield (
                mode_str,
                _coerce_messages_payload(data) if mode_str == "messages" else data,
            )

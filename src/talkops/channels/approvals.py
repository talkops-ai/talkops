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

"""Human-in-the-Loop (HITL) tool approval mediation for `talkops.channels`.

Supports all three approval interaction patterns across Slack, Microsoft Teams, and Discord:
1. Interactive UI buttons / Adaptive Cards (`approve` / `reject` button clicks).
2. Emoji reactions on the approval card message (👍 / `+1` to approve, 👎 / `-1` to reject).
3. Natural-language text replies in the conversation thread (`approve`, `yes`, `lgtm`,
   `deny`, `reject`, `no <reason>`).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from talkops.channels.formatting import normalize_reaction_decision
from talkops.channels.interfaces import (
    ApprovalCardSpec,
    ApprovalDecisionType,
    ToolCallSummary,
)
from talkops.channels.state_store import ChannelStateStore

_APPROVE_WORDS = frozenset({"approve", "approved", "yes", "y", "lgtm", "proceed", "ok"})
_REJECT_WORDS = frozenset({"reject", "rejected", "deny", "denied", "no", "n", "cancel"})


@dataclass(slots=True)
class PendingToolApproval:
    """State for one interrupted LangGraph run awaiting HITL tool approval."""

    approval_id: str
    channel: str
    conversation_id: str
    thread_id: str | None
    lg_thread_id: str
    run_id: str | None
    requester_id: str
    tool_calls: tuple[ToolCallSummary, ...]
    prompt_message_id: str | None = None
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_card_spec(self) -> ApprovalCardSpec:
        """Build an `ApprovalCardSpec` for rendering on Slack, Teams, or Discord."""
        lines: list[str] = []
        for idx, tc in enumerate(self.tool_calls, start=1):
            args_preview = ", ".join(f"{k}={v!r}" for k, v in list(tc.args.items())[:5])
            lines.append(f"{idx}. `{tc.name}({args_preview})`")
            if tc.description:
                lines.append(f"   _{tc.description}_")
        summary = "\n".join(lines) if lines else "A tool call requires operator approval."
        prompt_text = (
            f"**{len(self.tool_calls)} tool call(s) require approval before execution:**\n"
            f"{summary}\n\n"
            "Click **Approve** / **Reject**, react with 👍 / 👎, or reply `approve` / `deny <reason>`."
        )
        return ApprovalCardSpec(
            approval_id=self.approval_id,
            channel=self.channel,
            conversation_id=self.conversation_id,
            thread_id=self.thread_id,
            lg_thread_id=self.lg_thread_id,
            run_id=self.run_id,
            requester_id=self.requester_id,
            tool_calls=self.tool_calls,
            prompt_text=prompt_text,
            metadata=dict(self.metadata),
        )

    def build_resume_payload(
        self,
        decision: ApprovalDecisionType,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Construct the LangGraph `Command(resume=...)` payload for all pending tool calls."""
        count = max(1, len(self.tool_calls))
        decisions: list[dict[str, Any]] = []
        for _ in range(count):
            entry: dict[str, Any] = {"type": decision}
            if decision == "reject" and reason:
                entry["message"] = reason
            decisions.append(entry)
        return {"decisions": decisions}

    def to_dict(self) -> dict[str, Any]:
        """Serialize pending approval state for durable persistence across restarts/replicas."""
        return {
            "approval_id": self.approval_id,
            "channel": self.channel,
            "conversation_id": self.conversation_id,
            "thread_id": self.thread_id,
            "lg_thread_id": self.lg_thread_id,
            "run_id": self.run_id,
            "requester_id": self.requester_id,
            "tool_calls": [
                {
                    "name": tc.name,
                    "args": dict(tc.args),
                    "tool_call_id": tc.tool_call_id,
                    "description": tc.description,
                }
                for tc in self.tool_calls
            ],
            "prompt_message_id": self.prompt_message_id,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PendingToolApproval:
        """Reconstruct a `PendingToolApproval` from persisted dictionary state."""
        raw_tcs = data.get("tool_calls") or []
        tool_calls: list[ToolCallSummary] = []
        if isinstance(raw_tcs, Sequence):
            for tc in raw_tcs:
                if isinstance(tc, Mapping):
                    raw_args = tc.get("args")
                    tool_calls.append(
                        ToolCallSummary(
                            name=str(tc.get("name") or "tool"),
                            args=dict(raw_args) if isinstance(raw_args, Mapping) else {},
                            tool_call_id=str(tc["tool_call_id"]) if tc.get("tool_call_id") else None,
                            description=str(tc["description"]) if tc.get("description") else None,
                        )
                    )
        raw_meta = data.get("metadata")
        return cls(
            approval_id=str(data.get("approval_id") or ""),
            channel=str(data.get("channel") or ""),
            conversation_id=str(data.get("conversation_id") or ""),
            thread_id=str(data["thread_id"]) if data.get("thread_id") else None,
            lg_thread_id=str(data.get("lg_thread_id") or ""),
            run_id=str(data["run_id"]) if data.get("run_id") else None,
            requester_id=str(data.get("requester_id") or ""),
            tool_calls=tuple(tool_calls),
            prompt_message_id=str(data["prompt_message_id"]) if data.get("prompt_message_id") else None,
            created_at=float(data.get("created_at") or time.time()),
            metadata=dict(raw_meta) if isinstance(raw_meta, Mapping) else {},
        )


def extract_tool_calls_from_interrupt(interrupt_value: Any) -> tuple[ToolCallSummary, ...]:
    """Extract `ToolCallSummary` items from a LangGraph `__interrupt__` value."""
    if isinstance(interrupt_value, Mapping) and "value" in interrupt_value:
        interrupt_value = interrupt_value["value"]

    tool_calls: list[ToolCallSummary] = []

    if isinstance(interrupt_value, Mapping):
        # Format 1: `{"action_requests": [{"name": ..., "args": ...}, ...]}` (LangChain HITL middleware)
        action_requests = interrupt_value.get("action_requests") or interrupt_value.get("actions")
        if isinstance(action_requests, Sequence) and not isinstance(action_requests, (str, bytes)):
            for item in action_requests:
                if isinstance(item, Mapping):
                    name = str(item.get("name") or item.get("action") or "tool")
                    raw_args = item.get("args") or item.get("arguments") or {}
                    args = dict(raw_args) if isinstance(raw_args, Mapping) else {"value": raw_args}
                    tool_calls.append(
                        ToolCallSummary(
                            name=name,
                            args=args,
                            tool_call_id=str(item["id"]) if item.get("id") else None,
                            description=str(item["description"]) if item.get("description") else None,
                        )
                    )
        # Format 2: `{"tool_calls": [...]}` or single `{"tool": ..., "args": ...}`
        elif isinstance(interrupt_value.get("tool_calls"), Sequence):
            for item in interrupt_value["tool_calls"]:
                if isinstance(item, Mapping):
                    name = str(item.get("name") or item.get("tool") or "tool")
                    raw_args = item.get("args") or item.get("input") or {}
                    args = dict(raw_args) if isinstance(raw_args, Mapping) else {"value": raw_args}
                    tool_calls.append(
                        ToolCallSummary(
                            name=name,
                            args=args,
                            tool_call_id=str(item["id"]) if item.get("id") else None,
                            description=str(item["description"]) if item.get("description") else None,
                        )
                    )
        elif interrupt_value.get("name") or interrupt_value.get("tool"):
            name = str(interrupt_value.get("name") or interrupt_value.get("tool"))
            raw_args = interrupt_value.get("args") or interrupt_value.get("input") or {}
            args = dict(raw_args) if isinstance(raw_args, Mapping) else {"value": raw_args}
            tool_calls.append(
                ToolCallSummary(
                    name=name,
                    args=args,
                    description=str(interrupt_value["description"])
                    if interrupt_value.get("description")
                    else None,
                )
            )

    if not tool_calls:
        tool_calls.append(
            ToolCallSummary(
                name="confirm_action",
                args={},
                description=str(interrupt_value) if interrupt_value else "Approval requested",
            )
        )
    return tuple(tool_calls)


def parse_text_approval_decision(text: str) -> tuple[ApprovalDecisionType, str | None] | None:
    """Parse a natural-language `approve` or `deny [reason]` reply in a thread."""
    stripped = text.strip()
    if not stripped:
        return None
    parts = stripped.split(maxsplit=1)
    first = parts[0].lower().rstrip(".,!:")
    rest = parts[1].strip() if len(parts) > 1 else None

    if first in _APPROVE_WORDS and rest is None:
        return "approve", None
    if first in _REJECT_WORDS:
        return "reject", rest
    return None


class PendingApprovalRegistry:
    """Registry tracking pending HITL approvals per conversation and message ID.

    Optionally backed by a durable `ChannelStateStore` so interactive Approve/Reject
    buttons continue working across container restarts and horizontally scaled replicas.
    """

    def __init__(self, *, state_store: ChannelStateStore | None = None) -> None:
        """Initialize pending approval indices with optional durable `state_store`."""
        self._state_store = state_store
        self._by_conversation: dict[tuple[str, str], PendingToolApproval] = {}
        self._by_approval_id: dict[str, PendingToolApproval] = {}
        self._by_message_id: dict[tuple[str, str], PendingToolApproval] = {}

    def _cache_local(self, pending: PendingToolApproval) -> PendingToolApproval:
        old = self._by_conversation.pop((pending.channel, pending.conversation_id), None)
        if old is not None and old.approval_id != pending.approval_id:
            self._by_approval_id.pop(old.approval_id, None)
            if old.prompt_message_id:
                self._by_message_id.pop((pending.channel, old.prompt_message_id), None)
        self._by_conversation[(pending.channel, pending.conversation_id)] = pending
        self._by_approval_id[pending.approval_id] = pending
        if pending.prompt_message_id:
            self._by_message_id[(pending.channel, pending.prompt_message_id)] = pending
        return pending

    def register(
        self,
        *,
        channel: str,
        conversation_id: str,
        thread_id: str | None,
        lg_thread_id: str,
        run_id: str | None,
        requester_id: str,
        interrupt_value: Any,
        approval_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> PendingToolApproval:
        """Create and index a `PendingToolApproval` for `(channel, conversation_id)`."""
        aid = approval_id or f"appr_{uuid.uuid4().hex[:12]}"
        tool_calls = extract_tool_calls_from_interrupt(interrupt_value)
        pending = PendingToolApproval(
            approval_id=aid,
            channel=channel,
            conversation_id=conversation_id,
            thread_id=thread_id,
            lg_thread_id=lg_thread_id,
            run_id=run_id,
            requester_id=requester_id,
            tool_calls=tool_calls,
            metadata=dict(metadata or {}),
        )
        self._cache_local(pending)
        if self._state_store is not None:
            self._state_store.put_approval(aid, pending.to_dict())
        return pending

    def bind_message_id(self, approval_id: str, message_id: str | None) -> None:
        """Associate the platform `message_id` of the sent card so 👍/👎 reactions can resolve it."""
        if not message_id:
            return
        pending = self.get_by_approval_id(approval_id)
        if pending is None:
            return
        pending.prompt_message_id = message_id
        self._by_message_id[(pending.channel, message_id)] = pending
        if self._state_store is not None:
            self._state_store.put_approval(approval_id, pending.to_dict())

    def get_for_conversation(self, channel: str, conversation_id: str) -> PendingToolApproval | None:
        """Return active `PendingToolApproval` for `(channel, conversation_id)`, if any."""
        local = self._by_conversation.get((channel, conversation_id))
        if local is not None:
            return local
        if self._state_store is not None:
            rec = self._state_store.get_approval_by_conversation(channel, conversation_id)
            if rec is not None:
                return self._cache_local(PendingToolApproval.from_dict(rec))
        return None

    def get_by_approval_id(self, approval_id: str) -> PendingToolApproval | None:
        """Return active `PendingToolApproval` matching `approval_id`, if any."""
        local = self._by_approval_id.get(approval_id)
        if local is not None:
            return local
        if self._state_store is not None:
            rec = self._state_store.get_approval(approval_id)
            if rec is not None:
                return self._cache_local(PendingToolApproval.from_dict(rec))
        return None

    def get_by_message_id(self, channel: str, message_id: str) -> PendingToolApproval | None:
        """Return active `PendingToolApproval` bound to `(channel, message_id)`, if any."""
        local = self._by_message_id.get((channel, message_id))
        if local is not None:
            return local
        if self._state_store is not None:
            rec = self._state_store.get_approval_by_message(channel, message_id)
            if rec is not None:
                return self._cache_local(PendingToolApproval.from_dict(rec))
        return None

    def pop(self, approval_id: str) -> PendingToolApproval | None:
        """Remove and return the `PendingToolApproval` matching `approval_id`."""
        pending = self._by_approval_id.pop(approval_id, None)
        if pending is None and self._state_store is not None:
            rec = self._state_store.get_approval(approval_id)
            if rec is not None:
                pending = PendingToolApproval.from_dict(rec)
        if pending is not None:
            self._by_conversation.pop((pending.channel, pending.conversation_id), None)
            if pending.prompt_message_id:
                self._by_message_id.pop((pending.channel, pending.prompt_message_id), None)
        if self._state_store is not None:
            self._state_store.delete_approval(approval_id)
        return pending

    def pop_for_conversation(self, channel: str, conversation_id: str) -> PendingToolApproval | None:
        """Remove and return any pending approval on `(channel, conversation_id)`."""
        pending = self.get_for_conversation(channel, conversation_id)
        if pending is None:
            return None
        return self.pop(pending.approval_id)

    def match_reaction(
        self,
        *,
        channel: str,
        message_id: str,
        emoji: str,
        conversation_id: str | None = None,
    ) -> tuple[PendingToolApproval, ApprovalDecisionType] | None:
        """Resolve an emoji reaction on an approval message (or its thread) to `(PendingToolApproval, decision)`."""
        decision = normalize_reaction_decision(emoji)
        if decision not in ("approve", "reject"):
            return None
        pending = self.get_by_message_id(channel, message_id)
        if pending is None and conversation_id:
            pending = self.get_for_conversation(channel, conversation_id)
        if pending is None:
            return None
        return pending, decision  # type: ignore[return-value]

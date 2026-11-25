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

"""Central Multi-Channel Orchestrator (`ChannelHub`) for TalkOps Server Mode.

Adapts `TalonHost` orchestration patterns from `deepagents-talon` (`libs/talon`) to
TalkOps's multi-user `AgentRunGateway`, `ThreadStore`, `IdentityMapper`, and
`GraphStreamBridge`.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from langgraph.types import Command

from talkops.channels.approvals import (
    PendingApprovalRegistry,
    PendingToolApproval,
    parse_text_approval_decision,
)
from talkops.channels.commands import (
    COMMANDS_BY_NAME,
    ChatCommand,
    build_help_message,
    parse_chat_command,
)
from talkops.channels.formatting import (
    ExposureMode,
    apply_rca_feedback_state_to_discord_payload,
    apply_rca_feedback_state_to_slack_blocks,
    apply_rca_feedback_state_to_teams_cards,
    format_for_channel,
    normalize_reaction_decision,
    parse_incident_output,
)
from talkops.channels.interfaces import (
    ApprovalDecisionType,
    ChannelInteraction,
    ChannelMedia,
    ChannelMessage,
    ChannelReaction,
    ChannelStatus,
    SendResult,
    WebhookChannelAdapter,
)
from talkops.channels.media import (
    build_inbound_media_prompt,
    build_multimodal_content,
    extract_loopback_oauth_callback_url,
)
from talkops.channels.messaging import ACTIVE_CHANNEL_CONVERSATION
from talkops.channels.pairing import PairingStore, format_code
from talkops.channels.state_store import ChannelStateStore
from talkops.channels.stream_sink import ChannelStreamSink, normalize_gateway_stream
from talkops.integrations.stream_bridge import GraphStreamBridge
from talkops.integrations.thread_store import ThreadStore

TYPING_REFRESH_SECONDS = 4.0
THREAD_CONTEXT_MAX_CHARS = 12000
_RCA_MESSAGE_CACHE_CAP = 500


class ChannelHub:
    """Central orchestrator managing Slack, Microsoft Teams, and Discord webhook channels."""

    def __init__(
        self,
        *,
        gateway: Any | None = None,
        thread_store: Any | None = None,
        identity_mapper: Any | None = None,
        pairing_store: PairingStore | None = None,
        state_store: ChannelStateStore | None = None,
        event_bus: Any | None = None,
        project_root: Path | None = None,
        operational_store: Any | None = None,
        pairing_enabled: bool = True,
        verbosity: int = 2,
        edit_debounce_seconds: float = 1.0,
        default_approval_mode: str = "smart",
        multitask_strategy: Literal["interrupt", "enqueue"] = "interrupt",
        alert_target_channel: str | None = None,
        alert_target_conversation_id: str | None = None,
    ) -> None:
        """Initialize `ChannelHub` with optional gateway, thread store, state store, and pairing configuration."""
        self.gateway: Any = gateway
        self.thread_store: Any = thread_store if thread_store is not None else ThreadStore()
        self.identity_mapper: Any = identity_mapper
        self.pairing_store = pairing_store or PairingStore(
            Path.home() / ".talkops" / "channel_pairing.json"
        )
        self.state_store: ChannelStateStore | None = state_store
        self.event_bus: Any = event_bus or getattr(gateway, "event_bus", None)
        self.project_root: Path | None = project_root
        self.operational_store: Any | None = operational_store
        self.pairing_enabled = pairing_enabled
        self.verbosity = max(1, min(3, verbosity))
        self.edit_debounce_seconds = edit_debounce_seconds
        self.default_approval_mode = default_approval_mode
        self.multitask_strategy: Literal["interrupt", "enqueue"] = (
            "enqueue" if multitask_strategy == "enqueue" else "interrupt"
        )
        self.alert_target_channel = alert_target_channel
        self.alert_target_conversation_id = alert_target_conversation_id

        self._adapters: dict[str, WebhookChannelAdapter] = {}
        self._approvals = PendingApprovalRegistry(state_store=state_store)
        self._conversation_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._active_turn_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._active_turn_senders: dict[tuple[str, str], str] = {}
        self._hydrated_threads: set[str] = set()
        self._conversation_models: dict[tuple[str, str], str] = {}
        self._conversation_smart_models: dict[tuple[str, str], str] = {}
        self._conversation_efforts: dict[tuple[str, str], str] = {}
        self._conversation_modes: dict[tuple[str, str], str] = {}
        self._fallback_threads: dict[tuple[str, str], str] = {}
        self._rca_messages: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()

    @property
    def adapters(self) -> Mapping[str, WebhookChannelAdapter]:
        """Return mapping of registered provider names to `WebhookChannelAdapter` instances."""
        return dict(self._adapters)

    @property
    def approvals(self) -> PendingApprovalRegistry:
        """Return the active `PendingApprovalRegistry`."""
        return self._approvals

    def register_adapter(self, adapter: WebhookChannelAdapter) -> None:
        """Bind `ChannelHub` handlers onto `adapter` and register it by provider name."""
        norm = adapter.name.strip().lower()
        adapter.set_message_handler(self.handle_inbound_message)
        adapter.set_reaction_handler(self.handle_inbound_reaction)
        adapter.set_interaction_handler(self.handle_inbound_interaction)
        self._adapters[norm] = adapter

    def get_adapter(self, provider: str) -> WebhookChannelAdapter | None:
        """Return registered adapter for `provider`, or `None` if unregistered."""
        return self._adapters.get(provider.strip().lower())

    async def start(self) -> None:
        """Start all registered channel adapters."""
        if self.thread_store is not None and hasattr(self.thread_store, "initialize"):
            with contextlib.suppress(Exception):
                await self.thread_store.initialize()
        for adapter in self._adapters.values():
            await adapter.start()

    async def stop(self) -> None:
        """Cancel in-flight turns and stop all registered channel adapters."""
        for task in list(self._active_turn_tasks.values()):
            task.cancel()
        if self._active_turn_tasks:
            await asyncio.gather(*self._active_turn_tasks.values(), return_exceptions=True)
        self._active_turn_tasks.clear()
        for adapter in self._adapters.values():
            await adapter.stop()

    def status_all(self) -> list[ChannelStatus]:
        """Return diagnostic `ChannelStatus` entries for all registered adapters."""
        return [adapter.status() for adapter in self._adapters.values()]

    def _get_lock(self, channel: str, conversation_id: str) -> asyncio.Lock:
        key = (channel, conversation_id)
        lock = self._conversation_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._conversation_locks[key] = lock
        return lock

    async def _bind_lg_thread(
        self,
        channel: str,
        conversation_id: str,
        lg_thread_id: str,
    ) -> None:
        """Bind `(channel, conversation_id)` to `lg_thread_id` in memory and in `ThreadStore`."""
        self._fallback_threads[(channel, conversation_id)] = lg_thread_id
        if self.thread_store is not None:
            with contextlib.suppress(Exception):
                if hasattr(self.thread_store, "bind"):
                    res = self.thread_store.bind(channel, conversation_id, lg_thread_id)
                    if asyncio.iscoroutine(res):
                        await res
                elif hasattr(self.thread_store, "set_mapping"):
                    res = self.thread_store.set_mapping(channel, conversation_id, lg_thread_id)
                    if asyncio.iscoroutine(res):
                        await res

    async def _get_or_create_lg_thread(
        self,
        channel: str,
        conversation_id: str,
        *,
        force_new: bool = False,
    ) -> tuple[str, bool]:
        """Resolve `(channel, conversation_id)` to `(lg_thread_id, is_new_thread)`."""
        key = (channel, conversation_id)
        if force_new:
            new_tid = str(uuid.uuid4())
            await self._bind_lg_thread(channel, conversation_id, new_tid)
            return new_tid, True

        if key in self._fallback_threads:
            return self._fallback_threads[key], False

        if self.thread_store is not None:
            try:
                if hasattr(self.thread_store, "has_thread") and hasattr(self.thread_store, "resolve"):
                    existed = bool(self.thread_store.has_thread(channel, conversation_id))
                    resolved = str(self.thread_store.resolve(channel, conversation_id))
                    self._fallback_threads[key] = resolved
                    return resolved, not existed
                if hasattr(self.thread_store, "get_lg_thread_id"):
                    existing = await self.thread_store.get_lg_thread_id(channel, conversation_id)
                    if existing:
                        self._fallback_threads[key] = existing
                        return existing, False
                    created = await self.thread_store.get_or_create_lg_thread_id(
                        channel, conversation_id
                    )
                    self._fallback_threads[key] = created
                    return created, True
            except Exception:
                pass

        created = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{channel}:{conversation_id}"))
        self._fallback_threads[key] = created
        return created, True

    async def _thread_has_checkpoint_messages(self, lg_thread_id: str) -> bool:
        """Return `True` if `lg_thread_id` already has persisted messages in the checkpointer."""
        if self.gateway is None or not hasattr(self.gateway, "get_thread_state"):
            return False
        try:
            state = await self.gateway.get_thread_state(lg_thread_id)
            values = (
                state.get("values")
                if isinstance(state, Mapping)
                else getattr(state, "values", None)
            )
            if isinstance(values, Mapping):
                msgs = values.get("messages")
                return isinstance(msgs, list) and len(msgs) > 0
        except Exception:
            pass
        return False

    async def _is_active_bound_thread(self, message: ChannelMessage) -> bool:
        """Return `True` if `message` is a reply inside a thread TalkOps is already participating in."""
        if not message.thread_id:
            return False
        key = (message.channel, message.conversation_id)
        if key in self._fallback_threads:
            return True

        if self.thread_store is not None and hasattr(self.thread_store, "has_thread"):
            with contextlib.suppress(Exception):
                if self.thread_store.has_thread(message.channel, message.conversation_id):
                    resolved = str(self.thread_store.resolve(message.channel, message.conversation_id))
                    self._fallback_threads[key] = resolved
                    return True

        is_thread_reply = bool(
            (message.metadata or {}).get("thread_ts")
            or (message.thread_id and message.message_id and message.thread_id != message.message_id)
        )
        if is_thread_reply:
            adapter = self._adapters.get(message.channel)
            check_fn: Any = getattr(adapter, "has_bot_participated_in_thread", None)
            if check_fn is not None:
                with contextlib.suppress(Exception):
                    if await check_fn(message.conversation_id, message.thread_id):
                        deterministic_tid = str(
                            uuid.uuid5(
                                uuid.NAMESPACE_DNS,
                                f"{message.channel}:{message.conversation_id}",
                            )
                        )
                        await self._bind_lg_thread(
                            message.channel, message.conversation_id, deterministic_tid
                        )
                        return True
        return False

    def _get_exposure(self, channel: str) -> Any:
        adapter = self._adapters.get(channel)
        cfg = getattr(adapter, "config", None)
        return getattr(cfg, "exposure", None)

    def _is_operator(self, channel: str, sender_id: str) -> bool:
        exposure = self._get_exposure(channel)
        if exposure is not None and hasattr(exposure, "is_operator"):
            if exposure.is_operator(sender_id):
                return True
        if self.identity_mapper is not None and hasattr(self.identity_mapper, "resolve"):
            with contextlib.suppress(Exception):
                ident = self.identity_mapper.resolve(channel, sender_id)
                if getattr(ident, "role", "") == "admin":
                    return True
        return False

    def _is_authorized(
        self,
        *,
        channel: str,
        sender_id: str,
        conversation_id: str,
        parent_channel_id: str | None = None,
        is_dm: bool = False,
    ) -> bool:
        exposure = self._get_exposure(channel)
        is_paired = self.pairing_store.is_approved(channel, sender_id)
        if exposure is None:
            return True
        if self.identity_mapper is not None and hasattr(self.identity_mapper, "_identities"):
            if f"{channel}:{sender_id}" in getattr(self.identity_mapper, "_identities", {}):
                return True
        return bool(
            exposure.allows(
                sender_id=sender_id,
                conversation_id=conversation_id,
                parent_channel_id=parent_channel_id,
                is_dm=is_dm,
                is_paired=is_paired,
            )
        )

    def _resolve_user_identity(self, channel: str, sender_id: str, sender_name: str = "") -> dict[str, Any]:
        """Resolve TalkOps Cloud RBAC context dictionary for `(channel, sender_id)`."""
        if self.identity_mapper is not None and hasattr(self.identity_mapper, "resolve"):
            with contextlib.suppress(Exception):
                ident = self.identity_mapper.resolve(channel, sender_id, display_name=sender_name)
                if hasattr(ident, "to_context_dict"):
                    return ident.to_context_dict()
                if isinstance(ident, Mapping):
                    return dict(ident)
        role = "admin" if self._is_operator(channel, sender_id) else "operator"
        return {
            "platform": channel,
            "platform_user_id": sender_id,
            "display_name": sender_name or sender_id,
            "role": role,
        }

    async def handle_inbound_message(self, message: ChannelMessage) -> None:
        """Process a normalized `ChannelMessage` from Slack, Teams, or Discord."""
        adapter = self._adapters.get(message.channel)
        if adapter is None or message.from_bot:
            return

        exposure = self._get_exposure(message.channel)
        parsed_cmd = parse_chat_command(message.text)

        has_pending_approval = (
            self._approvals.get_for_conversation(message.channel, message.conversation_id)
            is not None
        )
        if (
            not message.is_dm
            and exposure is not None
            and getattr(exposure, "require_mention_in_channels", True)
            and not message.mentioned
            and parsed_cmd is None
            and not has_pending_approval
        ):
            is_active_bound_thread = await self._is_active_bound_thread(message)
            if not is_active_bound_thread:
                return

        admitted = self._is_authorized(
            channel=message.channel,
            sender_id=message.sender_id,
            conversation_id=message.conversation_id,
            parent_channel_id=message.parent_channel_id,
            is_dm=message.is_dm,
        )
        if not admitted:
            if (
                self.pairing_enabled
                and exposure is not None
                and getattr(exposure, "mode", None) != ExposureMode.SELF
            ):
                target_dm_conv: str | None = message.conversation_id if message.is_dm else None
                open_dm_fn: Any = getattr(adapter, "open_dm", None)
                if not message.is_dm and message.mentioned and open_dm_fn is not None:
                    with contextlib.suppress(Exception):
                        dm_res = await open_dm_fn(message.sender_id)
                        target_dm_conv = str(dm_res) if dm_res else None
                if not target_dm_conv and message.mentioned:
                    target_dm_conv = message.conversation_id
                if target_dm_conv:
                    req = self.pairing_store.issue_or_get_code(
                        channel=message.channel,
                        sender_id=message.sender_id,
                        sender_name=message.sender_name,
                        conversation_id=target_dm_conv,
                    )
                    formatted_code = format_code(req.code)
                    await adapter.send(
                        target_dm_conv,
                        (
                            f"🔒 You are not yet authorized to use TalkOps on `{message.channel}`.\n"
                            f"Share this pairing code with the operator: **`{formatted_code}`**\n"
                            f"The operator can approve your access by sending `/pair approve {formatted_code}`."
                        ),
                        thread_id=message.thread_id if message.is_dm else None,
                        metadata=message.metadata,
                    )
            return

        oauth_url = extract_loopback_oauth_callback_url(message.text)
        if oauth_url:
            await adapter.send(
                message.conversation_id,
                (
                    "🔑 Intercepted OAuth callback URL (`code=`/`state=` redacted from chat history "
                    "to protect your credentials)."
                ),
                thread_id=message.thread_id,
                metadata=message.metadata,
            )
            return

        if parsed_cmd is not None:
            cmd, arg_str = parsed_cmd
            reply = await self._execute_command(
                cmd,
                arg_str,
                channel=message.channel,
                conversation_id=message.conversation_id,
                sender_id=message.sender_id,
                is_dm=message.is_dm,
            )
            if reply:
                await adapter.send(
                    message.conversation_id,
                    reply,
                    thread_id=message.thread_id,
                    metadata=message.metadata,
                )
            return

        pending = self._approvals.get_for_conversation(message.channel, message.conversation_id)
        if pending is not None:
            decision_tuple = parse_text_approval_decision(message.text)
            if decision_tuple is not None:
                decision, reason = decision_tuple
                await self._resolve_approval(
                    pending,
                    decision=decision,
                    resolver_id=message.sender_id,
                    resolver_name=message.sender_name,
                    reason=reason,
                )
                return

        if not message.text.strip() and not message.media:
            return

        # Auto-spawn a dedicated conversation thread strictly AFTER authorization & command checks
        ensure_thread_fn: Any = getattr(adapter, "ensure_conversation_thread", None)
        if callable(ensure_thread_fn):
            with contextlib.suppress(Exception):
                updated_msg = await ensure_thread_fn(message)
                if isinstance(updated_msg, ChannelMessage):
                    message = updated_msg

        await self._schedule_conversation_turn(message)

    def register_rca_message(
        self,
        *,
        channel: str,
        conversation_id: str,
        message_id: str,
        lg_thread_id: str,
        thread_id: str | None = None,
        parent_channel_id: str | None = None,
        provider: str = "",
        title: str = "",
        severity: str = "",
        service_name: str = "",
        source_url: str = "",
        status: str = "completed",
        blocks_or_cards: Any = None,
        text: str = "",
    ) -> None:
        """Track a posted RCA card message so buttons and 👍/👎 reactions can lock & persist feedback."""
        norm_ch = channel.strip().lower()
        clean_msg_id = (message_id or "").strip()
        if not norm_ch or not clean_msg_id:
            return
        record: dict[str, Any] = {
            "channel": norm_ch,
            "conversation_id": conversation_id,
            "message_id": clean_msg_id,
            "thread_id": thread_id,
            "parent_channel_id": parent_channel_id or conversation_id,
            "lg_thread_id": lg_thread_id,
            "provider": provider,
            "title": title,
            "severity": severity,
            "service_name": service_name,
            "source_url": source_url,
            "status": status,
            "blocks_or_cards": blocks_or_cards,
            "text": text,
            "feedback_verdict": None,
            "feedback_by": None,
        }
        self._save_rca_message(record)

    def _save_rca_message(self, record: Mapping[str, Any]) -> None:
        norm_ch = str(record.get("channel") or "").strip().lower()
        msg_id = str(record.get("message_id") or "").strip()
        if not norm_ch or not msg_id:
            return
        rec_copy = dict(record)
        key = (norm_ch, msg_id)
        self._rca_messages[key] = rec_copy
        self._rca_messages.move_to_end(key)
        while len(self._rca_messages) > _RCA_MESSAGE_CACHE_CAP:
            self._rca_messages.popitem(last=False)
        if self.state_store is not None:
            with contextlib.suppress(Exception):
                self.state_store.put_conversation_ref(f"rca_msg:{norm_ch}", msg_id, rec_copy)

    def _get_rca_message(self, channel: str, message_id: str | None) -> dict[str, Any] | None:
        norm_ch = channel.strip().lower()
        clean_msg_id = (message_id or "").strip()
        if not norm_ch or not clean_msg_id:
            return None
        key = (norm_ch, clean_msg_id)
        cached = self._rca_messages.get(key)
        if cached is not None:
            return dict(cached)
        if self.state_store is not None:
            with contextlib.suppress(Exception):
                stored = self.state_store.get_conversation_ref(f"rca_msg:{norm_ch}", clean_msg_id)
                if isinstance(stored, Mapping):
                    rec = dict(stored)
                    self._rca_messages[key] = rec
                    return dict(rec)
        return None

    @staticmethod
    def _format_feedback_actor(channel: str, sender_id: str, sender_name: str) -> tuple[str, str]:
        plain = (sender_name or sender_id or "operator").strip()
        norm_ch = channel.strip().lower()
        if norm_ch == "slack" and sender_id:
            return f"<@{sender_id}>", plain
        if norm_ch == "discord":
            if sender_id.isdigit():
                return f"<@{sender_id}>", plain
            return (f"`@{sender_name}`" if sender_name else plain), plain
        return plain, plain

    async def _update_rca_card_for_feedback(
        self,
        *,
        channel: str,
        conversation_id: str,
        message_id: str | None,
        verdict: str,
        actor_markup: str,
        rca_rec: dict[str, Any] | None,
        meta: Mapping[str, Any],
    ) -> None:
        norm_ch = channel.strip().lower()
        adapter = self._adapters.get(norm_ch)
        if adapter is None or not message_id:
            return

        target_conv = str((rca_rec or {}).get("conversation_id") or conversation_id or "").strip()
        if not target_conv:
            return
        fallback_text = str(
            (rca_rec or {}).get("text")
            or meta.get("original_text")
            or meta.get("original_content")
            or "TalkOps Automated RCA"
        )
        updated_payload: Any = None

        if norm_ch == "slack":
            raw_blocks = (rca_rec or {}).get("blocks_or_cards")
            if not isinstance(raw_blocks, list) or not raw_blocks:
                raw_blocks = meta.get("original_blocks")
            if isinstance(raw_blocks, list) and raw_blocks:
                updated_payload = apply_rca_feedback_state_to_slack_blocks(
                    raw_blocks,
                    verdict=verdict,
                    actor=actor_markup,
                )
        elif norm_ch == "discord":
            stored = (rca_rec or {}).get("blocks_or_cards")
            embeds = stored.get("embeds") if isinstance(stored, Mapping) else None
            components = stored.get("components") if isinstance(stored, Mapping) else None
            if not isinstance(embeds, list) or not embeds:
                embeds = meta.get("original_embeds")
            if not isinstance(components, list) or not components:
                components = meta.get("original_components")
            if (isinstance(embeds, list) and embeds) or (isinstance(components, list) and components):
                updated_payload = apply_rca_feedback_state_to_discord_payload(
                    embeds if isinstance(embeds, list) else None,
                    components if isinstance(components, list) else None,
                    verdict=verdict,
                    actor=actor_markup,
                )
        elif norm_ch == "teams":
            raw_cards = (rca_rec or {}).get("blocks_or_cards")
            if isinstance(raw_cards, list) and raw_cards:
                updated_payload = apply_rca_feedback_state_to_teams_cards(
                    raw_cards,
                    verdict=verdict,
                    actor=actor_markup,
                )

        if updated_payload is None:
            return

        with contextlib.suppress(Exception):
            await adapter.update_message(
                target_conv,
                message_id,
                fallback_text,
                blocks_or_cards=updated_payload,
                metadata=meta,
            )
            if rca_rec is not None:
                rca_rec["blocks_or_cards"] = updated_payload
                self._save_rca_message(rca_rec)

    async def _persist_feedback_fallback(
        self,
        *,
        event_type: str,
        lg_thread_id: str,
        verdict: str,
        actor: str,
        reason: str,
        service_name: str,
        title: str,
    ) -> None:
        """Persist operator RCA feedback to OperationalStore when no event-bus subscriber handles it."""
        bus = self.event_bus or getattr(self.gateway, "event_bus", None)
        if bus is not None and hasattr(bus, "has_subscribers") and bus.has_subscribers(event_type):
            return
        if self.operational_store is None and self.project_root is None:
            return
        with contextlib.suppress(Exception):
            store = self.operational_store
            if store is None:
                from talkops.storage.factory import get_operational_store

                store = get_operational_store(project_root=self.project_root)
                await store.setup()
            from talkops.rubrics.stratified_autoqa import StratifiedAutoQAEvaluator

            evaluator = StratifiedAutoQAEvaluator(store=store)
            await evaluator.record_operator_feedback(
                lg_thread_id,
                verdict=verdict,
                actor=actor,
                reason=reason,
                service_name=service_name,
                title=title,
            )

    async def _handle_rca_feedback(
        self,
        *,
        channel: str,
        conversation_id: str,
        sender_id: str,
        sender_name: str = "",
        message_id: str | None = None,
        action_id: str = "",
        reason: str = "",
        is_dm: bool = False,
        metadata: Mapping[str, Any] | None = None,
        source: str = "button",
    ) -> str:
        """Authorize, lock, update card, publish, and persist operator RCA feedback."""
        meta = dict(metadata or {})
        rca_rec = self._get_rca_message(channel, message_id)
        parent_ch = (
            str(meta.get("parent_channel_id") or "").strip()
            or str((rca_rec or {}).get("parent_channel_id") or "").strip()
            or None
        )
        if not self._is_authorized(
            channel=channel,
            sender_id=sender_id,
            conversation_id=conversation_id,
            parent_channel_id=parent_ch,
            is_dm=is_dm,
        ):
            return "You are not authorized to submit feedback on this assistant."

        lg_tid = str(meta.get("lg_thread_id") or (rca_rec or {}).get("lg_thread_id") or "").strip()
        if not lg_tid:
            lg_tid, _ = await self._get_or_create_lg_thread(channel, conversation_id)

        verdict = str(meta.get("verdict") or action_id or "rejected").strip().lower()
        if ":" in verdict:
            verdict = verdict.split(":", 1)[0]
        if verdict in {"talkops_dispute", "dispute", "reject", "disputed"}:
            verdict = "rejected"
        elif verdict in {"talkops_confirm", "confirm", "useful", "helped", "confirmed"}:
            verdict = "helped"

        if rca_rec is not None and rca_rec.get("feedback_verdict"):
            prev_verdict = str(rca_rec["feedback_verdict"])
            prev_label = "Hypothesis Disputed" if prev_verdict == "rejected" else "RCA Confirmed"
            prev_by = str(rca_rec.get("feedback_by") or "an operator")
            return (
                f"Feedback already recorded ({prev_label}) by {prev_by} "
                f"for thread `{lg_tid[:8]}`."
            )

        actor_markup, actor_plain = self._format_feedback_actor(channel, sender_id, sender_name)
        service_name = str(
            meta.get("service_name") or (rca_rec or {}).get("service_name") or "*"
        ).strip()
        title = str(meta.get("title") or (rca_rec or {}).get("title") or "").strip()

        if message_id:
            if rca_rec is None:
                rca_rec = {
                    "channel": channel.strip().lower(),
                    "conversation_id": conversation_id,
                    "message_id": message_id,
                    "parent_channel_id": parent_ch or conversation_id,
                    "lg_thread_id": lg_tid,
                    "service_name": service_name,
                    "title": title,
                    "blocks_or_cards": None,
                    "text": str(meta.get("original_text") or meta.get("original_content") or ""),
                }
            rca_rec["feedback_verdict"] = verdict
            rca_rec["feedback_by"] = actor_plain
            self._save_rca_message(rca_rec)

        await self._update_rca_card_for_feedback(
            channel=channel,
            conversation_id=conversation_id,
            message_id=message_id,
            verdict=verdict,
            actor_markup=actor_markup,
            rca_rec=rca_rec,
            meta=meta,
        )

        event_type = f"feedback.{verdict}"
        canonical_status = (
            "disputed"
            if verdict == "rejected"
            else ("confirmed" if verdict == "helped" else verdict)
        )
        bus = self.event_bus or getattr(self.gateway, "event_bus", None)
        if bus is not None and hasattr(bus, "publish"):
            await bus.publish(
                event_type,
                {
                    "channel": channel,
                    "conversation_id": conversation_id,
                    "message_id": message_id or "",
                    "sender_id": sender_id,
                    "sender_name": actor_plain,
                    "action_id": action_id,
                    "verdict": verdict,
                    "canonical_status": canonical_status,
                    "source": source,
                    "reason": reason,
                    "rule_text": str(meta.get("rule_text") or reason or ""),
                    "service_name": service_name or "*",
                    "title": title,
                    "rule_id": meta.get("rule_id"),
                },
                thread_id=lg_tid,
                source_addon="channels",
            )

        await self._persist_feedback_fallback(
            event_type=event_type,
            lg_thread_id=lg_tid,
            verdict=verdict,
            actor=actor_plain,
            reason=reason,
            service_name=service_name,
            title=title,
        )

        if verdict == "helped":
            return f"Feedback recorded: RCA confirmed for thread `{lg_tid[:8]}`."
        if verdict == "rejected":
            return (
                f"Feedback recorded: Hypothesis disputed for thread `{lg_tid[:8]}`. "
                "Reply in this thread with corrections or missing context to continue investigating."
            )
        return f"Feedback (`{verdict}`) recorded for thread `{lg_tid[:8]}`."

    async def handle_inbound_reaction(self, reaction: ChannelReaction) -> None:
        """Resolve pending HITL tool approvals or record RCA feedback via 👍 / 👎 emoji reactions."""
        matched = self._approvals.match_reaction(
            channel=reaction.channel,
            message_id=reaction.message_id,
            emoji=reaction.emoji,
            conversation_id=reaction.conversation_id,
        )
        if matched is not None:
            pending, decision = matched
            await self._resolve_approval(
                pending,
                decision=decision,
                resolver_id=reaction.sender_id,
                resolver_name=reaction.sender_id,
            )
            return

        decision = normalize_reaction_decision(reaction.emoji)
        if decision not in {"approve", "reject"}:
            return
        rca_rec = self._get_rca_message(reaction.channel, reaction.message_id)
        if rca_rec is None:
            return

        meta = dict(reaction.metadata or {})
        verdict = "helped" if decision == "approve" else "rejected"
        is_dm = (
            reaction.conversation_id.startswith("D")
            if reaction.channel == "slack"
            else ("guild_id" in meta and meta.get("guild_id") is None)
        )
        await self._handle_rca_feedback(
            channel=reaction.channel,
            conversation_id=str(rca_rec.get("conversation_id") or reaction.conversation_id),
            sender_id=reaction.sender_id,
            sender_name=str(meta.get("sender_name") or "").strip(),
            message_id=reaction.message_id,
            action_id=f"reaction:{reaction.emoji}",
            is_dm=is_dm,
            metadata={
                **meta,
                "verdict": verdict,
                "lg_thread_id": str(rca_rec.get("lg_thread_id") or ""),
                "parent_channel_id": str(
                    meta.get("parent_channel_id") or rca_rec.get("parent_channel_id") or ""
                ),
            },
            source="reaction",
        )

    async def handle_inbound_interaction(self, interaction: ChannelInteraction) -> str | None:
        """Process interactive button clicks, Adaptive Card submissions, and slash commands."""
        if interaction.interaction_kind == "command" and interaction.command_name:
            cmd = COMMANDS_BY_NAME.get(interaction.command_name.lower())
            if cmd is None:
                return f"Unknown command `/{interaction.command_name}`. Try `/help`."
            parent_ch = str((interaction.metadata or {}).get("parent_channel_id") or "").strip() or None
            if not self._is_authorized(
                channel=interaction.channel,
                sender_id=interaction.sender_id,
                conversation_id=interaction.conversation_id,
                parent_channel_id=parent_ch,
                is_dm=interaction.is_dm,
            ):
                return "🔒 You are not authorized to run commands on this assistant."
            return await self._execute_command(
                cmd,
                interaction.command_args,
                channel=interaction.channel,
                conversation_id=interaction.conversation_id,
                sender_id=interaction.sender_id,
                is_dm=interaction.is_dm,
            )

        if interaction.interaction_kind == "approval" and interaction.decision:
            pending: PendingToolApproval | None = None
            if interaction.approval_id:
                pending = self._approvals.get_by_approval_id(interaction.approval_id)
            if pending is None and interaction.message_id:
                pending = self._approvals.get_by_message_id(
                    interaction.channel, interaction.message_id
                )
            if pending is None:
                pending = self._approvals.get_for_conversation(
                    interaction.channel, interaction.conversation_id
                )
            if pending is None:
                return "⚠️ This tool approval request has already been resolved or expired."

            await self._resolve_approval(
                pending,
                decision=interaction.decision,
                resolver_id=interaction.sender_id,
                resolver_name=interaction.sender_name or interaction.sender_id,
                reason=interaction.reason,
            )
            return f"Decision `{interaction.decision}` recorded."

        if interaction.interaction_kind == "feedback":
            return await self._handle_rca_feedback(
                channel=interaction.channel,
                conversation_id=interaction.conversation_id,
                sender_id=interaction.sender_id,
                sender_name=interaction.sender_name,
                message_id=interaction.message_id,
                action_id=interaction.action_id,
                reason=interaction.reason or "",
                is_dm=interaction.is_dm,
                metadata=interaction.metadata,
                source="button",
            )

        return None

    async def _resolve_approval(
        self,
        pending: PendingToolApproval,
        *,
        decision: ApprovalDecisionType,
        resolver_id: str,
        resolver_name: str = "",
        reason: str | None = None,
    ) -> None:
        """Verify authorization, update the approval card, and resume the LangGraph run."""
        adapter = self._adapters.get(pending.channel)
        if adapter is None:
            return

        is_allowed_resolver = (
            resolver_id == pending.requester_id
            or self._is_operator(pending.channel, resolver_id)
            or self._is_authorized(
                channel=pending.channel,
                sender_id=resolver_id,
                conversation_id=pending.conversation_id,
                is_dm=True,
            )
        )
        if not is_allowed_resolver:
            return

        popped = self._approvals.pop(pending.approval_id)
        if popped is None:
            return

        badge = "✅ Approved" if decision == "approve" else "🛑 Rejected"
        who = resolver_name or resolver_id
        summary_names = ", ".join(f"`{tc.name}`" for tc in pending.tool_calls)
        resolution_text = f"**{badge}** by `{who}` for {summary_names}."
        if reason:
            resolution_text += f"\n> Reason: _{reason}_"

        if pending.prompt_message_id:
            with contextlib.suppress(Exception):
                await adapter.update_message(
                    pending.conversation_id,
                    pending.prompt_message_id,
                    resolution_text,
                    blocks_or_cards=[],
                    metadata=pending.metadata,
                )
        else:
            with contextlib.suppress(Exception):
                await adapter.send(
                    pending.conversation_id,
                    resolution_text,
                    thread_id=pending.thread_id,
                    metadata=pending.metadata,
                )

        if self.gateway is None:
            return

        resume_payload = pending.build_resume_payload(decision, reason=reason)
        lock = self._get_lock(pending.channel, pending.conversation_id)
        async with lock:
            await self._stream_gateway_execution(
                adapter=adapter,
                conversation_id=pending.conversation_id,
                thread_id=pending.thread_id,
                lg_thread_id=pending.lg_thread_id,
                requester_id=pending.requester_id,
                input_or_command=resume_payload,
                is_resume=True,
                metadata=pending.metadata,
            )

    async def _execute_command(
        self,
        cmd: ChatCommand,
        arg_str: str,
        *,
        channel: str,
        conversation_id: str,
        sender_id: str,
        is_dm: bool,
    ) -> str:
        del is_dm
        key = (channel, conversation_id)

        if cmd.name == "help":
            return build_help_message()

        if cmd.name == "stop":
            cancelled = await self._cancel_active_turn(channel, conversation_id)
            self._approvals.pop_for_conversation(channel, conversation_id)
            return "🛑 Stopped current work." if cancelled else "No active run in this thread."

        if cmd.name in {"new", "reset-all-history"}:
            await self._cancel_active_turn(channel, conversation_id)
            self._approvals.pop_for_conversation(channel, conversation_id)
            new_tid, _ = await self._get_or_create_lg_thread(
                channel, conversation_id, force_new=True
            )
            self._hydrated_threads.discard(new_tid)
            return f"✨ Started a fresh conversation thread (`{new_tid[:8]}`)."

        if cmd.name == "status":
            lg_tid, _ = await self._get_or_create_lg_thread(channel, conversation_id)
            model = self._conversation_models.get(key, "default")
            mode = self._conversation_modes.get(key, self.default_approval_mode)
            ident = self._resolve_user_identity(channel, sender_id)
            busy = key in self._active_turn_tasks
            pending = self._approvals.get_for_conversation(channel, conversation_id)
            return (
                f"**TalkOps Conversation Status**\n"
                f"- **Channel**: `{channel}` (`{conversation_id}`)\n"
                f"- **LangGraph Thread**: `{lg_tid}`\n"
                f"- **State**: `{'awaiting_approval' if pending else ('running' if busy else 'idle')}`\n"
                f"- **Model**: `{model}`\n"
                f"- **Approval Mode**: `{mode}`\n"
                f"- **User RBAC Role**: `{ident.get('role', 'operator')}`"
            )

        if cmd.name == "model":
            val = arg_str.strip()
            if not val:
                current = self._conversation_models.get(key, "default")
                return f"Current model for this thread: `{current}`. Use `/model <provider:model|default>` to change."
            if val.lower() == "default":
                self._conversation_models.pop(key, None)
                return "Restored default model for this thread."
            self._conversation_models[key] = val
            return f"Switched model for this thread to `{val}`."

        if cmd.name == "smart-model":
            val = arg_str.strip()
            if not val:
                current = self._conversation_smart_models.get(key, "default")
                return f"Current smart-mode classifier model: `{current}`. Use `/smart-model <provider:model|default>`."
            if val.lower() == "default":
                self._conversation_smart_models.pop(key, None)
                return "Restored default smart-mode classifier model for this thread."
            self._conversation_smart_models[key] = val
            return f"Switched smart-mode classifier model for this thread to `{val}`."

        if cmd.name == "effort":
            val = arg_str.strip().lower()
            if not val:
                current = self._conversation_efforts.get(key, "default")
                return f"Current reasoning effort: `{current}` (`default` | `low` | `medium` | `high` | `max`)."
            if val not in {"default", "low", "medium", "high", "max"}:
                return "Invalid effort level. Choose `default`, `low`, `medium`, `high`, or `max`."
            if val == "default":
                self._conversation_efforts.pop(key, None)
                return "Restored default reasoning effort for this thread."
            self._conversation_efforts[key] = val
            return f"🧠 Reasoning effort set to `{val}` for this thread."

        if cmd.name == "mcp-reload":
            if self.gateway is not None and hasattr(self.gateway, "reload_mcp"):
                with contextlib.suppress(Exception):
                    await self.gateway.reload_mcp()
                    return "🔄 Reloaded MCP server connections."
            return "🔄 MCP reload acknowledged."

        if cmd.name == "mode":
            val = arg_str.strip().lower()
            if not val:
                current = self._conversation_modes.get(key, self.default_approval_mode)
                return f"Current tool approval mode: `{current}` (`manual` | `smart` | `jev`)."
            if val not in {"manual", "smart", "jev"}:
                return "Invalid approval mode. Choose `manual`, `smart`, or `jev`."
            self._conversation_modes[key] = val
            return f"🛡️ Tool approval mode set to `{val}` for this thread."

        if cmd.name == "context-doctor":
            lg_tid, _ = await self._get_or_create_lg_thread(channel, conversation_id)
            msg_count = 0
            if self.gateway is not None and hasattr(self.gateway, "get_thread_state"):
                with contextlib.suppress(Exception):
                    state = await self.gateway.get_thread_state(lg_tid)
                    values = (
                        state.get("values")
                        if isinstance(state, Mapping)
                        else getattr(state, "values", None)
                    )
                    if isinstance(values, Mapping) and isinstance(values.get("messages"), list):
                        msg_count = len(values["messages"])
            return (
                f"🩺 **Context Doctor**\n"
                f"- **Thread ID**: `{lg_tid}`\n"
                f"- **Hydrated History**: `{lg_tid in self._hydrated_threads}`\n"
                f"- **Checkpoint Messages**: `{msg_count}`"
            )

        if cmd.name == "pair":
            if not self._is_operator(channel, sender_id):
                return "🔒 Only the configured channel operator can run `/pair`."
            return await self._handle_pair_command(arg_str)

        return "Done."

    async def _handle_pair_command(self, arg_str: str) -> str:
        parts = arg_str.strip().split()
        sub = parts[0].lower() if parts else "list"

        if sub == "list":
            pending_list, approved_list = self.pairing_store.list_all()
            lines = ["**Sender Pairing Status**"]
            if pending_list:
                lines.append("\n_Pending Requests:_")
                for p in pending_list:
                    lines.append(
                        f"- `{p.channel}` sender `{p.sender_id}` ({p.sender_name or 'unknown'}) — code `{format_code(p.code)}`"
                    )
            else:
                lines.append("\n_No pending pairing requests._")
            if approved_list:
                lines.append("\n_Approved Senders:_")
                for a in approved_list:
                    lines.append(
                        f"- `{a.channel}` sender `{a.sender_id}` ({a.sender_name or 'unknown'})"
                    )
            else:
                lines.append("\n_No paired senders._")
            return "\n".join(lines)

        if sub == "approve" and len(parts) >= 2:
            raw_code = parts[1]
            paired = self.pairing_store.approve_code(raw_code)
            if paired is None:
                return f"⚠️ No active pending pairing request matches code `{raw_code}`."
            target_adapter = self._adapters.get(paired.channel)
            if target_adapter is not None and paired.conversation_id:
                with contextlib.suppress(Exception):
                    await target_adapter.send(
                        paired.conversation_id,
                        "✅ Your pairing request was approved by the operator! You can now chat with TalkOps.",
                    )
            return (
                f"✅ Approved `{paired.channel}` sender `{paired.sender_id}` "
                f"({paired.sender_name or 'paired user'})."
            )

        if sub == "revoke" and len(parts) >= 3:
            target_channel = parts[1].lower()
            target_sender = parts[2]
            revoked = self.pairing_store.revoke(target_channel, target_sender)
            if revoked is None:
                return f"⚠️ Sender `{target_sender}` was not found in approved list for `{target_channel}`."
            for (ch, conv_id), active_sender in list(self._active_turn_senders.items()):
                if ch == target_channel and active_sender == target_sender:
                    await self._cancel_active_turn(ch, conv_id)
            return f"🔒 Revoked pairing for `{target_channel}` sender `{target_sender}`."

        return "Usage: `/pair list`, `/pair approve <code>`, or `/pair revoke <channel> <sender_id>`."

    async def _cancel_active_turn(self, channel: str, conversation_id: str) -> bool:
        key = (channel, conversation_id)
        task = self._active_turn_tasks.get(key)
        cancelled = False
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            cancelled = True
        lg_tid, _ = await self._get_or_create_lg_thread(channel, conversation_id)
        if self.gateway is not None and hasattr(self.gateway, "cancel_thread_runs"):
            with contextlib.suppress(Exception):
                count = await self.gateway.cancel_thread_runs(lg_tid)
                cancelled = cancelled or bool(count)
        if cancelled:
            await self._recover_interrupted_thread(lg_tid)
        return cancelled

    async def _recover_interrupted_thread(self, lg_thread_id: str) -> None:
        """Patch dangling `tool_calls` on an interrupted LangGraph thread (matching Talon `recover_interrupted`)."""
        if self.gateway is None or not hasattr(self.gateway, "get_thread_state"):
            return
        try:
            state = await self.gateway.get_thread_state(lg_thread_id)
            values = (
                state.get("values")
                if isinstance(state, Mapping)
                else getattr(state, "values", None)
            )
            if not isinstance(values, Mapping):
                return
            messages = values.get("messages")
            if not isinstance(messages, list) or not messages:
                return
            last_msg = messages[-1]
            tool_calls = (
                last_msg.get("tool_calls")
                if isinstance(last_msg, Mapping)
                else getattr(last_msg, "tool_calls", None)
            )
            if not isinstance(tool_calls, list) or not tool_calls:
                return
            patch_messages: list[dict[str, Any]] = []
            for tc in tool_calls:
                tc_id = (
                    tc.get("id")
                    if isinstance(tc, Mapping)
                    else getattr(tc, "id", None)
                ) or "call_cancelled"
                tc_name = (
                    tc.get("name")
                    if isinstance(tc, Mapping)
                    else getattr(tc, "name", None)
                ) or "tool"
                patch_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(tc_id),
                        "name": str(tc_name),
                        "content": f"Tool call {tc_name} ({tc_id}) was cancelled because the turn was interrupted.",
                    }
                )
            if patch_messages and hasattr(self.gateway, "update_thread_state"):
                await self.gateway.update_thread_state(
                    lg_thread_id, {"messages": patch_messages}
                )
        except Exception:
            pass

    async def _schedule_conversation_turn(self, message: ChannelMessage) -> None:
        """Schedule a conversation turn using `multitask_strategy` (`interrupt` or `enqueue`)."""
        key = (message.channel, message.conversation_id)
        if self.multitask_strategy == "interrupt":
            existing = self._active_turn_tasks.get(key)
            if existing is not None and not existing.done():
                await self._cancel_active_turn(message.channel, message.conversation_id)

        task = asyncio.create_task(
            self._run_conversation_turn(message),
            name=f"talkops-channel-turn-{message.channel}-{message.conversation_id}",
        )
        self._active_turn_tasks[key] = task
        self._active_turn_senders[key] = message.sender_id
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            if self._active_turn_tasks.get(key) is task:
                self._active_turn_tasks.pop(key, None)
                self._active_turn_senders.pop(key, None)

    async def _run_conversation_turn(self, message: ChannelMessage) -> None:
        adapter = self._adapters.get(message.channel)
        if adapter is None:
            return
        lock = self._get_lock(message.channel, message.conversation_id)
        async with lock:
            lg_tid, is_new = await self._get_or_create_lg_thread(
                message.channel, message.conversation_id
            )

            prompt_text = message.text.strip()
            should_hydrate = is_new
            if not should_hydrate and lg_tid not in self._hydrated_threads:
                should_hydrate = not await self._thread_has_checkpoint_messages(lg_tid)
            if (
                should_hydrate
                and message.thread_id
                and not (message.metadata or {}).get("newly_created_thread")
            ):
                prior_context = await adapter.fetch_thread_context(
                    message.conversation_id,
                    message.thread_id,
                    metadata=message.metadata,
                )
                self._hydrated_threads.add(lg_tid)
                if prior_context:
                    bounded = prior_context[-THREAD_CONTEXT_MAX_CHARS:]
                    prompt_text = (
                        f"<thread_context>\n{bounded}\n</thread_context>\n\n"
                        f"User message: {prompt_text}"
                    )
            else:
                self._hydrated_threads.add(lg_tid)

            if message.media:
                prompt_text = build_inbound_media_prompt(prompt_text, message.media)

            content_payload = build_multimodal_content(prompt_text, message.media)
            input_payload = {"messages": [{"role": "user", "content": content_payload}]}

            add_react_fn: Any = getattr(adapter, "add_reaction", None)
            remove_react_fn: Any = getattr(adapter, "remove_reaction", None)
            if add_react_fn is not None and message.message_id:
                with contextlib.suppress(Exception):
                    await add_react_fn(message.conversation_id, message.message_id, "eyes")

            try:
                await self._stream_gateway_execution(
                    adapter=adapter,
                    conversation_id=message.conversation_id,
                    thread_id=message.thread_id,
                    lg_thread_id=lg_tid,
                    requester_id=message.sender_id,
                    requester_name=message.sender_name,
                    input_or_command=input_payload,
                    is_resume=False,
                    metadata=message.metadata,
                )
            finally:
                if remove_react_fn is not None and message.message_id:
                    with contextlib.suppress(Exception):
                        await remove_react_fn(message.conversation_id, message.message_id, "eyes")
                if add_react_fn is not None and message.message_id:
                    with contextlib.suppress(Exception):
                        await add_react_fn(
                            message.conversation_id,
                            message.message_id,
                            "white_check_mark",
                        )

    async def _stream_gateway_execution(
        self,
        *,
        adapter: WebhookChannelAdapter,
        conversation_id: str,
        thread_id: str | None,
        lg_thread_id: str,
        requester_id: str,
        requester_name: str = "",
        input_or_command: Any,
        is_resume: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if self.gateway is None:
            await adapter.send(
                conversation_id,
                "⚠️ TalkOps `AgentRunGateway` is not bound on this server instance.",
                thread_id=thread_id,
                metadata=metadata,
            )
            return

        key = (adapter.name, conversation_id)
        user_ctx = self._resolve_user_identity(adapter.name, requester_id, requester_name)
        run_context: dict[str, Any] = {
            "thread_id": lg_thread_id,
            "channel": adapter.name,
            "conversation_id": conversation_id,
            "user_identity": user_ctx,
            "approval_mode": self._conversation_modes.get(key, self.default_approval_mode),
        }
        if key in self._conversation_models:
            run_context["model"] = self._conversation_models[key]
        if key in self._conversation_smart_models:
            run_context["smart_model"] = self._conversation_smart_models[key]
        if key in self._conversation_efforts:
            run_context["effort"] = self._conversation_efforts[key]

        sink = ChannelStreamSink(
            adapter,
            conversation_id=conversation_id,
            thread_id=thread_id,
            lg_thread_id=lg_thread_id,
            requester_id=requester_id,
            approval_registry=self._approvals,
            verbosity=self.verbosity,
            edit_debounce_seconds=self.edit_debounce_seconds,
            metadata=metadata,
        )

        caps = getattr(adapter, "capabilities", None)
        typing_interval = getattr(caps, "typing_refresh_seconds", TYPING_REFRESH_SECONDS)
        typing_stop = asyncio.Event()
        typing_task: asyncio.Task[None] | None = None
        if isinstance(typing_interval, (int, float)) and typing_interval > 0:
            typing_task = asyncio.create_task(
                self._typing_loop(
                    adapter,
                    conversation_id,
                    thread_id,
                    typing_stop,
                    metadata,
                    refresh_seconds=float(typing_interval),
                )
            )
        ctx_token = ACTIVE_CHANNEL_CONVERSATION.set((adapter.name, conversation_id, thread_id))

        try:
            stream_target = input_or_command
            if is_resume and not isinstance(input_or_command, Command):
                stream_target = Command(resume=input_or_command)

            if hasattr(self.gateway, "stream_run"):
                raw_stream = self.gateway.stream_run(
                    stream_target,
                    thread_id=lg_thread_id,
                    source_addon="channels",
                    context=run_context,
                )
                norm_stream = normalize_gateway_stream(raw_stream)
                bridge = GraphStreamBridge(sink)
                await bridge.process_stream(
                    norm_stream,
                    channel_id=conversation_id,
                    thread_id=lg_thread_id,
                )
            elif hasattr(self.gateway, "trigger_run"):
                handle = await self.gateway.trigger_run(
                    stream_target,
                    thread_id=lg_thread_id,
                    source_addon="channels",
                    context=run_context,
                    wait=True,
                )
                analysis = getattr(handle, "analysis_text", "") or ""
                if analysis:
                    await sink.on_stream_start(conversation_id, lg_thread_id)
                    await sink.on_token(analysis)
                    await sink.on_stream_end()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await sink.on_error(exc)
        finally:
            ACTIVE_CHANNEL_CONVERSATION.reset(ctx_token)
            typing_stop.set()
            if typing_task is not None:
                typing_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await typing_task

    async def _typing_loop(
        self,
        adapter: WebhookChannelAdapter,
        conversation_id: str,
        thread_id: str | None,
        stop_event: asyncio.Event,
        metadata: Mapping[str, Any] | None,
        *,
        refresh_seconds: float = TYPING_REFRESH_SECONDS,
    ) -> None:
        while not stop_event.is_set():
            with contextlib.suppress(Exception):
                await adapter.send_typing(
                    conversation_id,
                    thread_id=thread_id,
                    metadata=metadata,
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=refresh_seconds)
            except TimeoutError:
                continue

    def resolve_thread_conversation_id(
        self,
        channel: str,
        conversation_id: str,
        sent: SendResult,
    ) -> str:
        """Resolve the bound thread conversation key for `sent` via the target adapter."""
        adapter = self._adapters.get(channel.strip().lower())
        resolver = getattr(adapter, "resolve_thread_conversation_id", None) if adapter else None
        if callable(resolver):
            return str(resolver(conversation_id, sent))
        return sent.thread_id or sent.conversation_id or conversation_id

    async def send_proactive_notification(
        self,
        *,
        channel: str,
        conversation_id: str,
        text: str,
        thread_id: str | None = None,
        lg_thread_id: str | None = None,
        media: Sequence[ChannelMedia] = (),
        blocks_or_cards: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Send an outbound notification or open an interactive incident thread on a channel.

        When `lg_thread_id` is supplied (for example, from an inbound PagerDuty or
        Alertmanager webhook run), binds the resulting platform thread to `lg_thread_id`
        in `ThreadStore` so engineers can reply in-thread to continue investigating.
        """
        adapter = self._adapters.get(channel.strip().lower())
        if adapter is None:
            raise ValueError(f"Channel provider '{channel}' is not registered or enabled.")

        outbound_text = (
            text if blocks_or_cards is not None else format_for_channel(adapter.name, text)
        )
        send_kwargs: dict[str, Any] = {
            "thread_id": thread_id,
            "blocks_or_cards": blocks_or_cards,
            "metadata": metadata,
        }
        if media:
            send_kwargs["media"] = media
        try:
            sent = await adapter.send(conversation_id, outbound_text, **send_kwargs)
        except TypeError:
            send_kwargs.pop("media", None)
            sent = await adapter.send(conversation_id, outbound_text, **send_kwargs)
        if lg_thread_id:
            target_conv = self.resolve_thread_conversation_id(adapter.name, conversation_id, sent)
            await self._bind_lg_thread(adapter.name, target_conv, lg_thread_id)
        return sent

    async def create_incident_thread(
        self,
        *,
        channel: str,
        conversation_id: str,
        title: str,
        summary_text: str,
        lg_thread_id: str | None = None,
        blocks_or_cards: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Create an incident thread on `channel` and bind it to `lg_thread_id` in `ThreadStore`."""
        adapter = self._adapters.get(channel.strip().lower())
        if adapter is None:
            raise ValueError(f"Channel provider '{channel}' is not registered or enabled.")

        merged_meta: dict[str, Any] = dict(metadata or {})
        if lg_thread_id and "lg_thread_id" not in merged_meta:
            merged_meta["lg_thread_id"] = lg_thread_id

        parsed = parse_incident_output(
            adapter.name,
            title=title,
            summary=str(merged_meta.get("summary") or summary_text),
            provider=str(merged_meta.get("provider") or ""),
            severity=str(merged_meta.get("severity") or ""),
            service_name=str(merged_meta.get("service_name") or ""),
            source_url=str(merged_meta.get("source_url") or ""),
            lg_thread_id=str(merged_meta.get("lg_thread_id") or ""),
            investigation_state=str(merged_meta.get("investigation_state") or "investigating"),  # type: ignore[arg-type]
            rca_preview=str(merged_meta.get("rca_preview") or ""),
        )
        effective_blocks_or_cards = (
            blocks_or_cards if blocks_or_cards is not None else parsed.blocks_or_cards
        )

        create_thread_fn: Any = getattr(adapter, "create_incident_thread", None)
        if create_thread_fn is not None:
            call_kwargs: dict[str, Any] = {
                "title": title,
                "initial_text": format_for_channel(adapter.name, summary_text),
                "metadata": merged_meta,
            }
            if effective_blocks_or_cards is not None:
                call_kwargs["blocks_or_cards"] = effective_blocks_or_cards
            try:
                sent = await create_thread_fn(conversation_id, **call_kwargs)
            except TypeError:
                call_kwargs.pop("blocks_or_cards", None)
                sent = await create_thread_fn(conversation_id, **call_kwargs)
        else:
            sent = await adapter.send(
                conversation_id,
                parsed.text,
                blocks_or_cards=effective_blocks_or_cards,
                metadata=merged_meta,
            )

        if lg_thread_id:
            target_conv = self.resolve_thread_conversation_id(adapter.name, conversation_id, sent)
            await self._bind_lg_thread(adapter.name, target_conv, lg_thread_id)
        return sent

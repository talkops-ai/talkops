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

"""Slack Central HTTP Webhook Channel Adapter (`SlackWebhookChannel`).

Handles Slack Events API (`POST /channels/slack/events`), Slack Interactivity
Block Kit callbacks (`POST /channels/slack/interactions`), and Slack Slash Commands
(`POST /channels/slack/commands`) with HMAC-SHA256 `X-Slack-Signature` verification
and Slack Web API outbound delivery.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import os
import re
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from talkops.channels.formatting import (
    SLACK_MAX_BLOCKS,
    SLACK_MAX_TEXT_CHARS,
    ChannelExposure,
    build_slack_incident_attachment_payload,
    build_slack_response_blocks,
    chunk_text,
    decode_slack_mrkdwn,
    exposure_from_mapping,
    format_markdown_for_slack,
    strip_slack_expand_flags,
    strip_slack_expand_flags_from_attachments,
)
from talkops.channels.interfaces import (
    ApprovalCardSpec,
    ChannelCapabilities,
    ChannelInteraction,
    ChannelMedia,
    ChannelMessage,
    ChannelReaction,
    ChannelStatus,
    InteractionHandler,
    MessageHandler,
    ReactionHandler,
    SendResult,
    WebhookHTTPResponse,
)
from talkops.channels.media import (
    contains_oauth_callback_url,
    resolve_bounded_media_path,
)
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

SLACK_API_BASE = "https://slack.com/api"
SIGNATURE_TTL_SECONDS = 300
_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]+)?>")
_SEEN_EVENT_CAP = 1024


def parse_slack_conversation_id(conversation_id: str) -> tuple[str, str | None]:
    """Split `<channel_id>:<thread_ts>` into `(channel_id, thread_ts)`."""
    if ":" in conversation_id:
        ch, ts = conversation_id.split(":", 1)
        return ch.strip(), ts.strip() or None
    return conversation_id.strip(), None


def format_slack_conversation_id(channel_id: str, thread_ts: str | None) -> str:
    """Format `(channel_id, thread_ts)` into canonical `<channel_id>:<thread_ts>` conversation notation."""
    if thread_ts:
        return f"{channel_id}:{thread_ts}"
    return channel_id


def build_slack_approval_blocks(spec: ApprovalCardSpec) -> list[dict[str, Any]]:
    """Build Slack Block Kit blocks with Approve and Reject interactive buttons."""
    tool_lines: list[str] = []
    for idx, tc in enumerate(spec.tool_calls, start=1):
        args_str = ", ".join(f"{k}={v!r}" for k, v in list(tc.args.items())[:5])
        tool_lines.append(f"*{idx}. `{tc.name}`*\n`{args_str or '(no args)'}`")
        if tc.description:
            tool_lines.append(f"_{tc.description}_")

    body_mrkdwn = "\n".join(tool_lines) if tool_lines else format_markdown_for_slack(spec.prompt_text)
    action_payload_approve = json.dumps(
        {
            "approval_id": spec.approval_id,
            "decision": "approve",
            "lg_thread_id": spec.lg_thread_id,
            "run_id": spec.run_id,
            "conversation_id": spec.conversation_id,
        }
    )
    action_payload_reject = json.dumps(
        {
            "approval_id": spec.approval_id,
            "decision": "reject",
            "lg_thread_id": spec.lg_thread_id,
            "run_id": spec.run_id,
            "conversation_id": spec.conversation_id,
        }
    )

    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🛡️ {spec.title}"[:150],
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": body_mrkdwn[:3000],
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Requested by <@{spec.requester_id}> • "
                        "Click a button, react with 👍 / 👎, or reply `approve` / `deny <reason>`."
                    ),
                }
            ],
        },
        {
            "type": "actions",
            "block_id": f"approval_actions_{spec.approval_id}",
            "elements": [
                {
                    "type": "button",
                    "action_id": f"talkops_approve:{spec.approval_id}",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "✅ Approve", "emoji": True},
                    "value": action_payload_approve,
                },
                {
                    "type": "button",
                    "action_id": f"talkops_reject:{spec.approval_id}",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "🛑 Reject", "emoji": True},
                    "value": action_payload_reject,
                },
            ],
        },
    ]


@dataclass(frozen=True, slots=True)
class SlackChannelConfig:
    """Configuration for the Slack HTTP webhook channel adapter."""

    bot_token: str = field(default="", repr=False)
    signing_secret: str = field(default="", repr=False)
    bot_user_id: str | None = None
    api_base: str = SLACK_API_BASE
    exposure: ChannelExposure = field(default_factory=ChannelExposure)
    verify_signatures: bool = True
    include_other_thread_participants: bool = False
    request_timeout_seconds: float = 30.0

    @classmethod
    def from_mapping(
        cls,
        cfg: Mapping[str, Any],
        env: Mapping[str, str],
    ) -> SlackChannelConfig:
        """Construct `SlackChannelConfig` from addon config mapping and environment variables."""
        bot_token = str(
            cfg.get("bot_token")
            or env.get("TALKOPS_CHANNELS_SLACK_BOT_TOKEN")
            or env.get("TALKOPS_SLACK_BOT_TOKEN")
            or env.get("SLACK_BOT_TOKEN")
            or ""
        ).strip()
        signing_secret = str(
            cfg.get("signing_secret")
            or env.get("TALKOPS_CHANNELS_SLACK_SIGNING_SECRET")
            or env.get("TALKOPS_SLACK_SIGNING_SECRET")
            or env.get("SLACK_SIGNING_SECRET")
            or ""
        ).strip()
        bot_user_id = (
            str(
                cfg.get("bot_user_id")
                or env.get("TALKOPS_CHANNELS_SLACK_BOT_USER_ID")
                or env.get("TALKOPS_SLACK_BOT_USER_ID")
                or env.get("SLACK_BOT_USER_ID")
                or ""
            ).strip()
            or None
        )
        api_base = str(
            cfg.get("api_base")
            or env.get("TALKOPS_CHANNELS_SLACK_API_BASE")
            or SLACK_API_BASE
        ).rstrip("/")
        verify_sig = cfg.get("verify_signatures")
        if verify_sig is None:
            verify_sig = env.get("TALKOPS_CHANNELS_SLACK_VERIFY_SIGNATURES", "1")
        verify_signatures = str(verify_sig).strip().lower() not in {"0", "false", "no", "off"}

        exposure = exposure_from_mapping("slack", cfg, env)
        return cls(
            bot_token=bot_token,
            signing_secret=signing_secret,
            bot_user_id=bot_user_id,
            api_base=api_base,
            exposure=exposure,
            verify_signatures=verify_signatures,
            include_other_thread_participants=bool(
                cfg.get("include_other_thread_participants", False)
            ),
        )


class SlackWebhookChannel:
    """Slack channel adapter operating over central HTTP webhooks (`Events API` + `Interactivity`)."""

    name = "slack"
    capabilities = ChannelCapabilities(
        max_text_chars=SLACK_MAX_TEXT_CHARS,
        edit_debounce_seconds=1.0,
        typing_refresh_seconds=0.0,
        supports_threads=True,
        auto_spawn_thread_on_mention=False,
        supports_streaming_edits=True,
        supports_reactions=True,
        supports_attachments=True,
        supports_ephemeral_interactions=True,
        interaction_token_ttl_seconds=1800.0,
        supported_transports=("webhook",),
    )

    def __init__(
        self,
        config: SlackChannelConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialize `SlackWebhookChannel` with `config` and optional `http_client`."""
        self.config = config
        self._client = http_client
        self._owns_client = http_client is None
        self._bot_user_id: str | None = config.bot_user_id
        self._message_handler: MessageHandler | None = None
        self._reaction_handler: ReactionHandler | None = None
        self._interaction_handler: InteractionHandler | None = None
        self._seen_events: OrderedDict[str, None] = OrderedDict()
        self._sent_threads: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def set_message_handler(self, handler: MessageHandler) -> None:
        """Register inbound message callback."""
        self._message_handler = handler

    def set_reaction_handler(self, handler: ReactionHandler) -> None:
        """Register inbound reaction callback."""
        self._reaction_handler = handler

    def set_interaction_handler(self, handler: InteractionHandler) -> None:
        """Register inbound interaction/command callback."""
        self._interaction_handler = handler

    async def start(self) -> None:
        """Initialize HTTP client and resolve bot user ID when configured."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.request_timeout_seconds)
        if (
            not self._bot_user_id
            and self.config.bot_token
            and (not self._owns_client or "PYTEST_CURRENT_TEST" not in os.environ)
        ):
            try:
                resp = await self._api_call("auth.test", {})
                if resp.get("ok") and resp.get("user_id"):
                    self._bot_user_id = str(resp["user_id"])
            except Exception:
                pass

    async def stop(self) -> None:
        """Cancel background tasks and close owned HTTP client."""
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _dedup_event(self, event_id: str) -> bool:
        """Return `True` if `event_id` is new, `False` if already processed."""
        if not event_id:
            return True
        if event_id in self._seen_events:
            return False
        self._seen_events[event_id] = None
        while len(self._seen_events) > _SEEN_EVENT_CAP:
            self._seen_events.popitem(last=False)
        return True

    def verify_signature(
        self,
        headers: Mapping[str, str],
        body: bytes,
        *,
        now: float | None = None,
    ) -> bool:
        """Verify Slack `X-Slack-Signature` (`v0=<hmac_sha256>`) and timestamp freshness."""
        if not self.config.verify_signatures:
            return True
        if not self.config.signing_secret:
            return False

        lowered = {k.lower(): v for k, v in headers.items()}
        timestamp_str = lowered.get("x-slack-request-timestamp", "").strip()
        signature = lowered.get("x-slack-signature", "").strip()
        if not timestamp_str or not signature:
            return False
        try:
            ts = int(timestamp_str)
        except ValueError:
            return False

        current = time.time() if now is None else now
        if abs(current - ts) > SIGNATURE_TTL_SECONDS:
            return False

        basestring = f"v0:{timestamp_str}:".encode() + body
        digest = hmac.new(
            self.config.signing_secret.encode("utf-8"),
            basestring,
            hashlib.sha256,
        ).hexdigest()
        expected = f"v0={digest}"
        return hmac.compare_digest(expected, signature)

    async def handle_webhook_event(
        self,
        headers: Mapping[str, str],
        body: bytes,
        query_params: Mapping[str, str] | None = None,
    ) -> WebhookHTTPResponse:
        """Handle `POST /channels/slack/events` (URL verification + Events API callbacks)."""
        del query_params
        lowered = {k.lower(): v for k, v in headers.items()}
        if not self.verify_signature(lowered, body):
            return WebhookHTTPResponse(
                status_code=401,
                body={"ok": False, "error": "invalid_slack_signature"},
            )

        if lowered.get("x-slack-retry-num"):
            return WebhookHTTPResponse(status_code=200, body={"ok": True, "ignored": "retry"})

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return WebhookHTTPResponse(status_code=400, body={"ok": False, "error": "invalid_json"})

        if not isinstance(payload, Mapping):
            return WebhookHTTPResponse(status_code=400, body={"ok": False, "error": "invalid_payload"})

        if payload.get("type") == "url_verification":
            challenge = str(payload.get("challenge", ""))
            return WebhookHTTPResponse(status_code=200, body={"challenge": challenge})

        if not self._bot_user_id:
            auths = payload.get("authorizations")
            if isinstance(auths, Sequence) and auths and isinstance(auths[0], Mapping):
                uid = auths[0].get("user_id")
                if uid:
                    self._bot_user_id = str(uid)

        event_id = str(payload.get("event_id") or "")
        if event_id and not self._dedup_event(event_id):
            return WebhookHTTPResponse(status_code=200, body={"ok": True, "deduplicated": True})

        event = payload.get("event")
        if not isinstance(event, Mapping):
            return WebhookHTTPResponse(status_code=200, body={"ok": True})

        event_type = str(event.get("type") or "")
        if event_type in {"message", "app_mention"}:
            parsed_msg = self._parse_message_event(event, event_type=event_type)
            if parsed_msg is not None and self._message_handler is not None:
                self._spawn(self._message_handler(parsed_msg))
        elif event_type == "reaction_added":
            parsed_react = self._parse_reaction_event(event)
            if parsed_react is not None and self._reaction_handler is not None:
                self._spawn(self._reaction_handler(parsed_react))

        return WebhookHTTPResponse(status_code=200, body={"ok": True})

    def _parse_message_event(
        self,
        event: Mapping[str, Any],
        *,
        event_type: str,
    ) -> ChannelMessage | None:
        subtype = str(event.get("subtype") or "")
        if subtype and subtype not in {"file_share", "thread_broadcast"}:
            return None
        if event.get("bot_id"):
            return None
        sender_id = str(event.get("user") or "").strip()
        if not sender_id:
            return None
        if self._bot_user_id and sender_id == self._bot_user_id:
            return None

        channel_id = str(event.get("channel") or "").strip()
        ts = str(event.get("ts") or "").strip()
        if not channel_id or not ts:
            return None

        msg_dedup_key = f"msg:{channel_id}:{ts}"
        if not self._dedup_event(msg_dedup_key):
            return None

        thread_ts = str(event.get("thread_ts") or ts).strip()
        channel_type = str(event.get("channel_type") or "")
        is_dm = channel_type in {"im", "mpim"} or channel_id.startswith("D")

        raw_text = str(event.get("text") or "")
        mentioned = event_type == "app_mention"
        if self._bot_user_id and f"<@{self._bot_user_id}" in raw_text:
            mentioned = True

        cleaned_text = raw_text
        if self._bot_user_id:
            cleaned_text = re.sub(
                rf"<@{re.escape(self._bot_user_id)}(?:\|[^>]+)?>",
                "",
                cleaned_text,
            ).strip()
        elif event_type == "app_mention":
            cleaned_text = _MENTION_RE.sub("", cleaned_text, count=1).strip()

        cleaned_text = decode_slack_mrkdwn(cleaned_text)
        if not mentioned and self.config.exposure.matches_mention_pattern(cleaned_text):
            mentioned = True

        has_explicit_thread = bool(event.get("thread_ts"))
        conv_id = (
            format_slack_conversation_id(channel_id, thread_ts)
            if (not is_dm or has_explicit_thread)
            else channel_id
        )

        media_items: list[ChannelMedia] = []
        raw_files = event.get("files")
        if isinstance(raw_files, Sequence):
            for f in raw_files:
                if not isinstance(f, Mapping):
                    continue
                mime = str(f.get("mimetype") or "application/octet-stream")
                kind = "document"
                if mime.startswith("image/"):
                    kind = "image"
                elif mime.startswith("audio/"):
                    kind = "audio"
                elif mime.startswith("video/"):
                    kind = "video"
                media_items.append(
                    ChannelMedia(
                        media_type=kind,  # type: ignore[arg-type]
                        url=str(f.get("url_private_download") or f.get("url_private") or ""),
                        mime_type=mime,
                        filename=str(f.get("name") or f.get("id") or "file"),
                        size_bytes=int(f["size"]) if isinstance(f.get("size"), int) else None,
                        metadata={"file_id": f.get("id")},
                    )
                )

        return ChannelMessage(
            channel="slack",
            conversation_id=conv_id,
            sender_id=sender_id,
            text=cleaned_text,
            message_id=ts,
            thread_id=thread_ts if (not is_dm or has_explicit_thread) else None,
            parent_channel_id=channel_id,
            is_dm=is_dm,
            mentioned=mentioned,
            media=tuple(media_items),
            metadata={
                "channel_id": channel_id,
                "ts": ts,
                "thread_ts": event.get("thread_ts"),
                "channel_type": channel_type,
            },
        )

    def _parse_reaction_event(self, event: Mapping[str, Any]) -> ChannelReaction | None:
        sender_id = str(event.get("user") or "").strip()
        if not sender_id or (self._bot_user_id and sender_id == self._bot_user_id):
            return None
        emoji = str(event.get("reaction") or "").strip()
        item = event.get("item")
        if not emoji or not isinstance(item, Mapping) or item.get("type") != "message":
            return None
        channel_id = str(item.get("channel") or "").strip()
        message_ts = str(item.get("ts") or "").strip()
        if not channel_id or not message_ts:
            return None
        conv_id = self._sent_threads.get((channel_id, message_ts), channel_id)
        return ChannelReaction(
            channel="slack",
            conversation_id=conv_id,
            sender_id=sender_id,
            message_id=message_ts,
            emoji=emoji,
            metadata={
                "channel_id": channel_id,
                "parent_channel_id": channel_id,
                "ts": message_ts,
            },
        )

    async def handle_webhook_interaction(
        self,
        headers: Mapping[str, str],
        body: bytes,
        query_params: Mapping[str, str] | None = None,
    ) -> WebhookHTTPResponse:
        """Handle `POST /channels/slack/interactions` and `/channels/slack/commands`."""
        del query_params
        lowered = {k.lower(): v for k, v in headers.items()}
        if not self.verify_signature(lowered, body):
            return WebhookHTTPResponse(
                status_code=401,
                body={"ok": False, "error": "invalid_slack_signature"},
            )

        content_type = lowered.get("content-type", "")
        payload: dict[str, Any] = {}

        if "application/x-www-form-urlencoded" in content_type:
            form = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
            if "payload" in form and form["payload"]:
                try:
                    payload = json.loads(form["payload"][0])
                except Exception:
                    return WebhookHTTPResponse(
                        status_code=400, body={"ok": False, "error": "invalid_payload_json"}
                    )
            elif "command" in form:
                return await self._handle_slash_command_form(form)
        else:
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                return WebhookHTTPResponse(
                    status_code=400, body={"ok": False, "error": "invalid_json"}
                )

        if payload.get("type") == "block_actions":
            interaction = self._parse_block_actions(payload)
            if interaction is not None and self._interaction_handler is not None:
                self._spawn(self._dispatch_block_action(interaction))
            return WebhookHTTPResponse(status_code=200, body={"ok": True})

        return WebhookHTTPResponse(status_code=200, body={"ok": True})

    async def send_ephemeral_response(
        self,
        *,
        text: str,
        response_url: str | None = None,
        channel_id: str = "",
        user_id: str = "",
        thread_ts: str | None = None,
    ) -> None:
        """Deliver an ephemeral acknowledgment back to Slack via `response_url` or `chat.postEphemeral`."""
        cleaned = (text or "").strip()
        if not cleaned:
            return
        allowlist = self.config.exposure.mention_allowlist_user_ids or None
        formatted = format_markdown_for_slack(cleaned, mention_allowlist=allowlist)[:SLACK_MAX_TEXT_CHARS]
        if response_url:
            try:
                if self._client is None:
                    self._client = httpx.AsyncClient(timeout=self.config.request_timeout_seconds)
                await self._client.post(
                    response_url,
                    json={
                        "response_type": "ephemeral",
                        "replace_original": False,
                        "text": formatted,
                    },
                )
                return
            except Exception as exc:
                logger.debug("Slack response_url ephemeral post failed: %s", exc)
        if self.config.bot_token and channel_id and user_id:
            payload: dict[str, Any] = {
                "channel": channel_id,
                "user": user_id,
                "text": formatted,
            }
            if thread_ts:
                payload["thread_ts"] = thread_ts
            try:
                await self._api_call("chat.postEphemeral", payload)
            except Exception as exc:
                logger.debug("Slack chat.postEphemeral failed: %s", exc)

    async def _dispatch_block_action(self, interaction: ChannelInteraction) -> None:
        """Execute interaction handler for a Slack `block_actions` click and send ephemeral feedback."""
        if self._interaction_handler is None:
            return
        reply = await self._interaction_handler(interaction)
        if not reply:
            return
        should_ack = interaction.interaction_kind == "feedback" or (
            interaction.interaction_kind == "approval"
            and ("already been resolved" in reply.lower() or "not authorized" in reply.lower())
        )
        if should_ack:
            meta = interaction.metadata or {}
            channel_id = str(meta.get("channel_id") or "").strip()
            if not channel_id:
                channel_id, _ = parse_slack_conversation_id(interaction.conversation_id)
            await self.send_ephemeral_response(
                text=reply,
                response_url=interaction.response_url,
                channel_id=channel_id,
                user_id=interaction.sender_id,
                thread_ts=interaction.thread_id,
            )

    async def _handle_slash_command_form(
        self, form: Mapping[str, list[str]]
    ) -> WebhookHTTPResponse:
        raw_cmd = (form.get("command") or [""])[0].strip().lstrip("/")
        raw_text = (form.get("text") or [""])[0].strip()
        user_id = (form.get("user_id") or [""])[0].strip()
        user_name = (form.get("user_name") or [""])[0].strip()
        channel_id = (form.get("channel_id") or [""])[0].strip()
        channel_name = (form.get("channel_name") or [""])[0].strip()
        response_url = (form.get("response_url") or [""])[0].strip() or None
        is_dm = channel_name == "directmessage" or channel_id.startswith("D")

        cmd_name = raw_cmd.lower()
        cmd_args = raw_text
        if cmd_name in {"talkops", "ops"}:
            if not raw_text:
                cmd_name = "help"
                cmd_args = ""
            else:
                parts = raw_text.split(maxsplit=1)
                cmd_name = parts[0].lstrip("/").lower()
                cmd_args = parts[1].strip() if len(parts) > 1 else ""

        interaction = ChannelInteraction(
            channel="slack",
            conversation_id=channel_id,
            sender_id=user_id,
            sender_name=user_name,
            interaction_kind="command",
            command_name=cmd_name,
            command_args=cmd_args,
            response_url=response_url,
            is_dm=is_dm,
            metadata={"channel_id": channel_id, "channel_name": channel_name},
        )
        if self._interaction_handler is not None:
            reply_text = await self._interaction_handler(interaction)
            if reply_text:
                return WebhookHTTPResponse(
                    status_code=200,
                    body={
                        "response_type": "ephemeral",
                        "text": format_markdown_for_slack(reply_text),
                    },
                )
        return WebhookHTTPResponse(status_code=200, body={"response_type": "ephemeral", "text": "Done."})

    def _parse_block_actions(self, payload: Mapping[str, Any]) -> ChannelInteraction | None:
        raw_user = payload.get("user")
        user: Mapping[str, Any] = raw_user if isinstance(raw_user, Mapping) else {}
        sender_id = str(user.get("id") or "").strip()
        sender_name = str(user.get("username") or user.get("name") or "").strip()
        raw_channel_info = payload.get("channel")
        channel_info: Mapping[str, Any] = (
            raw_channel_info if isinstance(raw_channel_info, Mapping) else {}
        )
        channel_id = str(channel_info.get("id") or "").strip()
        raw_message = payload.get("message")
        message: Mapping[str, Any] = raw_message if isinstance(raw_message, Mapping) else {}
        message_ts = str(message.get("ts") or "").strip() or None
        thread_ts = str(message.get("thread_ts") or message_ts or "").strip() or None
        response_url = str(payload.get("response_url") or "").strip() or None

        actions = payload.get("actions")
        if not isinstance(actions, Sequence) or not actions or not isinstance(actions[0], Mapping):
            return None
        action = actions[0]
        action_id = str(action.get("action_id") or "")
        raw_val = str(action.get("value") or "")

        decision: str | None = None
        approval_id: str | None = None
        run_id: str | None = None
        conv_id = format_slack_conversation_id(channel_id, thread_ts)

        if action_id.startswith("talkops_approve:"):
            decision = "approve"
            approval_id = action_id.split(":", 1)[1]
        elif action_id.startswith("talkops_reject:"):
            decision = "reject"
            approval_id = action_id.split(":", 1)[1]
        elif action_id.startswith(("talkops_confirm:", "talkops_dispute:")) or action_id in {
            "talkops_feedback_confirmed",
            "talkops_feedback_false_positive",
        }:
            verdict = (
                "helped"
                if action_id.startswith("talkops_confirm:") or action_id == "talkops_feedback_confirmed"
                else "rejected"
            )
            lg_tid = action_id.split(":", 1)[1] if ":" in action_id else raw_val
            if raw_val.startswith("{"):
                try:
                    parsed_fb = json.loads(raw_val)
                    lg_tid = str(parsed_fb.get("thread_id") or lg_tid)
                except Exception:
                    pass
            return ChannelInteraction(
                channel="slack",
                conversation_id=conv_id,
                sender_id=sender_id,
                sender_name=sender_name,
                interaction_kind="feedback",
                message_id=message_ts,
                thread_id=thread_ts,
                action_id=action_id,
                response_url=response_url,
                is_dm=channel_id.startswith("D"),
                metadata={
                    "channel_id": channel_id,
                    "parent_channel_id": channel_id,
                    "message_ts": message_ts,
                    "thread_ts": thread_ts,
                    "verdict": verdict,
                    "lg_thread_id": lg_tid,
                    "original_blocks": message.get("blocks")
                    if isinstance(message.get("blocks"), list)
                    else None,
                    "original_text": str(message.get("text") or ""),
                },
            )

        if raw_val.startswith("{"):
            try:
                parsed_val = json.loads(raw_val)
                decision = str(parsed_val.get("decision") or decision or "")
                approval_id = str(parsed_val.get("approval_id") or approval_id or "")
                run_id = str(parsed_val.get("run_id") or "") or None
                if parsed_val.get("conversation_id"):
                    conv_id = str(parsed_val["conversation_id"])
            except Exception:
                pass

        if decision in {"approve", "reject"}:
            return ChannelInteraction(
                channel="slack",
                conversation_id=conv_id,
                sender_id=sender_id,
                sender_name=sender_name,
                interaction_kind="approval",
                message_id=message_ts,
                thread_id=thread_ts,
                action_id=action_id,
                decision=decision,  # type: ignore[arg-type]
                approval_id=approval_id,
                run_id=run_id,
                response_url=response_url,
                is_dm=channel_id.startswith("D"),
                metadata={"channel_id": channel_id, "message_ts": message_ts},
            )
        return None

    async def _api_call(self, method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.request_timeout_seconds)
        url = f"{self.config.api_base}/{method.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self.config.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        response = await self._client.post(url, headers=headers, json=dict(payload))
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {"ok": False}

    def _record_sent_thread(self, channel_id: str, message_ts: str | None, conversation_id: str) -> None:
        if not channel_id or not message_ts:
            return
        self._sent_threads[(channel_id, message_ts)] = conversation_id
        while len(self._sent_threads) > _SEEN_EVENT_CAP:
            self._sent_threads.popitem(last=False)

    async def open_dm(self, user_id: str) -> str | None:
        """Open (or retrieve) a direct-message channel with `user_id` via `conversations.open`."""
        uid = user_id.strip()
        if not uid or not self.config.bot_token:
            return None
        try:
            resp = await self._api_call("conversations.open", {"users": uid})
            ch = resp.get("channel")
            if isinstance(ch, Mapping) and ch.get("id"):
                return str(ch["id"])
        except Exception:
            pass
        return None

    async def _upload_file(
        self,
        channel_id: str,
        media_item: ChannelMedia,
        *,
        thread_ts: str | None = None,
        initial_comment: str | None = None,
    ) -> dict[str, Any]:
        """Upload a local or in-memory file via Slack's 3-step external upload API."""
        if media_item.content_bytes is not None:
            file_bytes = media_item.content_bytes
            filename = media_item.filename or "report.md"
        elif media_item.path:
            resolved = resolve_bounded_media_path(media_item.path)
            file_bytes = resolved.read_bytes()
            filename = media_item.filename or resolved.name
        else:
            return {"ok": False}

        get_url_resp = await self._api_call(
            "files.getUploadURLExternal",
            {"filename": filename, "length": len(file_bytes)},
        )
        upload_url = get_url_resp.get("upload_url")
        file_id = get_url_resp.get("file_id")
        if not upload_url or not file_id:
            return {"ok": False}

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.request_timeout_seconds)
        up_resp = await self._client.post(
            str(upload_url),
            content=file_bytes,
            headers={"Content-Type": "application/octet-stream"},
        )
        up_resp.raise_for_status()

        complete_payload: dict[str, Any] = {
            "files": [{"id": str(file_id), "title": filename}],
            "channel_id": channel_id,
        }
        if thread_ts:
            complete_payload["thread_ts"] = thread_ts
        if initial_comment:
            complete_payload["initial_comment"] = initial_comment
        return await self._api_call("files.completeUploadExternal", complete_payload)

    async def _post_or_update(
        self,
        method: str,
        req_body: dict[str, Any],
        *,
        raw_text: str = "",
        allowlist: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """Execute `chat.postMessage` or `chat.update` with automatic Block Kit & attachment fallback."""
        resp = await self._api_call(method, req_body)
        if resp.get("ok") or ("blocks" not in req_body and "attachments" not in req_body):
            return resp

        # Attachment path fallback (colored incident root cards): strip `expand` inside attachment blocks first
        if "attachments" in req_body and isinstance(req_body.get("attachments"), Sequence):
            atts = req_body["attachments"]
            stripped_atts = strip_slack_expand_flags_from_attachments(atts)
            if stripped_atts != list(atts):
                resp = await self._api_call(method, dict(req_body, attachments=stripped_atts))
                if resp.get("ok"):
                    return resp
            # Unwrap attachment blocks into top-level blocks if attachments fail
            unwrapped = [
                blk
                for att in stripped_atts
                if isinstance(att, Mapping) and isinstance(att.get("blocks"), Sequence)
                for blk in att["blocks"]
            ]
            if unwrapped:
                unwrapped_body = {k: v for k, v in req_body.items() if k != "attachments"}
                unwrapped_body["blocks"] = unwrapped
                resp = await self._api_call(method, unwrapped_body)
                if resp.get("ok"):
                    return resp
            plain_att_body = {k: v for k, v in req_body.items() if k not in {"attachments", "blocks"}}
            return await self._api_call(method, plain_att_body)

        blocks = req_body.get("blocks")
        # Fallback 1: Strip `section.expand` if a workspace rejects the `expand` property
        has_expand = isinstance(blocks, Sequence) and any(
            isinstance(b, Mapping) and "expand" in b for b in blocks
        )
        if has_expand:
            stripped_blocks = strip_slack_expand_flags(blocks)
            retry_expand_body = dict(req_body, blocks=stripped_blocks)
            resp = await self._api_call(method, retry_expand_body)
            if resp.get("ok"):
                return resp
            blocks = stripped_blocks

        # Fallback 2: Replace native `table` blocks with structured `section` cards
        has_native_table = isinstance(blocks, Sequence) and any(
            isinstance(b, Mapping) and b.get("type") == "table" for b in blocks
        )
        if has_native_table and raw_text:
            fallback_blocks = build_slack_response_blocks(
                raw_text,
                mention_allowlist=allowlist,
                use_native_table=False,
                expand_sections=False,
            )
            if fallback_blocks:
                retry_body = dict(req_body, blocks=fallback_blocks)
                resp = await self._api_call(method, retry_body)
                if resp.get("ok"):
                    return resp

        plain_body = {k: v for k, v in req_body.items() if k != "blocks"}
        return await self._api_call(method, plain_body)

    async def send(
        self,
        conversation_id: str,
        text: str,
        *,
        thread_id: str | None = None,
        media: Sequence[ChannelMedia] = (),
        blocks_or_cards: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Send chunked text, Block Kit blocks, colored attachments, and files to Slack."""
        del metadata
        channel_id, conv_thread_ts = parse_slack_conversation_id(conversation_id)
        effective_thread = thread_id or conv_thread_ts
        allowlist = self.config.exposure.mention_allowlist_user_ids or None

        last_ts: str | None = None
        last_resp: dict[str, Any] = {}

        # Fast path A: Colored Slack attachment payload (`{"attachments": [{"color": ..., "blocks": [...]}]}`)
        if isinstance(blocks_or_cards, Mapping) and isinstance(blocks_or_cards.get("attachments"), list):
            fallback_text = (
                format_markdown_for_slack(text, mention_allowlist=allowlist)[:SLACK_MAX_TEXT_CHARS]
                if text
                else "TalkOps notification"
            )
            req_body: dict[str, Any] = {
                "channel": channel_id,
                "text": fallback_text,
                "attachments": blocks_or_cards["attachments"],
                "unfurl_links": False,
                "unfurl_media": False,
            }
            if effective_thread:
                req_body["thread_ts"] = effective_thread
            last_resp = await self._post_or_update(
                "chat.postMessage",
                req_body,
                raw_text=text,
                allowlist=allowlist,
            )
            last_ts = str(last_resp.get("ts") or "") or last_ts
            self._record_sent_thread(
                channel_id,
                last_ts,
                format_slack_conversation_id(channel_id, effective_thread or last_ts),
            )
        # Fast path B: explicit pre-compiled Block Kit blocks (e.g., RCA reports, approval cards)
        elif isinstance(blocks_or_cards, list) and blocks_or_cards:
            fallback_text = (
                format_markdown_for_slack(text, mention_allowlist=allowlist)[:SLACK_MAX_TEXT_CHARS]
                if text
                else "TalkOps notification"
            )
            block_batches = [
                blocks_or_cards[i : i + SLACK_MAX_BLOCKS]
                for i in range(0, len(blocks_or_cards), SLACK_MAX_BLOCKS)
            ]
            for batch in block_batches:
                req_body = {
                    "channel": channel_id,
                    "text": fallback_text,
                    "blocks": batch,
                    "unfurl_links": False,
                    "unfurl_media": False,
                }
                if effective_thread:
                    req_body["thread_ts"] = effective_thread
                last_resp = await self._post_or_update(
                    "chat.postMessage",
                    req_body,
                    raw_text=text,
                    allowlist=allowlist,
                )
                last_ts = str(last_resp.get("ts") or "") or last_ts
                self._record_sent_thread(
                    channel_id,
                    last_ts,
                    format_slack_conversation_id(channel_id, effective_thread or last_ts),
                )
        else:
            raw_chunks = chunk_text(text, max_chars=SLACK_MAX_TEXT_CHARS) if text else []
            if not raw_chunks and not media:
                raw_chunks = [text]

            for raw_chunk in raw_chunks:
                formatted_chunk = format_markdown_for_slack(raw_chunk, mention_allowlist=allowlist)
                req_body = {
                    "channel": channel_id,
                    "text": formatted_chunk,
                    "unfurl_links": False,
                    "unfurl_media": False,
                }
                if effective_thread:
                    req_body["thread_ts"] = effective_thread
                if raw_chunk.strip():
                    auto_blocks = build_slack_response_blocks(
                        raw_chunk,
                        mention_allowlist=allowlist,
                    )
                    if auto_blocks:
                        req_body["blocks"] = auto_blocks

                last_resp = await self._post_or_update(
                    "chat.postMessage",
                    req_body,
                    raw_text=raw_chunk,
                    allowlist=allowlist,
                )
                last_ts = str(last_resp.get("ts") or "") or last_ts
                self._record_sent_thread(
                    channel_id,
                    last_ts,
                    format_slack_conversation_id(channel_id, effective_thread or last_ts),
                )

        for item in media:
            if item.path or item.content_bytes is not None:
                try:
                    await self._upload_file(
                        channel_id,
                        item,
                        thread_ts=effective_thread or last_ts,
                        initial_comment=str(item.metadata.get("initial_comment") or "") or None,
                    )
                except Exception:
                    pass

        return SendResult(
            channel="slack",
            conversation_id=conversation_id,
            message_id=last_ts,
            thread_id=effective_thread or last_ts,
            raw_response=last_resp,
        )

    async def send_in_thread(
        self,
        conversation_id: str,
        thread_id: str,
        text: str,
        *,
        media: Sequence[ChannelMedia] = (),
        blocks_or_cards: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Send a reply inside a Slack `thread_ts`."""
        return await self.send(
            conversation_id,
            text,
            thread_id=thread_id,
            media=media,
            blocks_or_cards=blocks_or_cards,
            metadata=metadata,
        )

    async def update_message(
        self,
        conversation_id: str,
        message_id: str,
        text: str,
        *,
        blocks_or_cards: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Update an existing Slack message in place via `chat.update`."""
        del metadata
        channel_id, conv_thread_ts = parse_slack_conversation_id(conversation_id)
        allowlist = self.config.exposure.mention_allowlist_user_ids or None
        formatted = format_markdown_for_slack(text, mention_allowlist=allowlist)[:SLACK_MAX_TEXT_CHARS]
        req_body: dict[str, Any] = {
            "channel": channel_id,
            "ts": message_id,
            "text": formatted,
        }
        if isinstance(blocks_or_cards, Mapping) and isinstance(blocks_or_cards.get("attachments"), list):
            req_body["attachments"] = blocks_or_cards["attachments"]
        elif blocks_or_cards is not None:
            req_body["blocks"] = blocks_or_cards
        elif text.strip():
            auto_blocks = build_slack_response_blocks(text, mention_allowlist=allowlist)
            if auto_blocks:
                req_body["blocks"] = auto_blocks

        resp = await self._post_or_update(
            "chat.update",
            req_body,
            raw_text=text,
            allowlist=allowlist,
        )
        return SendResult(
            channel="slack",
            conversation_id=conversation_id,
            message_id=message_id,
            thread_id=conv_thread_ts,
            raw_response=resp,
        )

    def resolve_thread_conversation_id(
        self,
        conversation_id: str,
        sent: SendResult,
    ) -> str:
        """Return the canonical Slack thread conversation key (`<channel_id>:<thread_ts>`) for a sent message."""
        if (sent.thread_id or sent.message_id) and ":" not in conversation_id:
            return f"{conversation_id}:{sent.thread_id or sent.message_id}"
        return conversation_id

    async def create_incident_thread(
        self,
        conversation_id: str,
        *,
        title: str,
        initial_text: str,
        blocks_or_cards: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Create an incident root message in `conversation_id` with structured Slack Block Kit blocks."""
        meta = dict(metadata or {})
        allowlist = self.config.exposure.mention_allowlist_user_ids or None
        blocks = blocks_or_cards
        if blocks is None:
            blocks = build_slack_incident_attachment_payload(
                title=title,
                summary=str(meta.get("summary") or initial_text),
                provider=str(meta.get("provider") or ""),
                severity=str(meta.get("severity") or ""),
                service_name=str(meta.get("service_name") or ""),
                source_url=str(meta.get("source_url") or ""),
                lg_thread_id=str(meta.get("lg_thread_id") or ""),
                investigation_state=str(meta.get("investigation_state") or "investigating"),  # type: ignore[arg-type]
                rca_preview=str(meta.get("rca_preview") or ""),
                duration_seconds=meta.get("duration_seconds"),
                mention_allowlist=allowlist,
            )
        fallback_text = f"*{title}*\n\n{initial_text}".strip()
        return await self.send(
            conversation_id,
            fallback_text,
            blocks_or_cards=blocks,
            metadata=metadata,
        )

    async def send_approval_prompt(
        self,
        spec: ApprovalCardSpec,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Render and send a Slack Block Kit HITL approval card."""
        blocks = build_slack_approval_blocks(spec)
        return await self.send(
            spec.conversation_id,
            spec.prompt_text or spec.title,
            thread_id=spec.thread_id,
            blocks_or_cards=blocks,
            metadata=metadata,
        )

    async def add_reaction(
        self,
        conversation_id: str,
        message_id: str,
        emoji: str,
    ) -> None:
        """Add an emoji reaction (e.g. `eyes`, `white_check_mark`) to a Slack message."""
        if not self.config.bot_token or not message_id:
            return
        channel_id, _ = parse_slack_conversation_id(conversation_id)
        try:
            await self._api_call(
                "reactions.add",
                {
                    "channel": channel_id,
                    "timestamp": message_id,
                    "name": emoji.strip(":"),
                },
            )
        except Exception:
            pass

    async def remove_reaction(
        self,
        conversation_id: str,
        message_id: str,
        emoji: str,
    ) -> None:
        """Remove an emoji reaction from a Slack message."""
        if not self.config.bot_token or not message_id:
            return
        channel_id, _ = parse_slack_conversation_id(conversation_id)
        try:
            await self._api_call(
                "reactions.remove",
                {
                    "channel": channel_id,
                    "timestamp": message_id,
                    "name": emoji.strip(":"),
                },
            )
        except Exception:
            pass

    async def send_typing(
        self,
        conversation_id: str,
        *,
        thread_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """No-op typing indicator (Slack Web API does not expose bot typing over HTTP)."""
        del conversation_id, thread_id, metadata

    async def has_bot_participated_in_thread(
        self,
        conversation_id: str,
        thread_id: str | None = None,
    ) -> bool:
        """Return `True` if this bot was mentioned in or has replied to the Slack thread."""
        channel_id, conv_thread_ts = parse_slack_conversation_id(conversation_id)
        target_ts = thread_id or conv_thread_ts
        if not target_ts or not self.config.bot_token:
            return False
        try:
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=self.config.request_timeout_seconds)
            url = f"{self.config.api_base}/conversations.replies"
            resp = await self._client.get(
                url,
                headers={"Authorization": f"Bearer {self.config.bot_token}"},
                params={"channel": channel_id, "ts": target_ts, "limit": "20"},
            )
            resp.raise_for_status()
            data = resp.json()
            messages = data.get("messages") if isinstance(data, Mapping) else None
            if not isinstance(messages, Sequence) or not messages:
                return False
            first = messages[0]
            if isinstance(first, Mapping):
                reply_users = first.get("reply_users")
                if (
                    self._bot_user_id
                    and isinstance(reply_users, Sequence)
                    and self._bot_user_id in reply_users
                ):
                    return True
                if self._bot_user_id and f"<@{self._bot_user_id}" in str(first.get("text") or ""):
                    return True
            for m in messages:
                if not isinstance(m, Mapping):
                    continue
                if self._bot_user_id and str(m.get("user") or "") == self._bot_user_id:
                    return True
            return False
        except Exception:
            return False

    async def fetch_thread_context(
        self,
        conversation_id: str,
        thread_id: str | None = None,
        *,
        limit: int = 40,
        metadata: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Fetch prior messages in a Slack thread via `conversations.replies`."""
        del metadata
        channel_id, conv_thread_ts = parse_slack_conversation_id(conversation_id)
        target_ts = thread_id or conv_thread_ts
        if not target_ts or not self.config.bot_token:
            return None
        try:
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=self.config.request_timeout_seconds)
            url = f"{self.config.api_base}/conversations.replies"
            resp = await self._client.get(
                url,
                headers={"Authorization": f"Bearer {self.config.bot_token}"},
                params={"channel": channel_id, "ts": target_ts, "limit": str(limit)},
            )
            resp.raise_for_status()
            data = resp.json()
            messages = data.get("messages") if isinstance(data, Mapping) else None
            if not isinstance(messages, Sequence) or len(messages) <= 1:
                return None

            lines: list[str] = []
            for m in messages[:-1]:
                if not isinstance(m, Mapping):
                    continue
                raw_txt = str(m.get("text") or "").strip()
                if not raw_txt or contains_oauth_callback_url(raw_txt):
                    continue
                user_id = str(m.get("user") or "").strip()
                bot_id = str(m.get("bot_id") or "").strip()
                if self._bot_user_id and not bot_id:
                    raw_txt = re.sub(
                        rf"<@{re.escape(self._bot_user_id)}(?:\|[^>]+)?>",
                        "",
                        raw_txt,
                    ).strip()
                txt = decode_slack_mrkdwn(raw_txt)[:1000]
                if not txt:
                    continue
                if self._bot_user_id and user_id == self._bot_user_id:
                    if "_Working on your request..._" in txt:
                        continue
                    lines.append(f"[Assistant (TalkOps)]: {html.escape(txt)}")
                elif bot_id and (not self._bot_user_id or user_id != self._bot_user_id):
                    escaped_sender = html.escape(bot_id, quote=True)
                    escaped_body = html.escape(txt)
                    lines.append(
                        f'<input-message sender="{escaped_sender}" sender_type="bot">{escaped_body}</input-message>'
                    )
                else:
                    author = user_id or bot_id or "unknown"
                    lines.append(f"[{author}]: {html.escape(txt)}")
            return "\n".join(lines) if lines else None
        except Exception:
            return None

    def status(self) -> ChannelStatus:
        """Return Slack adapter configuration and endpoint health status."""
        return ChannelStatus(
            channel="slack",
            configured=bool(self.config.bot_token and self.config.signing_secret),
            webhook_events_path="/channels/slack/events",
            webhook_interactions_path="/channels/slack/interactions",
            exposure_mode=str(self.config.exposure.mode),
            detail="Slack Events API + Interactivity HTTP Webhook",
            metadata={
                "has_bot_token": bool(self.config.bot_token),
                "has_signing_secret": bool(self.config.signing_secret),
                "bot_user_id": self._bot_user_id,
            },
        )

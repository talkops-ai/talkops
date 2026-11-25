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

"""Comprehensive unit & cross-addon integration tests for built-in `talkops.channels` and `ChannelsAddon`.

Covers:
1. Built-in default activation of `channels` & `webhooks` across server profiles + legacy spec aliasing
2. Slack Events API (`X-Slack-Signature` HMAC), URL verification, Block Kit HITL approval, DM pairing, & reactions
3. Microsoft Teams Bot Framework Activity (`HMAC` + `Bearer` JWT) & Adaptive Card v1.4 HITL approval
4. Discord HTTP Interactions (`Ed25519` signature), `PING`->`PONG`, Slash Commands, & ActionRow Button HITL
5. `ChannelHub` commands (`/model`, `/smart-model`, `/effort`, `/mode`, `/status`, `/context-doctor`, `/new`, `/stop`, `/pair`)
6. `ChannelStreamSink` verbosity=3 live edits, media bounds/SSRF protection, and loopback OAuth callback redaction
7. End-to-end `WebhookAddon` -> `HookEventBus` -> `ChannelsAddon` 2-stage Slack thread binding + `generic_webhook` coexistence
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from starlette.testclient import TestClient

from talkops.addons import AddonManager, HookEventBus, load_addon_from_spec
from talkops.addons.builtin import (
    ChannelsAddon,
    WebhookAddon,
    get_builtin_addons,
    set_webhook_dispatcher,
)
from talkops.channels import (
    ACTIVE_CHANNEL_CONVERSATION,
    ApprovalCardSpec,
    ChannelExposure,
    ChannelHub,
    ChannelMedia,
    ChannelMediaError,
    ChannelStatus,
    ChannelStreamSink,
    DiscordChannelConfig,
    DiscordWebhookChannel,
    ExposureMode,
    PairingStore,
    SendResult,
    SlackChannelConfig,
    SlackWebhookChannel,
    TeamsChannelConfig,
    TeamsWebhookChannel,
    WebhookHTTPResponse,
    build_inbound_media_prompt,
    build_multimodal_content,
    build_slack_response_blocks,
    chunk_text,
    contains_oauth_callback_url,
    create_send_message_tool,
    decode_slack_mrkdwn,
    download_slack_file_safely,
    extract_loopback_oauth_callback_url,
    extract_markdown_media,
    extract_tool_calls_from_interrupt,
    format_code,
    format_markdown_for_discord,
    format_markdown_for_slack,
    parse_chat_command,
    parse_text_approval_decision,
    resolve_bounded_media_path,
)
from talkops.webhooks import WebhookConfig, WebhookDispatcher, WebhookRunResult


class FakeGateway:
    """Fake `AgentRunGateway` for deterministic `ChannelHub` streaming and HITL interrupt tests."""

    def __init__(self) -> None:
        """Initialize `FakeGateway`."""
        self.calls: list[dict[str, Any]] = []
        self.cancelled_threads: list[str] = []
        self.interrupt_on_first_turn: bool = False
        self._turn_count: int = 0

    async def stream_run(
        self,
        input_payload: Any,
        *,
        thread_id: str,
        source_addon: str = "channels",
        context: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[tuple[tuple[str, ...], str, Any]]:
        """Yield simulated LangGraph stream chunks or HITL interrupts."""
        del kwargs
        self._turn_count += 1
        self.calls.append(
            {
                "input": input_payload,
                "thread_id": thread_id,
                "source_addon": source_addon,
                "context": context,
            }
        )
        if self.interrupt_on_first_turn and self._turn_count == 1:
            yield (
                (),
                "updates",
                {
                    "__interrupt__": [
                        {
                            "value": {
                                "action_requests": [
                                    {
                                        "name": "aws_eks_update_nodegroup",
                                        "args": {"cluster": "prod-eks", "desired_size": 5},
                                        "description": "Scale EKS nodegroup prod-eks to 5 nodes",
                                    }
                                ]
                            }
                        }
                    ]
                },
            )
            return

        class _Chunk:
            def __init__(self, content: str) -> None:
                self.content = content
                self.additional_kwargs: dict[str, Any] = {}
                self.response_metadata: dict[str, Any] = {}
                self.tool_call_chunks: list[Any] = []

        yield ((), "messages", (_Chunk(" scaling complete on **prod-eks**."), {}))

    async def cancel_thread_runs(self, thread_id: str) -> int:
        """Cancel active runs on `thread_id`."""
        self.cancelled_threads.append(thread_id)
        return 1


class InMemoryAdapter:
    """In-memory `WebhookChannelAdapter` for deterministic `ChannelHub` testing."""

    def __init__(self, name: str = "slack", *, exposure: ChannelExposure | None = None) -> None:
        """Initialize `InMemoryAdapter`."""
        self.name = name
        self.config = type(
            "_Cfg",
            (),
            {
                "exposure": exposure
                or ChannelExposure(
                    mode=ExposureMode.ALLOWLIST,
                    operator_id="U_OP",
                    allowed_users=frozenset({"U_ALLOWED"}),
                    allowed_chats=frozenset({"C_ALLOWED"}),
                )
            },
        )()
        self.sent: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.approvals_sent: list[ApprovalCardSpec] = []
        self.typing_count: int = 0
        self.thread_context_to_return: str | None = "[alice]: DB connections hit 95%"
        self._msg_handler: Any = None
        self._react_handler: Any = None
        self._inter_handler: Any = None

    def set_message_handler(self, handler: Any) -> None:
        """Bind message handler."""
        self._msg_handler = handler

    def set_reaction_handler(self, handler: Any) -> None:
        """Bind reaction handler."""
        self._react_handler = handler

    def set_interaction_handler(self, handler: Any) -> None:
        """Bind interaction handler."""
        self._inter_handler = handler

    async def start(self) -> None:
        """Start adapter."""

    async def stop(self) -> None:
        """Stop adapter."""

    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> bool:
        """Accept all signatures in memory."""
        del headers, body
        return True

    async def handle_webhook_event(
        self,
        headers: Mapping[str, str],
        body: bytes,
        query_params: Mapping[str, str] | None = None,
    ) -> WebhookHTTPResponse:
        """Return 200 OK."""
        del headers, body, query_params
        return WebhookHTTPResponse()

    async def handle_webhook_interaction(
        self,
        headers: Mapping[str, str],
        body: bytes,
        query_params: Mapping[str, str] | None = None,
    ) -> WebhookHTTPResponse:
        """Return 200 OK."""
        del headers, body, query_params
        return WebhookHTTPResponse()

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
        """Record sent message in memory."""
        del media, metadata
        msg_id = f"m_{len(self.sent) + 1}"
        self.sent.append(
            {
                "conversation_id": conversation_id,
                "thread_id": thread_id,
                "text": text,
                "blocks_or_cards": blocks_or_cards,
                "message_id": msg_id,
            }
        )
        return SendResult(
            channel=self.name,
            conversation_id=conversation_id,
            message_id=msg_id,
            thread_id=thread_id or msg_id,
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
        """Delegate thread reply to `send`."""
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
        """Record updated message in memory."""
        del metadata
        self.updated.append(
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "text": text,
                "blocks_or_cards": blocks_or_cards,
            }
        )
        return SendResult(channel=self.name, conversation_id=conversation_id, message_id=message_id)

    async def send_approval_prompt(
        self,
        spec: ApprovalCardSpec,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> SendResult:
        """Record approval card in memory."""
        self.approvals_sent.append(spec)
        return await self.send(
            spec.conversation_id,
            spec.prompt_text,
            thread_id=spec.thread_id,
            metadata=metadata,
        )

    async def send_typing(
        self,
        conversation_id: str,
        *,
        thread_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Increment typing counter."""
        del conversation_id, thread_id, metadata
        self.typing_count += 1

    async def fetch_thread_context(
        self,
        conversation_id: str,
        thread_id: str | None = None,
        *,
        limit: int = 40,
        metadata: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Return pre-configured thread context."""
        del conversation_id, thread_id, limit, metadata
        return self.thread_context_to_return

    def resolve_thread_conversation_id(self, conversation_id: str, sent: SendResult) -> str:
        """Format Slack-style `<channel>:<thread_ts>` key."""
        if (sent.thread_id or sent.message_id) and ":" not in conversation_id:
            return f"{conversation_id}:{sent.thread_id or sent.message_id}"
        return conversation_id

    def status(self) -> ChannelStatus:
        """Return healthy `ChannelStatus`."""
        return ChannelStatus(
            channel=self.name,
            configured=True,
            webhook_events_path=f"/channels/{self.name}/events",
            webhook_interactions_path=f"/channels/{self.name}/interactions",
        )


def _slack_sign(secret: str, timestamp: int, body: bytes) -> dict[str, str]:
    basestring = f"v0:{timestamp}:".encode() + body
    digest = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return {
        "x-slack-request-timestamp": str(timestamp),
        "x-slack-signature": f"v0={digest}",
        "content-type": "application/json",
    }


def test_builtin_channels_and_webhooks_enabled_by_default_and_legacy_spec_aliases() -> None:
    """Verify `channels` and `webhooks` are built-in, enabled by default, and resolve legacy specs."""
    builtins = {a.name: a for a in get_builtin_addons()}
    assert {"core-auth", "client-gateway", "webhooks", "channels", "context-graph", "intelligence"} == set(
        builtins.keys()
    )
    assert builtins["channels"].enabled_by_default is True
    assert builtins["webhooks"].enabled_by_default is True

    # Legacy `addons.json` specs (`talkops_channels.addon:ChannelsAddon`) resolve cleanly to built-ins
    legacy_ch = load_addon_from_spec("talkops_channels.addon:ChannelsAddon")
    assert legacy_ch is not None
    assert legacy_ch.name == "channels"

    legacy_wh = load_addon_from_spec("talkops_webhooks.addon:WebhookAddon")
    assert legacy_wh is not None
    assert legacy_wh.name == "webhooks"

    # Verify env var alias fallbacks (`TALKOPS_SLACK_BOT_TOKEN`, `TALKOPS_TEAMS_APP_ID`, `TALKOPS_DISCORD_BOT_TOKEN`)
    slack_cfg = SlackChannelConfig.from_mapping(
        {},
        {
            "TALKOPS_SLACK_BOT_TOKEN": "xoxb-alias",
            "TALKOPS_SLACK_SIGNING_SECRET": "sig-alias",
        },
    )
    assert slack_cfg.bot_token == "xoxb-alias"
    assert slack_cfg.signing_secret == "sig-alias"

    teams_cfg = TeamsChannelConfig.from_mapping(
        {},
        {
            "TALKOPS_TEAMS_APP_ID": "teams-app-alias",
            "TALKOPS_TEAMS_APP_PASSWORD": "teams-pw-alias",
        },
    )
    assert teams_cfg.app_id == "teams-app-alias"
    assert teams_cfg.app_password == "teams-pw-alias"

    disc_cfg = DiscordChannelConfig.from_mapping(
        {},
        {
            "TALKOPS_DISCORD_BOT_TOKEN": "disc-tok-alias",
            "TALKOPS_DISCORD_PUBLIC_KEY": "disc-pub-alias",
        },
    )
    assert disc_cfg.bot_token == "disc-tok-alias"
    assert disc_cfg.public_key == "disc-pub-alias"


def test_formatting_commands_and_pairing_store(tmp_path: Path) -> None:
    """Verify Markdown formatting, balanced fence chunking, command parsing, and DM pairing store."""
    md = "# Incident Report\nCheck **pod-1** at [Grafana](https://grafana.local/d/1) and ~~old~~."
    slack_txt = format_markdown_for_slack(md)
    assert "*Incident Report*" in slack_txt
    assert "*pod-1*" in slack_txt
    assert "<https://grafana.local/d/1|Grafana>" in slack_txt
    assert "~old~" in slack_txt

    code_doc = "Intro\n```python\n" + ("print('hello world')\n" * 40) + "```\nOutro"
    chunks = chunk_text(code_doc, max_chars=300)
    assert len(chunks) >= 3
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0

    parsed = parse_chat_command("/mode jev")
    assert parsed is not None and parsed[0].name == "mode" and parsed[1] == "jev"

    umbrella = parse_chat_command("/talkops model anthropic:claude-sonnet-4-5")
    assert umbrella is not None and umbrella[0].name == "model"

    assert parse_text_approval_decision("approve") == ("approve", None)
    assert parse_text_approval_decision("deny unsafe in prod") == ("reject", "unsafe in prod")
    assert parse_text_approval_decision("what is the status?") is None

    store = PairingStore(tmp_path / "pairing.json")
    req = store.issue_or_get_code(
        channel="slack",
        sender_id="U_STRANGER",
        sender_name="alice",
        conversation_id="D12345",
    )
    assert len(req.code) == 8
    assert not store.is_approved("slack", "U_STRANGER")
    approved = store.approve_code(format_code(req.code))
    assert approved is not None and store.is_approved("slack", "U_STRANGER")
    revoked = store.revoke("slack", "U_STRANGER")
    assert revoked is not None and not store.is_approved("slack", "U_STRANGER")


@pytest.mark.asyncio
async def test_slack_webhook_events_hitl_approval_pairing_and_reactions(tmp_path: Path) -> None:
    """Verify Slack HMAC verification, URL challenge, Block Kit HITL approval, DM pairing, and 👍 reaction."""
    sent_requests: list[tuple[str, dict[str, Any]]] = []

    def _mock_slack_api(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        sent_requests.append((request.url.path, body))
        return httpx.Response(200, json={"ok": True, "ts": f"1700000.{len(sent_requests)}"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_slack_api))
    gateway = FakeGateway()
    gateway.interrupt_on_first_turn = True

    pairing_store = PairingStore(tmp_path / "pairing.json")
    hub = ChannelHub(
        gateway=gateway,
        pairing_store=pairing_store,
        verbosity=1,
    )
    slack_cfg = SlackChannelConfig(
        bot_token="xoxb-test-token",
        signing_secret="slack-test-secret",
        bot_user_id="U_BOT",
        exposure=ChannelExposure(
            mode=ExposureMode.ALLOWLIST,
            operator_id="U_OPERATOR",
            allowed_chats=frozenset({"C_OPS"}),
        ),
    )
    slack = SlackWebhookChannel(slack_cfg, http_client=client)
    hub.register_adapter(slack)
    await hub.start()

    now = int(time.time())
    challenge_body = json.dumps({"type": "url_verification", "challenge": "xyz_challenge"}).encode()
    resp = await slack.handle_webhook_event(
        _slack_sign("slack-test-secret", now, challenge_body),
        challenge_body,
    )
    assert resp.status_code == 200
    assert resp.body == {"challenge": "xyz_challenge"}

    # Tampered signature rejected with 401
    bad_resp = await slack.handle_webhook_event(
        {"x-slack-request-timestamp": str(now), "x-slack-signature": "v0=bad"},
        challenge_body,
    )
    assert bad_resp.status_code == 401

    # Inbound app_mention triggers interrupted run and sends Block Kit approval card
    event_payload = {
        "event_id": "Ev001",
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "user": "U_OPERATOR",
            "channel": "C_OPS",
            "ts": "1700000.001",
            "text": "<@U_BOT> scale prod-eks to 5 nodes",
        },
    }
    raw_event = json.dumps(event_payload).encode()
    resp2 = await slack.handle_webhook_event(
        _slack_sign("slack-test-secret", now, raw_event),
        raw_event,
    )
    assert resp2.status_code == 200
    await asyncio.sleep(0.05)

    pending = hub.approvals.get_for_conversation("slack", "C_OPS:1700000.001")
    assert pending is not None
    assert pending.tool_calls[0].name == "aws_eks_update_nodegroup"
    assert any("blocks" in req[1] for req in sent_requests)

    # Resolve via Slack Block Kit button click
    interaction_payload = {
        "type": "block_actions",
        "user": {"id": "U_OPERATOR", "username": "ops_lead"},
        "channel": {"id": "C_OPS"},
        "message": {"ts": pending.prompt_message_id, "thread_ts": "1700000.001"},
        "actions": [
            {
                "action_id": f"talkops_approve:{pending.approval_id}",
                "value": json.dumps(
                    {
                        "approval_id": pending.approval_id,
                        "decision": "approve",
                        "conversation_id": "C_OPS:1700000.001",
                    }
                ),
            }
        ],
    }
    form_body = urllib.parse.urlencode({"payload": json.dumps(interaction_payload)}).encode()
    headers = _slack_sign("slack-test-secret", now, form_body)
    headers["content-type"] = "application/x-www-form-urlencoded"
    resp3 = await slack.handle_webhook_interaction(headers, form_body)
    assert resp3.status_code == 200
    await asyncio.sleep(0.05)

    assert hub.approvals.get_for_conversation("slack", "C_OPS:1700000.001") is None
    assert len(gateway.calls) == 2
    assert any("scaling complete" in req[1].get("text", "") for req in sent_requests)

    # Unknown DM sender triggers pairing code; operator approves via `/pair approve <code>`
    dm_raw = json.dumps(
        {
            "event_id": "Ev_DM_1",
            "type": "event_callback",
            "event": {
                "type": "message",
                "channel_type": "im",
                "user": "U_NEW_DEV",
                "channel": "D_NEW_DEV",
                "ts": "1700009.001",
                "text": "hello can I use talkops?",
            },
        }
    ).encode()
    await slack.handle_webhook_event(_slack_sign("slack-test-secret", now, dm_raw), dm_raw)
    await asyncio.sleep(0.05)
    pending_reqs, _ = pairing_store.list_all()
    assert len(pending_reqs) == 1
    issued_code = format_code(pending_reqs[0].code)

    op_raw = json.dumps(
        {
            "event_id": "Ev_OP_1",
            "type": "event_callback",
            "event": {
                "type": "message",
                "channel_type": "im",
                "user": "U_OPERATOR",
                "channel": "D_OP",
                "ts": "1700009.002",
                "text": f"/pair approve {issued_code}",
            },
        }
    ).encode()
    await slack.handle_webhook_event(_slack_sign("slack-test-secret", now, op_raw), op_raw)
    await asyncio.sleep(0.05)
    assert pairing_store.is_approved("slack", "U_NEW_DEV")
    await hub.stop()


@pytest.mark.asyncio
async def test_teams_webhook_hmac_jwt_and_adaptive_card(tmp_path: Path) -> None:
    """Verify Microsoft Teams RS256 JWKS JWT, SSRF serviceUrl guard, persistence, threading, and Action.Execute."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    from talkops.channels.state_store import PersistentChannelStateStore

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    pub_jwk["kid"] = "bf-test-kid-1"
    pub_jwk["use"] = "sig"
    pub_jwk["alg"] = "RS256"

    sent_activities: list[tuple[str, str, dict[str, Any]]] = []
    tenant_uuid = "11111111-2222-3333-4444-555555555555"

    def _mock_teams_api(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if ".well-known/openid" in url_str:
            return httpx.Response(200, json={"jwks_uri": "https://login.botframework.com/v1/.well-known/keys"})
        if ".well-known/keys" in url_str:
            return httpx.Response(200, json={"keys": [pub_jwk]})
        if "oauth2/v2.0/token" in url_str:
            return httpx.Response(200, json={"access_token": "bf-token", "expires_in": 3600})
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        sent_activities.append((request.method, url_str, body))
        return httpx.Response(200, json={"id": f"act_{len(sent_activities)}"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_teams_api))
    gateway = FakeGateway()
    gateway.interrupt_on_first_turn = True

    db_path = tmp_path / "teams_state.db"
    state_store = PersistentChannelStateStore(backend="sqlite", sqlite_path=db_path)

    hub = ChannelHub(
        gateway=gateway,
        pairing_store=PairingStore(tmp_path / "pairing.json"),
        state_store=state_store,
        verbosity=1,
    )
    secret_b64 = base64.b64encode(b"teams-shared-hmac-key").decode("ascii")
    teams_cfg = TeamsChannelConfig(
        app_id="app-123",
        app_password="pw-123",
        tenant_id=tenant_uuid,
        webhook_secret=secret_b64,
        exposure=ChannelExposure(
            mode=ExposureMode.ALLOWLIST,
            operator_id="29:operator-1",
        ),
    )
    teams = TeamsWebhookChannel(teams_cfg, http_client=client, state_store=state_store)
    hub.register_adapter(teams)
    await hub.start()

    now = int(time.time())
    # 1. Real RS256 JWKS JWT verification (accepts valid Bot Framework & Single-Tenant Entra tokens,
    #    rejects HS256 downgrade, forged signature, wrong aud, wrong iss, wrong tid)
    valid_bf_jwt = jwt.encode(
        {
            "iss": "https://api.botframework.com",
            "aud": "app-123",
            "tid": tenant_uuid,
            "serviceurl": "https://smba.trafficmanager.net/emea",
            "exp": now + 600,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "bf-test-kid-1"},
    )
    assert await teams.verify_request_async(
        {"Authorization": f"Bearer {valid_bf_jwt}"},
        b"{}",
        expected_service_url="https://smba.trafficmanager.net/emea/",
    )

    # Forged HS256 token MUST be rejected
    hs256_jwt = jwt.encode(
        {"iss": "https://api.botframework.com", "aud": "app-123", "exp": now + 600},
        "0123456789abcdef0123456789abcdef",
        algorithm="HS256",
        headers={"kid": "bf-test-kid-1"},
    )
    assert not await teams.verify_request_async({"Authorization": f"Bearer {hs256_jwt}"}, b"{}")

    # Tampered RS256 signature (wrong key) MUST be rejected
    tampered_jwt = jwt.encode(
        {"iss": "https://api.botframework.com", "aud": "app-123", "exp": now + 600},
        other_private_key,
        algorithm="RS256",
        headers={"kid": "bf-test-kid-1"},
    )
    assert not await teams.verify_request_async({"Authorization": f"Bearer {tampered_jwt}"}, b"{}")

    # Wrong audience, wrong issuer, and cross-tenant `tid` MUST be rejected
    for bad_claims in (
        {"iss": "https://api.botframework.com", "aud": "other-app", "exp": now + 600},
        {"iss": "https://evil.example.com", "aud": "app-123", "exp": now + 600},
        {"iss": "https://api.botframework.com", "aud": "app-123", "tid": "99999999-9999-9999-9999-999999999999", "exp": now + 600},
    ):
        bad_tok = jwt.encode(
            bad_claims,
            private_key,
            algorithm="RS256",
            headers={"kid": "bf-test-kid-1"},
        )
        assert not await teams.verify_request_async({"Authorization": f"Bearer {bad_tok}"}, b"{}")

    # 2. SSRF guard on untrusted serviceUrl
    assert not teams._is_trusted_service_url("https://evil.attacker.example/v3")
    assert not teams._is_trusted_service_url("http://smba.trafficmanager.net/emea")
    assert teams._is_trusted_service_url("https://smba.trafficmanager.net/emea")

    # 3. Inbound channel message authenticated via RS256 Bearer JWT -> persists EMEA serviceUrl
    #    and sends Adaptive Card HITL approval WITHOUT duplicated `text` field!
    activity = {
        "type": "message",
        "id": "msg-100",
        "serviceUrl": "https://smba.trafficmanager.net/emea",
        "from": {"id": "29:operator-1", "name": "Cloud Admin"},
        "recipient": {"id": "28:bot-id", "name": "TalkOps"},
        "conversation": {
            "id": "19:ops@thread.tacv2;messageid=1700000100",
            "conversationType": "channel",
            "tenantId": tenant_uuid,
        },
        "text": "<at>TalkOps</at> scale prod-eks",
        "entities": [{"type": "mention", "mentioned": {"id": "28:bot-id"}}],
    }
    raw_body = json.dumps(activity).encode("utf-8")

    resp = await teams.handle_webhook_event(
        {"authorization": f"Bearer {valid_bf_jwt}", "content-type": "application/json"},
        raw_body,
    )
    assert resp.status_code == 200
    await asyncio.sleep(0.05)

    pending = hub.approvals.get_for_conversation("teams", "19:ops@thread.tacv2;messageid=1700000100")
    assert pending is not None
    card_posts = [act for act in sent_activities if "attachments" in act[2]]
    assert len(card_posts) == 1
    assert card_posts[0][1].startswith("https://smba.trafficmanager.net/emea/")
    # Card activity omits `text` (uses `summary`) so Teams doesn't duplicate text above the card
    assert "text" not in card_posts[0][2]
    assert card_posts[0][2].get("summary")
    assert card_posts[0][2]["from"]["id"] == "28:bot-id"
    card_actions = card_posts[0][2]["attachments"][0]["content"]["actions"]
    assert all(a["type"] == "Action.Execute" for a in card_actions)

    # 4. Action.Execute (`type: "invoke"`, `name: "adaptiveCard/action"`) returns Teams' exact
    #    AdaptiveCardInvokeResponse schema and resolves the approval (clearing card via PUT).
    card_invoke = {
        "type": "invoke",
        "name": "adaptiveCard/action",
        "id": "inv-101",
        "serviceUrl": "https://smba.trafficmanager.net/emea",
        "from": {"id": "29:operator-1", "name": "Cloud Admin"},
        "conversation": {
            "id": "19:ops@thread.tacv2;messageid=1700000100",
            "conversationType": "channel",
            "tenantId": tenant_uuid,
        },
        "replyToId": pending.prompt_message_id,
        "value": {
            "action": {
                "type": "Action.Execute",
                "verb": "talkops_approve",
                "data": {
                    "talkops_action": "approval",
                    "decision": "approve",
                    "approval_id": pending.approval_id,
                    "conversation_id": "19:ops@thread.tacv2;messageid=1700000100",
                },
            }
        },
    }
    raw_invoke = json.dumps(card_invoke).encode("utf-8")
    resp2 = await teams.handle_webhook_interaction(
        {"authorization": f"Bearer {valid_bf_jwt}", "content-type": "application/json"},
        raw_invoke,
    )
    assert resp2.status_code == 200
    assert resp2.body["statusCode"] == 200
    assert resp2.body["type"] == "application/vnd.microsoft.activity.message"
    await asyncio.sleep(0.05)

    # Resolved approval PUT explicitly clears `attachments: []` and includes plain `text`
    put_updates = [act for act in sent_activities if act[0] == "PUT"]
    assert put_updates
    assert put_updates[0][2]["attachments"] == []
    assert "Approved" in put_updates[0][2]["text"]

    # 5. Restart simulation: fresh adapter with a fresh PersistentChannelStateStore on the same
    #    SQLite DB still resolves EMEA `serviceUrl` for base channel ID AND binds channel thread IDs
    #    with `;messageid=<root_id>`!
    sent_after_restart: list[tuple[str, str, dict[str, Any]]] = []

    def _mock_restart_api(request: httpx.Request) -> httpx.Response:
        if "oauth2/v2.0/token" in str(request.url):
            return httpx.Response(200, json={"access_token": "bf-token-2", "expires_in": 3600})
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        sent_after_restart.append((request.method, str(request.url), body))
        return httpx.Response(200, json={"id": "1700000999"})

    fresh_store = PersistentChannelStateStore(backend="sqlite", sqlite_path=db_path)
    fresh_teams = TeamsWebhookChannel(
        teams_cfg,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_mock_restart_api)),
        state_store=fresh_store,
    )
    inc_res = await fresh_teams.create_incident_thread(
        "19:ops@thread.tacv2",
        title="Checkout 5xx Spike",
        initial_text="High error rate",
    )
    assert sent_after_restart[0][1].startswith("https://smba.trafficmanager.net/emea/")
    assert "text" not in sent_after_restart[0][2]
    assert sent_after_restart[0][2]["attachments"]
    assert (
        fresh_teams.resolve_thread_conversation_id("19:ops@thread.tacv2", inc_res)
        == "19:ops@thread.tacv2;messageid=1700000999"
    )
    # 1:1 personal DM conversation IDs stay unchanged (no `;messageid=`)
    assert (
        fresh_teams.resolve_thread_conversation_id("a:1personal-dm-id", inc_res)
        == "a:1personal-dm-id"
    )
    assert len(gateway.calls) == 2

    await fresh_teams.stop()
    await hub.stop()


@pytest.mark.asyncio
async def test_discord_ed25519_interactions_and_builtin_channels_routes(tmp_path: Path) -> None:
    """Verify Discord Ed25519 PING/PONG, `/help` slash command, and `/channels/notify` via `AddonManager`."""
    priv_key = Ed25519PrivateKey.generate()
    pub_hex = priv_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    sent_discord: list[tuple[str, dict[str, Any]]] = []

    def _mock_discord_api(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        sent_discord.append((request.url.path, body))
        return httpx.Response(200, json={"id": f"disc_msg_{len(sent_discord)}"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_discord_api))
    gateway = FakeGateway()
    hub = ChannelHub(
        gateway=gateway,
        pairing_store=PairingStore(tmp_path / "pairing.json"),
        verbosity=1,
    )
    discord_cfg = DiscordChannelConfig(
        bot_token="discord-bot-token",
        application_id="app_999",
        public_key=pub_hex,
        exposure=ChannelExposure(
            mode=ExposureMode.ALLOWLIST,
            operator_id="U_DISC_OP",
        ),
    )
    discord_adapter = DiscordWebhookChannel(discord_cfg, http_client=client)
    hub.register_adapter(discord_adapter)

    mgr = AddonManager(
        profile="default",
        project_root=tmp_path,
        extra_addons=[ChannelsAddon(hub=hub)],
    )
    asgi_app = mgr.create_asgi_app()

    def _sign_discord(body_bytes: bytes) -> dict[str, str]:
        ts = str(int(time.time()))
        sig = priv_key.sign(ts.encode("utf-8") + body_bytes).hex()
        return {
            "x-signature-ed25519": sig,
            "x-signature-timestamp": ts,
            "content-type": "application/json",
        }

    with TestClient(asgi_app) as test_client:
        status_res = test_client.get("/channels/status")
        assert status_res.status_code == 200
        assert status_res.json()["addon"] == "channels"

        ping_bytes = json.dumps({"type": 1}).encode("utf-8")
        ping_res = test_client.post(
            "/channels/discord/interactions",
            content=ping_bytes,
            headers=_sign_discord(ping_bytes),
        )
        assert ping_res.status_code == 200
        assert ping_res.json() == {"type": 1}

        help_bytes = json.dumps(
            {
                "type": 2,
                "id": "inter_1",
                "token": "tok_1",
                "channel_id": "C_DISC_1",
                "member": {"user": {"id": "U_DISC_OP", "username": "disc_admin"}},
                "data": {"name": "help", "options": []},
            }
        ).encode("utf-8")
        help_res = test_client.post(
            "/channels/discord/interactions",
            content=help_bytes,
            headers=_sign_discord(help_bytes),
        )
        assert help_res.status_code == 200
        assert help_res.json()["type"] == 4
        assert "/new" in help_res.json()["data"]["content"]

        notify_res = test_client.post(
            "/channels/notify",
            json={
                "channel": "discord",
                "conversation_id": "C_DISC_INCIDENT",
                "text": "🚨 High CPU on prod-eks",
                "lg_thread_id": "lg-incident-123",
            },
        )
        assert notify_res.status_code == 200
        assert notify_res.json()["ok"] is True
        assert len(sent_discord) == 1


@pytest.mark.asyncio
async def test_hub_commands_stream_sink_media_and_oauth_security(tmp_path: Path) -> None:
    """Verify `ChannelHub` commands, `ChannelStreamSink` v3 live edits, media bounds, and OAuth redaction."""
    adapter = InMemoryAdapter("slack")
    sink = ChannelStreamSink(
        adapter,
        conversation_id="C_ALLOWED:2.0",
        thread_id="2.0",
        lg_thread_id="lg_v3",
        requester_id="U_OP",
        verbosity=3,
        edit_debounce_seconds=0.0,
    )
    await sink.on_stream_start("C_ALLOWED:2.0", "lg_v3")
    await sink.on_thinking("Checking CloudWatch alarms...")
    await sink.on_tool_call_started("aws_cloudwatch_describe_alarms", "call_1", {"state": "ALARM"})
    await sink.on_tool_call_completed("aws_cloudwatch_describe_alarms", "call_1", "2 alarms")
    await sink.on_token("All alarms analyzed.")
    await sink.on_stream_end()
    assert len(adapter.sent) == 1
    assert len(adapter.updated) >= 3
    assert adapter.updated[-1]["text"] == "All alarms analyzed."

    tcs = extract_tool_calls_from_interrupt({"tool": "kubectl_apply", "input": {"file": "dep.yaml"}})
    assert tcs[0].name == "kubectl_apply"

    # Media path traversal, symlink rejection, and SSRF protection
    img_path = tmp_path / "chart.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\nfakeimage")
    assert resolve_bounded_media_path(img_path, allowed_roots=(tmp_path,)) == img_path.resolve()

    symlink_path = tmp_path / "link.png"
    symlink_path.symlink_to(img_path)
    with pytest.raises(ChannelMediaError, match="Symlink"):
        resolve_bounded_media_path(symlink_path)

    with pytest.raises(ChannelMediaError, match="Refusing to download"):
        await download_slack_file_safely(
            "https://evil.example.com/file.png",
            bot_token="xoxb-secret",
            dest_dir=tmp_path,
        )

    cleaned_md, extracted = extract_markdown_media(f"CPU:\n![Chart]({img_path})")
    assert len(extracted) == 1 and str(img_path) not in cleaned_md
    prompt = build_inbound_media_prompt("Analyze chart", extracted)
    mm = build_multimodal_content(prompt, extracted)
    assert isinstance(mm, list) and mm[1]["type"] == "image_url"

    # Inbound/outbound Slack/Discord mrkdwn & broadcast neutralization + OAuth callback redaction
    assert "&" in decode_slack_mrkdwn("<https://x.io?a=1&amp;b=2|x.io>")
    assert "<!here>" not in format_markdown_for_slack("Alert <!here>")
    assert "@\u200beveryone" in format_markdown_for_discord("Alert @everyone")

    oauth_msg = "Callback: http://127.0.0.1:8765/oauth/callback?code=secret123&state=xyz"
    assert contains_oauth_callback_url(oauth_msg) is True
    assert extract_loopback_oauth_callback_url(oauth_msg) is not None

    hub = ChannelHub(
        gateway=None,
        pairing_store=PairingStore(tmp_path / "pairing.json"),
        pairing_enabled=False,
    )
    hub.register_adapter(adapter)
    send_tool = create_send_message_tool(hub)
    token = ACTIVE_CHANNEL_CONVERSATION.set(("slack", "C_ALLOWED", None))
    try:
        res = await send_tool.ainvoke({"text": "Progress update!"})
        assert res["status"] == "sent"
    finally:
        ACTIVE_CHANNEL_CONVERSATION.reset(token)


@pytest.mark.asyncio
async def test_webhook_to_channels_two_stage_thread_binding_and_generic_webhook_coexistence(
    tmp_path: Path,
) -> None:
    """Verify `WebhookAddon` + `ChannelsAddon` deliver 2-stage Slack threads AND still fire `generic_webhook` sinks."""
    bus = HookEventBus()
    gateway = FakeGateway()

    slack_adapter = InMemoryAdapter("slack")
    hub = ChannelHub(
        gateway=gateway,
        pairing_store=PairingStore(tmp_path / "pairing.json"),
        verbosity=1,
    )
    hub.register_adapter(slack_adapter)
    channels_addon = ChannelsAddon(hub=hub)

    async def _fake_rca_executor(
        event: Any,
        lg_thread_id: str,
        delivery_id: str,
        cfg: Any,
    ) -> WebhookRunResult:
        del event, cfg
        return WebhookRunResult(
            run_id=delivery_id,
            thread_id=lg_thread_id,
            status="success",
            analysis_text="### Executive Summary\nPod `checkout-api-0` OOMKilled due to 512Mi memory limit.",
        )

    wh_cfg = WebhookConfig(
        default_secret="wh-test-sec",
        outbound_channels=[
            {
                "channel": "slack",
                "conversation_id": "C_INCIDENTS",
                "notify_on": ["received", "completed"],
                "bind_thread": True,
            },
            {
                "channel": "generic_webhook",
                "conversation_id": "http://sink.internal/rca-hook",
                "notify_on": ["received", "completed"],
            },
        ],
    )
    dispatcher = WebhookDispatcher(
        config=wh_cfg,
        run_executor=_fake_rca_executor,
        event_bus=bus,
    )
    set_webhook_dispatcher(dispatcher)
    wh_addon = WebhookAddon(dispatcher=dispatcher)

    generic_sink_posts: list[dict[str, Any]] = []

    async def _fake_client_post(
        _self: httpx.AsyncClient,
        url: str,
        *,
        json: Any = None,
        **kwargs: Any,
    ) -> httpx.Response:
        del kwargs
        generic_sink_posts.append({"url": str(url), "payload": json})
        return httpx.Response(200, json={"ok": True})

    mgr = AddonManager(
        profile="default",
        project_root=tmp_path,
        event_bus=bus,
        extra_addons=[wh_addon, channels_addon],
    )
    mgr.resolve_and_validate()
    await mgr.startup()
    try:
        with patch.object(httpx.AsyncClient, "post", new=_fake_client_post):
            alert_payload = {
                "event": {
                    "id": "evt-pd-200",
                    "event_type": "incident.triggered",
                    "data": {
                        "id": "PD-200",
                        "title": "Checkout Pod CrashLoopBackOff",
                        "urgency": "high",
                        "service": {"summary": "checkout-api"},
                    },
                }
            }
            raw_pd = json.dumps(alert_payload).encode("utf-8")
            sig = "v1=" + hmac.new(b"wh-test-sec", raw_pd, hashlib.sha256).hexdigest()

            status_code, resp = await dispatcher.handle_webhook(
                provider="pagerduty",
                headers={"X-PagerDuty-Signature": sig},
                raw_body=raw_pd,
            )
            assert status_code == 202
            lg_tid = resp["thread_id"]
            await dispatcher.drain_background_tasks()

        # 1. `ChannelsAddon` created the root Slack incident card, posted the RCA reply in-thread,
        #    updated the root card to "RCA Complete", and bound `C_INCIDENTS:m_1` -> `lg_tid`!
        assert len(slack_adapter.sent) == 2
        assert slack_adapter.sent[0]["conversation_id"] == "C_INCIDENTS"
        assert slack_adapter.sent[1]["conversation_id"] == "C_INCIDENTS:m_1"
        assert "OOMKilled" in slack_adapter.sent[1]["text"]
        assert len(slack_adapter.updated) == 1
        assert slack_adapter.updated[0]["message_id"] == "m_1"
        resolved_tid, is_new = await hub._get_or_create_lg_thread("slack", "C_INCIDENTS:m_1")
        assert resolved_tid == lg_tid
        assert is_new is False

        # 2. Non-chat `generic_webhook` route ALSO received both `received` and `completed` HTTP callbacks
        #    (while `sinks.py` did NOT double-post to Slack)!
        assert len(generic_sink_posts) == 2
        assert all(p["url"] == "http://sink.internal/rca-hook" for p in generic_sink_posts)
    finally:
        await mgr.shutdown()


def test_slack_mrkdwn_bold_inline_code_and_rich_blocks() -> None:
    """Verify Slack mrkdwn never leaks `**` or `*` around inline code spans."""
    md = (
        "### Built-in Core Subagents\n"
        "- **`general-purpose`**: Researches complex queries.\n"
        "- **`aws-finops-engineer`** (`@talkops-devops-plugins`): Audits AWS costs.\n"
        "- **`pod-1`:** colon inside bold.\n"
        "- **Run `kubectl get pods` now** and check __`namespace`__.\n"
        "- **Inspect `pod`s** or ~~`deprecated-flag`~~ and *`italic-code`*.\n"
        "- ***Critical `prod-db` failure*** with [check `status`](https://ops.local/s).\n"
        "#### **Nested Bold Heading** with `code`\n"
        "```markdown\n"
        "- **`untouched-in-fence`**: keep raw\n"
        "```\n"
    )
    slack_txt = format_markdown_for_slack(md)
    assert "• `general-purpose`: Researches complex queries." in slack_txt
    assert "• `aws-finops-engineer` (`@talkops-devops-plugins`): Audits AWS costs." in slack_txt
    assert "• `pod-1`: colon inside bold." in slack_txt
    assert "• *Run* `kubectl get pods` *now* and check `namespace`." in slack_txt
    assert "• *Inspect* `pod`s or `deprecated-flag` and `italic-code`." in slack_txt
    assert "• _*Critical*_ `prod-db` _*failure*_ with <https://ops.local/s|check `status`>." in slack_txt
    assert "*Nested Bold Heading with* `code`" in slack_txt
    assert "**`general-purpose`**" not in slack_txt
    assert "*`general-purpose`*" not in slack_txt
    assert "- **`untouched-in-fence`**: keep raw" in slack_txt

    # Block Kit section + native table rich_text formatting
    blocks = build_slack_response_blocks(
        "#### **`subagent`**\n\n"
        "| Agent | Role |\n"
        "| --- | --- |\n"
        "| **`general-purpose`** | **Runs `kubectl`** |\n"
    )
    heading_sections = [
        b["text"]["text"]
        for b in blocks
        if b.get("type") == "section" and isinstance(b.get("text"), dict)
    ]
    assert heading_sections == ["`subagent`"]

    table_blocks = [b for b in blocks if b.get("type") == "table"]
    assert len(table_blocks) == 1
    body_row = table_blocks[0]["rows"][1]
    agent_cell_runs = body_row[0]["elements"][0]["elements"]
    role_cell_runs = body_row[1]["elements"][0]["elements"]
    assert agent_cell_runs == [
        {"type": "text", "text": "general-purpose", "style": {"bold": True, "code": True}}
    ]
    assert role_cell_runs == [
        {"type": "text", "text": "Runs ", "style": {"bold": True}},
        {"type": "text", "text": "kubectl", "style": {"bold": True, "code": True}},
    ]


@pytest.mark.asyncio
async def test_industry_standard_rca_formatting_slack_discord_and_teams(tmp_path: Path) -> None:
    """Verify industry-standard RCA formatting across Slack, Discord, and Teams without duplication or truncation."""
    from talkops.webhooks.formatting import (
        build_discord_incident_embed,
        build_discord_rca_embed,
        build_slack_rca_blocks,
        build_teams_incident_card,
        build_teams_rca_card,
        extract_rca_takeaway,
    )

    sample_rca = (
        "All active `otel-demo` alert issues have been resolved and verified in the live cluster (`docker-desktop`).\n\n"
        "### 1. Fixes Applied (After `--dry-run=server` Validation)\n"
        "1. **Fixed `flagd` Metrics Scrape Port (`otel-demo/ServiceMonitor/otel-demo-flagd`)**\n"
        "   - Added a `relabelings` rule on `spec.endpoints[0]` to rewrite `__address__` from port `8016` to `8014`.\n\n"
        "### 2. Live Verification Results\n"
        "| Component | State | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Prometheus Scrape Pool | **Healthy (`up`)** | `http://10.244.0.12:8014/metrics` HTTP 200 |\n"
        "| Active Alerts | **Cleared (`[]`)** | `OtelDemoTargetDown` resolved |\n"
    )

    # 1. Takeaway stops at the first prose paragraph and NEVER glues numbered list steps or cuts mid-token
    takeaway = extract_rca_takeaway(sample_rca)
    assert "All active `otel-demo` alert issues have been resolved" in takeaway
    assert "1. Fixed" not in takeaway
    assert takeaway.count("`") % 2 == 0

    # 2. Slack RCA blocks: single top header, compact metadata context, expanded sections (no `Show more`),
    #    no giant `header` blocks for `###` subsections, native table, and interactive feedback actions.
    slack_blocks = build_slack_rca_blocks(
        sample_rca,
        provider="alertmanager",
        title="[ALERTMANAGER] OtelDemoTargetDown (3 alerts) (CRITICAL)",
        severity="critical",
        status="completed",
        source_url="https://prom.internal/graph",
        lg_thread_id="lg-otel-1",
        include_feedback_actions=True,
    )
    headers = [b for b in slack_blocks if b.get("type") == "header"]
    assert len(headers) == 1
    assert "Root Cause Analysis" in headers[0]["text"]["text"]
    assert "OtelDemoTargetDown (3 alerts)" in headers[0]["text"]["text"]
    # Metadata context strip immediately follows header
    assert slack_blocks[1]["type"] == "context"
    assert "RCA Complete" in slack_blocks[1]["elements"][0]["text"]
    assert "Alertmanager" in slack_blocks[1]["elements"][0]["text"]
    # Body sections set expand=True to prevent Slack thread sidebar `Show more` truncation
    body_sections = [
        b for b in slack_blocks if b.get("type") == "section" and isinstance(b.get("text"), dict)
    ]
    assert body_sections and all(b.get("expand") is True for b in body_sections)
    # Feedback action buttons present
    action_blocks = [b for b in slack_blocks if b.get("type") == "actions"]
    assert len(action_blocks) == 1
    action_ids = [el["action_id"] for el in action_blocks[0]["elements"]]
    assert "talkops_confirm:lg-otel-1" in action_ids
    assert "talkops_dispute:lg-otel-1" in action_ids

    # 3. Slack adapter retries stripping `expand` when a legacy workspace rejects it,
    #    AND preserves pre-compiled blocks even when fallback text exceeds 4,000 chars.
    slack_api_payloads: list[dict[str, Any]] = []

    def _mock_slack_expand_fallback(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        slack_api_payloads.append(body)
        if any("expand" in b for b in body.get("blocks", [])):
            return httpx.Response(200, json={"ok": False, "error": "invalid_blocks"})
        return httpx.Response(200, json={"ok": True, "channel": "C_OPS", "ts": "1710.999"})

    slack_adapter = SlackWebhookChannel(
        SlackChannelConfig(
            bot_token="xoxb-test",
            signing_secret="sec",
            exposure=ChannelExposure(mode=ExposureMode.OPEN, open_ack=True),
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_mock_slack_expand_fallback)),
    )
    long_fallback = "x" * 4500
    await slack_adapter.send("C_OPS", long_fallback, blocks_or_cards=slack_blocks)
    assert len(slack_api_payloads) == 2
    assert any("expand" in b for b in slack_api_payloads[0]["blocks"])
    assert all("expand" not in b for b in slack_api_payloads[1]["blocks"])
    assert slack_api_payloads[1]["blocks"][0]["type"] == "header"

    # Verify Slack RCA feedback button action parsing (`talkops_confirm:` / `talkops_dispute:`)
    fb_interaction = slack_adapter._parse_block_actions(
        {
            "user": {"id": "U_OP", "username": "sandeep"},
            "channel": {"id": "C_OPS"},
            "message": {"ts": "1710.999", "thread_ts": "1710.001"},
            "actions": [{"action_id": "talkops_confirm:lg-otel-1", "value": "lg-otel-1"}],
        }
    )
    assert fb_interaction is not None
    assert fb_interaction.interaction_kind == "feedback"
    assert fb_interaction.metadata["verdict"] == "helped"
    assert fb_interaction.metadata["lg_thread_id"] == "lg-otel-1"
    await slack_adapter.stop()

    # 4. Discord incident root card & RCA embed suppress duplicate `content` completely
    disc_incident = build_discord_incident_embed(
        title="[ALERTMANAGER] OtelDemoTargetDown (3 alerts) (CRITICAL)",
        summary="Otel-demo Prometheus target is down\n- flagd-58cfbfd68c-r4bpf: Target is down.",
        provider="alertmanager",
        severity="critical",
        service_name="flagd",
        source_url="https://prom.internal/graph",
        lg_thread_id="lg-otel-1",
        investigation_state="completed",
        rca_preview=sample_rca,
    )
    assert disc_incident["suppress_content"] is True
    assert disc_incident["embeds"][0]["timestamp"]
    assert "1. Fixed" not in disc_incident["embeds"][0]["description"]

    disc_rca = build_discord_rca_embed(
        sample_rca,
        provider="alertmanager",
        title="OtelDemoTargetDown (3 alerts)",
        severity="critical",
        status="completed",
        source_url="https://prom.internal/graph",
        lg_thread_id="lg-otel-1",
    )
    assert disc_rca["suppress_content"] is True
    assert "Fixes Applied" in disc_rca["embeds"][0]["description"]
    assert "Live Verification Results" in disc_rca["embeds"][0]["description"]

    discord_payloads: list[dict[str, Any]] = []

    def _mock_discord_clean(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        discord_payloads.append(body)
        return httpx.Response(200, json={"id": "disc_1"})

    disc_adapter = DiscordWebhookChannel(
        DiscordChannelConfig(
            bot_token="disc-tok",
            application_id="APP1",
            gateway_enabled=False,
            auto_create_threads=False,
            exposure=ChannelExposure(mode=ExposureMode.OPEN, open_ack=True),
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_mock_discord_clean)),
    )
    await disc_adapter.send("CH_OPS", "DUPLICATE TEXT MUST NOT APPEAR", blocks_or_cards=disc_incident)
    await disc_adapter.update_message("CH_OPS", "disc_1", "DUPLICATE UPDATE TEXT", blocks_or_cards=disc_incident)
    await disc_adapter.send("CH_OPS", "DUPLICATE RCA TEXT", blocks_or_cards=disc_rca)
    assert len(discord_payloads) == 3
    for payload in discord_payloads:
        assert payload["content"] == ""
        assert payload["embeds"]
        assert "suppress_content" not in payload
    await disc_adapter.stop()

    # 5. Microsoft Teams Adaptive Cards v1.5 with semantic header banner and native `Table`
    teams_inc = build_teams_incident_card(
        title="OtelDemoTargetDown (3 alerts)",
        summary="Otel-demo Prometheus target is down",
        provider="alertmanager",
        severity="critical",
        service_name="flagd",
        source_url="https://prom.internal/graph",
        lg_thread_id="lg-otel-1",
        investigation_state="completed",
        rca_preview=sample_rca,
    )
    assert teams_inc[0]["content"]["version"] == "1.5"
    assert teams_inc[0]["content"]["body"][0]["style"] == "good"

    teams_rca = build_teams_rca_card(
        sample_rca,
        provider="alertmanager",
        title="OtelDemoTargetDown (3 alerts)",
        severity="critical",
        status="completed",
        source_url="https://prom.internal/graph",
        lg_thread_id="lg-otel-1",
    )
    rca_card = teams_rca[0]["content"]
    assert rca_card["version"] == "1.5"
    assert rca_card["body"][0]["style"] == "good"
    # Native Adaptive Card v1.5 Table inside the body container
    nested_items = rca_card["body"][2]["items"]
    assert any(item.get("type") == "Table" for item in nested_items)
    del tmp_path


@pytest.mark.asyncio
async def test_next_level_enhancements_duration_slack_accent_bar_and_long_rca_attachments() -> None:
    """Verify triage duration, Slack Red->Green accent bar, Slack/Discord `.md` uploads, and Teams 28 KB cap."""
    from talkops.webhooks.formatting import (
        TEAMS_ADAPTIVE_CARD_MAX_BYTES,
        format_duration_human,
        parse_incident_output,
        parse_rca_output,
    )

    # 1. Duration formatting
    assert format_duration_human(0.4) == "<1s"
    assert format_duration_human(42.2) == "42s"
    assert format_duration_human(135) == "2m 15s"
    assert format_duration_human(3720) == "1h 2m"

    # 2. Slack parent incident card transitions Red (#E01E5A) -> Green (#2EB67D) with triage time
    slack_inc_start = parse_incident_output(
        "slack",
        title="Checkout 5xx",
        summary="High 5xx rate",
        provider="alertmanager",
        severity="critical",
        investigation_state="investigating",
    )
    assert slack_inc_start.blocks_or_cards["attachments"][0]["color"] == "#E01E5A"

    slack_inc_done = parse_incident_output(
        "slack",
        title="Checkout 5xx",
        summary="High 5xx rate",
        provider="alertmanager",
        severity="critical",
        investigation_state="completed",
        rca_preview="Fixed connection pool leak.",
        duration_seconds=42.0,
    )
    assert slack_inc_done.blocks_or_cards["attachments"][0]["color"] == "#2EB67D"
    done_blocks = slack_inc_done.blocks_or_cards["attachments"][0]["blocks"]
    assert any("42s" in str(b) for b in done_blocks)

    # 3. Long RCA triggers downloadable `.md` attachment on Slack & Discord + 28 KB guard on Teams
    long_rca = "\n\n".join(
        f"### Section {i}\nDetailed telemetry finding #{i}: " + ("error_rate=12.4% pod=checkout-api-0 " * 25)
        for i in range(1, 35)
    )
    slack_long = parse_rca_output(
        "slack",
        long_rca,
        provider="alertmanager",
        title="Checkout 5xx",
        severity="critical",
        lg_thread_id="lg-long-1",
        duration_seconds=83.0,
    )
    assert slack_long.attachment_markdown is not None
    md_name, md_bytes = slack_long.attachment_markdown
    assert md_name == "rca-checkout-5xx.md"
    assert b"Triage Time" in md_bytes and b"1m 23s" in md_bytes
    assert any("1m 23s" in str(b) for b in slack_long.blocks_or_cards)

    # Verify Slack adapter executes 3-step external upload for in-memory `ChannelMedia(content_bytes=...)`
    slack_calls: list[tuple[str, Any]] = []

    def _mock_slack_upload(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "files.getUploadURLExternal" in url:
            slack_calls.append(("getUploadURLExternal", json.loads(request.content.decode())))
            return httpx.Response(
                200, json={"ok": True, "upload_url": "https://files.slack.com/upload/v1/abc", "file_id": "F_RCA_1"}
            )
        if "files.slack.com/upload" in url:
            slack_calls.append(("upload_bytes", len(request.content)))
            return httpx.Response(200, text="OK")
        if "files.completeUploadExternal" in url:
            slack_calls.append(("completeUploadExternal", json.loads(request.content.decode())))
            return httpx.Response(200, json={"ok": True})
        slack_calls.append(("postMessage", json.loads(request.content.decode())))
        return httpx.Response(200, json={"ok": True, "channel": "C_OPS", "ts": "1710.100"})

    slack_adapter = SlackWebhookChannel(
        SlackChannelConfig(
            bot_token="xoxb-test",
            signing_secret="sec",
            exposure=ChannelExposure(mode=ExposureMode.OPEN, open_ack=True),
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_mock_slack_upload)),
    )
    await slack_adapter.send(
        "C_OPS",
        slack_long.text,
        thread_id="1710.001",
        blocks_or_cards=slack_long.blocks_or_cards,
        media=(
            ChannelMedia(
                media_type="document",
                content_bytes=md_bytes,
                filename=md_name,
                mime_type="text/markdown",
            ),
        ),
    )
    assert [c[0] for c in slack_calls] == [
        "postMessage",
        "getUploadURLExternal",
        "upload_bytes",
        "completeUploadExternal",
    ]
    assert slack_calls[3][1]["thread_ts"] == "1710.001"
    assert slack_calls[3][1]["channel_id"] == "C_OPS"
    await slack_adapter.stop()

    # Verify Discord multipart upload sends `payload_json` (with `attachments` + `embeds` + `content=""`) + `files[0]`
    discord_long = parse_rca_output(
        "discord",
        long_rca,
        provider="alertmanager",
        title="Checkout 5xx",
        severity="critical",
        lg_thread_id="lg-long-1",
        duration_seconds=83.0,
    )
    assert discord_long.attachment_markdown is not None
    d_name, d_bytes = discord_long.attachment_markdown
    assert any(f["name"] == "Triage Time" and "1m 23s" in f["value"] for f in discord_long.blocks_or_cards["embeds"][0]["fields"])

    discord_reqs: list[tuple[str, bytes]] = []

    def _mock_discord_multipart(request: httpx.Request) -> httpx.Response:
        discord_reqs.append((request.headers.get("content-type", ""), request.content))
        return httpx.Response(200, json={"id": "disc_long_1"})

    disc_adapter = DiscordWebhookChannel(
        DiscordChannelConfig(
            bot_token="disc-tok",
            application_id="APP1",
            gateway_enabled=False,
            auto_create_threads=False,
            exposure=ChannelExposure(mode=ExposureMode.OPEN, open_ack=True),
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_mock_discord_multipart)),
    )
    await disc_adapter.send(
        "CH_OPS",
        discord_long.text,
        blocks_or_cards=discord_long.blocks_or_cards,
        media=(
            ChannelMedia(
                media_type="document",
                content_bytes=d_bytes,
                filename=d_name,
                mime_type="text/markdown",
            ),
        ),
    )
    assert len(discord_reqs) == 1
    assert "multipart/form-data" in discord_reqs[0][0]
    assert b'name="payload_json"' in discord_reqs[0][1]
    assert b'name="files[0]"' in discord_reqs[0][1]
    assert b'"content": ""' in discord_reqs[0][1]
    assert d_name.encode("utf-8") in discord_reqs[0][1]
    await disc_adapter.stop()

    # Verify Microsoft Teams Adaptive Card strictly stays under 28 KB and emits overflow chunks
    teams_long = parse_rca_output(
        "teams",
        long_rca,
        provider="alertmanager",
        title="Checkout 5xx",
        severity="critical",
        lg_thread_id="lg-long-1",
        duration_seconds=83.0,
    )
    card_bytes = len(
        json.dumps(teams_long.blocks_or_cards[0]["content"], separators=(",", ":")).encode("utf-8")
    )
    assert card_bytes <= TEAMS_ADAPTIVE_CARD_MAX_BYTES
    assert len(teams_long.overflow_texts) >= 1
    facts = teams_long.blocks_or_cards[0]["content"]["body"][1]["facts"]
    assert any(f["title"] == "Triage Time" and f["value"] == "1m 23s" for f in facts)

    # Enterprise minimalist emoji governance: zero decorative emojis in Slack/Discord headers/timers/buttons,
    # and zero emojis anywhere in Microsoft Teams Adaptive Cards (per Fluent/WCAG guidance).
    decorative_emojis = ("✅", "⚠️", "⏱", "📄", "🔧", "🚨", "🟡", "🟢")
    slack_rca_json = json.dumps(slack_long.blocks_or_cards, ensure_ascii=False)
    discord_rca_json = json.dumps(discord_long.blocks_or_cards, ensure_ascii=False)
    teams_rca_json = json.dumps(teams_long.blocks_or_cards, ensure_ascii=False)
    for icon in decorative_emojis:
        assert icon not in slack_rca_json
        assert icon not in discord_rca_json
        assert icon not in teams_rca_json
    assert "🔴" not in teams_rca_json
    assert "🔴" in slack_rca_json  # Single semantic severity dot kept on Slack
    assert "🔴" in discord_rca_json  # Single semantic severity dot kept on Discord


@pytest.mark.asyncio
async def test_rca_feedback_buttons_reactions_and_backend_persistence_across_slack_discord_teams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Confirm/Dispute RCA buttons, 👍/👎 reactions, card locking, and OperationalStore persistence across Slack, Discord, and Teams."""
    from talkops.addons.builtin.intelligence import IntelligenceAddon
    from talkops.addons.protocol import AddonContext
    from talkops.channels.interfaces import ChannelReaction
    from talkops.channels.state_store import InMemoryChannelStateStore
    from talkops.storage.factory import reset_operational_store_cache
    from talkops.storage.sqlite_store import SqliteOperationalStore

    def _sign_slack(secret: str, ts: str, body: bytes) -> str:
        basestring = f"v0:{ts}:{body.decode('utf-8')}".encode()
        return "v0=" + hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()

    db_path = tmp_path / "feedback_ops.sqlite"
    monkeypatch.setenv("TALKOPS_SQLITE_PATH", str(db_path))
    reset_operational_store_cache()

    op_store = SqliteOperationalStore(db_path)
    await op_store.setup()
    bus = HookEventBus()

    # Mount IntelligenceAddon onto the bus (SRE profile path)
    intel = IntelligenceAddon()
    ctx = AddonContext(
        settings=None,  # type: ignore[arg-type]
        project_root=tmp_path,
        server_url="http://127.0.0.1:8123",
        gateway=None,  # type: ignore[arg-type]
        event_bus=bus,
    )
    await intel.on_startup(ctx)

    state_store = InMemoryChannelStateStore()
    hub = ChannelHub(
        pairing_store=PairingStore(tmp_path / "pairing.json"),
        state_store=state_store,
        event_bus=bus,
        project_root=tmp_path,
    )

    # ── 1. Slack: button click -> ephemeral ack via response_url + chat.update card lock + DB persistence ──
    slack_calls: list[tuple[str, dict[str, Any]]] = []

    def _mock_slack(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        slack_calls.append((str(request.url), body))
        if "chat.postMessage" in str(request.url):
            return httpx.Response(200, json={"ok": True, "channel": "C_SRE", "ts": f"1710.{len(slack_calls)}"})
        return httpx.Response(200, json={"ok": True})

    slack_secret = "slack-test-secret"
    slack_adapter = SlackWebhookChannel(
        SlackChannelConfig(
            bot_token="xoxb-test",
            signing_secret=slack_secret,
            exposure=ChannelExposure(mode=ExposureMode.OPEN, open_ack=True),
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_mock_slack)),
    )
    hub.register_adapter(slack_adapter)

    # ── 2. Discord: button click + 👍 reaction -> ephemeral ack + PATCH locked button + DB persistence ──
    discord_calls: list[tuple[str, str, dict[str, Any]]] = []

    def _mock_discord(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        discord_calls.append((request.method, str(request.url), body))
        return httpx.Response(200, json={"id": f"disc_{len(discord_calls)}"})

    discord_adapter = DiscordWebhookChannel(
        DiscordChannelConfig(
            bot_token="disc-tok",
            application_id="APP_FB",
            gateway_enabled=False,
            auto_create_threads=False,
            exposure=ChannelExposure(mode=ExposureMode.OPEN, open_ack=True),
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_mock_discord)),
    )
    hub.register_adapter(discord_adapter)

    # ── 3. Microsoft Teams: Adaptive Card Action.Execute + reaction -> toast ack + PUT locked card + DB persistence ──
    teams_calls: list[tuple[str, str, dict[str, Any]]] = []

    def _mock_teams(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        teams_calls.append((request.method, str(request.url), body))
        return httpx.Response(200, json={"id": f"teams_msg_{len(teams_calls)}"})

    teams_adapter = TeamsWebhookChannel(
        TeamsChannelConfig(
            app_id="",
            app_password="",
            verify_signatures=False,
            default_service_url="https://smba.trafficmanager.net/amer",
            exposure=ChannelExposure(mode=ExposureMode.OPEN, open_ack=True),
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_mock_teams)),
        state_store=state_store,
    )
    hub.register_adapter(teams_adapter)

    addon = ChannelsAddon(
        hub=hub,
        state_store=state_store,
        environ={
            "TALKOPS_WEBHOOK_NOTIFY_CHANNELS": "slack:C_SRE,discord:CH_SRE,teams:19:sre@thread.tacv2"
        },
    )

    # Simulate webhook completion delivering RCA cards across Slack, Discord, and Teams
    class _FakeEvent:
        def __init__(self, payload: dict[str, Any], thread_id: str) -> None:
            self.payload = payload
            self.thread_id = thread_id

    await addon._on_webhook_completed(
        _FakeEvent(
            {
                "thread_id": "lg-fb-thread-1",
                "provider": "alertmanager",
                "title": "OtelDemoTargetDown",
                "severity": "critical",
                "service_name": "flagd",
                "status": "completed",
                "analysis": "## Root Cause\nScrape port pointed to 8016 instead of 8014.",
                "source_url": "https://prom.internal/alerts",
            },
            "lg-fb-thread-1",
        )
    )

    # Find posted RCA message IDs
    slack_rca_msg_id = next(
        k[1] for k in hub._rca_messages if k[0] == "slack"
    )
    discord_rca_msg_id = next(
        k[1] for k in hub._rca_messages if k[0] == "discord"
    )
    teams_rca_msg_id = next(
        k[1] for k in hub._rca_messages if k[0] == "teams"
    )

    # Verify Teams RCA Adaptive Card actually rendered Confirm RCA + Dispute Hypothesis + View Alert
    teams_post = [c for c in teams_calls if c[0] == "POST" and "activities" in c[1]][-1][2]
    teams_actions = teams_post["attachments"][0]["content"]["actions"]
    teams_verbs = [a.get("verb") for a in teams_actions if a.get("type") == "Action.Execute"]
    assert teams_verbs == ["talkops_confirm", "talkops_dispute"]
    assert any(a.get("type") == "Action.OpenUrl" and a.get("title") == "View Alert" for a in teams_actions)

    # Test Slack `Confirm RCA` button click end-to-end via handle_webhook_interaction
    slack_calls.clear()
    slack_payload = {
        "type": "block_actions",
        "user": {"id": "U_SANDEEP", "username": "sandeep"},
        "channel": {"id": "C_SRE"},
        "response_url": "https://hooks.slack.com/actions/T1/B1/resp1",
        "message": {"ts": slack_rca_msg_id, "thread_ts": "1710.1"},
        "actions": [{"action_id": "talkops_confirm:lg-fb-thread-1", "value": "lg-fb-thread-1"}],
    }
    raw_form = urllib.parse.urlencode({"payload": json.dumps(slack_payload)}).encode("utf-8")
    ts_str = str(int(time.time()))
    sig = _sign_slack(slack_secret, ts_str, raw_form)
    resp = await slack_adapter.handle_webhook_interaction(
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": ts_str,
            "X-Slack-Signature": sig,
        },
        raw_form,
    )
    assert resp.status_code == 200
    await asyncio.gather(*list(slack_adapter._background_tasks), return_exceptions=True)

    # Slack must have called BOTH `chat.update` (locking buttons) AND `response_url` (ephemeral ack)
    update_calls = [body for url, body in slack_calls if "chat.update" in url]
    ephem_calls = [body for url, body in slack_calls if "hooks.slack.com/actions" in url]
    assert len(update_calls) == 1
    assert len(ephem_calls) == 1
    assert ephem_calls[0]["response_type"] == "ephemeral"
    assert "RCA confirmed" in ephem_calls[0]["text"]
    assert "📝" not in ephem_calls[0]["text"]
    updated_blocks = update_calls[0]["blocks"]
    updated_action_ids = [
        el.get("action_id")
        for b in updated_blocks
        if b.get("type") == "actions"
        for el in b.get("elements", [])
    ]
    assert "talkops_confirm:lg-fb-thread-1" not in updated_action_ids
    assert "talkops_dispute:lg-fb-thread-1" not in updated_action_ids
    assert "talkops_open_alert_source" in updated_action_ids
    assert any(
        b.get("block_id") == "talkops_rca_feedback_status"
        and "RCA Confirmed" in b["elements"][0]["text"]
        and "<@U_SANDEEP>" in b["elements"][0]["text"]
        for b in updated_blocks
    )

    # Verify backend OperationalStore recorded the confirmation via IntelligenceAddon
    scores = await op_store.get_autoqa_scores(thread_id="lg-fb-thread-1")
    assert len(scores) == 1
    assert scores[0].dispute_status == "none"
    assert scores[0].overall_score == 1.0
    assert "sandeep" in scores[0].feedback_notes

    # Second conflicting click on the same Slack card is cleanly locked (no duplicate update)
    slack_calls.clear()
    slack_dispute_payload = {
        **slack_payload,
        "actions": [{"action_id": "talkops_dispute:lg-fb-thread-1", "value": "lg-fb-thread-1"}],
    }
    raw_form2 = urllib.parse.urlencode({"payload": json.dumps(slack_dispute_payload)}).encode("utf-8")
    sig2 = _sign_slack(slack_secret, ts_str, raw_form2)
    await slack_adapter.handle_webhook_interaction(
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": ts_str,
            "X-Slack-Signature": sig2,
        },
        raw_form2,
    )
    await asyncio.gather(*list(slack_adapter._background_tasks), return_exceptions=True)
    assert not [url for url, _ in slack_calls if "chat.update" in url]
    dup_ephem = [body for url, body in slack_calls if "hooks.slack.com/actions" in url]
    assert len(dup_ephem) == 1 and "already recorded" in dup_ephem[0]["text"]

    # Test Discord `Dispute Hypothesis` button click -> ephemeral ack + PATCH locked disabled button + DB dispute
    discord_calls.clear()
    disc_reply = await discord_adapter._process_interaction_payload(
        {
            "type": 3,
            "id": "INT_D1",
            "token": "TOK_D1",
            "channel_id": "CH_SRE",
            "guild_id": "G_SRE",
            "member": {"user": {"id": "998877", "username": "alice_sre"}},
            "message": {"id": discord_rca_msg_id},
            "data": {"custom_id": "talkops_dispute:lg-fb-thread-1"},
        }
    )
    assert disc_reply is not None
    assert disc_reply["data"]["flags"] == 64
    assert "Hypothesis disputed" in disc_reply["data"]["content"]
    assert "📝" not in disc_reply["data"]["content"]
    patch_calls = [body for m, url, body in discord_calls if m == "PATCH" and discord_rca_msg_id in url]
    assert len(patch_calls) == 1
    row_btns = patch_calls[0]["components"][0]["components"]
    assert any(b.get("style") == 5 and b.get("label") == "View Alert" for b in row_btns)
    assert any(b.get("disabled") is True and b.get("label") == "Hypothesis Disputed" for b in row_btns)
    assert not any(str(b.get("custom_id") or "").startswith(("talkops_confirm:", "talkops_dispute:")) for b in row_btns)
    assert any(f["name"] == "Hypothesis Disputed" and "<@998877>" in f["value"] for f in patch_calls[0]["embeds"][-1]["fields"])

    scores_after_dispute = await op_store.get_autoqa_scores(thread_id="lg-fb-thread-1")
    assert scores_after_dispute[0].dispute_status == "disputed"

    # Test Microsoft Teams `Action.Execute` (`talkops_confirm`) -> invoke toast ack + PUT locked Adaptive Card
    teams_calls.clear()
    teams_invoke = {
        "type": "invoke",
        "id": "ACT_T1",
        "name": "adaptiveCard/action",
        "serviceUrl": "https://smba.trafficmanager.net/amer",
        "replyToId": teams_rca_msg_id,
        "from": {"id": "teams-user-1", "name": "Priya SRE"},
        "conversation": {"id": "19:sre@thread.tacv2", "conversationType": "channel"},
        "value": {
            "action": {
                "type": "Action.Execute",
                "verb": "talkops_confirm",
                "data": {
                    "action_id": "talkops_confirm:lg-fb-thread-1",
                    "verdict": "helped",
                    "lg_thread_id": "lg-fb-thread-1",
                },
            }
        },
    }
    teams_resp = await teams_adapter.handle_webhook_event({}, json.dumps(teams_invoke).encode("utf-8"))
    assert teams_resp.status_code == 200
    assert "RCA confirmed" in str(teams_resp.body.get("value", ""))
    teams_puts = [body for m, url, body in teams_calls if m == "PUT" and teams_rca_msg_id in url]
    assert len(teams_puts) == 1
    locked_card = teams_puts[0]["attachments"][0]["content"]
    assert all(a.get("type") == "Action.OpenUrl" for a in locked_card.get("actions", []))
    assert any(
        isinstance(el, dict) and el.get("id") == "talkops_rca_feedback_status" and el.get("style") == "good"
        for el in locked_card["body"]
    )

    # ── 4. Test 👍 / 👎 emoji reactions on RCA messages (including default-profile persistence fallback without IntelligenceAddon!) ──
    await intel.on_shutdown(ctx)
    assert not bus.has_subscribers("feedback.helped")

    await addon._on_webhook_completed(
        _FakeEvent(
            {
                "thread_id": "lg-fb-react-2",
                "provider": "alertmanager",
                "title": "Checkout5xx",
                "severity": "critical",
                "service_name": "checkout",
                "status": "completed",
                "analysis": "## Root Cause\nConnection pool exhausted.",
                "source_url": "https://prom.internal/alerts/2",
            },
            "lg-fb-react-2",
        )
    )
    slack_rca_2 = [k[1] for k in hub._rca_messages if k[0] == "slack"][-1]
    discord_rca_2 = [k[1] for k in hub._rca_messages if k[0] == "discord"][-1]
    teams_rca_2 = [k[1] for k in hub._rca_messages if k[0] == "teams"][-1]

    # Slack 👍 (`+1`) reaction confirms RCA, locks card, and persists via fallback store even without IntelligenceAddon
    slack_calls.clear()
    await hub.handle_inbound_reaction(
        ChannelReaction(
            channel="slack",
            conversation_id="C_SRE",
            sender_id="U_REACT_1",
            message_id=slack_rca_2,
            emoji="+1",
            metadata={"channel_id": "C_SRE", "parent_channel_id": "C_SRE"},
        )
    )
    assert any("chat.update" in url for url, _ in slack_calls)
    react_scores = await op_store.get_autoqa_scores(thread_id="lg-fb-react-2")
    assert len(react_scores) == 1
    assert react_scores[0].dispute_status == "none"
    assert "U_REACT_1" in react_scores[0].feedback_notes

    # Discord 👎 reaction disputes RCA, locks card, and marks `disputed` in OperationalStore
    discord_calls.clear()
    await hub.handle_inbound_reaction(
        ChannelReaction(
            channel="discord",
            conversation_id="CH_SRE",
            sender_id="445566",
            message_id=discord_rca_2,
            emoji="👎",
            metadata={"guild_id": "G_SRE", "sender_name": "bob_oncall", "parent_channel_id": "CH_SRE"},
        )
    )
    assert any(m == "PATCH" and discord_rca_2 in url for m, url, _ in discord_calls)
    react_scores_disputed = await op_store.get_autoqa_scores(thread_id="lg-fb-react-2")
    assert react_scores_disputed[0].dispute_status == "disputed"

    # Teams `like` reaction confirms RCA and updates Teams Adaptive Card in-place
    teams_calls.clear()
    await hub.handle_inbound_reaction(
        ChannelReaction(
            channel="teams",
            conversation_id="19:sre@thread.tacv2",
            sender_id="teams-u2",
            message_id=teams_rca_2,
            emoji="like",
            metadata={"sender_name": "Dev OnCall", "parent_channel_id": "19:sre@thread.tacv2"},
        )
    )
    assert any(m == "PUT" and teams_rca_2 in url for m, url, _ in teams_calls)

    await hub.stop()
    await op_store.aclose()
    reset_operational_store_cache()

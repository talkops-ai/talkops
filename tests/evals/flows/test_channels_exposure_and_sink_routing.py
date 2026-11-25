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

"""Scope 1: Deterministic Channel Exposure, Cryptographic Verifier & Sink Routing Evals.

Fast, zero-network verification of:
1. `ChannelExposure` admission truth tables (`SELF`, `ALLOWLIST`, `OPEN`, DM pairing, glob rules).
2. `WebhookDispatcher._filter_builtin_sink_routes` coexistence between `ChannelsAddon`
   (handling `slack`/`teams`/`discord`) and `sinks.py` (handling `generic_webhook`/`http`),
   including wildcard observer isolation (`MACEObserverSuite`).
3. Cryptographic webhook signature guardrails (Slack HMAC + replay TTL, Teams HMAC + JWT aud,
   Discord Ed25519) and loopback OAuth callback secret redaction.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from talkops.addons.gateway import HookEventBus
from talkops.channels import (
    ChannelExposure,
    DiscordChannelConfig,
    DiscordWebhookChannel,
    ExposureMode,
    SlackChannelConfig,
    SlackWebhookChannel,
    TeamsChannelConfig,
    TeamsWebhookChannel,
    contains_oauth_callback_url,
    format_markdown_for_discord,
    format_markdown_for_slack,
)
from talkops.webhooks.dispatcher import WebhookDispatcher


@pytest.mark.eval_regression
@pytest.mark.eval_scope("router_logic")
@pytest.mark.eval_category("guardrails")
@pytest.mark.parametrize(
    ("mode", "sender_id", "conv_id", "parent_id", "is_dm", "is_paired", "expected_allowed"),
    [
        (ExposureMode.SELF, "U_OP", "C_ANY", "C_ANY", False, False, True),
        (ExposureMode.SELF, "U_STRANGER", "D_STRANGER", None, True, False, False),
        (ExposureMode.SELF, "U_PAIRED", "D_PAIRED", None, True, True, True),
        (ExposureMode.ALLOWLIST, "U_OP", "D_OP", None, True, False, True),
        (ExposureMode.ALLOWLIST, "sre-alice", "D_ALICE", None, True, False, True),
        (ExposureMode.ALLOWLIST, "U_RANDOM", "C_OPS_PROD:1700.1", "C_OPS_PROD", False, False, True),
        (ExposureMode.ALLOWLIST, "U_RANDOM", "C_GENERAL:1700.1", "C_GENERAL", False, False, False),
        (ExposureMode.ALLOWLIST, "U_RANDOM", "D_RANDOM", None, True, False, False),
        (ExposureMode.OPEN, "U_ANYONE", "C_PUBLIC", "C_PUBLIC", False, False, True),
    ],
)
def test_channel_exposure_policy_router_enforces_self_allowlist_pairing_and_open_modes(
    mode: ExposureMode,
    sender_id: str,
    conv_id: str,
    parent_id: str | None,
    is_dm: bool,
    is_paired: bool,
    expected_allowed: bool,
) -> None:
    """Verify `ChannelExposure` admission truth table across modes, globs, and pairing."""
    exposure = ChannelExposure(
        mode=mode,
        operator_id="U_OP",
        allowed_users=frozenset({"sre-*"}),
        allowed_chats=frozenset({"C_OPS_*"}),
        open_ack=(mode == ExposureMode.OPEN),
    )
    exposure.validate("slack")
    decision = exposure.allows(
        sender_id=sender_id,
        conversation_id=conv_id,
        parent_channel_id=parent_id,
        is_dm=is_dm,
        is_paired=is_paired,
    )
    assert decision is expected_allowed


@pytest.mark.eval_regression
@pytest.mark.eval_scope("router_logic")
@pytest.mark.eval_category("graph_routing")
def test_webhook_dispatcher_sink_router_delegates_chat_providers_to_channels_while_preserving_http_callbacks() -> None:
    """Verify `WebhookDispatcher._filter_builtin_sink_routes` avoids duplicate Slack posts without dropping HTTP callbacks."""
    bus = HookEventBus()
    # Wildcard subscriber (e.g. MACEObserverSuite) must NOT falsely suppress sinks.py when ChannelsAddon is inactive
    bus.subscribe("*", lambda _ev: None)
    bus.subscribe("webhook.completed", lambda _ev: None)

    dispatcher = WebhookDispatcher(event_bus=bus)
    mixed_routes = [
        {"channel": "slack", "conversation_id": "C_WARROOM"},
        {"channel": "teams", "conversation_id": "19:teams_ops"},
        {"channel": "discord", "conversation_id": "987654321"},
        {"channel": "generic_webhook", "conversation_id": "http://127.0.0.1:9999/rca-sink"},
    ]

    # Case 1: ChannelsAddon is disabled -> all routes (including Slack/Teams/Discord) pass through to sinks.py
    assert dispatcher._filter_builtin_sink_routes(mixed_routes) == mixed_routes

    # Case 2: ChannelsAddon is active -> chat routes handled by ChannelsAddon are filtered out,
    #         while generic_webhook HTTP callback still passes through to sinks.py!
    bus._channels_addon_active = True  # type: ignore[attr-defined]
    bus._channels_addon_providers = {"slack", "teams", "discord"}  # type: ignore[attr-defined]
    filtered = dispatcher._filter_builtin_sink_routes(mixed_routes)
    assert filtered == [{"channel": "generic_webhook", "conversation_id": "http://127.0.0.1:9999/rca-sink"}]

    # Case 3: Only Slack route configured while ChannelsAddon is active -> returns None for sinks.py
    assert dispatcher._filter_builtin_sink_routes([{"channel": "slack", "conversation_id": "C_WARROOM"}]) is None


@pytest.mark.eval_regression
@pytest.mark.eval_scope("router_logic")
@pytest.mark.eval_category("guardrails")
def test_channel_cryptographic_verifiers_and_oauth_loopback_redaction_guardrails() -> None:
    """Verify Slack HMAC TTL, Teams HMAC/JWT audience, Discord Ed25519, broadcast neutralization & OAuth redaction."""
    body = b'{"type":"event_callback"}'
    now = int(time.time())

    # 1. Slack HMAC-SHA256 + 300s replay protection
    slack = SlackWebhookChannel(SlackChannelConfig(bot_token="xoxb-1", signing_secret="slack-sec"))
    valid_slack_sig = "v0=" + hmac.new(b"slack-sec", f"v0:{now}:".encode() + body, hashlib.sha256).hexdigest()
    assert slack.verify_signature(
        {"x-slack-request-timestamp": str(now), "x-slack-signature": valid_slack_sig},
        body,
        now=now,
    )
    stale_sig = "v0=" + hmac.new(b"slack-sec", f"v0:{now - 600}:".encode() + body, hashlib.sha256).hexdigest()
    assert not slack.verify_signature(
        {"x-slack-request-timestamp": str(now - 600), "x-slack-signature": stale_sig},
        body,
        now=now,
    )

    # 2. Teams HMAC + Bot Framework RS256 JWKS JWT signature & audience check
    import json

    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    raw_teams_key = b"teams-shared-secret"
    teams = TeamsWebhookChannel(
        TeamsChannelConfig(
            app_id="talkops-teams-app",
            app_password="pw",
            webhook_secret=base64.b64encode(raw_teams_key).decode("ascii"),
        )
    )
    teams_hmac = base64.b64encode(hmac.new(raw_teams_key, body, hashlib.sha256).digest()).decode("ascii")
    assert teams.verify_signature({"Authorization": f"HMAC {teams_hmac}"}, body)

    rsa_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk_pub = json.loads(RSAAlgorithm.to_jwk(rsa_priv.public_key()))
    jwk_pub["kid"] = "eval-bf-kid"
    teams.register_jwks_keys([jwk_pub])

    good_jwt = jwt.encode(
        {"iss": "https://api.botframework.com", "aud": "talkops-teams-app", "exp": now + 600},
        rsa_priv,
        algorithm="RS256",
        headers={"kid": "eval-bf-kid"},
    )
    bad_aud_jwt = jwt.encode(
        {"iss": "https://api.botframework.com", "aud": "wrong-app", "exp": now + 600},
        rsa_priv,
        algorithm="RS256",
        headers={"kid": "eval-bf-kid"},
    )
    forged_hs256 = jwt.encode(
        {"iss": "https://api.botframework.com", "aud": "talkops-teams-app", "exp": now + 600},
        "0123456789abcdef0123456789abcdef",
        algorithm="HS256",
        headers={"kid": "eval-bf-kid"},
    )
    assert teams.verify_signature({"Authorization": f"Bearer {good_jwt}"}, body)
    assert not teams.verify_signature({"Authorization": f"Bearer {bad_aud_jwt}"}, body)
    assert not teams.verify_signature({"Authorization": f"Bearer {forged_hs256}"}, body)

    # 3. Discord Ed25519 signature verification
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    discord = DiscordWebhookChannel(DiscordChannelConfig(bot_token="bot-1", public_key=pub_hex))
    ts_str = str(now)
    disc_sig = priv.sign(ts_str.encode("utf-8") + body).hex()
    assert discord.verify_signature({"x-signature-ed25519": disc_sig, "x-signature-timestamp": ts_str}, body)
    assert not discord.verify_signature({"x-signature-ed25519": "00" * 32, "x-signature-timestamp": ts_str}, body)

    # 4. Outbound broadcast neutralization + loopback OAuth callback secret interception
    assert "<!channel>" not in format_markdown_for_slack("Alert <!channel> check pods")
    assert "@\u200beveryone" in format_markdown_for_discord("Alert @everyone check pods")
    assert contains_oauth_callback_url("http://localhost:8765/callback?code=SECRET_AUTH_CODE&state=s1") is True
    assert contains_oauth_callback_url("https://grafana.internal/d/checkout?orgId=1") is False

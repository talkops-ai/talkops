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

"""Built-In `channels` Addon, Multi-Provider ChatOps, HITL Mediation & Webhook Thread Handoff Evals.

End-to-end Swiss-Cheese evaluations for the integrated `channels` + `webhooks` server addons:
1. Two-stage Alertmanager -> `HookEventBus` -> `ChannelsAddon` Slack Block Kit thread binding
   + simultaneous `generic_webhook` HTTP callback coexistence + live in-thread Slack SRE follow-up
   persisting an operational memory rule into `SqliteOperationalStore`.
2. Live Slack `@mention` HITL mediation in `manual` mode: agent pauses before `kubectl_rollout_undo`,
   posts an interactive Slack Block Kit approval card, and resumes cleanly upon a signed button click.
3. Multi-provider Discord (Ed25519 Interactions `/ask`) and Microsoft Teams (HMAC Activity + Adaptive Card)
   live agent diagnostics through the built-in `/channels/{provider}/*` ASGI endpoints.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from talkops.channels import (
    ChannelExposure,
    DiscordChannelConfig,
    DiscordWebhookChannel,
    ExposureMode,
    SlackChannelConfig,
    SlackWebhookChannel,
    TeamsChannelConfig,
    TeamsWebhookChannel,
)
from tests.evals.fixtures.topology_and_tools import seed_production_checkout_topology
from tests.evals.fixtures.webhook_payloads import build_alertmanager_crashloop_payload
from tests.evals.framework import (
    EvalTelemetry,
    build_handoff_continuity_judge,
    evaluate_required_tools_superset,
)


def _sign_slack_request(secret: str, timestamp: int, body: bytes) -> dict[str, str]:
    basestring = f"v0:{timestamp}:".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return {
        "x-slack-request-timestamp": str(timestamp),
        "x-slack-signature": f"v0={digest}",
        "content-type": "application/json",
    }


def _attach_mocked_slack_adapter(
    harness: Any,
    *,
    signing_secret: str = "eval-slack-signing-secret",
    operator_id: str = "U_SRE_ONCALL",
    allowed_chats: frozenset[str] = frozenset({"C_SRE_WARROOM"}),
) -> tuple[SlackWebhookChannel, list[tuple[str, dict[str, Any]]]]:
    """Register a mocked-HTTP `SlackWebhookChannel` onto `harness.channels_addon.hub`."""
    slack_api_calls: list[tuple[str, dict[str, Any]]] = []

    def _mock_slack_api(request: httpx.Request) -> httpx.Response:
        if "conversations.replies" in str(request.url):
            return httpx.Response(200, json={"ok": True, "messages": []})
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        slack_api_calls.append((request.url.path, body))
        return httpx.Response(200, json={"ok": True, "ts": f"1715000.{len(slack_api_calls):03d}"})

    slack_http = httpx.AsyncClient(transport=httpx.MockTransport(_mock_slack_api))
    slack_adapter = SlackWebhookChannel(
        SlackChannelConfig(
            bot_token="xoxb-eval-slack-token",
            signing_secret=signing_secret,
            bot_user_id="U_TALKOPS_BOT",
            exposure=ChannelExposure(
                mode=ExposureMode.ALLOWLIST,
                operator_id=operator_id,
                allowed_chats=allowed_chats,
            ),
        ),
        http_client=slack_http,
    )
    ch_addon = harness.channels_addon
    assert ch_addon is not None, "Expected built-in `channels` addon to be active"
    ch_addon.hub.register_adapter(slack_adapter)
    return slack_adapter, slack_api_calls


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("multiturn_simulation")
@pytest.mark.eval_category("webhook_rca")
@pytest.mark.serve_profile("sre")
async def test_sre_profile_webhook_creates_bound_slack_thread_and_oncall_reply_continues_investigation_and_saves_rule(
    serve_harness_factory: Any,
    eval_judge_model: Any,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify Alertmanager -> Slack 2-stage thread binding + `generic_webhook` coexistence + in-thread SRE follow-up."""
    harness = await serve_harness_factory(profile="sre", no_subagents=True)
    await seed_production_checkout_topology(harness.sqlite_store)

    _slack_adapter, slack_api_calls = _attach_mocked_slack_adapter(harness)

    # Configure BOTH `slack` (handled by built-in `ChannelsAddon`) and `generic_webhook` (handled by `sinks.py`)
    harness.dispatcher.unregister_outbound_sink("eval_capture")
    harness.dispatcher.config.outbound_channels = [
        {
            "channel": "slack",
            "conversation_id": "C_SRE_WARROOM",
            "providers": ["*"],
            "severities": ["*"],
            "notify_on": ["received", "completed"],
            "bind_thread": True,
        },
        {
            "channel": "generic_webhook",
            "conversation_id": "http://sink.internal/rca-callback",
            "providers": ["*"],
            "severities": ["*"],
            "notify_on": ["received", "completed"],
        },
    ]

    generic_webhook_posts: list[dict[str, Any]] = []
    real_async_post = httpx.AsyncClient.post

    async def _intercept_external_sink_post(
        client_self: httpx.AsyncClient,
        url: Any,
        *args: Any,
        **kwargs: Any,
    ) -> httpx.Response:
        if "sink.internal/rca-callback" in str(url):
            generic_webhook_posts.append({"url": str(url), "json": kwargs.get("json")})
            return httpx.Response(
                200,
                json={"ok": True},
                request=httpx.Request("POST", str(url)),
            )
        return await real_async_post(client_self, url, *args, **kwargs)

    t0 = time.perf_counter()
    with patch.object(httpx.AsyncClient, "post", new=_intercept_external_sink_post):
        wh_resp = await harness.http_client.post(
            "/webhooks/alertmanager",
            json=build_alertmanager_crashloop_payload(service="checkout-api", namespace="prod"),
        )
        assert wh_resp.status_code == 202
        webhook_thread_id = wh_resp.json()["thread_id"]
        await harness.wait_for_webhook_runs()

    # Layer 1A: Verify `ChannelsAddon` posted root card, posted chunked RCA in-thread, and updated root card in-place
    post_msgs = [b for path, b in slack_api_calls if path.endswith("/chat.postMessage")]
    update_msgs = [b for path, b in slack_api_calls if path.endswith("/chat.update")]
    assert len(post_msgs) >= 2, f"Expected root card + >=1 in-thread RCA post, got: {slack_api_calls}"
    assert post_msgs[0]["channel"] == "C_SRE_WARROOM"
    root_slack_ts = "1715000.001"
    assert all(m.get("thread_ts") == root_slack_ts for m in post_msgs[1:])
    combined_rca_thread = "\n".join(m.get("text", "").lower() for m in post_msgs[1:])
    assert "orders-rds" in combined_rca_thread or "hikari" in combined_rca_thread
    assert len(update_msgs) >= 1 and update_msgs[0]["ts"] == root_slack_ts
    pre_followup_call_count = len(slack_api_calls)

    # Layer 1B: Verify `generic_webhook` HTTP callback ALSO fired for both `received` and `completed`
    assert len(generic_webhook_posts) == 2

    # Layer 1C: Verify `ChannelHub` bound `("slack", "C_SRE_WARROOM:1715000.001")` -> `webhook_thread_id`
    bound_tid, is_new = await harness.channels_addon.hub._get_or_create_lg_thread(
        "slack", f"C_SRE_WARROOM:{root_slack_ts}"
    )
    assert bound_tid == webhook_thread_id
    assert is_new is False

    # Turn 2: On-call SRE replies inside the bound Slack thread WITHOUT an @mention
    now = int(time.time())
    followup_text = (
        "Thanks! Quick check — did redis-cart show any latency or errors during this spike? "
        "Also please save an operational memory rule for checkout-api stating: "
        "'Pause nightly analytics exports whenever orders-rds replica lag exceeds 15 seconds.'"
    )
    slack_reply_event = {
        "event_id": "Ev_SRE_FOLLOWUP_1",
        "type": "event_callback",
        "event": {
            "type": "message",
            "user": "U_SRE_ONCALL",
            "channel": "C_SRE_WARROOM",
            "ts": "1715000.050",
            "thread_ts": root_slack_ts,
            "text": followup_text,
        },
    }
    raw_reply = json.dumps(slack_reply_event).encode("utf-8")
    ev_resp = await harness.http_client.post(
        "/channels/slack/events",
        content=raw_reply,
        headers=_sign_slack_request("eval-slack-signing-secret", now, raw_reply),
    )
    assert ev_resp.status_code == 200
    await harness.wait_for_channel_runs()
    elapsed = time.perf_counter() - t0

    # Layer 1D: Verify Slack thread received the assistant's follow-up answer & SQLite stored the memory rule
    followup_replies = [
        b["text"]
        for path, b in slack_api_calls[pre_followup_call_count:]
        if (path.endswith("/chat.postMessage") or path.endswith("/chat.update")) and b.get("text")
    ]
    assert any("redis" in t.lower() for t in followup_replies)

    saved_rules = await harness.sqlite_store.list_active_memory_rules(service_name="checkout-api")
    rule_texts = " ".join(r.rule_text.lower() for r in saved_rules)
    rule_saved = "15" in rule_texts or "analytics" in rule_texts or "replica" in rule_texts
    assert rule_saved is True, f"Expected memory rule in SQLite, got: {saved_rules}"

    # Layer 2: `agentevals` superset trajectory check across the unified webhook + Slack thread
    st = await harness.gateway.get_thread_state(webhook_thread_id)
    traj_grade = evaluate_required_tools_superset(
        st.values["messages"],
        "Webhook + Slack follow-up",
        [
            ("kubectl_get_pods", {}),
            ("record_operational_memory_rule", {}),
        ],
    )
    assert traj_grade["score"] is True

    # Layer 3: Calibrated binary `openevals` handoff continuity judge
    handoff_judge = build_handoff_continuity_judge(eval_judge_model)
    transcript = "\n\n".join(
        f"{getattr(m, 'type', 'msg')}: {getattr(m, 'content', '')}"
        for m in st.values["messages"]
        if getattr(m, "content", "")
    )
    judge_res = handoff_judge(
        inputs="Alertmanager checkout-api alert followed by in-thread Slack SRE follow-up",
        outputs=transcript,
        reference_outputs=(
            "Preserves prior checkout-api / orders-rds investigation context, confirms redis-cart remained healthy "
            "(~1ms latency, 0 errors), and saves the operational memory rule about pausing nightly analytics exports."
        ),
    )
    eval_telemetry.log_feedback(key="slack_webhook_thread_handoff_pass", score=bool(judge_res["score"]))
    assert judge_res["score"] is True
    eval_telemetry.record_efficiency(
        st.values["messages"],
        ideal_steps=4,
        ideal_tool_calls=3,
        elapsed_seconds=elapsed,
        solved=True,
    )


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("deep_agent_outcome")
@pytest.mark.eval_category("guardrails")
@pytest.mark.serve_profile("default", "sre")
async def test_slack_chatops_manual_mode_pauses_on_mutating_rollback_and_resumes_after_signed_block_kit_approval(
    serve_harness_factory: Any,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify Slack `@mention` in `manual` mode emits a Block Kit HITL card before mutating and resumes on click."""
    harness = await serve_harness_factory(
        profile="sre",
        interactive=False,
        auto_approve=False,
        no_subagents=True,
    )
    _slack_adapter, slack_api_calls = _attach_mocked_slack_adapter(harness)
    hub = harness.channels_addon.hub
    hub.verbosity = 1
    hub.default_approval_mode = "manual"

    now = int(time.time())
    mention_event = {
        "event_id": "Ev_HITL_ROLLBACK_1",
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "user": "U_SRE_ONCALL",
            "channel": "C_SRE_WARROOM",
            "ts": "1716000.001",
            "text": (
                "<@U_TALKOPS_BOT> Please roll back the checkout-api deployment in prod "
                "to its previous stable revision right now using kubectl_rollout_undo."
            ),
        },
    }
    raw_mention = json.dumps(mention_event).encode("utf-8")
    resp1 = await harness.http_client.post(
        "/channels/slack/events",
        content=raw_mention,
        headers=_sign_slack_request("eval-slack-signing-secret", now, raw_mention),
    )
    assert resp1.status_code == 200
    await harness.wait_for_channel_runs()

    # Layer 1 (Paused State): Mutation has NOT executed yet, and a Block Kit approval card is pending!
    conv_key = "C_SRE_WARROOM:1716000.001"
    pending = hub.approvals.get_for_conversation("slack", conv_key)
    assert pending is not None, f"Expected pending HITL approval on {conv_key}, calls={slack_api_calls}"
    assert not any(c["name"] == "kubectl_rollout_undo" for c in harness.tool_call_log)
    assert any(
        path.endswith("/chat.postMessage") and "blocks" in body
        for path, body in slack_api_calls
    )

    # Operator clicks "✅ Approve" on the Slack Block Kit card (resuming up to 3 sequential tool gates if needed)
    for step_idx in range(3):
        current_pending = hub.approvals.get_for_conversation("slack", conv_key)
        if current_pending is None:
            break
        block_action_payload = {
            "type": "block_actions",
            "user": {"id": "U_SRE_ONCALL", "username": "sre_lead"},
            "channel": {"id": "C_SRE_WARROOM"},
            "message": {"ts": current_pending.prompt_message_id, "thread_ts": "1716000.001"},
            "actions": [
                {
                    "action_id": f"talkops_approve:{current_pending.approval_id}",
                    "value": json.dumps(
                        {
                            "approval_id": current_pending.approval_id,
                            "decision": "approve",
                            "conversation_id": conv_key,
                        }
                    ),
                }
            ],
        }
        form_bytes = urllib.parse.urlencode({"payload": json.dumps(block_action_payload)}).encode("utf-8")
        inter_headers = _sign_slack_request("eval-slack-signing-secret", now + step_idx + 1, form_bytes)
        inter_headers["content-type"] = "application/x-www-form-urlencoded"
        resp_inter = await harness.http_client.post(
            "/channels/slack/interactions",
            content=form_bytes,
            headers=inter_headers,
        )
        assert resp_inter.status_code == 200
        await harness.wait_for_channel_runs()

    # Layer 1 (Resumed State): Pending approval cleared, card updated, and `kubectl_rollout_undo` executed!
    assert hub.approvals.get_for_conversation("slack", conv_key) is None
    assert any(c["name"] == "kubectl_rollout_undo" for c in harness.tool_call_log)
    assert any(
        path.endswith("/chat.update") and "Approved" in body.get("text", "")
        for path, body in slack_api_calls
    )
    eval_telemetry.log_feedback(key="slack_block_kit_hitl_rollback_verified", score=True)


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("deep_agent_outcome")
@pytest.mark.eval_category("tool_use")
@pytest.mark.serve_profile("default")
async def test_discord_ed25519_slash_command_and_teams_hmac_activity_investigate_cluster_via_builtin_channels(
    serve_harness_factory: Any,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify built-in Discord (Ed25519 `/ask`) and Microsoft Teams (HMAC Activity) adapters run live diagnostics."""
    harness = await serve_harness_factory(profile="default", no_subagents=True)
    hub = harness.channels_addon.hub
    hub.verbosity = 1

    # 1. Attach mocked Discord adapter with real Ed25519 keypair
    priv_key = Ed25519PrivateKey.generate()
    pub_hex = priv_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    discord_outbound: list[dict[str, Any]] = []

    def _mock_discord_api(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/typing"):
            return httpx.Response(204)
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        discord_outbound.append(body)
        return httpx.Response(200, json={"id": f"disc_{len(discord_outbound)}"})

    discord_adapter = DiscordWebhookChannel(
        DiscordChannelConfig(
            bot_token="disc-eval-token",
            application_id="disc-app-1",
            public_key=pub_hex,
            exposure=ChannelExposure(mode=ExposureMode.ALLOWLIST, operator_id="U_DISC_SRE"),
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_mock_discord_api)),
    )
    hub.register_adapter(discord_adapter)

    # 2. Attach mocked Microsoft Teams adapter with HMAC webhook secret
    teams_raw_key = b"teams-eval-hmac-key"
    teams_b64_key = base64.b64encode(teams_raw_key).decode("ascii")
    teams_outbound: list[dict[str, Any]] = []

    def _mock_teams_api(request: httpx.Request) -> httpx.Response:
        if "oauth2/v2.0/token" in str(request.url):
            return httpx.Response(200, json={"access_token": "bf-eval-tok", "expires_in": 3600})
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if body.get("type") != "typing":
            teams_outbound.append(body)
        return httpx.Response(200, json={"id": f"teams_{len(teams_outbound)}"})

    teams_adapter = TeamsWebhookChannel(
        TeamsChannelConfig(
            app_id="teams-eval-app",
            app_password="pw",
            webhook_secret=teams_b64_key,
            exposure=ChannelExposure(mode=ExposureMode.ALLOWLIST, operator_id="29:teams-sre"),
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_mock_teams_api)),
    )
    hub.register_adapter(teams_adapter)

    def _sign_discord(payload_bytes: bytes) -> dict[str, str]:
        ts = str(int(time.time()))
        sig = priv_key.sign(ts.encode("utf-8") + payload_bytes).hex()
        return {
            "x-signature-ed25519": sig,
            "x-signature-timestamp": ts,
            "content-type": "application/json",
        }

    # Verify `/channels/status` reports both `discord` and `teams` configured
    st_resp = await harness.http_client.get("/channels/status")
    assert st_resp.status_code == 200
    by_ch = {c["channel"]: c for c in st_resp.json()["channels"]}
    assert by_ch["discord"]["configured"] is True
    assert by_ch["teams"]["configured"] is True

    # A. Discord Ed25519 `/ask` slash command -> deferred response (type 5) + live pod diagnostics
    disc_ask = json.dumps(
        {
            "type": 2,
            "id": "inter_disc_1",
            "token": "tok_disc_1",
            "channel_id": "C_DISC_OPS",
            "member": {"user": {"id": "U_DISC_SRE", "username": "disc_sre"}},
            "data": {
                "name": "ask",
                "options": [
                    {
                        "name": "prompt",
                        "value": "Check the checkout-api pods in prod and tell me why they are failing.",
                    }
                ],
            },
        }
    ).encode("utf-8")
    disc_resp = await harness.http_client.post(
        "/channels/discord/interactions",
        content=disc_ask,
        headers=_sign_discord(disc_ask),
    )
    assert disc_resp.status_code == 200
    assert disc_resp.json() == {"type": 5}
    await harness.wait_for_channel_runs()

    assert len(discord_outbound) >= 1
    disc_combined = " ".join(m.get("content", "").lower() for m in discord_outbound)
    assert "checkout-api" in disc_combined or "crashloopbackoff" in disc_combined or "orders-rds" in disc_combined

    # B. Microsoft Teams HMAC Activity -> live RDS check + Adaptive Card attachment delivery
    teams_act = json.dumps(
        {
            "type": "message",
            "id": "act_teams_1",
            "serviceUrl": "https://smba.trafficmanager.net/amer",
            "from": {"id": "29:teams-sre", "name": "Teams SRE"},
            "recipient": {"id": "28:talkops-bot", "name": "TalkOps"},
            "conversation": {"id": "19:teams_ops_chan", "conversationType": "channel"},
            "text": "<at>TalkOps</at> Inspect the orders-rds RDS instance in ap-south-1 and report connection usage.",
            "entities": [{"type": "mention", "mentioned": {"id": "28:talkops-bot"}}],
        }
    ).encode("utf-8")
    teams_sig = base64.b64encode(hmac.new(teams_raw_key, teams_act, hashlib.sha256).digest()).decode("ascii")
    teams_resp = await harness.http_client.post(
        "/channels/teams/events",
        content=teams_act,
        headers={"authorization": f"HMAC {teams_sig}", "content-type": "application/json"},
    )
    assert teams_resp.status_code == 200
    await harness.wait_for_channel_runs()

    assert len(teams_outbound) >= 1
    assert any("attachments" in m for m in teams_outbound)
    assert any(c["name"] == "aws_rds_describe_instances" for c in harness.tool_call_log)
    eval_telemetry.log_feedback(key="discord_and_teams_live_diagnostics_pass", score=True)

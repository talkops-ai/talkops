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

"""Outbound write-back sinks and SNS handshake confirmation for TalkOps webhooks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx

from talkops.integrations.content_filter import filter_internal_content
from talkops.integrations.hooks import dispatch_hook_fire_and_forget
from talkops.utils.logger import get_logger
from talkops.webhooks.formatting import (
    build_slack_rca_blocks,
    parse_incident_output,
    parse_rca_output,
)
from talkops.webhooks.models import NormalizedWebhookEvent, WebhookConfig
from talkops.webhooks.security import is_valid_sns_subscribe_url

logger = get_logger(__name__)

_PAGERDUTY_NOTES_MAX_CHARS = 20000
_SLACK_MAX_CHARS = 3500


@dataclass(slots=True)
class WebhookRunResult:
    """Outcome of a background LangGraph run triggered by a webhook."""

    run_id: str
    thread_id: str
    status: str
    analysis_text: str
    error: str | None = None


async def confirm_sns_subscription(
    subscribe_url: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> bool:
    """Confirm an AWS SNS topic subscription by visiting its validated SubscribeURL."""
    if not is_valid_sns_subscribe_url(subscribe_url):
        logger.warning("Refusing to confirm invalid SNS SubscribeURL: %r", subscribe_url)
        return False

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=10.0, follow_redirects=False)
    try:
        resp = await client.get(subscribe_url)
        resp.raise_for_status()
        logger.info("Confirmed AWS SNS subscription via %s", subscribe_url)
        return True
    except Exception as exc:
        logger.warning("Failed to confirm AWS SNS subscription: %s", exc)
        return False
    finally:
        if owns_client:
            await client.aclose()


async def dispatch_completion_sinks(
    event: NormalizedWebhookEvent,
    result: WebhookRunResult,
    config: WebhookConfig,
    *,
    http_client: httpx.AsyncClient | None = None,
    outbound_routes: list[dict[str, Any]] | None = None,
    incident_thread_refs: dict[tuple[str, str], dict[str, str]] | None = None,
) -> dict[str, bool]:
    """Deliver the agent's root-cause analysis to all configured outbound sinks."""
    cleaned_analysis = filter_internal_content(result.analysis_text).strip()
    if not cleaned_analysis and result.error:
        cleaned_analysis = f"Run failed with error: {result.error}"

    # 1. Dispatch local TalkOps hook event
    dispatch_hook_fire_and_forget(
        "webhook.completed",
        {
            "provider": event.provider,
            "event_type": event.event_type,
            "incident_id": event.incident_id,
            "thread_id": result.thread_id,
            "run_id": result.run_id,
            "status": result.status,
            "title": event.title,
            "severity": str(event.severity),
            "service_name": event.service_name,
            "source_url": event.source_url,
            "analysis": cleaned_analysis,
            "error": result.error,
        },
    )

    outcomes: dict[str, bool] = {}
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=15.0)

    try:
        # 2. PagerDuty Incident Note write-back
        pd_incident_id = event.reply_target.get("incident_id") if event.provider == "pagerduty" else None
        if pd_incident_id and config.pagerduty_api_key and config.pagerduty_from_email:
            outcomes["pagerduty"] = await _post_pagerduty_note(
                client,
                incident_id=str(pd_incident_id),
                analysis=cleaned_analysis,
                api_key=config.pagerduty_api_key,
                from_email=config.pagerduty_from_email,
            )

        # 3. ServiceNow Work Note write-back
        snow_sys_id = event.reply_target.get("sys_id") if event.provider == "servicenow" else None
        if snow_sys_id and config.servicenow_instance_url and config.servicenow_api_token:
            outcomes["servicenow"] = await _post_servicenow_work_note(
                client,
                instance_url=config.servicenow_instance_url,
                sys_id=str(snow_sys_id),
                analysis=cleaned_analysis,
                api_token=config.servicenow_api_token,
            )

        # 4. GitHub Issue / PR Comment write-back
        gh_repo = event.reply_target.get("repository") if event.provider == "github" else None
        gh_issue = event.reply_target.get("issue_number") if event.provider == "github" else None
        if gh_repo and gh_issue and config.github_token:
            outcomes["github"] = await _post_github_comment(
                client,
                repository=str(gh_repo),
                issue_number=int(gh_issue),
                analysis=cleaned_analysis,
                token=config.github_token,
            )

        # 5. Slack Incoming Webhook notification
        if config.slack_webhook_url:
            outcomes["slack"] = await _post_slack_notification(
                client,
                webhook_url=config.slack_webhook_url,
                event=event,
                result=result,
                analysis=cleaned_analysis,
            )

        # 6. Generic Outbound Callback URL
        callback_url = event.callback_url or config.default_callback_url
        if callback_url:
            outcomes["callback"] = await _post_callback_webhook(
                client,
                callback_url=callback_url,
                event=event,
                result=result,
                analysis=cleaned_analysis,
            )

        # 7. Built-in One-Way Outbound Channel Routes (`TALKOPS_WEBHOOK_NOTIFY_CHANNELS`)
        #    Executed automatically when no external two-way `channels` addon is active.
        if outbound_routes:
            channel_outcomes = await dispatch_outbound_channel_completed(
                event=event,
                result=result,
                analysis=cleaned_analysis,
                routes=outbound_routes,
                thread_refs=incident_thread_refs,
                http_client=client,
            )
            outcomes.update(channel_outcomes)
    finally:
        if owns_client:
            await client.aclose()

    if outcomes:
        logger.info(
            "Webhook outbound sinks dispatched | provider=%s incident_id=%s thread_id=%s outcomes=%s",
            event.provider,
            event.incident_id,
            result.thread_id,
            outcomes,
        )
    else:
        logger.info(
            "Webhook run finalized | provider=%s incident_id=%s thread_id=%s (no external write-back sinks configured)",
            event.provider,
            event.incident_id,
            result.thread_id,
        )

    return outcomes


async def _post_pagerduty_note(
    client: httpx.AsyncClient,
    *,
    incident_id: str,
    analysis: str,
    api_key: str,
    from_email: str,
) -> bool:
    note_body = f"[TalkOps Automated RCA]\n{analysis}"[:_PAGERDUTY_NOTES_MAX_CHARS]
    try:
        resp = await client.post(
            f"https://api.pagerduty.com/incidents/{incident_id}/notes",
            headers={
                "Authorization": f"Token token={api_key}",
                "From": from_email,
                "Accept": "application/vnd.pagerduty+json;version=2",
                "Content-Type": "application/json",
            },
            json={"note": {"content": note_body}},
        )
        resp.raise_for_status()
        logger.info("Posted TalkOps RCA note to PagerDuty incident %s", incident_id)
        return True
    except Exception as exc:
        logger.warning("Failed to post PagerDuty note for incident %s: %s", incident_id, exc)
        return False


async def _post_servicenow_work_note(
    client: httpx.AsyncClient,
    *,
    instance_url: str,
    sys_id: str,
    analysis: str,
    api_token: str,
) -> bool:
    base_url = instance_url.rstrip("/")
    try:
        resp = await client.patch(
            f"{base_url}/api/now/table/incident/{sys_id}",
            headers={
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"work_notes": f"[TalkOps Automated RCA]\n{analysis}"},
        )
        resp.raise_for_status()
        logger.info("Posted TalkOps work note to ServiceNow incident %s", sys_id)
        return True
    except Exception as exc:
        logger.warning("Failed to post ServiceNow work note for %s: %s", sys_id, exc)
        return False


async def _post_github_comment(
    client: httpx.AsyncClient,
    *,
    repository: str,
    issue_number: int,
    analysis: str,
    token: str,
) -> bool:
    try:
        resp = await client.post(
            f"https://api.github.com/repos/{repository}/issues/{issue_number}/comments",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            json={"body": f"### [TalkOps Automated Analysis]\n\n{analysis}"},
        )
        resp.raise_for_status()
        logger.info("Posted TalkOps analysis comment to GitHub %s#%d", repository, issue_number)
        return True
    except Exception as exc:
        logger.warning("Failed to post GitHub comment to %s#%d: %s", repository, issue_number, exc)
        return False


async def _post_slack_notification(
    client: httpx.AsyncClient,
    *,
    webhook_url: str,
    event: NormalizedWebhookEvent,
    result: WebhookRunResult,
    analysis: str,
) -> bool:
    truncated = analysis[:_SLACK_MAX_CHARS]
    header = (
        f"*[TalkOps Automated RCA]* `{event.provider.upper()}` | "
        f"*{event.title}* (`{event.severity.value.upper()}`)\n"
        f"Thread: `{result.thread_id}` | Status: `{result.status}`"
    )
    if event.source_url:
        header += f" | <{event.source_url}|View Source>"

    fields: list[dict[str, str]] = [
        {"type": "mrkdwn", "text": f"*Provider*\n`{event.provider.upper()}`"},
        {"type": "mrkdwn", "text": f"*Severity*\n`{event.severity.value.upper()}`"},
        {"type": "mrkdwn", "text": f"*Status*\n`{result.status}`"},
        {"type": "mrkdwn", "text": f"*Service*\n`{event.service_name or 'infrastructure'}`"},
    ]
    footer = f"Thread `{result.thread_id}`  ·  Incident `{event.incident_id}`"
    if event.source_url:
        footer += f"  ·  <{event.source_url}|View Source>"

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"TalkOps Automated RCA — {event.title}"[:150],
                "emoji": True,
            },
        },
        {"type": "section", "fields": fields},
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": truncated[:2900] or "_No analysis details._"},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": footer[:1900]}],
        },
    ]

    try:
        resp = await client.post(
            webhook_url,
            json={"text": f"{header}\n\n{truncated}", "blocks": blocks},
        )
        resp.raise_for_status()
        logger.info("Posted TalkOps RCA notification to Slack webhook for incident %s", event.incident_id)
        return True
    except Exception as exc:
        logger.warning("Failed to post Slack webhook notification: %s", exc)
        return False


async def _post_callback_webhook(
    client: httpx.AsyncClient,
    *,
    callback_url: str,
    event: NormalizedWebhookEvent,
    result: WebhookRunResult,
    analysis: str,
) -> bool:
    payload: dict[str, Any] = {
        "provider": event.provider,
        "event_type": event.event_type,
        "incident_id": event.incident_id,
        "thread_id": result.thread_id,
        "run_id": result.run_id,
        "status": result.status,
        "title": event.title,
        "severity": str(event.severity),
        "service_name": event.service_name,
        "source_url": event.source_url,
        "analysis": analysis,
        "error": result.error,
    }
    try:
        resp = await client.post(callback_url, json=payload)
        resp.raise_for_status()
        logger.info("Posted TalkOps RCA payload to callback URL %s for incident %s", callback_url, event.incident_id)
        return True
    except Exception as exc:
        logger.warning("Failed to post outbound callback webhook to %s: %s", callback_url, exc)
        return False


def _resolve_slack_bot_token() -> str | None:
    from talkops.config.settings import resolve_env_var

    raw = resolve_env_var(
        "SLACK_BOT_TOKEN",
        fallback_names=(
            "SLACK_TOKEN",
            "TALKOPS_SLACK_BOT_TOKEN",
            "TALKOPS_CHANNELS_SLACK_BOT_TOKEN",
        ),
    )
    if not raw:
        return None
    cleaned = raw.strip().strip("'\"")
    return cleaned or None


def _resolve_discord_bot_token() -> str | None:
    from talkops.config.settings import resolve_env_var

    raw = resolve_env_var(
        "DISCORD_BOT_TOKEN",
        fallback_names=(
            "TALKOPS_DISCORD_BOT_TOKEN",
            "TALKOPS_CHANNELS_DISCORD_BOT_TOKEN",
        ),
    )
    if not raw:
        return None
    cleaned = raw.strip().strip("'\"")
    return cleaned or None


def _split_channel_and_thread(conversation_id: str) -> tuple[str, str | None]:
    parts = [p.strip() for p in conversation_id.split(":") if p.strip()]
    if not parts:
        return conversation_id.strip(), None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], parts[1]


async def dispatch_outbound_channel_received(
    event: NormalizedWebhookEvent,
    *,
    lg_thread_id: str,
    routes: list[dict[str, Any]],
    http_client: httpx.AsyncClient | None = None,
) -> dict[tuple[str, str], dict[str, str]]:
    """Post initial one-way incident thread parent cards (`Investigating...`) for `outbound_channels`."""
    thread_refs: dict[tuple[str, str], dict[str, str]] = {}
    if not routes:
        return thread_refs

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=15.0)
    try:
        for route in routes:
            ch = str(route.get("channel") or "").strip().lower()
            conv_id = str(route.get("conversation_id") or "").strip()
            if not ch or not conv_id:
                continue
            try:
                if ch == "slack":
                    ref = await _post_slack_channel_received(
                        client,
                        conversation_id=conv_id,
                        event=event,
                        lg_thread_id=lg_thread_id,
                    )
                    if ref:
                        thread_refs[(ch, conv_id)] = ref
                elif ch == "discord":
                    ref = await _post_discord_channel_received(
                        client,
                        conversation_id=conv_id,
                        event=event,
                        lg_thread_id=lg_thread_id,
                    )
                    if ref:
                        thread_refs[(ch, conv_id)] = ref
                elif ch == "teams":
                    ref = await _post_teams_channel_received(
                        client,
                        conversation_id=conv_id,
                        event=event,
                        lg_thread_id=lg_thread_id,
                    )
                    if ref:
                        thread_refs[(ch, conv_id)] = ref
            except Exception as exc:
                logger.warning(
                    "Failed to post initial incident card to %s:%s for incident %s: %s",
                    ch,
                    conv_id,
                    event.incident_id,
                    exc,
                )
    finally:
        if owns_client:
            await client.aclose()
    return thread_refs


async def dispatch_outbound_channel_completed(
    event: NormalizedWebhookEvent,
    result: WebhookRunResult,
    *,
    analysis: str,
    routes: list[dict[str, Any]],
    thread_refs: dict[tuple[str, str], dict[str, str]] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, bool]:
    """Post completed one-way RCA thread replies and update parent incident cards for `outbound_channels`."""
    outcomes: dict[str, bool] = {}
    if not routes:
        return outcomes

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=15.0)
    try:
        for route in routes:
            ch = str(route.get("channel") or "").strip().lower()
            conv_id = str(route.get("conversation_id") or "").strip()
            if not ch or not conv_id:
                continue
            key = f"channel:{ch}:{conv_id}"
            root_ref = (thread_refs or {}).get((ch, conv_id))
            try:
                if ch == "slack":
                    outcomes[key] = await _post_slack_channel_completed(
                        client,
                        conversation_id=conv_id,
                        event=event,
                        result=result,
                        analysis=analysis,
                        root_ref=root_ref,
                    )
                elif ch == "discord":
                    outcomes[key] = await _post_discord_channel_completed(
                        client,
                        conversation_id=conv_id,
                        event=event,
                        result=result,
                        analysis=analysis,
                        root_ref=root_ref,
                    )
                elif ch == "teams":
                    outcomes[key] = await _post_teams_channel_completed(
                        client,
                        conversation_id=conv_id,
                        event=event,
                        result=result,
                        analysis=analysis,
                        root_ref=root_ref,
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to post completed RCA to %s:%s for incident %s: %s",
                    ch,
                    conv_id,
                    event.incident_id,
                    exc,
                )
                outcomes[key] = False
    finally:
        if owns_client:
            await client.aclose()
    return outcomes


async def _post_slack_channel_received(
    client: httpx.AsyncClient,
    *,
    conversation_id: str,
    event: NormalizedWebhookEvent,
    lg_thread_id: str,
) -> dict[str, str] | None:
    token = _resolve_slack_bot_token()
    if not token:
        logger.warning(
            "Cannot post incident card to slack:%s — SLACK_BOT_TOKEN is not configured",
            conversation_id,
        )
        return None

    channel_id, existing_thread_ts = _split_channel_and_thread(conversation_id)
    inc_out = parse_incident_output(
        "slack",
        title=event.title or f"Incident {event.incident_id}",
        summary=event.summary,
        provider=event.provider,
        severity=event.severity.value,
        service_name=event.service_name,
        source_url=event.source_url,
        lg_thread_id=lg_thread_id,
        investigation_state="investigating",
    )
    payload: dict[str, Any] = {
        "channel": channel_id,
        "text": inc_out.text[:_SLACK_MAX_CHARS],
        "blocks": inc_out.blocks_or_cards,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if existing_thread_ts:
        payload["thread_ts"] = existing_thread_ts

    resp = await client.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json=payload,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        logger.warning(
            "Slack chat.postMessage (incident card) returned error for %s: %s",
            channel_id,
            data.get("error"),
        )
        return None

    resolved_channel = str(data.get("channel") or channel_id)
    msg_ts = str(data.get("ts") or "")
    thread_ts = existing_thread_ts or msg_ts
    logger.info(
        "Posted incident card to Slack channel %s (ts=%s) for incident %s",
        resolved_channel,
        msg_ts,
        event.incident_id,
    )
    return {
        "channel_id": resolved_channel,
        "message_ts": msg_ts,
        "thread_ts": thread_ts,
    }


async def _post_slack_channel_completed(
    client: httpx.AsyncClient,
    *,
    conversation_id: str,
    event: NormalizedWebhookEvent,
    result: WebhookRunResult,
    analysis: str,
    root_ref: dict[str, str] | None = None,
) -> bool:
    token = _resolve_slack_bot_token()
    if not token:
        logger.warning(
            "Cannot post RCA to slack:%s — SLACK_BOT_TOKEN is not configured",
            conversation_id,
        )
        return False

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    inv_state: Literal["completed", "error"] = (
        "error" if result.status.lower() in {"error", "failed"} else "completed"
    )
    channel_id, existing_thread_ts = _split_channel_and_thread(conversation_id)
    resolved_channel = (root_ref or {}).get("channel_id") or channel_id
    parent_msg_ts = (root_ref or {}).get("message_ts") or ""
    thread_ts = (root_ref or {}).get("thread_ts") or existing_thread_ts or ""
    created_fresh_parent = False

    # 1. If no parent incident card was posted on `received`, create the parent incident card first
    if not thread_ts:
        completed_inc = parse_incident_output(
            "slack",
            title=event.title or f"Incident {event.incident_id}",
            summary=event.summary,
            provider=event.provider,
            severity=event.severity.value,
            service_name=event.service_name,
            source_url=event.source_url,
            lg_thread_id=result.thread_id,
            investigation_state=inv_state,
            rca_preview=analysis,
        )
        parent_resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers=headers,
            json={
                "channel": resolved_channel,
                "text": completed_inc.text[:_SLACK_MAX_CHARS],
                "blocks": completed_inc.blocks_or_cards,
                "unfurl_links": False,
                "unfurl_media": False,
            },
        )
        parent_resp.raise_for_status()
        parent_data = parent_resp.json()
        if parent_data.get("ok"):
            resolved_channel = str(parent_data.get("channel") or resolved_channel)
            parent_msg_ts = str(parent_data.get("ts") or "")
            thread_ts = parent_msg_ts
            created_fresh_parent = True

    # 2. Post the structured RCA report inside the Slack thread
    rca_out = parse_rca_output(
        "slack",
        analysis,
        provider=event.provider,
        title=event.title,
        severity=event.severity.value,
        status=inv_state,
        source_url=event.source_url,
        lg_thread_id=result.thread_id,
    )
    rca_payload: dict[str, Any] = {
        "channel": resolved_channel,
        "text": rca_out.text[:_SLACK_MAX_CHARS],
        "blocks": rca_out.blocks_or_cards,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if thread_ts:
        rca_payload["thread_ts"] = thread_ts

    rca_resp = await client.post(
        "https://slack.com/api/chat.postMessage",
        headers=headers,
        json=rca_payload,
    )
    rca_resp.raise_for_status()
    rca_data = rca_resp.json()
    if not rca_data.get("ok") and rca_out.blocks_or_cards:
        fallback_blocks = build_slack_rca_blocks(
            analysis,
            provider=event.provider,
            title=event.title,
            severity=event.severity.value,
            status=inv_state,
            source_url=event.source_url or "",
            lg_thread_id=result.thread_id,
            use_native_table=False,
        )
        rca_payload["blocks"] = fallback_blocks
        rca_resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers=headers,
            json=rca_payload,
        )
        rca_resp.raise_for_status()
        rca_data = rca_resp.json()

    if not rca_data.get("ok"):
        logger.warning(
            "Slack chat.postMessage (RCA reply) returned error for %s: %s",
            resolved_channel,
            rca_data.get("error"),
        )
        return False

    # 3. Update the parent incident card in-place (`Investigating...` -> `RCA Complete` + Root Cause Takeaway)
    if not created_fresh_parent and parent_msg_ts:
        completed_inc = parse_incident_output(
            "slack",
            title=event.title or f"Incident {event.incident_id}",
            summary=event.summary,
            provider=event.provider,
            severity=event.severity.value,
            service_name=event.service_name,
            source_url=event.source_url,
            lg_thread_id=result.thread_id,
            investigation_state=inv_state,
            rca_preview=analysis,
        )
        try:
            await client.post(
                "https://slack.com/api/chat.update",
                headers=headers,
                json={
                    "channel": resolved_channel,
                    "ts": parent_msg_ts,
                    "text": completed_inc.text[:_SLACK_MAX_CHARS],
                    "blocks": completed_inc.blocks_or_cards,
                },
            )
        except Exception as exc:
            logger.debug("Slack chat.update on incident root card failed: %s", exc)

    logger.info(
        "Posted TalkOps RCA thread reply to Slack channel %s (thread_ts=%s) for incident %s",
        resolved_channel,
        thread_ts,
        event.incident_id,
    )
    return True


async def _post_discord_channel_received(
    client: httpx.AsyncClient,
    *,
    conversation_id: str,
    event: NormalizedWebhookEvent,
    lg_thread_id: str,
) -> dict[str, str] | None:
    token = _resolve_discord_bot_token()
    if not token:
        logger.warning(
            "Cannot post incident card to discord:%s — DISCORD_BOT_TOKEN is not configured",
            conversation_id,
        )
        return None

    channel_id, existing_thread_id = _split_channel_and_thread(conversation_id)
    target_ch = existing_thread_id or channel_id
    inc_out = parse_incident_output(
        "discord",
        title=event.title or f"Incident {event.incident_id}",
        summary=event.summary,
        provider=event.provider,
        severity=event.severity.value,
        service_name=event.service_name,
        source_url=event.source_url,
        lg_thread_id=lg_thread_id,
        investigation_state="investigating",
    )
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    body: dict[str, Any] = {"content": inc_out.text[:1900]}
    if isinstance(inc_out.blocks_or_cards, dict):
        if inc_out.blocks_or_cards.get("embeds"):
            body["embeds"] = inc_out.blocks_or_cards["embeds"]
        if inc_out.blocks_or_cards.get("components"):
            body["components"] = inc_out.blocks_or_cards["components"]

    resp = await client.post(
        f"https://discord.com/api/v10/channels/{target_ch}/messages",
        headers=headers,
        json=body,
    )
    resp.raise_for_status()
    data = resp.json()
    msg_id = str(data.get("id") or "")
    thread_id = existing_thread_id or ""
    if msg_id and not existing_thread_id:
        try:
            th_resp = await client.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}/threads",
                headers=headers,
                json={"name": (event.title or f"Incident {event.incident_id}")[:95]},
            )
            if th_resp.status_code < 300:
                thread_id = str(th_resp.json().get("id") or "")
        except Exception:
            pass
    return {
        "channel_id": channel_id,
        "message_id": msg_id,
        "thread_id": thread_id or channel_id,
    }


async def _post_discord_channel_completed(
    client: httpx.AsyncClient,
    *,
    conversation_id: str,
    event: NormalizedWebhookEvent,
    result: WebhookRunResult,
    analysis: str,
    root_ref: dict[str, str] | None = None,
) -> bool:
    token = _resolve_discord_bot_token()
    if not token:
        return False

    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    inv_state: Literal["completed", "error"] = (
        "error" if result.status.lower() in {"error", "failed"} else "completed"
    )
    channel_id, existing_thread_id = _split_channel_and_thread(conversation_id)
    target_thread = (root_ref or {}).get("thread_id") or existing_thread_id or channel_id
    parent_msg_id = (root_ref or {}).get("message_id") or ""

    rca_out = parse_rca_output(
        "discord",
        analysis,
        provider=event.provider,
        title=event.title,
        severity=event.severity.value,
        status=inv_state,
        source_url=event.source_url,
        lg_thread_id=result.thread_id,
    )
    body: dict[str, Any] = {"content": rca_out.text[:1900]}
    if isinstance(rca_out.blocks_or_cards, dict) and rca_out.blocks_or_cards.get("embeds"):
        body["embeds"] = rca_out.blocks_or_cards["embeds"]

    resp = await client.post(
        f"https://discord.com/api/v10/channels/{target_thread}/messages",
        headers=headers,
        json=body,
    )
    resp.raise_for_status()

    if parent_msg_id:
        completed_inc = parse_incident_output(
            "discord",
            title=event.title or f"Incident {event.incident_id}",
            summary=event.summary,
            provider=event.provider,
            severity=event.severity.value,
            service_name=event.service_name,
            source_url=event.source_url,
            lg_thread_id=result.thread_id,
            investigation_state=inv_state,
            rca_preview=analysis,
        )
        patch_body: dict[str, Any] = {"content": completed_inc.text[:1900]}
        if isinstance(completed_inc.blocks_or_cards, dict):
            if completed_inc.blocks_or_cards.get("embeds"):
                patch_body["embeds"] = completed_inc.blocks_or_cards["embeds"]
            if completed_inc.blocks_or_cards.get("components"):
                patch_body["components"] = completed_inc.blocks_or_cards["components"]
        try:
            await client.patch(
                f"https://discord.com/api/v10/channels/{channel_id}/messages/{parent_msg_id}",
                headers=headers,
                json=patch_body,
            )
        except Exception:
            pass
    return True


async def _get_teams_bot_token(client: httpx.AsyncClient) -> str | None:
    from talkops.config.settings import resolve_env_var

    app_id = resolve_env_var(
        "TEAMS_APP_ID",
        fallback_names=("TALKOPS_TEAMS_APP_ID", "TALKOPS_CHANNELS_TEAMS_APP_ID"),
    )
    app_password = resolve_env_var(
        "TEAMS_APP_PASSWORD",
        fallback_names=("TALKOPS_TEAMS_APP_PASSWORD", "TALKOPS_CHANNELS_TEAMS_APP_PASSWORD"),
    )
    if not app_id or not app_password:
        return None
    tenant_id = (
        resolve_env_var(
            "TEAMS_TENANT_ID",
            fallback_names=("TALKOPS_TEAMS_TENANT_ID", "TALKOPS_CHANNELS_TEAMS_TENANT_ID"),
        )
        or "botframework.com"
    )
    token_url = f"https://login.microsoftonline.com/{tenant_id.strip()}/oauth2/v2.0/token"
    resp = await client.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": app_id.strip().strip("'\""),
            "client_secret": app_password.strip().strip("'\""),
            "scope": "https://api.botframework.com/.default",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    return str(data.get("access_token") or "") or None


async def _post_teams_channel_received(
    client: httpx.AsyncClient,
    *,
    conversation_id: str,
    event: NormalizedWebhookEvent,
    lg_thread_id: str,
) -> dict[str, str] | None:
    from talkops.config.settings import resolve_env_var

    inc_out = parse_incident_output(
        "teams",
        title=event.title or f"Incident {event.incident_id}",
        summary=event.summary,
        provider=event.provider,
        severity=event.severity.value,
        service_name=event.service_name,
        source_url=event.source_url,
        lg_thread_id=lg_thread_id,
        investigation_state="investigating",
    )

    # Mode 1: Teams Incoming Webhook URL
    webhook_url = (
        conversation_id
        if conversation_id.startswith(("http://", "https://"))
        else resolve_env_var("TEAMS_WEBHOOK_URL", fallback_names=("TALKOPS_TEAMS_WEBHOOK_URL",))
    )
    if webhook_url and conversation_id.startswith(("http://", "https://")):
        resp = await client.post(
            webhook_url,
            json={"type": "message", "text": inc_out.text, "attachments": inc_out.blocks_or_cards or []},
        )
        resp.raise_for_status()
        return {"webhook_url": webhook_url}

    # Mode 2: Teams Bot Framework Connector API (`19:...` conversation ID)
    service_url = (
        resolve_env_var(
            "TEAMS_SERVICE_URL",
            fallback_names=("TALKOPS_TEAMS_SERVICE_URL", "TALKOPS_CHANNELS_TEAMS_SERVICE_URL"),
        )
        or "https://smba.trafficmanager.net/teams"
    ).rstrip("/")
    token = await _get_bot_token_or_none(client)
    if not token and webhook_url:
        resp = await client.post(
            webhook_url,
            json={"type": "message", "text": inc_out.text, "attachments": inc_out.blocks_or_cards or []},
        )
        resp.raise_for_status()
        return {"webhook_url": webhook_url}

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = await client.post(
        f"{service_url}/v3/conversations/{conversation_id}/activities",
        headers=headers,
        json={
            "type": "message",
            "textFormat": "markdown",
            "text": inc_out.text,
            "attachments": inc_out.blocks_or_cards or [],
        },
    )
    resp.raise_for_status()
    data = resp.json() if resp.content else {}
    msg_id = str(data.get("id") or "")
    base_conv = conversation_id.split(";messageid=")[0]
    thread_conv = f"{base_conv};messageid={msg_id}" if msg_id else conversation_id
    return {
        "service_url": service_url,
        "conversation_id": conversation_id,
        "message_id": msg_id,
        "thread_conv_id": thread_conv,
    }


async def _get_bot_token_or_none(client: httpx.AsyncClient) -> str | None:
    try:
        return await _get_teams_bot_token(client)
    except Exception:
        return None


async def _post_teams_channel_completed(
    client: httpx.AsyncClient,
    *,
    conversation_id: str,
    event: NormalizedWebhookEvent,
    result: WebhookRunResult,
    analysis: str,
    root_ref: dict[str, str] | None = None,
) -> bool:
    from talkops.config.settings import resolve_env_var

    inv_state: Literal["completed", "error"] = (
        "error" if result.status.lower() in {"error", "failed"} else "completed"
    )
    rca_out = parse_rca_output(
        "teams",
        analysis,
        provider=event.provider,
        title=event.title,
        severity=event.severity.value,
        status=inv_state,
        source_url=event.source_url,
        lg_thread_id=result.thread_id,
    )

    webhook_url = (
        (root_ref or {}).get("webhook_url")
        or (conversation_id if conversation_id.startswith(("http://", "https://")) else None)
        or resolve_env_var("TEAMS_WEBHOOK_URL", fallback_names=("TALKOPS_TEAMS_WEBHOOK_URL",))
    )
    if webhook_url and (
        (root_ref or {}).get("webhook_url") or conversation_id.startswith(("http://", "https://"))
    ):
        resp = await client.post(
            webhook_url,
            json={"type": "message", "text": rca_out.text, "attachments": rca_out.blocks_or_cards or []},
        )
        resp.raise_for_status()
        return True

    service_url = (
        (root_ref or {}).get("service_url")
        or resolve_env_var(
            "TEAMS_SERVICE_URL",
            fallback_names=("TALKOPS_TEAMS_SERVICE_URL", "TALKOPS_CHANNELS_TEAMS_SERVICE_URL"),
        )
        or "https://smba.trafficmanager.net/teams"
    ).rstrip("/")
    token = await _get_bot_token_or_none(client)
    if not token and webhook_url:
        resp = await client.post(
            webhook_url,
            json={"type": "message", "text": rca_out.text, "attachments": rca_out.blocks_or_cards or []},
        )
        resp.raise_for_status()
        return True

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    parent_msg_id = (root_ref or {}).get("message_id") or ""
    thread_conv = (root_ref or {}).get("thread_conv_id") or conversation_id
    activity_body: dict[str, Any] = {
        "type": "message",
        "textFormat": "markdown",
        "text": rca_out.text,
        "attachments": rca_out.blocks_or_cards or [],
    }
    if parent_msg_id:
        activity_body["replyToId"] = parent_msg_id
        post_url = f"{service_url}/v3/conversations/{thread_conv}/activities/{parent_msg_id}"
    else:
        post_url = f"{service_url}/v3/conversations/{conversation_id}/activities"

    resp = await client.post(post_url, headers=headers, json=activity_body)
    resp.raise_for_status()

    if parent_msg_id:
        completed_inc = parse_incident_output(
            "teams",
            title=event.title or f"Incident {event.incident_id}",
            summary=event.summary,
            provider=event.provider,
            severity=event.severity.value,
            service_name=event.service_name,
            source_url=event.source_url,
            lg_thread_id=result.thread_id,
            investigation_state=inv_state,
            rca_preview=analysis,
        )
        try:
            await client.put(
                f"{service_url}/v3/conversations/{conversation_id}/activities/{parent_msg_id}",
                headers=headers,
                json={
                    "id": parent_msg_id,
                    "type": "message",
                    "textFormat": "markdown",
                    "text": completed_inc.text,
                    "attachments": completed_inc.blocks_or_cards or [],
                },
            )
        except Exception:
            pass
    return True

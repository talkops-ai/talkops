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

"""Built-in `ChannelsAddon` (`channels` / `ChatOpsAddon`) for TalkOps Server Mode.

Mounts central HTTP webhook routes for Slack, Microsoft Teams, and Discord by default
on `talkops serve`:
- `GET  /channels/status` & `GET /channels/health`
- `POST /channels/notify`
- `POST /channels/{provider}/events`
- `POST /channels/{provider}/interactions`
- `POST /channels/{provider}/commands`
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from talkops.addons.config import AddonSettings
from talkops.addons.protocol import (
    AddonCapabilities,
    AddonHealthStatus,
    AddonTier,
    BaseServerAddon,
)
from talkops.channels.commands import CHAT_COMMANDS
from talkops.channels.formatting import (
    parse_incident_output,
    parse_rca_output,
)
from talkops.channels.hub import ChannelHub
from talkops.channels.interfaces import ChannelMedia, WebhookHTTPResponse
from talkops.channels.pairing import PairingStore
from talkops.channels.registry import ChannelAdapterRegistry
from talkops.channels.state_store import (
    ChannelStateStore,
    PersistentChannelStateStore,
)


class ChannelsAddonSettings(AddonSettings):
    """Configuration schema for built-in `ChannelsAddon` (`channels`)."""

    enabled: bool = True
    verbosity: int = Field(default=2, ge=1, le=3)
    pairing_enabled: bool = True
    default_approval_mode: str = "smart"
    multitask_strategy: Literal["interrupt", "enqueue"] = "interrupt"
    edit_debounce_seconds: float = 1.0
    slack: dict[str, Any] = Field(default_factory=dict)
    teams: dict[str, Any] = Field(default_factory=dict)
    discord: dict[str, Any] = Field(default_factory=dict)
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    outbound_channels: list[dict[str, Any]] | str | None = None


def _to_starlette_response(wh_resp: WebhookHTTPResponse) -> Response:
    if isinstance(wh_resp.body, dict):
        return JSONResponse(
            wh_resp.body,
            status_code=wh_resp.status_code,
            headers=dict(wh_resp.headers),
        )
    if isinstance(wh_resp.body, bytes):
        return Response(
            content=wh_resp.body,
            status_code=wh_resp.status_code,
            media_type=wh_resp.content_type,
            headers=dict(wh_resp.headers),
        )
    return Response(
        content=wh_resp.body,
        status_code=wh_resp.status_code,
        media_type=wh_resp.content_type,
        headers=dict(wh_resp.headers),
    )


class ChannelsAddon(BaseServerAddon):
    """Built-in Open-Source multi-channel HTTP webhook addon (`channels`) for Slack, Teams, and Discord."""

    name: str = "channels"
    tier: AddonTier = "oss"
    version: str = "1.0.0"
    api_version: int = 1
    description: str = (
        "Central multi-channel HTTP webhook integration hub for Slack, Microsoft Teams, and Discord"
    )
    dependencies: tuple[str, ...] = ()
    capabilities = AddonCapabilities(
        routes=True,
        background_workers=True,
        event_sinks=True,
        event_subscriber=True,
        gateway_runner=True,
    )
    settings_schema: type[BaseModel] | None = ChannelsAddonSettings
    enabled_by_default: bool = True

    def __init__(
        self,
        *,
        hub: ChannelHub | None = None,
        environ: Mapping[str, str] | None = None,
        state_store: ChannelStateStore | None = None,
        adapter_registry: ChannelAdapterRegistry | None = None,
    ) -> None:
        """Initialize `ChannelsAddon` with optional pre-configured `ChannelHub`, `state_store`, and registry."""
        super().__init__()
        self._hub = hub
        self._environ = environ
        self._state_store: ChannelStateStore = (
            state_store
            if state_store is not None
            else (
                hub.state_store
                if hub is not None and hub.state_store is not None
                else PersistentChannelStateStore(backend="auto")
            )
        )
        self._adapter_registry = adapter_registry or ChannelAdapterRegistry()
        self._event_unsubscribers: list[Any] = []
        self._webhook_incident_threads: dict[tuple[str, str, str], str] = {}
        self._webhook_incident_root_messages: dict[tuple[str, str, str], dict[str, Any]] = {}

    @property
    def hub(self) -> ChannelHub:
        """Return or lazily construct the `ChannelHub` orchestrator."""
        if self._hub is None:
            self._hub = self._build_hub()
        return self._hub

    def _build_hub(self, ctx: Any = None) -> ChannelHub:
        env = self._environ if self._environ is not None else os.environ
        cfg = (
            self._config
            if isinstance(self._config, ChannelsAddonSettings)
            else ChannelsAddonSettings()
        )

        pairing_path = Path(
            env.get(
                "TALKOPS_CHANNELS_PAIRING_FILE",
                str(Path.home() / ".talkops" / "channel_pairing.json"),
            )
        )
        gateway = getattr(ctx, "gateway", None) if ctx is not None else None
        identity_mapper = getattr(ctx, "identity_mapper", None) if ctx is not None else None
        event_bus = getattr(ctx, "event_bus", None) if ctx is not None else None
        project_root = getattr(ctx, "project_root", None) if ctx is not None else None

        hub = ChannelHub(
            gateway=gateway,
            identity_mapper=identity_mapper,
            pairing_store=PairingStore(pairing_path),
            state_store=self._state_store,
            event_bus=event_bus,
            project_root=project_root,
            pairing_enabled=cfg.pairing_enabled,
            verbosity=cfg.verbosity,
            edit_debounce_seconds=cfg.edit_debounce_seconds,
            default_approval_mode=cfg.default_approval_mode,
            multitask_strategy=cfg.multitask_strategy,
        )

        provider_configs: dict[str, Mapping[str, Any]] = {
            **{k.strip().lower(): v for k, v in cfg.providers.items() if isinstance(v, Mapping)},
            "slack": cfg.slack,
            "teams": cfg.teams,
            "discord": cfg.discord,
        }
        for adapter in self._adapter_registry.build_all(
            provider_configs,
            env,
            state_store=self._state_store,
        ):
            hub.register_adapter(adapter)
        return hub

    def _resolve_matching_outbound_routes(
        self,
        payload: Mapping[str, Any],
        *,
        stage: str,
    ) -> list[dict[str, Any]]:
        """Resolve matching outbound channel targets from `webhooks` payload or local `channels` config."""
        routes: list[dict[str, Any]] = []
        payload_routes = payload.get("outbound_channels")
        if isinstance(payload_routes, list) and payload_routes:
            for item in payload_routes:
                if isinstance(item, Mapping):
                    routes.append(dict(item))
            return routes

        env = self._environ if self._environ is not None else os.environ
        cfg = self._config if isinstance(self._config, ChannelsAddonSettings) else None
        raw_local = (
            (cfg.outbound_channels or cfg.config.get("outbound_channels"))
            if cfg is not None
            else None
        ) or env.get("TALKOPS_WEBHOOK_NOTIFY_CHANNELS")
        if not raw_local:
            return routes

        provider = str(payload.get("provider") or "").strip().lower()
        severity = str(payload.get("severity") or "unknown").strip().lower()

        parsed_list: list[dict[str, Any]] = []
        if isinstance(raw_local, str):
            for tok in raw_local.split(","):
                tok = tok.strip()
                if ":" in tok:
                    ch, cid = tok.split(":", 1)
                    parsed_list.append(
                        {
                            "channel": ch.strip().lower(),
                            "conversation_id": cid.strip(),
                            "providers": ["*"],
                            "severities": ["*"],
                            "notify_on": ["received", "completed"],
                            "bind_thread": True,
                        }
                    )
        elif isinstance(raw_local, list):
            for entry in raw_local:
                if isinstance(entry, Mapping):
                    parsed_list.append(dict(entry))

        for r in parsed_list:
            notify_on = [str(s).lower() for s in r.get("notify_on", ["completed"])]
            if "*" not in notify_on and stage.lower() not in notify_on:
                continue
            provs = [str(p).lower() for p in r.get("providers", ["*"])]
            if "*" not in provs and provider not in provs:
                continue
            sevs = [str(s).lower() for s in r.get("severities", ["*"])]
            if "*" not in sevs and severity not in sevs:
                continue
            routes.append(r)
        return routes

    async def _on_webhook_received(self, event: Any) -> None:
        """Handle `webhook.received` from `HookEventBus` and create initial incident threads."""
        payload = getattr(event, "payload", None)
        if not isinstance(payload, Mapping):
            return
        routes = self._resolve_matching_outbound_routes(payload, stage="received")
        if not routes:
            return

        lg_thread_id = str(payload.get("thread_id") or getattr(event, "thread_id", "") or "").strip()
        provider_raw = str(payload.get("provider") or "webhook").strip()
        provider = provider_raw.upper()
        title = str(payload.get("title") or payload.get("incident_id") or "Alert")
        severity_raw = str(payload.get("severity") or "unknown").strip()
        severity = severity_raw.upper()
        service_name = str(payload.get("service_name") or "").strip()
        summary = str(payload.get("summary") or "").strip()
        source_url = str(payload.get("source_url") or "").strip()

        header_title = f"[{provider}] {title} ({severity})"
        body_lines = ["⏳ *TalkOps Autonomous Investigation Started*"]
        if summary:
            body_lines.append(summary[:1200])
        if source_url:
            body_lines.append(f"Source: {source_url}")
        if lg_thread_id:
            body_lines.append(f"Thread ID: `{lg_thread_id}`")

        incident_meta: dict[str, Any] = {
            "provider": provider_raw,
            "severity": severity_raw,
            "service_name": service_name,
            "summary": summary[:1200],
            "source_url": source_url,
            "lg_thread_id": lg_thread_id,
            "investigation_state": "investigating",
        }

        for route in routes:
            ch = str(route.get("channel") or "").strip().lower()
            conv_id = str(route.get("conversation_id") or "").strip()
            bind_thread = bool(route.get("bind_thread", True))
            if not ch or not conv_id or not lg_thread_id or self.hub.get_adapter(ch) is None:
                continue
            try:
                parsed_incident = parse_incident_output(
                    ch,
                    title=title,
                    summary=summary[:1200],
                    provider=provider_raw,
                    severity=severity_raw,
                    service_name=service_name,
                    source_url=source_url,
                    lg_thread_id=lg_thread_id,
                    investigation_state="investigating",
                )
                if bind_thread:
                    res = await self.hub.create_incident_thread(
                        channel=ch,
                        conversation_id=conv_id,
                        title=header_title,
                        summary_text="\n".join(body_lines),
                        lg_thread_id=lg_thread_id,
                        blocks_or_cards=parsed_incident.blocks_or_cards,
                        metadata=incident_meta,
                    )
                    thread_conv = self.hub.resolve_thread_conversation_id(ch, conv_id, res)
                    self._webhook_incident_threads[(lg_thread_id, ch, conv_id)] = thread_conv
                    root_record: dict[str, Any] | None = None
                    if res.message_id:
                        root_record = {
                            "message_id": res.message_id,
                            "thread_id": res.thread_id or res.message_id,
                            "thread_conv": thread_conv,
                            "title": title,
                            "header_title": header_title,
                            "summary": summary[:1200],
                            "provider": provider_raw,
                            "severity": severity_raw,
                            "service_name": service_name,
                            "source_url": source_url,
                            "lg_thread_id": lg_thread_id,
                            "started_at": time.time(),
                        }
                        self._webhook_incident_root_messages[(lg_thread_id, ch, conv_id)] = root_record
                    self._state_store.put_incident_ref(
                        lg_thread_id,
                        ch,
                        conv_id,
                        root_record or {"thread_conv": thread_conv, "lg_thread_id": lg_thread_id},
                    )
                else:
                    await self.hub.send_proactive_notification(
                        channel=ch,
                        conversation_id=conv_id,
                        text=parsed_incident.text,
                        blocks_or_cards=parsed_incident.blocks_or_cards,
                        metadata=incident_meta,
                    )
            except Exception:
                pass

    async def _on_webhook_completed(self, event: Any) -> None:
        """Handle `webhook.completed` from `HookEventBus` and deliver RCA report to bound channel threads."""
        payload = getattr(event, "payload", None)
        if not isinstance(payload, Mapping):
            return
        routes = self._resolve_matching_outbound_routes(payload, stage="completed")
        if not routes:
            return

        lg_thread_id = str(payload.get("thread_id") or getattr(event, "thread_id", "") or "").strip()
        provider_raw = str(payload.get("provider") or "webhook").strip()
        provider = provider_raw.upper()
        title = str(payload.get("title") or payload.get("incident_id") or "Alert")
        severity_raw = str(payload.get("severity") or "unknown").strip()
        severity = severity_raw.upper()
        service_name = str(payload.get("service_name") or "").strip()
        summary = str(payload.get("summary") or "").strip()
        status = str(payload.get("status") or "completed")
        analysis = str(payload.get("analysis") or "").strip()
        error = str(payload.get("error") or "").strip()
        source_url = str(payload.get("source_url") or "").strip()
        raw_duration = payload.get("duration_seconds")
        payload_duration: float | None = (
            float(raw_duration) if isinstance(raw_duration, (int, float)) and raw_duration >= 0 else None
        )

        if not analysis and error:
            analysis = f"Investigation failed: {error}"
        elif not analysis:
            analysis = f"Investigation finished with status `{status}`."

        header_title = f"[{provider}] {title} ({severity})"
        summary_lines = [analysis]
        if source_url:
            summary_lines.append(f"\nSource: {source_url}")
        if lg_thread_id:
            summary_lines.append(
                f"_Reply in this thread to continue investigating (`{lg_thread_id}`)._"
            )
        full_summary = "\n".join(summary_lines)
        is_err = status.lower() in {"error", "failed"} or bool(error and not payload.get("analysis"))
        inv_state: Literal["investigating", "completed", "error"] = (
            "error" if is_err else "completed"
        )

        for route in routes:
            ch = str(route.get("channel") or "").strip().lower()
            conv_id = str(route.get("conversation_id") or "").strip()
            bind_thread = bool(route.get("bind_thread", True))
            if not ch or not conv_id or self.hub.get_adapter(ch) is None:
                continue
            existing_thread = (
                self._webhook_incident_threads.get((lg_thread_id, ch, conv_id))
                if lg_thread_id
                else None
            )
            persisted_ref = (
                self._state_store.get_incident_ref(lg_thread_id, ch, conv_id)
                if lg_thread_id and not existing_thread
                else None
            )
            if not existing_thread and persisted_ref and persisted_ref.get("thread_conv"):
                existing_thread = str(persisted_ref["thread_conv"])
                self._webhook_incident_threads[(lg_thread_id, ch, conv_id)] = existing_thread
                if persisted_ref.get("message_id"):
                    self._webhook_incident_root_messages[(lg_thread_id, ch, conv_id)] = persisted_ref
            try:
                root_info = (
                    self._webhook_incident_root_messages.get((lg_thread_id, ch, conv_id))
                    if lg_thread_id
                    else None
                )
                eff_duration = payload_duration
                if eff_duration is None and root_info and isinstance(root_info.get("started_at"), (int, float)):
                    eff_duration = max(0.0, time.time() - float(root_info["started_at"]))

                parsed_rca = parse_rca_output(
                    ch,
                    analysis,
                    provider=provider_raw,
                    title=title,
                    severity=severity_raw,
                    status=inv_state,
                    source_url=source_url,
                    lg_thread_id=lg_thread_id,
                    duration_seconds=eff_duration,
                    include_feedback_actions=(ch in {"slack", "discord", "teams"}),
                )
                rca_media: tuple[ChannelMedia, ...] = ()
                if parsed_rca.attachment_markdown is not None:
                    md_name, md_bytes = parsed_rca.attachment_markdown
                    rca_media = (
                        ChannelMedia(
                            media_type="document",
                            content_bytes=md_bytes,
                            filename=md_name,
                            mime_type="text/markdown",
                            size_bytes=len(md_bytes),
                            metadata={
                                "description": f"Full TalkOps RCA report ({title})",
                                "initial_comment": f"Full RCA report (`{md_name}`)",
                            },
                        ),
                    )
                if existing_thread:
                    root_thread_id = str(root_info.get("thread_id") or "") if root_info else ""
                    rca_sent = await self.hub.send_proactive_notification(
                        channel=ch,
                        conversation_id=existing_thread,
                        text=parsed_rca.text,
                        thread_id=root_thread_id or None,
                        lg_thread_id=lg_thread_id if bind_thread else None,
                        media=rca_media,
                        blocks_or_cards=parsed_rca.blocks_or_cards,
                    )
                    if rca_sent.message_id and lg_thread_id:
                        self.hub.register_rca_message(
                            channel=ch,
                            conversation_id=existing_thread,
                            message_id=rca_sent.message_id,
                            lg_thread_id=lg_thread_id,
                            thread_id=root_thread_id or rca_sent.thread_id,
                            parent_channel_id=conv_id,
                            provider=provider_raw,
                            title=title,
                            severity=severity_raw,
                            service_name=service_name,
                            source_url=source_url,
                            status=inv_state,
                            blocks_or_cards=parsed_rca.blocks_or_cards,
                            text=parsed_rca.text,
                        )
                    for overflow_chunk in parsed_rca.overflow_texts:
                        await self.hub.send_proactive_notification(
                            channel=ch,
                            conversation_id=existing_thread,
                            text=overflow_chunk,
                            thread_id=root_thread_id or None,
                        )
                    if root_info and root_info.get("message_id"):
                        adapter = self.hub.get_adapter(ch)
                        if adapter is not None:
                            updated_root = parse_incident_output(
                                ch,
                                title=str(root_info.get("title") or title),
                                summary=str(root_info.get("summary") or summary),
                                provider=str(root_info.get("provider") or provider_raw),
                                severity=str(root_info.get("severity") or severity_raw),
                                service_name=str(root_info.get("service_name") or service_name),
                                source_url=str(root_info.get("source_url") or source_url),
                                lg_thread_id=lg_thread_id,
                                investigation_state=inv_state,
                                rca_preview=analysis,
                                duration_seconds=eff_duration,
                            )
                            await adapter.update_message(
                                conv_id,
                                str(root_info["message_id"]),
                                updated_root.text,
                                blocks_or_cards=updated_root.blocks_or_cards,
                            )
                elif bind_thread and lg_thread_id:
                    completed_incident = parse_incident_output(
                        ch,
                        title=title,
                        summary=summary[:1200],
                        provider=provider_raw,
                        severity=severity_raw,
                        service_name=service_name,
                        source_url=source_url,
                        lg_thread_id=lg_thread_id,
                        investigation_state=inv_state,
                        rca_preview=analysis,
                        duration_seconds=eff_duration,
                    )
                    res = await self.hub.create_incident_thread(
                        channel=ch,
                        conversation_id=conv_id,
                        title=header_title,
                        summary_text=full_summary,
                        lg_thread_id=lg_thread_id,
                        blocks_or_cards=completed_incident.blocks_or_cards,
                        metadata={
                            "provider": provider_raw,
                            "severity": severity_raw,
                            "service_name": service_name,
                            "summary": summary[:1200],
                            "source_url": source_url,
                            "lg_thread_id": lg_thread_id,
                            "investigation_state": inv_state,
                            "rca_preview": analysis,
                            "duration_seconds": eff_duration,
                        },
                    )
                    thread_conv = self.hub.resolve_thread_conversation_id(ch, conv_id, res)
                    self._webhook_incident_threads[(lg_thread_id, ch, conv_id)] = thread_conv
                    if res.message_id:
                        rca_sent = await self.hub.send_proactive_notification(
                            channel=ch,
                            conversation_id=thread_conv,
                            text=parsed_rca.text,
                            thread_id=res.thread_id or res.message_id,
                            lg_thread_id=lg_thread_id,
                            media=rca_media,
                            blocks_or_cards=parsed_rca.blocks_or_cards,
                        )
                        if rca_sent.message_id:
                            self.hub.register_rca_message(
                                channel=ch,
                                conversation_id=thread_conv,
                                message_id=rca_sent.message_id,
                                lg_thread_id=lg_thread_id,
                                thread_id=res.thread_id or res.message_id,
                                parent_channel_id=conv_id,
                                provider=provider_raw,
                                title=title,
                                severity=severity_raw,
                                service_name=service_name,
                                source_url=source_url,
                                status=inv_state,
                                blocks_or_cards=parsed_rca.blocks_or_cards,
                                text=parsed_rca.text,
                            )
                        for overflow_chunk in parsed_rca.overflow_texts:
                            await self.hub.send_proactive_notification(
                                channel=ch,
                                conversation_id=thread_conv,
                                text=overflow_chunk,
                                thread_id=res.thread_id or res.message_id,
                            )
                else:
                    rca_sent = await self.hub.send_proactive_notification(
                        channel=ch,
                        conversation_id=conv_id,
                        text=parsed_rca.text,
                        media=rca_media,
                        blocks_or_cards=parsed_rca.blocks_or_cards,
                    )
                    if rca_sent.message_id and lg_thread_id:
                        self.hub.register_rca_message(
                            channel=ch,
                            conversation_id=conv_id,
                            message_id=rca_sent.message_id,
                            lg_thread_id=lg_thread_id,
                            parent_channel_id=conv_id,
                            provider=provider_raw,
                            title=title,
                            severity=severity_raw,
                            service_name=service_name,
                            source_url=source_url,
                            status=inv_state,
                            blocks_or_cards=parsed_rca.blocks_or_cards,
                            text=parsed_rca.text,
                        )
                    for overflow_chunk in parsed_rca.overflow_texts:
                        await self.hub.send_proactive_notification(
                            channel=ch,
                            conversation_id=conv_id,
                            text=overflow_chunk,
                        )
            except Exception:
                pass

    async def on_startup(self, ctx: Any) -> None:
        """Start `ChannelHub` adapters and subscribe to `webhooks` lifecycle events."""
        await super().on_startup(ctx)
        if self._hub is None:
            self._hub = self._build_hub(ctx)
        else:
            if getattr(ctx, "gateway", None) is not None and self._hub.gateway is None:
                self._hub.gateway = ctx.gateway
            if getattr(ctx, "identity_mapper", None) is not None and self._hub.identity_mapper is None:
                self._hub.identity_mapper = ctx.identity_mapper
            if getattr(ctx, "event_bus", None) is not None and self._hub.event_bus is None:
                self._hub.event_bus = ctx.event_bus
            if getattr(ctx, "project_root", None) is not None and self._hub.project_root is None:
                self._hub.project_root = ctx.project_root
        await self._hub.start()

        event_bus = getattr(ctx, "event_bus", None)
        if event_bus is not None and hasattr(event_bus, "subscribe"):
            self._event_unsubscribers.append(
                event_bus.subscribe("webhook.received", self._on_webhook_received)
            )
            self._event_unsubscribers.append(
                event_bus.subscribe("webhook.completed", self._on_webhook_completed)
            )
            event_bus._channels_addon_active = True
            event_bus._channels_addon_providers = set(self._hub.adapters.keys())

    async def on_shutdown(self, ctx: Any) -> None:
        """Unsubscribe from `HookEventBus` and stop `ChannelHub` adapters."""
        event_bus = getattr(ctx, "event_bus", None)
        if event_bus is not None:
            event_bus._channels_addon_active = False
            event_bus._channels_addon_providers = set()

        for unsub in self._event_unsubscribers:
            try:
                unsub()
            except Exception:
                pass
        self._event_unsubscribers.clear()
        if self._hub is not None:
            await self._hub.stop()
        await super().on_shutdown(ctx)

    def get_routes(self) -> list[BaseRoute]:
        """Return HTTP routes mounted by `ChannelsAddon`."""

        async def _events_endpoint(request: Request) -> Response:
            provider = str(request.path_params.get("provider") or "").strip().lower()
            adapter = self.hub.get_adapter(provider)
            if adapter is None:
                return JSONResponse(
                    {"ok": False, "error": f"unknown_channel_provider:{provider}"},
                    status_code=404,
                )
            body = await request.body()
            wh_resp = await adapter.handle_webhook_event(
                dict(request.headers),
                body,
                dict(request.query_params),
            )
            return _to_starlette_response(wh_resp)

        async def _interactions_endpoint(request: Request) -> Response:
            provider = str(request.path_params.get("provider") or "").strip().lower()
            adapter = self.hub.get_adapter(provider)
            if adapter is None:
                return JSONResponse(
                    {"ok": False, "error": f"unknown_channel_provider:{provider}"},
                    status_code=404,
                )
            body = await request.body()
            wh_resp = await adapter.handle_webhook_interaction(
                dict(request.headers),
                body,
                dict(request.query_params),
            )
            return _to_starlette_response(wh_resp)

        async def _status_endpoint(_request: Request) -> JSONResponse:
            statuses = [
                {
                    "channel": s.channel,
                    "configured": s.configured,
                    "webhook_events_path": s.webhook_events_path,
                    "webhook_interactions_path": s.webhook_interactions_path,
                    "exposure_mode": s.exposure_mode,
                    "detail": s.detail,
                    "metadata": dict(s.metadata),
                }
                for s in self.hub.status_all()
            ]
            return JSONResponse(
                {
                    "status": "ok",
                    "addon": self.name,
                    "version": self.version,
                    "channels": statuses,
                }
            )

        async def _notify_endpoint(request: Request) -> JSONResponse:
            try:
                payload = await request.json()
            except Exception:
                return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
            if not isinstance(payload, Mapping):
                return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=400)

            channel = str(payload.get("channel") or "").strip().lower()
            conversation_id = str(payload.get("conversation_id") or "").strip()
            text = str(payload.get("text") or "").strip()
            thread_id = str(payload.get("thread_id") or "").strip() or None
            lg_thread_id = str(payload.get("lg_thread_id") or "").strip() or None

            if not channel or not conversation_id or not text:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "channel, conversation_id, and text are required",
                    },
                    status_code=400,
                )
            try:
                sent = await self.hub.send_proactive_notification(
                    channel=channel,
                    conversation_id=conversation_id,
                    text=text,
                    thread_id=thread_id,
                    lg_thread_id=lg_thread_id,
                )
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

            return JSONResponse(
                {
                    "ok": True,
                    "channel": sent.channel,
                    "conversation_id": sent.conversation_id,
                    "message_id": sent.message_id,
                    "thread_id": sent.thread_id,
                }
            )

        async def _sync_commands_endpoint(request: Request) -> JSONResponse:
            provider = str(request.path_params.get("provider") or "").strip().lower()
            adapter = self.hub.get_adapter(provider)
            if adapter is None:
                return JSONResponse(
                    {"ok": False, "error": f"unknown_channel_provider:{provider}"},
                    status_code=404,
                )
            sync_fn: Any = getattr(adapter, "sync_commands", None)
            if not callable(sync_fn):
                return JSONResponse(
                    {"ok": False, "error": f"command_sync_not_supported:{provider}"},
                    status_code=400,
                )
            guild_id: str | None = None
            try:
                body = await request.json()
                if isinstance(body, Mapping) and body.get("guild_id"):
                    guild_id = str(body["guild_id"]).strip() or None
            except Exception:
                pass
            if not guild_id:
                guild_id = str(request.query_params.get("guild_id") or "").strip() or None
            try:
                result = await sync_fn(CHAT_COMMANDS, guild_id=guild_id)
                return JSONResponse({"ok": True, "channel": provider, "result": result})
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

        return [
            Route("/channels/status", _status_endpoint, methods=["GET"]),
            Route("/channels/health", _status_endpoint, methods=["GET"]),
            Route("/channels/notify", _notify_endpoint, methods=["POST"]),
            Route("/channels/{provider}/events", _events_endpoint, methods=["POST"]),
            Route("/channels/{provider}/interactions", _interactions_endpoint, methods=["POST"]),
            Route("/channels/{provider}/commands", _interactions_endpoint, methods=["POST"]),
            Route("/channels/{provider}/sync-commands", _sync_commands_endpoint, methods=["POST"]),
        ]

    async def health_check(self) -> AddonHealthStatus:
        """Return current `AddonHealthStatus` for `ChannelsAddon`."""
        statuses = self.hub.status_all()
        configured = [s.channel for s in statuses if s.configured]
        transports = {
            s.channel: s.metadata.get("transport")
            for s in statuses
            if isinstance(s.metadata, Mapping) and s.metadata.get("transport") is not None
        }
        has_transport_error = any(
            isinstance(t, Mapping) and t.get("state") == "error" for t in transports.values()
        )
        port = getattr(self._config, "port", None) if self._config is not None else None
        return AddonHealthStatus(
            name=self.name,
            status="degraded" if has_transport_error else "ok",
            tier=self.tier,
            version=self.version,
            port=port,
            source=self.source,
            details={
                "configured_channels": configured,
                "available_channels": [s.channel for s in statuses],
                "transports": transports,
                "endpoints": [
                    "/channels/status",
                    "/channels/health",
                    "/channels/notify",
                    "/channels/{provider}/events",
                    "/channels/{provider}/interactions",
                    "/channels/{provider}/commands",
                    "/channels/{provider}/sync-commands",
                ],
            },
        )


ChatOpsAddon = ChannelsAddon
addon = ChannelsAddon

__all__ = [
    "ChannelsAddon",
    "ChannelsAddonSettings",
    "ChatOpsAddon",
    "addon",
]

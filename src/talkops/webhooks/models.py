"""Data models and configuration for TalkOps central webhook ingestion and dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
from typing import TYPE_CHECKING, Any

from talkops.integrations.base import IncomingMessage

if TYPE_CHECKING:
    from talkops.config.settings import Settings


class WebhookSeverity(StrEnum):
    """Normalized severity levels across monitoring, paging, and CI/CD providers."""

    CRITICAL = "critical"
    HIGH = "high"
    WARNING = "warning"
    INFO = "info"
    UNKNOWN = "unknown"

    @classmethod
    def from_raw(cls, value: str | int | None) -> WebhookSeverity:
        """Normalize provider-specific severity or urgency values."""
        if value is None:
            return cls.UNKNOWN
        raw = str(value).strip().lower()
        if raw in {"critical", "crit", "p1", "sev1", "sev-1", "fatal", "emergency", "page", "1"}:
            return cls.CRITICAL
        if raw in {"high", "error", "err", "p2", "sev2", "sev-2", "major", "alarm", "alert", "failure", "failed", "2"}:
            return cls.HIGH
        if raw in {"warning", "warn", "medium", "moderate", "p3", "sev3", "sev-3", "degraded", "unstable", "3"}:
            return cls.WARNING
        if raw in {"info", "informational", "low", "ok", "normal", "p4", "p5", "sev4", "4", "5"}:
            return cls.INFO
        return cls.UNKNOWN


class WebhookEventStatus(StrEnum):
    """Normalized lifecycle state of an incoming webhook event."""

    FIRING = "firing"
    RESOLVED = "resolved"
    ACKNOWLEDGED = "acknowledged"
    ANNOTATED = "annotated"
    FAILED = "failed"
    SUCCESS = "success"
    CHALLENGE = "challenge"
    INFO = "info"


@dataclass(slots=True)
class NormalizedWebhookEvent:
    """Platform-agnostic representation of a verified inbound webhook event."""

    provider: str
    """Provider key (e.g. 'sns', 'pagerduty', 'alertmanager', 'cloudevents', 'jenkins')."""

    event_type: str
    """Provider-specific event type (e.g. 'incident.triggered', 'alarm.state_change')."""

    incident_id: str
    """Stable identifier used to map related events into the same LangGraph thread."""

    title: str
    """Short human-readable alert/event title."""

    summary: str
    """Detailed description, alert message, or failure log excerpt."""

    severity: WebhookSeverity = WebhookSeverity.UNKNOWN
    """Normalized severity level."""

    status: WebhookEventStatus = WebhookEventStatus.FIRING
    """Normalized event lifecycle status."""

    service_name: str | None = None
    """Affected service, application, cluster, or pipeline name."""

    source_url: str | None = None
    """Deep link back to the originating incident, alarm, or build run."""

    caller_id: str = "webhook"
    """Caller identifier for IdentityMapper RBAC resolution."""

    dedup_key: str | None = None
    """Optional key for retry/storm deduplication. Defaults to provider:incident_id:event_type."""

    raw_payload: dict[str, Any] = field(default_factory=dict)
    """Unmodified webhook payload for full context inspection."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Extracted structured labels, annotations, dimensions, or tags."""

    callback_url: str | None = None
    """Optional outbound webhook URL to notify when the agent run finishes."""

    reply_target: dict[str, Any] = field(default_factory=dict)
    """Provider-specific coordinates for bidirectional write-back (e.g. incident_id, repo/issue)."""

    should_trigger_run: bool = True
    """Whether this event should start an autonomous agent run."""

    ignore_reason: str | None = None
    """Reason why should_trigger_run is False (e.g. 'resolved event', 'handshake challenge')."""

    challenge_response: dict[str, Any] | None = None
    """Immediate JSON response payload for provider verification handshakes."""

    @property
    def effective_dedup_key(self) -> str:
        """Return the composite deduplication key for this event."""
        if self.dedup_key:
            return f"{self.provider}:{self.dedup_key}"
        return f"{self.provider}:{self.incident_id}:{self.event_type}:{self.status}"

    def to_incoming_message(self, custom_instructions: str | None = None) -> IncomingMessage:
        """Convert this webhook event into an TalkOps IncomingMessage."""
        return IncomingMessage(
            text=self.format_agent_prompt(custom_instructions=custom_instructions),
            user_id=self.caller_id,
            thread_id=self.incident_id,
            channel_id=f"webhook:{self.provider}",
            platform=self.provider,
            raw_event=self.raw_payload,
            context={
                "event_type": self.event_type,
                "severity": str(self.severity),
                "status": str(self.status),
                "service_name": self.service_name,
                "source_url": self.source_url,
                "metadata": self.metadata,
                "reply_target": self.reply_target,
            },
        )

    def format_agent_prompt(self, custom_instructions: str | None = None) -> str:
        """Format a structured SRE/DevOps investigation prompt for the TalkOps agent."""
        lines: list[str] = [
            f"[WEBHOOK ALERT] Provider: {self.provider.upper()} | Event: {self.event_type}",
            f"Title: {self.title}",
            f"Severity: {self.severity.value.upper()} | Status: {self.status.value.upper()}",
            f"Incident / Group ID: {self.incident_id}",
        ]
        if self.service_name:
            lines.append(f"Service / Component: {self.service_name}")
        if self.source_url:
            lines.append(f"Source URL: {self.source_url}")

        lines.append("\n### Alert / Event Summary")
        lines.append(self.summary.strip() if self.summary else "(No summary provided)")

        if self.metadata:
            lines.append("\n### Extracted Labels & Metadata")
            try:
                lines.append("```json\n" + json.dumps(self.metadata, indent=2, default=str) + "\n```")
            except Exception:
                lines.append(str(self.metadata))

        lines.append("\n### Required Action")
        if custom_instructions and custom_instructions.strip():
            lines.append(custom_instructions.strip())
        else:
            lines.append(
                "1. Investigate the root cause of this alert/event using your available cloud, Kubernetes, "
                "observability, and repository tools.\n"
                "2. Correlate recent deployments, configuration changes, logs, or metric anomalies related "
                "to the affected service/resource.\n"
                "3. Provide a concise Root Cause Analysis (RCA), blast radius assessment, and recommended "
                "remediation steps."
            )
        return "\n".join(lines)


@dataclass(slots=True)
class OutboundChannelRoute:
    """Declarative routing rule connecting inbound webhooks to marketplace integration channels.

    Allows `webhooks` (built-in) to route incident alerts and RCA reports to any
    installed marketplace channel addon (`slack`, `teams`, `discord`, or custom channels)
    and bind the resulting chat thread to the LangGraph `thread_id`.
    """

    channel: str
    conversation_id: str
    providers: tuple[str, ...] = ("*",)
    severities: tuple[str, ...] = ("*",)
    notify_on: tuple[str, ...] = ("completed",)
    bind_thread: bool = True

    def matches(self, event: NormalizedWebhookEvent, stage: str = "completed") -> bool:
        """Return True if this route matches `event` at lifecycle `stage` (`'received'` or `'completed'`)."""
        norm_stage = stage.strip().lower()
        allowed_stages = {s.strip().lower() for s in self.notify_on if s.strip()}
        if "*" not in allowed_stages and norm_stage not in allowed_stages:
            return False

        allowed_providers = {p.strip().lower() for p in self.providers if p.strip()}
        if "*" not in allowed_providers and event.provider.strip().lower() not in allowed_providers:
            return False

        allowed_severities = {s.strip().lower() for s in self.severities if s.strip()}
        if "*" not in allowed_severities and event.severity.value.lower() not in allowed_severities:
            return False

        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "conversation_id": self.conversation_id,
            "providers": list(self.providers),
            "severities": list(self.severities),
            "notify_on": list(self.notify_on),
            "bind_thread": self.bind_thread,
        }


def parse_outbound_channel_routes(raw: Any) -> list[OutboundChannelRoute]:
    """Parse `outbound_channels` from `.talkops/addons.json` or `TALKOPS_WEBHOOK_NOTIFY_CHANNELS`.

    Supported formats:
    1. Shorthand string: `"slack:C0123456789,teams:19:ops_alerts@thread.tacv2"`
    2. List of dicts:
       `[{"channel": "slack", "conversation_id": "C123", "providers": ["pagerduty"], "severities": ["critical"], "notify_on": ["received", "completed"]}]`
    """
    routes: list[OutboundChannelRoute] = []
    if not raw:
        return routes

    if isinstance(raw, str):
        # Try JSON array first if string starts with '['
        stripped = raw.strip()
        if stripped.startswith("["):
            try:
                parsed_json = json.loads(stripped)
                return parse_outbound_channel_routes(parsed_json)
            except Exception:
                pass
        for item in stripped.split(","):
            token = item.strip()
            if not token or ":" not in token:
                continue
            ch, conv_id = token.split(":", 1)
            if ch.strip() and conv_id.strip():
                routes.append(
                    OutboundChannelRoute(
                        channel=ch.strip().lower(),
                        conversation_id=conv_id.strip(),
                        notify_on=("received", "completed"),
                    )
                )
        return routes

    if isinstance(raw, (list, tuple)):
        for entry in raw:
            if isinstance(entry, OutboundChannelRoute):
                routes.append(entry)
            elif isinstance(entry, str):
                routes.extend(parse_outbound_channel_routes(entry))
            elif isinstance(entry, dict):
                ch = str(entry.get("channel") or "").strip().lower()
                conv_id = str(
                    entry.get("conversation_id")
                    or entry.get("conversationId")
                    or entry.get("target")
                    or ""
                ).strip()
                if not ch or not conv_id:
                    continue
                raw_prov = entry.get("providers", ["*"])
                providers = (
                    tuple(str(p).strip() for p in raw_prov if str(p).strip())
                    if isinstance(raw_prov, (list, tuple))
                    else (str(raw_prov).strip(),)
                ) or ("*",)
                raw_sev = entry.get("severities", ["*"])
                severities = (
                    tuple(str(s).strip() for s in raw_sev if str(s).strip())
                    if isinstance(raw_sev, (list, tuple))
                    else (str(raw_sev).strip(),)
                ) or ("*",)
                raw_notify = entry.get("notify_on", entry.get("notifyOn", ["completed"]))
                notify_on = (
                    tuple(str(n).strip() for n in raw_notify if str(n).strip())
                    if isinstance(raw_notify, (list, tuple))
                    else (str(raw_notify).strip(),)
                ) or ("completed",)
                bind_thread = bool(entry.get("bind_thread", entry.get("bindThread", True)))
                routes.append(
                    OutboundChannelRoute(
                        channel=ch,
                        conversation_id=conv_id,
                        providers=providers,
                        severities=severities,
                        notify_on=notify_on,
                        bind_thread=bind_thread,
                    )
                )
    return routes


@dataclass(slots=True)
class WebhookConfig:
    """Runtime configuration for webhook verification, deduplication, and write-back sinks.

    Hydrated through TalkOps's canonical configuration pipeline
    (`env_vars.py` -> `manifest.py` -> `config.toml` -> `Settings`).
    """

    secret_env_prefix: str = "TALKOPS_WEBHOOK_SECRET_"
    default_secret: str | None = None
    provider_secrets: dict[str, str] = field(default_factory=dict)
    auto_confirm_sns: bool = True
    ignore_resolved: bool = True
    dedup_ttl_seconds: float = 300.0
    max_concurrent_runs: int = 5
    default_callback_url: str | None = None
    slack_webhook_url: str | None = None
    pagerduty_api_key: str | None = None
    pagerduty_from_email: str | None = None
    servicenow_instance_url: str | None = None
    servicenow_api_token: str | None = None
    github_token: str | None = None
    custom_prompt_instructions: str | None = None
    assistant_id: str = "talkops"
    outbound_channels: list[OutboundChannelRoute] = field(default_factory=list)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> WebhookConfig:
        """Hydrate WebhookConfig from TalkOps's canonical `Settings` object."""
        from talkops.config.env_vars import classify_env_bool
        from talkops.config.settings import Settings as SettingsCls, resolve_env_var

        cfg = settings if settings is not None else SettingsCls.from_env()

        provider_secrets: dict[str, str] = {}
        if cfg.webhook_pagerduty_secret:
            provider_secrets["pagerduty"] = cfg.webhook_pagerduty_secret
        if cfg.webhook_github_secret:
            provider_secrets["github"] = cfg.webhook_github_secret
        if cfg.webhook_gitlab_token:
            provider_secrets["gitlab"] = cfg.webhook_gitlab_token
        if cfg.webhook_jenkins_token:
            provider_secrets["jenkins"] = cfg.webhook_jenkins_token

        # Support legacy/alias env vars via canonical resolve_env_var
        dedup_ttl = float(cfg.webhook_dedupe_ttl_seconds)
        legacy_dedup = resolve_env_var("WEBHOOK_DEDUP_TTL")
        if legacy_dedup is not None:
            try:
                dedup_ttl = max(0.0, float(legacy_dedup))
            except ValueError:
                pass

        auto_confirm_sns = bool(cfg.webhook_sns_auto_confirm)
        legacy_sns_confirm = resolve_env_var("WEBHOOK_AUTO_CONFIRM_SNS")
        if legacy_sns_confirm is not None:
            parsed_bool = classify_env_bool(legacy_sns_confirm)
            if parsed_bool is not None:
                auto_confirm_sns = parsed_bool

        ignore_resolved = True
        raw_ignore_resolved = resolve_env_var("WEBHOOK_IGNORE_RESOLVED")
        if raw_ignore_resolved is not None:
            parsed_ignore = classify_env_bool(raw_ignore_resolved)
            if parsed_ignore is not None:
                ignore_resolved = parsed_ignore

        return cls(
            secret_env_prefix=resolve_env_var("WEBHOOK_SECRET_PREFIX") or "TALKOPS_WEBHOOK_SECRET_",
            default_secret=cfg.webhook_secret or None,
            provider_secrets=provider_secrets,
            auto_confirm_sns=auto_confirm_sns,
            ignore_resolved=ignore_resolved,
            dedup_ttl_seconds=dedup_ttl,
            max_concurrent_runs=max(1, int(cfg.webhook_max_concurrent_runs)),
            default_callback_url=cfg.webhook_callback_url or None,
            slack_webhook_url=(
                cfg.webhook_slack_url
                or resolve_env_var("SLACK_WEBHOOK_URL")
                or None
            ),
            pagerduty_api_key=(
                cfg.webhook_pagerduty_api_key
                or resolve_env_var("PAGERDUTY_API_KEY")
                or None
            ),
            pagerduty_from_email=(
                cfg.webhook_pagerduty_from_email
                or resolve_env_var("PAGERDUTY_FROM_EMAIL")
                or None
            ),
            servicenow_instance_url=(
                resolve_env_var("SERVICENOW_URL", fallback_names=("SERVICENOW_INSTANCE_URL",))
                or None
            ),
            servicenow_api_token=(
                resolve_env_var("SERVICENOW_TOKEN", fallback_names=("SERVICENOW_API_TOKEN",))
                or None
            ),
            github_token=(
                cfg.webhook_github_token
                or resolve_env_var("GITHUB_TOKEN")
                or None
            ),
            custom_prompt_instructions=resolve_env_var("WEBHOOK_INSTRUCTIONS") or None,
            assistant_id=resolve_env_var("SERVER_ASSISTANT_ID") or cfg.assistant_id or "talkops",
            outbound_channels=parse_outbound_channel_routes(
                resolve_env_var(
                    "WEBHOOK_NOTIFY_CHANNELS",
                    fallback_names=("WEBHOOK_NOTIFY_CHANNEL", "TALKOPS_WEBHOOK_NOTIFY_CHANNELS"),
                )
            ),
        )

    @classmethod
    def from_env(cls) -> WebhookConfig:
        """Load WebhookConfig through TalkOps's canonical Settings resolution pipeline."""
        from talkops.config.settings import Settings as SettingsCls

        return cls.from_settings(SettingsCls.from_env())

    def get_provider_secret(self, provider: str) -> str | None:
        """Resolve the webhook signing secret for a specific provider.

        Checks in order:
        1. Canonical provider secret from `Settings` (`webhooks.<provider>_secret` / `webhooks.<provider>_token`)
        2. Provider-specific env var (`TALKOPS_WEBHOOK_SECRET_<PROVIDER>` / `TALKOPS_WEBHOOK_<PROVIDER>_SECRET`)
        3. Default webhook secret (`webhooks.secret` / `TALKOPS_WEBHOOK_SECRET`)
        """
        from talkops.config.settings import resolve_env_var

        norm = provider.lower().strip()
        if norm in self.provider_secrets and self.provider_secrets[norm]:
            return self.provider_secrets[norm]

        provider_upper = norm.upper().replace("-", "_")
        prefix_clean = self.secret_env_prefix
        if prefix_clean.startswith("TALKOPS_"):
            prefix_clean = prefix_clean[len("TALKOPS_") :]
        dynamic_secret = resolve_env_var(
            f"{prefix_clean}{provider_upper}",
            fallback_names=(
                f"{self.secret_env_prefix}{provider_upper}",
                f"TALKOPS_WEBHOOK_{provider_upper}_SECRET",
                f"TALKOPS_WEBHOOK_{provider_upper}_TOKEN",
            ),
        )
        return dynamic_secret or self.default_secret

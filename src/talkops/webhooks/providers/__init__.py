"""Provider adapter registry and default built-in adapters."""

from __future__ import annotations

from talkops.webhooks.providers.alertmanager import AlertmanagerWebhookAdapter
from talkops.webhooks.providers.base import (
    WebhookParseError,
    WebhookProviderAdapter,
    WebhookProviderRegistry,
)
from talkops.webhooks.providers.cloudevents import CloudEventsWebhookAdapter
from talkops.webhooks.providers.github import GitHubWebhookAdapter
from talkops.webhooks.providers.jenkins import JenkinsWebhookAdapter
from talkops.webhooks.providers.observability_itsm import (
    DatadogWebhookAdapter,
    GenericWebhookAdapter,
    ServiceNowWebhookAdapter,
)
from talkops.webhooks.providers.pagerduty import PagerDutyWebhookAdapter
from talkops.webhooks.providers.sns import SNSWebhookAdapter


def create_default_registry() -> WebhookProviderRegistry:
    """Create a WebhookProviderRegistry populated with all built-in DevOps & CNCF adapters."""
    registry = WebhookProviderRegistry()
    for adapter in (
        SNSWebhookAdapter(),
        PagerDutyWebhookAdapter(),
        AlertmanagerWebhookAdapter(),
        CloudEventsWebhookAdapter(),
        JenkinsWebhookAdapter(),
        GitHubWebhookAdapter(),
        DatadogWebhookAdapter(),
        ServiceNowWebhookAdapter(),
        GenericWebhookAdapter(),
    ):
        registry.register(adapter)
    return registry


__all__ = [
    "AlertmanagerWebhookAdapter",
    "CloudEventsWebhookAdapter",
    "DatadogWebhookAdapter",
    "GenericWebhookAdapter",
    "GitHubWebhookAdapter",
    "JenkinsWebhookAdapter",
    "PagerDutyWebhookAdapter",
    "SNSWebhookAdapter",
    "ServiceNowWebhookAdapter",
    "WebhookParseError",
    "WebhookProviderAdapter",
    "WebhookProviderRegistry",
    "create_default_registry",
]

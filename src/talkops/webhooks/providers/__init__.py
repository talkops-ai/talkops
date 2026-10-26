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

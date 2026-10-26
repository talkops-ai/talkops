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

"""TalkOps integrations — event hooks, external event bus, notifications, and stream bridging."""

from talkops.integrations.base import (
    IncomingMessage,
    InteractionPayload,
    MessagingIntegration,
)
from talkops.integrations.content_filter import (
    filter_internal_content,
    is_internal_message,
)
from talkops.integrations.event_bus import (
    BypassTier,
    EventBus,
    EventSink,
    ExternalEvent,
    ExternalEventKind,
    ExternalSignal,
    default_unix_socket_path,
)
from talkops.integrations.hooks import (
    ALL_KNOWN_EVENTS,
    DEVOPS_EVENTS,
    STANDARD_EVENTS,
    HookConfig,
    dispatch_hook,
    dispatch_hook_fire_and_forget,
    drain_pending_hooks,
    load_hooks,
)
from talkops.integrations.identity import IdentityMapper, UserIdentity
from talkops.integrations.notifications import (
    notify_cloud_event,
    notify_deploy_event,
    notify_session_end,
    notify_session_start,
    notify_task_complete,
    notify_tool_result,
    notify_tool_use,
)
from talkops.integrations.stream_bridge import (
    GraphStreamBridge,
    StreamSink,
)
from talkops.integrations.thread_store import ThreadStore

__all__ = [
    "ALL_KNOWN_EVENTS",
    "BypassTier",
    "DEVOPS_EVENTS",
    "EventBus",
    "EventSink",
    "ExternalEvent",
    "ExternalEventKind",
    "ExternalSignal",
    "GraphStreamBridge",
    "HookConfig",
    "IdentityMapper",
    "IncomingMessage",
    "InteractionPayload",
    "MessagingIntegration",
    "STANDARD_EVENTS",
    "StreamSink",
    "ThreadStore",
    "UserIdentity",
    "default_unix_socket_path",
    "dispatch_hook",
    "dispatch_hook_fire_and_forget",
    "drain_pending_hooks",
    "filter_internal_content",
    "is_internal_message",
    "load_hooks",
    "notify_cloud_event",
    "notify_deploy_event",
    "notify_session_end",
    "notify_session_start",
    "notify_task_complete",
    "notify_tool_result",
    "notify_tool_use",
]

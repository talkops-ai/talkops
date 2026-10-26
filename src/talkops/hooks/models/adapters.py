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

"""Cached runtime validators for hook model boundaries."""

from pydantic import TypeAdapter

from talkops.hooks.models.domain import (
    HookDecision,
    HookDomainEvent,
    HookInvocation,
)
from talkops.hooks.models.transport import (
    HookInvocationRequest,
    HookInvocationResponse,
)

HOOK_DOMAIN_EVENT_ADAPTER: TypeAdapter[HookDomainEvent] = TypeAdapter(HookDomainEvent)
HOOK_INVOCATION_ADAPTER: TypeAdapter[HookInvocation] = TypeAdapter(HookInvocation)
HOOK_DECISION_ADAPTER: TypeAdapter[HookDecision] = TypeAdapter(HookDecision)
HOOK_INVOCATION_REQUEST_ADAPTER: TypeAdapter[HookInvocationRequest] = TypeAdapter(HookInvocationRequest)
HOOK_INVOCATION_RESPONSE_ADAPTER: TypeAdapter[HookInvocationResponse] = TypeAdapter(HookInvocationResponse)

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

"""State channels for Webhook & Incident Channel Autonomous SRE / RCA execution."""

from __future__ import annotations

from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.types import AgentState, PrivateStateAttr


class WebhookSREChannels(AgentState):
    """Private state channels persisted when a thread is triggered via a webhook or incident channel."""

    _webhook_sre_mode: NotRequired[Annotated[bool | None, PrivateStateAttr]]
    """True when the conversation thread is operating in Autonomous SRE / Incident RCA mode."""

    _webhook_metadata: NotRequired[Annotated[dict[str, Any] | None, PrivateStateAttr]]
    """Normalized webhook or incident channel metadata (provider, event_type, incident_id, channel, source_addon)."""


__all__ = ["WebhookSREChannels"]

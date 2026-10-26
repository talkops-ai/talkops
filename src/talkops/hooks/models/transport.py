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

"""Versioned hook invocation transport models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from talkops.hooks.models.domain import HookDecision, HookInvocation


class _TransportModel(BaseModel):
    """Base model for hook transport payloads with wire-format helpers."""

    model_config = ConfigDict(extra="forbid")


class HookInvocationRequest(_TransportModel):
    """Request sent for a server-owned hook invocation."""

    protocol_version: Literal[1]
    invocation_id: UUID
    snapshot_id: str
    run_id: str
    invocation: HookInvocation
    deadline: datetime


class HookInvocationResponse(_TransportModel):
    """Response returned for a server-owned hook invocation."""

    protocol_version: Literal[1]
    invocation_id: UUID
    snapshot_id: str
    decision: HookDecision

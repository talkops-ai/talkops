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

"""State channels for per-checkpoint facts restored on resume.

Tracks model selections, token counts, cold cache timestamps, and pending goal
reviews that survive process boundaries and thread restores.
"""

from __future__ import annotations

from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.types import PrivateStateAttr

from talkops.state.goal_channels import (
    INHERIT_RUBRIC_MODEL,
    GoalRubricChannels,
    coerce_model_spec,
)


class ResumeState(GoalRubricChannels):
    """Extends agent state with per-checkpoint operational facts restored on resume."""

    _context_tokens: Annotated[
        NotRequired[int | None],
        PrivateStateAttr,
    ]
    """Total context tokens reported by the model's last usage_metadata."""

    _model_spec: Annotated[
        NotRequired[str | None],
        PrivateStateAttr,
    ]
    """Model spec (provider:model) effectively in use for the latest turn."""

    _model_params: Annotated[
        NotRequired[dict[str, Any] | None],
        PrivateStateAttr,
    ]
    """Model configuration parameters effectively in use for the latest turn."""

    _last_model_request_at: Annotated[
        NotRequired[str | None],
        PrivateStateAttr,
    ]
    """UTC request timestamp for the latest successful main model call."""

    _last_cache_model_spec: Annotated[
        NotRequired[str | None],
        PrivateStateAttr,
    ]
    """Requested model spec associated with _last_model_request_at."""


__all__ = [
    "INHERIT_RUBRIC_MODEL",
    "ResumeState",
    "coerce_model_spec",
]

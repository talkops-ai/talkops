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

"""Trajectory evaluator — deterministic checks on agent tool call sequences."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TrajectoryMatch:
    """Test suite or helper class for trajectory match."""
    matched: bool
    score: float
    expected_tools: list[str]
    actual_tools: list[str]
    missing_tools: list[str] = field(default_factory=list)


def evaluate_tool_sequence(
    actual_tool_calls: list[dict[str, Any]],
    expected_tool_names: list[str],
) -> TrajectoryMatch:
    """Provide evaluate tool sequence test helper or fixture."""
    actual_names = [tc.get("name", "") for tc in actual_tool_calls]
    actual_set = set(actual_names)
    expected_set = set(expected_tool_names)
    missing = expected_set - actual_set

    matched = len(missing) == 0
    score = (len(expected_set) - len(missing)) / len(expected_set) if expected_set else 1.0

    return TrajectoryMatch(
        matched=matched,
        score=score,
        expected_tools=expected_tool_names,
        actual_tools=actual_names,
        missing_tools=list(missing),
    )

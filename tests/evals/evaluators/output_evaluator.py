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

"""Output evaluator — regex pattern matching on final agent responses."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class OutputMatch:
    """Test suite or helper class for output match."""
    matched: bool
    score: float
    matched_patterns: list[str]
    unmatched_patterns: list[str]


def evaluate_output_patterns(output: str, patterns: list[str]) -> OutputMatch:
    """Provide evaluate output patterns test helper or fixture."""
    matched = []
    unmatched = []
    for pat in patterns:
        if re.search(pat, output, re.IGNORECASE):
            matched.append(pat)
        else:
            unmatched.append(pat)

    score = len(matched) / len(patterns) if patterns else 1.0
    return OutputMatch(
        matched=len(unmatched) == 0,
        score=score,
        matched_patterns=matched,
        unmatched_patterns=unmatched,
    )

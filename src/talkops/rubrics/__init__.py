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

"""Rubrics and evaluation package for talkops."""

from talkops.rubrics.evaluator import (
    _RUBRIC_GRADER_SYSTEM_PROMPT,
    _create_rubric_grader_tools,
    _rubric_grader_system_prompt,
    create_rubric_grader_agent,
    evaluate_rubric,
)
from talkops.rubrics.evidence_extractor import extract_grading_evidence
from talkops.rubrics.generator import (
    GOAL_AMENDMENT_SYSTEM_PROMPT,
    GOAL_RUBRIC_SYSTEM_PROMPT,
    _goal_amendment_human_prompt,
    _goal_rubric_human_prompt,
    generate_rubric,
)
from talkops.rubrics.jev_compiler import CompiledRubric, JevCriteriaCompiler
from talkops.rubrics.jev_grader import (
    BLOCKER_DISPATCH,
    JevHybridRubricGrader,
    build_criteria_evaluations,
)

__all__ = [
    "BLOCKER_DISPATCH",
    "CompiledRubric",
    "GOAL_AMENDMENT_SYSTEM_PROMPT",
    "GOAL_RUBRIC_SYSTEM_PROMPT",
    "JevCriteriaCompiler",
    "JevHybridRubricGrader",
    "_RUBRIC_GRADER_SYSTEM_PROMPT",
    "_create_rubric_grader_tools",
    "_goal_amendment_human_prompt",
    "_goal_rubric_human_prompt",
    "_rubric_grader_system_prompt",
    "build_criteria_evaluations",
    "create_rubric_grader_agent",
    "evaluate_rubric",
    "extract_grading_evidence",
    "generate_rubric",
]

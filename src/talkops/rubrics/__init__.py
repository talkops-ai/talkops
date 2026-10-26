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

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "_RUBRIC_GRADER_SYSTEM_PROMPT": ("talkops.rubrics.evaluator", "_RUBRIC_GRADER_SYSTEM_PROMPT"),
    "_create_rubric_grader_tools": ("talkops.rubrics.evaluator", "_create_rubric_grader_tools"),
    "_rubric_grader_system_prompt": ("talkops.rubrics.evaluator", "_rubric_grader_system_prompt"),
    "create_rubric_grader_agent": ("talkops.rubrics.evaluator", "create_rubric_grader_agent"),
    "evaluate_rubric": ("talkops.rubrics.evaluator", "evaluate_rubric"),
    "extract_grading_evidence": ("talkops.rubrics.evidence_extractor", "extract_grading_evidence"),
    "GOAL_AMENDMENT_SYSTEM_PROMPT": ("talkops.rubrics.generator", "GOAL_AMENDMENT_SYSTEM_PROMPT"),
    "GOAL_RUBRIC_SYSTEM_PROMPT": ("talkops.rubrics.generator", "GOAL_RUBRIC_SYSTEM_PROMPT"),
    "_goal_amendment_human_prompt": ("talkops.rubrics.generator", "_goal_amendment_human_prompt"),
    "_goal_rubric_human_prompt": ("talkops.rubrics.generator", "_goal_rubric_human_prompt"),
    "generate_rubric": ("talkops.rubrics.generator", "generate_rubric"),
    "CompiledRubric": ("talkops.rubrics.jev_compiler", "CompiledRubric"),
    "JevCriteriaCompiler": ("talkops.rubrics.jev_compiler", "JevCriteriaCompiler"),
    "BLOCKER_DISPATCH": ("talkops.rubrics.jev_grader", "BLOCKER_DISPATCH"),
    "JevHybridRubricGrader": ("talkops.rubrics.jev_grader", "JevHybridRubricGrader"),
    "build_criteria_evaluations": ("talkops.rubrics.jev_grader", "build_criteria_evaluations"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_IMPORTS:
        module_name, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_name)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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

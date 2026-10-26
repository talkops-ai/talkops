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

"""Unit tests for rubric evaluation and goal prompt generation."""
from unittest.mock import MagicMock

from talkops.middleware.goal_state_notice import (
    build_goal_state_notice,
    is_conversation_control_message,
)
from talkops.rubrics.generator import (
    GOAL_RUBRIC_SYSTEM_PROMPT,
    _goal_amendment_human_prompt,
    _goal_rubric_human_prompt,
)


def test_goal_prompts():
    """Verify goal prompts."""
    prompt = _goal_rubric_human_prompt("Migrate EC2 workloads to Graviton")
    assert "Migrate EC2 workloads to Graviton" in prompt
    assert "<goal>" in prompt
    assert "acceptance criteria" in GOAL_RUBRIC_SYSTEM_PROMPT.lower()

    amend_prompt = _goal_amendment_human_prompt("Deploy EKS", "Criteria 1", "Add Karpenter")
    assert "Deploy EKS" in amend_prompt
    assert "Add Karpenter" in amend_prompt


def test_goal_state_notice_identification():
    """Verify goal state notice identification."""
    notice = build_goal_state_notice({"goal": "Upgrade RDS Postgres to 16", "rubric_criteria": "- Check version\n- Verify read replica"})
    assert is_conversation_control_message(notice) is True

    # Regular message
    assert is_conversation_control_message({"role": "user", "content": "hello"}) is False


def test_evaluate_rubric_structured_and_fallback(monkeypatch):
    """Verify evaluate rubric structured and fallback."""
    from deepagents.middleware.rubric import GraderResponse

    from talkops.rubrics.evaluator import evaluate_rubric

    mock_grader = MagicMock()
    monkeypatch.setattr("talkops.rubrics.evaluator.create_rubric_grader_agent", lambda **kwargs: mock_grader)

    # 1. Structured response path
    expected = GraderResponse(
        result="satisfied",
        explanation="All criteria satisfied",
        criteria=[{"name": "c1", "passed": True}],
    )
    mock_grader.invoke.return_value = {"structured_response": expected}
    res = evaluate_rubric("criteria text", "evidence text")
    assert res.result == "satisfied"
    assert res.explanation == "All criteria satisfied"

    # 2. String fallback path (needs_revision)
    mock_grader.invoke.return_value = "The security group is missing port 443"
    res_fallback = evaluate_rubric("criteria text", "evidence text")
    assert res_fallback.result == "needs_revision"
    assert "security group" in res_fallback.explanation
    assert len(res_fallback.criteria) == 1
    assert res_fallback.criteria[0]["passed"] is False


def test_rubric_grader_prompt_contains_6_axis_conformance_checklist():
    """Verify _rubric_grader_system_prompt includes Harness²'s 6-Axis Deliverable Conformance Checklist."""
    from talkops.rubrics.evaluator import _rubric_grader_system_prompt

    prompt = _rubric_grader_system_prompt()
    assert "## 6-Axis Deliverable Conformance Checklist" in prompt
    for axis in ("Deliverable", "Completeness", "Conformance", "Grounding", "Appropriateness", "Safety"):
        assert f"**{axis}**" in prompt


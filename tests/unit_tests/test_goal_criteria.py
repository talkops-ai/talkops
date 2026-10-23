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

"""Unit tests for GoalCriteriaMiddleware, GoalProposal, and criteria agent factories."""

from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from talkops.middleware.goal_criteria import (
    _REPOSITORY_RECURSION_LIMIT,
    _STRUCTURED_OUTPUT_TOOL_NAME,
    _WEB_SEARCH_CALL_LIMIT,
    GoalAmendRequest,
    GoalCreateRequest,
    GoalCriteriaMiddleware,
    GoalProposal,
    _raise_terminal_goal_state_size_error,
    create_goal_criteria_agent,
    create_goal_criteria_fallback_agent,
)
from talkops.state.goal_state_limits import (
    GOAL_APPLICATION_CHAR_LIMIT,
    GOAL_OBJECTIVE_CHAR_LIMIT,
    RUBRIC_CHAR_LIMIT,
    GoalStateSizeError,
)


class TestGoalProposal:
    """Test suite or helper class for test goal proposal."""
    def test_valid_proposal(self):
        """Verify valid proposal."""
        prop = GoalProposal(
            objective="Deploy AWS RDS Postgres instance",
            criteria="- Multi-AZ enabled\n- KMS encryption enabled\n- Subnet group configured",
        )
        assert prop.objective == "Deploy AWS RDS Postgres instance"
        assert "Multi-AZ" in prop.criteria

    def test_empty_objective_rejected(self):
        """Verify empty objective rejected."""
        with pytest.raises(ValidationError):
            GoalProposal(objective="   ", criteria="- Valid criterion")

    def test_empty_criteria_rejected(self):
        """Verify empty criteria rejected."""
        with pytest.raises(ValidationError):
            GoalProposal(objective="Valid objective", criteria="   ")

    def test_overlong_individual_field_rejected(self):
        """Verify overlong individual field rejected."""
        with pytest.raises(ValidationError):
            GoalProposal(
                objective="x" * (GOAL_OBJECTIVE_CHAR_LIMIT + 1),
                criteria="- Criteria",
            )
        with pytest.raises(ValidationError):
            GoalProposal(
                objective="Valid objective",
                criteria="x" * (RUBRIC_CHAR_LIMIT + 1),
            )

    def test_combined_length_exceeded_rejected(self):
        # Even if individual fields fit their limits (8k and 12k), their sum cannot exceed 12k
        """Verify combined length exceeded rejected."""
        with pytest.raises(ValidationError) as exc_info:
            GoalProposal(
                objective="o" * 7_000,
                criteria="c" * 7_000,
            )
        assert "Goal objective and criteria combined is 14,000 characters" in str(exc_info.value)


class TestTerminalGoalStateSizeError:
    """Test suite or helper class for test terminal goal state size error."""
    def test_combined_overshoot_raises_terminal_size_error(self):
        # Construct a fake structured-output exception where tool call arguments exceed 12k combined
        """Verify combined overshoot raises terminal size error."""
        msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "GoalProposal",
                    "args": {
                        "objective": "o" * 7_000,
                        "criteria": "c" * 7_000,
                    },
                    "id": "call_1",
                }
            ],
        )
        exc = Exception("Structured output validation failed")
        setattr(exc, "ai_message", msg)

        # Must raise GoalStateSizeError directly (terminal, not retried)
        with pytest.raises(GoalStateSizeError):
            _raise_terminal_goal_state_size_error(exc)

    def test_per_field_overshoot_returns_retry_feedback(self):
        # When an individual field overshot its max_length (e.g. objective > 8000),
        # this is published in the schema so the model should be allowed to retry
        """Verify per field overshoot returns retry feedback."""
        msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "GoalProposal",
                    "args": {
                        "objective": "o" * 8_500,
                        "criteria": "c" * 500,
                    },
                    "id": "call_2",
                }
            ],
        )
        exc = Exception("Field max_length exceeded")
        setattr(exc, "ai_message", msg)

        res = _raise_terminal_goal_state_size_error(exc)
        assert "Please fix your mistakes" in res


class TestAgentCreation:
    """Test suite or helper class for test agent creation."""
    def test_create_fallback_agent(self):
        """Verify create fallback agent."""
        model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
        agent = create_goal_criteria_fallback_agent(model=model)
        assert agent is not None

    def test_create_criteria_agent(self):
        """Verify create criteria agent."""
        from deepagents.backends import StateBackend

        model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
        backend = StateBackend()
        agent = create_goal_criteria_agent(
            model=model,
            repository_backend=backend,
            repository_root="/tmp/repo",
            context_tools=[],
        )
        assert agent is not None


class TestGoalCriteriaMiddleware:
    """Test suite or helper class for test goal criteria middleware."""
    def test_update_valid_proposal(self):
        """Verify update valid proposal."""
        middleware = GoalCriteriaMiddleware(criteria_agent=None, fallback_agent=None)
        req: GoalCreateRequest = {
            "kind": "create",
            "request_id": "req-1",
            "objective": "Deploy Lambda function",
        }
        res = {
            "structured_response": GoalProposal(
                objective="Deploy Lambda function",
                criteria="- Handler is configured\n- Memory set to 512MB",
            )
        }
        update = middleware._update(req, res)
        assert update["_pending_goal_objective"] == "Deploy Lambda function"
        assert "- Handler is configured" in update["_pending_goal_rubric"]
        assert update["_pending_goal_plan"] is not None
        assert "## Summary" in update["_pending_goal_plan"]
        assert isinstance(update["_pending_goal_todos"], list)
        assert len(update["_pending_goal_todos"]) >= 2
        assert update["jump_to"] == "end"
        assert update["goal_criteria_request"] is None

    def test_update_proposal_with_explicit_plan_and_todos(self):
        """Verify update proposal with explicit plan and todos."""
        middleware = GoalCriteriaMiddleware(criteria_agent=None, fallback_agent=None)
        req: GoalCreateRequest = {
            "kind": "create",
            "request_id": "req-2",
            "objective": "Migrate VPC NAT Gateway",
        }
        explicit_plan = (
            "## Summary\nMigrate NAT gateway to public subnet B.\n\n"
            "## Open Questions / Assumptions\n- Proceeding with zero-downtime route switch."
        )
        explicit_todos = [
            "Inspect existing route tables and Elastic IPs",
            "Provision new NAT Gateway in public subnet B",
            "Switch private route table default route and verify connectivity",
        ]
        res = {
            "structured_response": GoalProposal(
                objective="Migrate VPC NAT Gateway",
                plan=explicit_plan,
                todos=explicit_todos,
                criteria="- New NAT Gateway is active\n- Private route table targets new NAT Gateway",
            )
        }
        update = middleware._update(req, res)
        assert update["_pending_goal_plan"] == explicit_plan
        assert update["_pending_goal_todos"] == explicit_todos


class TestGoalReviewMenuPlanRendering:
    """Test suite or helper class for test goal review menu plan rendering."""
    def test_goal_review_menu_builds_plan_todos_and_rubric_markdown(self):
        """Verify goal review menu builds plan todos and rubric markdown."""
        from talkops.ui.widgets.goal_review import GoalReviewMenu

        menu = GoalReviewMenu(
            objective="Configure S3 bucket encryption",
            criteria="- SSE-KMS enabled\n- Public access blocked",
            plan="## Summary\nEnable KMS encryption and block public ACLs on the target S3 bucket.",
            todos=[
                "Inspect current bucket encryption and public access block configuration",
                "Apply KMS default encryption and public access block rules",
                "Verify bucket configuration via AWS CLI",
            ],
        )
        assert menu._plan is not None
        assert "Enable KMS encryption" in menu._plan
        assert menu._todos is not None and len(menu._todos) == 3
        rendered = menu._build_review_markdown()
        assert "**Implementation Plan**" in rendered
        assert "## Summary" in rendered
        assert "**Tactical Execution Checklist (`write_todos`)**" in rendered
        assert "1. `[pending]` Inspect current bucket encryption" in rendered
        assert "Jev Verification Rubric" in rendered
        assert "- SSE-KMS enabled" in rendered

    def test_extract_clarifying_questions_from_inline_markdown_and_choices(self):
        """Verify extract clarifying questions from inline markdown and choices."""
        from talkops.rubrics.generator import extract_clarifying_questions

        plan_with_inline_questions = (
            "## Summary & Questions\n"
            "- **Summary**: Audit AWS S3 buckets for public access and encryption.\n"
            "- **Clarifying Question**: Should the script target a specific single region or iterate across all enabled AWS regions?\n"
            "- **Clarifying Question**: Would you prefer the output formatted as a console summary table, or exported to a file (CSV/JSON)?\n\n"
            "## Proposed Changes\n"
            "- Run read-only S3 audit commands."
        )
        cleaned_plan, questions = extract_clarifying_questions(plan_with_inline_questions)
        assert cleaned_plan is not None
        assert "Clarifying Question" not in cleaned_plan
        assert "## Summary" in cleaned_plan
        assert len(questions) == 2
        assert questions[0]["type"] == "multiple_choice"
        raw_choices_0: Any = questions[0]["choices"]
        q0_choices = [c["value"] for c in raw_choices_0]
        assert len(q0_choices) == 3
        assert "(Recommended)" in q0_choices[0]
        assert any("single region" in c.lower() for c in q0_choices)
        assert any("all enabled aws regions" in c.lower() for c in q0_choices)

        assert questions[1]["type"] == "multiple_choice"
        raw_choices_1: Any = questions[1]["choices"]
        q1_choices = [c["value"] for c in raw_choices_1]
        assert len(q1_choices) == 3
        assert any("console summary table" in c.lower() for c in q1_choices)
        assert any("exported to a file" in c.lower() for c in q1_choices)

    def test_goal_review_menu_interactive_questions_and_labeled_inputs(self):
        """Verify goal review menu interactive questions and labeled inputs."""
        from talkops.ui.widgets.goal_review import GoalReviewMenu

        plan_md = (
            "## Summary & Questions\n"
            "- **Summary**: Check unused EBS volumes.\n"
            "- **Clarifying Question**: Should we scan only us-east-1 or all enabled regions?\n\n"
            "## Proposed Changes\n"
            "- Query EC2 describe-volumes."
        )
        menu = GoalReviewMenu(
            objective="Check unused EBS volumes",
            criteria="- Unused volumes listed",
            plan=plan_md,
            todos=["Describe available EBS volumes"],
        )
        assert len(menu._questions) == 1
        assert menu._options[0][1] == "answer_questions"
        assert "Clarifying Question" not in (menu._plan or "")

        # Simulate answering the clarifying question
        menu._answers = ["Scan only us-east-1 (Recommended)"]
        formatted = menu._format_question_answers()
        assert "User answers to clarifying questions:" in formatted
        assert "Q1: Should we scan only us-east-1 or all enabled regions?" in formatted
        assert "Answer: Scan only us-east-1 (Recommended)" in formatted

        # Verify edit and reject modes display the input label and editor
        menu.action_reject_with_message()
        assert menu._input_mode == "reject"
        assert menu._input_label.display is True
        assert menu._edit_input.display is True

        menu.action_cancel()
        assert menu._input_mode is None
        assert menu._input_label.display is False
        assert menu._edit_input.display is False


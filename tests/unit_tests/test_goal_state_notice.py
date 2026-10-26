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

"""Unit tests for goal_state_notice — notice building, classification, and summarization cutoff."""

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from talkops.middleware.goal_state_notice import (
    GOAL_CONTROL_MESSAGE_SOURCE,
    GOAL_MESSAGE_SCHEMA_VERSION,
    GOAL_STATE_MESSAGE_SOURCE,
    SUPERSEDED_GOAL_STATE_SOURCE,
    build_goal_continuation,
    build_goal_state_notice,
    goal_state_fingerprint,
    goal_state_notice_info,
    is_conversation_control_message,
    is_goal_internal_message,
    is_goal_state_message,
    is_human_message,
    is_internal_message,
    is_oversized_goal_state_message,
    latest_goal_state_message_index,
    latest_goal_state_notice,
    latest_human_is_unsaved_goal_continuation,
    log_malformed_summarization_event,
    message_source,
    message_text,
    summarization_cutoff,
    superseded_goal_state_placeholder,
    validated_summarization_cutoff,
)
from talkops.state.goal_state_limits import GOAL_APPLICATION_CHAR_LIMIT


class TestMessageClassification:
    """Test suite or helper class for test message classification."""
    def test_is_human_message(self):
        """Verify is human message."""
        assert is_human_message(HumanMessage(content="hi")) is True
        assert is_human_message(AIMessage(content="hi")) is False
        assert is_human_message({"role": "user", "content": "hi"}) is True
        assert is_human_message({"type": "human", "content": "hi"}) is True
        assert is_human_message({"role": "assistant", "content": "hi"}) is False

    def test_message_text(self):
        """Verify message text."""
        assert message_text(HumanMessage(content="hello")) == "hello"
        assert message_text({"content": "from dict"}) == "from dict"
        assert message_text({"content": [{"type": "text", "text": "block"}]}) == "block"
        assert message_text({}) == ""

    def test_message_source(self):
        """Verify message source."""
        msg = HumanMessage(
            content="test",
            additional_kwargs={"lc_source": GOAL_STATE_MESSAGE_SOURCE},
        )
        assert message_source(msg) == GOAL_STATE_MESSAGE_SOURCE
        assert is_goal_state_message(msg) is True
        assert is_goal_internal_message(msg) is True
        assert is_conversation_control_message(msg) is True
        assert is_internal_message(msg) is True

    def test_superseded_source_classification(self):
        """Verify superseded source classification."""
        placeholder = superseded_goal_state_placeholder(HumanMessage(content="old", id="msg-1"))
        assert placeholder.id == "msg-1"
        assert placeholder.additional_kwargs.get("lc_source") == SUPERSEDED_GOAL_STATE_SOURCE
        assert is_goal_state_message(placeholder) is False
        assert is_conversation_control_message(placeholder) is True


class TestSummarizationCutoff:
    """Test suite or helper class for test summarization cutoff."""
    def test_valid_cutoff(self):
        """Verify valid cutoff."""
        event = {"cutoff_index": 5}
        assert validated_summarization_cutoff(event, message_count=10) == 5
        assert summarization_cutoff(event, message_count=10) == 5

    def test_cutoff_past_message_count_rejected(self):
        """Verify cutoff past message count rejected."""
        event = {"cutoff_index": 12}
        assert validated_summarization_cutoff(event, message_count=10) is None
        assert summarization_cutoff(event, message_count=10) == 0

    def test_malformed_event(self):
        """Verify malformed event."""
        assert validated_summarization_cutoff("not a dict") is None
        assert validated_summarization_cutoff({"cutoff_index": -1}) is None
        assert validated_summarization_cutoff({"cutoff_index": True}) is None
        assert summarization_cutoff("not a dict") == 0

    def test_log_malformed_summarization_event(self):
        # Should not raise
        """Verify log malformed summarization event."""
        log_malformed_summarization_event({"cutoff_index": "invalid"}, message_count=5)
        log_malformed_summarization_event("unexpected string", message_count=5)


class TestBuildGoalStateNotice:
    """Test suite or helper class for test build goal state notice."""
    def test_embedded_objective_and_criteria(self):
        """Verify embedded objective and criteria."""
        state = {
            "_goal_objective": "Deploy ECS service to production",
            "_goal_rubric": "- Tasks are healthy\n- ALB target group 200 OK",
            "_goal_status": "active",
        }
        notice = build_goal_state_notice(state)
        assert notice is not None
        assert is_goal_state_message(notice) is True
        assert "<goal_objective>Deploy ECS service to production</goal_objective>" in notice.content
        assert "<acceptance_criteria>- Tasks are healthy\n- ALB target group 200 OK</acceptance_criteria>" in notice.content
        assert "- Goal status: active" in notice.content
        assert "- Goal actionable: yes" in notice.content
        assert "- Rubric active: yes" in notice.content

    def test_oversized_state_graceful_degradation(self):
        """Verify oversized state graceful degradation."""
        oversized_text = "x" * (GOAL_APPLICATION_CHAR_LIMIT + 100)
        state = {
            "_goal_objective": oversized_text,
            "_goal_rubric": "- Valid criteria",
            "_goal_status": "active",
        }
        notice = build_goal_state_notice(state)
        assert notice is not None
        # Must suppress sensitive oversized details and mark status as unavailable
        assert "- Goal status: unavailable" in notice.content
        assert "- Goal actionable: no" in notice.content
        assert "- Rubric active: no" in notice.content
        assert "<goal_objective>" not in notice.content
        assert "Saved goal/rubric state is too large to include safely" in notice.content

    def test_latest_goal_state_notice_and_index(self):
        """Verify latest goal state notice and index."""
        state = {
            "_goal_objective": "Test objective",
            "_goal_status": "active",
        }
        notice = build_goal_state_notice(state)
        messages = [
            HumanMessage(content="User prompt"),
            notice,
            AIMessage(content="Working..."),
        ]
        assert latest_goal_state_message_index(messages) == 1
        found = latest_goal_state_notice(messages)
        assert found is not None
        index, info = found
        assert index == 1
        assert info["schema_version"] == GOAL_MESSAGE_SCHEMA_VERSION
        assert info["state_fingerprint"] == goal_state_fingerprint(state)


class TestGoalContinuation:
    """Test suite or helper class for test goal continuation."""
    def test_created_persisted(self):
        """Verify created persisted."""
        msg = build_goal_continuation("created")
        assert "The accepted goal state is saved." in msg.content
        assert "goal/rubric state notice" in msg.content

    def test_created_unsaved_fallback(self):
        """Verify created unsaved fallback."""
        msg = build_goal_continuation(
            "created",
            unsaved_objective="Fix VPC routing",
            unsaved_criteria="- Route table has 0.0.0.0/0 to igw",
        )
        assert "Fix VPC routing" in msg.content
        assert "Route table has 0.0.0.0/0 to igw" in msg.content
        assert latest_human_is_unsaved_goal_continuation([msg]) is True


class TestGoalPlanNoticeAndTodoSeeding:
    """Test suite or helper class for test goal plan notice and todo seeding."""
    def test_notice_embeds_implementation_plan_when_active(self):
        """Verify notice embeds implementation plan when active."""
        state = {
            "_goal_objective": "Upgrade EKS cluster to 1.31",
            "_goal_plan": "## Summary\nUpgrade control plane then managed node groups.",
            "_goal_rubric": "- Control plane version is 1.31\n- All nodes Ready",
            "_goal_status": "active",
        }
        notice = build_goal_state_notice(state)
        assert "<implementation_plan>## Summary\nUpgrade control plane then managed node groups.</implementation_plan>" in notice.content

    def test_goal_tools_middleware_auto_seeds_todos_once(self):
        """Verify goal tools middleware auto seeds todos once."""
        from talkops.middleware.goal_tools import GoalToolsMiddleware

        state: Any = {
            "messages": [HumanMessage(content="Start goal")],
            "_goal_objective": "Upgrade EKS cluster to 1.31",
            "_goal_status": "active",
            "_goal_todos": [
                "Inspect current EKS version and addon compatibility",
                "Upgrade EKS control plane to 1.31",
                "Verify node readiness and pod health",
            ],
        }
        update = GoalToolsMiddleware._notice_update(state)
        assert update is not None
        assert "todos" in update
        assert update["todos"] == [
            {"content": "Inspect current EKS version and addon compatibility", "status": "in_progress"},
            {"content": "Upgrade EKS control plane to 1.31", "status": "pending"},
            {"content": "Verify node readiness and pod health", "status": "pending"},
        ]

        # Once todos are already populated in state, _notice_update must not overwrite them
        progressed_todos = [
            {"content": "Inspect current EKS version and addon compatibility", "status": "completed"},
            {"content": "Upgrade EKS control plane to 1.31", "status": "in_progress"},
            {"content": "Verify node readiness and pod health", "status": "pending"},
        ]
        state_with_todos: Any = {
            **state,
            "messages": [*state["messages"], *update["messages"]],
            "todos": progressed_todos,
        }
        second_update = GoalToolsMiddleware._notice_update(state_with_todos)
        assert second_update is None or "todos" not in second_update


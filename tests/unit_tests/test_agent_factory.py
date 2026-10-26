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

"""Unit tests for TalkOps agent factory and LangGraph compilation."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from talkops.agent.factory import create_talkops_agent
from talkops.backend.composite import TalkOpsCompositeBackend


def test_create_talkops_agent_compilation(tmp_path):
    # Mock chat model
    """Verify create talkops agent compilation."""
    mock_model = MagicMock(spec=BaseChatModel)
    mock_model.bind_tools = MagicMock(return_value=mock_model)
    mock_model.profile = {}

    agent_graph, backend = create_talkops_agent(
        model=mock_model,
        cwd=tmp_path,
        interactive=False,
        auto_approve=True,
    )

    assert agent_graph is not None
    assert isinstance(backend, TalkOpsCompositeBackend)
    assert hasattr(agent_graph, "invoke")
    assert hasattr(agent_graph, "astream")


def test_create_talkops_agent_with_project_context(tmp_path):
    """Verify create talkops agent with project context."""
    from talkops.project_utils import ProjectContext

    mock_model = MagicMock(spec=BaseChatModel)
    mock_model.bind_tools = MagicMock(return_value=mock_model)
    mock_model.profile = {}

    user_dir = tmp_path / "custom_worktree"
    user_dir.mkdir()
    p_ctx = ProjectContext.from_user_cwd(user_dir)

    agent_graph, backend = create_talkops_agent(
        model=mock_model,
        project_context=p_ctx,
        interactive=False,
        auto_approve=True,
    )

    assert agent_graph is not None
    # Verify the backend's default cwd matches project_context.user_cwd
    default_backend = getattr(backend, "default", None)
    assert default_backend is not None
    assert Path(default_backend.cwd).resolve() == user_dir.resolve()


@pytest.mark.asyncio
async def test_create_talkops_agent_webhook_sre_vs_coding_prompt(tmp_path):
    """Verify create talkops agent webhook sre vs coding prompt."""
    from talkops.agent.config import CLIContextSchema

    captured_messages: list[list[BaseMessage]] = []

    class _RecordingChatModel(BaseChatModel):
        model_name: str = "gpt-5.6-luna"

        @property
        def _llm_type(self) -> str:
            return "openai-chat"

        def bind_tools(self, tools, **kwargs):
            """Provide bind tools test helper or fixture."""
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            captured_messages.append(list(messages))
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="Completed analysis."))])

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            captured_messages.append(list(messages))
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="Completed analysis."))])

    model = _RecordingChatModel()
    agent_graph, _ = create_talkops_agent(
        model=model,
        cwd=tmp_path,
        interactive=False,
        auto_approve=True,
    )

    # 1. Normal non-webhook invocation -> retains Dual-Engine Coding + Cloud Ops prompt
    await agent_graph.ainvoke(
        {"messages": [{"role": "user", "content": "Help me refactor main.tf"}]},
        context=CLIContextSchema.from_payload({"thread_id": "t-coding", "interactive": True}),
    )
    assert len(captured_messages) == 1
    coding_sys_prompt = str(captured_messages[0][0].content)
    assert "Engine 1: Platform Engineering & DevOps Coding Specialist" in coding_sys_prompt
    assert "Platform Engineering & DevOps Coding Discipline" in coding_sys_prompt

    # 2. Webhook / incident channel invocation -> swaps base prompt to Autonomous SRE & RCA
    captured_messages.clear()
    await agent_graph.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "[WEBHOOK ALERT] Provider: ALERTMANAGER | Event: alertmanager.firing\nPod OOMKilled",
                }
            ]
        },
        context=CLIContextSchema.from_payload(
            {
                "thread_id": "t-webhook-sre",
                "interactive": False,
                "source_addon": "webhooks",
                "webhook_provider": "alertmanager",
                "webhook_event_type": "alertmanager.firing",
                "webhook_incident_id": "inc-99",
            }
        ),
    )
    assert len(captured_messages) == 1
    sre_sys_prompt = str(captured_messages[0][0].content)
    assert "Autonomous SRE & Cloud Incident Root Cause Analysis (RCA) Orchestrator" in sre_sys_prompt
    assert "Engine 1: Platform Engineering & DevOps Coding Specialist" not in sre_sys_prompt
    assert "Platform Engineering & DevOps Coding Discipline" not in sre_sys_prompt
    assert "### Active Webhook / Incident Channel Context" in sre_sys_prompt
    # Downstream middleware sections remain attached in exact order
    assert "## Shell paths vs. virtual paths" in sre_sys_prompt
    assert "<conversation_transcript>" in sre_sys_prompt
    assert "## Local Context" in sre_sys_prompt
    assert "## Non-Blocking Background Command Execution" in sre_sys_prompt
    assert "## Subagent Delegation & Orchestration Architecture" in sre_sys_prompt


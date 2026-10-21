"""Integration tests for TalkOps agent graph compilation."""

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from talkops.agent.factory import create_talkops_agent


def test_agent_graph_compilation(tmp_path):
    # Fake chat model with tool calling support capability
    model = FakeListChatModel(responses=["Hello from TalkOps!"])
    agent, backend = create_talkops_agent(
        model=model,
        cwd=tmp_path,
        interactive=False,
    )
    assert agent is not None
    assert backend is not None

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

"""Integration tests for TalkOps agent graph compilation."""

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from talkops.agent.factory import create_talkops_agent


def test_agent_graph_compilation(tmp_path):
    # Fake chat model with tool calling support capability
    """Verify agent graph compilation."""
    model = FakeListChatModel(responses=["Hello from TalkOps!"])
    agent, backend = create_talkops_agent(
        model=model,
        cwd=tmp_path,
        interactive=False,
    )
    assert agent is not None
    assert backend is not None

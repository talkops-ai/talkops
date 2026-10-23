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

"""Unit tests for ResumeStateMiddleware context token persistence."""
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from talkops.middleware.resume_state import (
    ResumeStateMiddleware,
    _extract_context_tokens,
)


def test_extract_context_tokens():
    """Verify extract context tokens."""
    msg = AIMessage(
        content="Testing",
        usage_metadata={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
    )
    assert _extract_context_tokens(msg) == 150

    msg_no_usage = AIMessage(content="No tokens")
    assert _extract_context_tokens(msg_no_usage) is None

    msg_only_total = AIMessage(
        content="Total only",
        usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 88},
    )
    assert _extract_context_tokens(msg_only_total) == 88


def test_resume_state_middleware_after_model():
    """Verify resume state middleware after model."""
    mw = ResumeStateMiddleware()
    runtime = MagicMock()

    msg1 = HumanMessage(content="Hello")
    msg2 = AIMessage(
        content="Hi there",
        usage_metadata={"input_tokens": 200, "output_tokens": 50, "total_tokens": 250},
    )
    state: Any = {"messages": [msg1, msg2]}

    update = mw.after_model(state, runtime)
    assert update is not None
    assert update["_context_tokens"] == 250


def test_resume_state_middleware_empty_messages():
    """Verify resume state middleware empty messages."""
    mw = ResumeStateMiddleware()
    runtime = MagicMock()

    empty_state: Any = {}
    empty_msgs_state: Any = {"messages": []}
    assert mw.after_model(empty_state, runtime) is None
    assert mw.after_model(empty_msgs_state, runtime) is None


@pytest.mark.asyncio
async def test_resume_state_middleware_aafter_model():
    """Verify resume state middleware aafter model."""
    mw = ResumeStateMiddleware()
    runtime = MagicMock()

    msg = AIMessage(
        content="Async message",
        usage_metadata={"input_tokens": 400, "output_tokens": 100, "total_tokens": 500},
    )
    state: Any = {"messages": [msg]}

    update = await mw.aafter_model(state, runtime)
    assert update is not None
    assert update["_context_tokens"] == 500

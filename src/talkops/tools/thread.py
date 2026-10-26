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

"""Runtime thread identification tool for TalkOps."""

from __future__ import annotations

from langchain_core.tools import tool
from langgraph.config import get_config

from talkops.utils.logger import get_logger

logger = get_logger(__name__)


@tool
def get_current_thread_id() -> str:
    """Get the current TalkOps thread ID for LangSmith or MCP tooling.

    Returns:
        The current `configurable.thread_id`, or an explanatory message if missing.
    """
    try:
        from langgraph.config import get_config

        thread_id = get_config().get("configurable", {}).get("thread_id")
    except Exception:
        thread_id = None

    if isinstance(thread_id, str) and thread_id:
        return thread_id
    return "No current thread ID is available."




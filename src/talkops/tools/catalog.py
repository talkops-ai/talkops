"""Catalog of built-in tools for TalkOps."""

from __future__ import annotations

from typing import Any

from talkops.tools.cloud_tools import (
    cloud_get_status,
    cloud_list_contexts,
    cloud_switch_context,
)
from talkops.tools.fetch_url import fetch_url
from talkops.tools.goal_tools import (
    get_goal,
    get_rubric,
    update_goal,
)
from talkops.tools.registry import ToolRegistry
from talkops.tools.thread import get_current_thread_id
from talkops.tools.web_search import create_web_search_tool, web_search


def register_all_tools() -> None:
    """Register all available tools with the ToolRegistry."""
    registry = ToolRegistry.get_instance()

    # Core general-purpose tools
    def _build_web_search(**kwargs: Any):
        api_key = kwargs.get("api_key")
        if api_key and isinstance(api_key, str):
            return create_web_search_tool(api_key)
        return web_search

    registry.register("web_search", _build_web_search, category="web")
    registry.register("fetch_url", lambda **kwargs: fetch_url, category="web")
    registry.register("get_current_thread_id", lambda **kwargs: get_current_thread_id, category="system")

    # Multi-Cloud Context & Authentication tools
    registry.register("cloud_list_contexts", lambda **kwargs: cloud_list_contexts, category="cloud")
    registry.register("cloud_switch_context", lambda **kwargs: cloud_switch_context, category="cloud")
    registry.register("cloud_get_status", lambda **kwargs: cloud_get_status, category="cloud")

    # Goal and Rubric tools
    registry.register("get_rubric", lambda **kwargs: get_rubric, category="goal")
    registry.register("get_goal", lambda **kwargs: get_goal, category="goal")
    registry.register("update_goal", lambda **kwargs: update_goal, category="goal")


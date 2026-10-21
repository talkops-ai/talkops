"""Subagents module for TalkOps.

Provides subagent metadata types, filesystem discovery, and dynamic agent plugin loading.
"""

from talkops.subagents.loader import (
    get_built_in_subagents,
    list_subagents,
    list_subagents_async,
    load_async_subagents,
    load_async_subagents_async,
    parse_subagent_file,
)
from talkops.subagents.subagents_parser import (
    parse_built_in_subagents,
    parse_subagent_bundle,
)
from talkops.subagents.types import SubagentMetadata

__all__ = [
    "SubagentMetadata",
    "get_built_in_subagents",
    "list_subagents",
    "list_subagents_async",
    "load_async_subagents",
    "load_async_subagents_async",
    "parse_built_in_subagents",
    "parse_subagent_bundle",
    "parse_subagent_file",
]

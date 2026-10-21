"""Utility modules for TalkOps."""

from talkops.utils.cost_estimation import TokenCostEstimator, estimate_cost
from talkops.utils.git import find_git_dir, get_git_branch, get_git_remote_url, get_git_root
from talkops.utils.logger import AgentLogger, get_logger
from talkops.utils.session_stats import ModelStats, SessionStats, format_cost, format_token_count
from talkops.utils.startup_error import STARTUP_ERROR_MARKER, emit_startup_failure

__all__ = [
    "STARTUP_ERROR_MARKER",
    "AgentLogger",
    "ModelStats",
    "SessionStats",
    "TokenCostEstimator",
    "emit_startup_failure",
    "estimate_cost",
    "find_git_dir",
    "format_cost",
    "format_token_count",
    "get_git_branch",
    "get_git_remote_url",
    "get_git_root",
    "get_logger",
]

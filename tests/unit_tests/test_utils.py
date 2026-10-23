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

"""Unit tests for TalkOps utils (git metadata, cost estimation, session stats)."""

from pathlib import Path

import pytest

from talkops.utils.cost_estimation import TokenCostEstimator
from talkops.utils.git import (
    RepositoryMetadata,
    find_git_root,
    parse_repository_metadata,
    resolve_git_branch,
)
from talkops.utils.session_stats import SessionStats


def test_parse_repository_metadata():
    # SSH format
    """Verify parse repository metadata."""
    meta = parse_repository_metadata("git@github.com:talkops-ai/talkops.git")
    assert meta is not None
    assert meta.provider == "github"
    assert meta.name == "talkops-ai/talkops"
    assert meta.url == "https://github.com/talkops-ai/talkops"

    # HTTPS format
    meta_https = parse_repository_metadata("https://gitlab.com/group/subproject.git")
    assert meta_https is not None
    assert meta_https.provider == "gitlab"
    assert meta_https.name == "group/subproject"
    assert meta_https.url == "https://gitlab.com/group/subproject"

    # Invalid
    assert parse_repository_metadata("") is None
    assert parse_repository_metadata("not-a-valid-url") is None


def test_git_root_and_branch_in_current_repo():
    """Verify git root and branch in current repo."""
    root = find_git_root(Path.cwd())
    assert root is not None
    assert root.exists()

    branch = resolve_git_branch(Path.cwd())
    assert branch is not None
    assert len(branch) > 0


def test_session_stats():
    """Verify session stats."""
    stats = SessionStats()
    stats.record_request("gpt-4o", input_toks=100, output_toks=50, provider="openai", cost_usd=0.005)
    assert stats.request_count == 1
    assert stats.input_tokens == 100
    assert stats.output_tokens == 50
    assert stats.total_cost_usd == 0.005


def test_token_cost_estimator():
    """Verify token cost estimator."""
    assert isinstance(TokenCostEstimator.is_available(), bool)

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

"""Unit tests for rubric CLI resolution, parser arguments, and flag validation."""

import tempfile
from pathlib import Path

import pytest

from talkops.cli.main import _resolve_rubric_text, build_parser, main
from talkops.state.goal_state_limits import RUBRIC_CHAR_LIMIT, GoalStateSizeError


def test_resolve_rubric_text_none():
    """Verify resolve rubric text none."""
    assert _resolve_rubric_text(None) is None


def test_resolve_rubric_text_literal():
    """Verify resolve rubric text literal."""
    text = "- Terraform plan succeeded\n- KMS key is enabled"
    assert _resolve_rubric_text(text) == text


def test_resolve_rubric_text_whitespace():
    """Verify resolve rubric text whitespace."""
    with pytest.raises(ValueError, match="must not be empty"):
        _resolve_rubric_text("   \n\t  ")


def test_resolve_rubric_text_at_path():
    """Verify resolve rubric text at path."""
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
        f.write("- CloudWatch alarms are created\n- SNS subscription confirmed")
        f.flush()
        file_path = f.name

    try:
        resolved = _resolve_rubric_text(f"@{file_path}")
        assert resolved is not None
        assert "- CloudWatch alarms are created" in resolved
    finally:
        Path(file_path).unlink(missing_ok=True)


def test_resolve_rubric_text_missing_file():
    """Verify resolve rubric text missing file."""
    with pytest.raises(ValueError, match="Could not read rubric file"):
        _resolve_rubric_text("@/tmp/nonexistent_rubric_file_xyz_123.md")


def test_resolve_rubric_text_empty_file():
    """Verify resolve rubric text empty file."""
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
        f.write("   \n")
        f.flush()
        file_path = f.name

    try:
        with pytest.raises(ValueError, match="is empty"):
            _resolve_rubric_text(f"@{file_path}")
    finally:
        Path(file_path).unlink(missing_ok=True)


def test_resolve_rubric_text_oversized():
    """Verify resolve rubric text oversized."""
    oversized = "x" * (RUBRIC_CHAR_LIMIT + 1)
    with pytest.raises(GoalStateSizeError):
        _resolve_rubric_text(oversized)


def test_parser_rubric_arguments():
    """Verify parser rubric arguments."""
    parser = build_parser()
    args = parser.parse_args([
        "-p", "Migrate DynamoDB table",
        "--rubric", "- Point in time recovery is enabled",
        "--rubric-model", "anthropic:claude-3-5-haiku",
        "--rubric-max-iterations", "5",
    ])
    assert args.prompt == "Migrate DynamoDB table"
    assert args.rubric == "- Point in time recovery is enabled"
    assert args.rubric_model == "anthropic:claude-3-5-haiku"
    assert args.rubric_max_iterations == 5


def test_main_conflicting_goal_and_rubric(capsys):
    """Verify main conflicting goal and rubric."""
    ret = main([
        "--goal", "Interactive goal",
        "--rubric", "Headless rubric",
    ])
    assert ret == 1
    captured = capsys.readouterr()
    assert "Cannot specify both --goal and --rubric" in captured.err


def test_main_rubric_without_prompt(capsys):
    """Verify main rubric without prompt."""
    ret = main([
        "--rubric", "Headless rubric",
    ])
    assert ret == 1
    captured = capsys.readouterr()
    assert "--rubric requires -p/--prompt" in captured.err

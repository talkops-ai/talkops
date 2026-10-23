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

"""Unit tests for rubric grader inspection tools."""

import tempfile
from pathlib import Path

import pytest

from talkops.rubrics.evaluator import _create_rubric_grader_tools


def test_grader_tools_creation():
    """Verify grader tools creation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "main.tf"
        test_file.write_text('resource "aws_s3_bucket" "test" {\n  bucket = "my-bucket"\n}\n')

        tools = _create_rubric_grader_tools(repository_root=tmpdir)
        tool_names = {t.name for t in tools}
        assert "read_file" in tool_names
        assert "ls" in tool_names
        assert "glob" in tool_names
        assert "grep" in tool_names

        read_tool = next(t for t in tools if t.name == "read_file")

        # 1. Successful read of workspace file
        result = read_tool.invoke({"file_path": str(test_file)})
        assert "aws_s3_bucket" in result
        assert "my-bucket" in result

        # 2. Path traversal rejection
        traversal_result = read_tool.invoke({"file_path": "/etc/shadow"})
        assert "cannot read path" in traversal_result.lower() or "denied" in traversal_result.lower() or "invalid" in traversal_result.lower()

        # 3. Offload artifact path check
        offload_result = read_tool.invoke({"file_path": "artifact://nonexistent_id"})
        assert "cannot read path" in offload_result.lower() or "not found" in offload_result.lower() or "artifact" in offload_result.lower()

        # 4. Grep tool
        grep_tool = next(t for t in tools if t.name == "grep")
        grep_res = grep_tool.invoke({"pattern": "aws_s3_bucket"})
        assert "main.tf" in grep_res

        # 5. Glob tool
        glob_tool = next(t for t in tools if t.name == "glob")
        glob_res = glob_tool.invoke({"pattern": "*.tf"})
        assert "main.tf" in glob_res

        # 6. Ls tool
        ls_tool = next(t for t in tools if t.name == "ls")
        ls_res = ls_tool.invoke({})
        assert "main.tf" in ls_res

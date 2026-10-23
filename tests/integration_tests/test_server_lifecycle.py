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

"""Integration tests for ServerConfig and langgraph.json generation."""

from pathlib import Path

from talkops.server import ServerConfig, generate_langgraph_json


def test_server_config_roundtrip():
    """Verify server config roundtrip."""
    cfg = ServerConfig(
        model="anthropic:claude-3-5-sonnet-latest",
        interactive=True,
        approval_mode="auto",
    )
    env = cfg.to_env()
    assert "TALKOPS_SERVER_INTERACTIVE" in env
    assert env["TALKOPS_SERVER_INTERACTIVE"] == "true"


def test_generate_langgraph_json(tmp_path: Path):
    """Verify generate langgraph json."""
    target = generate_langgraph_json(tmp_path)
    assert target.exists()
    assert target.name == "langgraph.json"
    content = target.read_text(encoding="utf-8")
    assert "server_graph.py:make_graph" in content

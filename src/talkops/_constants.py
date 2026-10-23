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

"""Global constant definitions for talkops."""

from pathlib import Path

DEFAULT_AGENT_NAME = "talkops"
DEFAULT_ASSISTANT_ID = "talkops"

# App directories
USER_HOME = Path.home()
TALKOPS_HOME = USER_HOME / ".talkops"
STATE_DIR = TALKOPS_HOME / ".state"
SESSIONS_DB_NAME = "sessions.db"
SESSIONS_DB_PATH = STATE_DIR / SESSIONS_DB_NAME
SERVER_RUNTIME_DIR = STATE_DIR / "server"
CONFIG_FILE_PATH = TALKOPS_HOME / "config.toml"
PLUGINS_CACHE_DIR = TALKOPS_HOME / "plugins" / "cache"

# Environment variables prefixes
ENV_PREFIX = "TALKOPS_"
SERVER_ENV_PREFIX = "TALKOPS_SERVER_"

# Tooling and shell execution
SHELL_TIMEOUT_SECONDS = 60
DEFAULT_PORT = 0  # Ephemeral
DEFAULT_CONTROL_PLANE_URL = "https://console.talkops.ai"

# Large tool results prefix (used by rubrics and compaction)
LARGE_TOOL_RESULTS_PREFIX = "/large_tool_results/"
SYSTEM_MESSAGE_PREFIX = "[System Message]"

FS_TOOL_NAMES = frozenset(
    {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute", "run_command"}
)

READONLY_FS_TOOLS = frozenset(
    {
        "ls",
        "read_file",
        "glob",
        "grep",
        "view_file",
        "list_dir",
        "dir_list",
        "grep_search",
        "file_search",
        "read_url_content",
        "fetch_web_page",
        "search_web",
        "get_goal",
        "get_rubric",
        "update_goal",
        "write_todos",
        "cloud_list_contexts",
        "cloud_get_status",
    }
)

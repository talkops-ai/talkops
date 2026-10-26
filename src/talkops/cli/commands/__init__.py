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

"""CLI subcommands."""

from talkops.cli.commands.auth import run_auth_command
from talkops.cli.commands.doctor import run_doctor_command
from talkops.cli.commands.mcp import run_mcp_command, setup_mcp_parser
from talkops.cli.commands.serve import run_serve_command, setup_serve_parser
from talkops.cli.commands.threads import run_threads_command

__all__ = [
    "run_auth_command",
    "run_doctor_command",
    "run_mcp_command",
    "run_serve_command",
    "run_threads_command",
    "setup_mcp_parser",
    "setup_serve_parser",
]


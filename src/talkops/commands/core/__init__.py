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

"""Core essential command handlers package."""

from talkops.commands.core.auth import LoginHandler, LogoutHandler
from talkops.commands.core.bug import BugHandler
from talkops.commands.core.clear import ClearHandler, ForceClearHandler
from talkops.commands.core.cloud import CloudHandler
from talkops.commands.core.compact import CompactHandler
from talkops.commands.core.config_cmd import ConfigHandler
from talkops.commands.core.context import ContextHandler
from talkops.commands.core.cost import CostHandler
from talkops.commands.core.doctor import DoctorHandler
from talkops.commands.core.effort import EffortHandler
from talkops.commands.core.exit_cmd import ExitHandler
from talkops.commands.core.fast import FastHandler
from talkops.commands.core.help_cmd import HelpHandler
from talkops.commands.core.mcp import McpHandler
from talkops.commands.core.model import ModelHandler
from talkops.commands.core.permissions import PermissionsHandler
from talkops.commands.core.plugins import PluginsHandler
from talkops.commands.core.pool import PoolHandler
from talkops.commands.core.resume import ResumeHandler
from talkops.commands.core.skills import SkillsHandler

__all__ = [
    "BugHandler",
    "ClearHandler",
    "CloudHandler",
    "CompactHandler",
    "ConfigHandler",
    "ContextHandler",
    "CostHandler",
    "DoctorHandler",
    "EffortHandler",
    "ExitHandler",
    "FastHandler",
    "ForceClearHandler",
    "HelpHandler",
    "LoginHandler",
    "LogoutHandler",
    "McpHandler",
    "ModelHandler",
    "PermissionsHandler",
    "PluginsHandler",
    "PoolHandler",
    "ResumeHandler",
    "SkillsHandler",
]

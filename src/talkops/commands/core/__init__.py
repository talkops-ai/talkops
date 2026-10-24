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

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "LoginHandler": ("talkops.commands.core.auth", "LoginHandler"),
    "LogoutHandler": ("talkops.commands.core.auth", "LogoutHandler"),
    "BugHandler": ("talkops.commands.core.bug", "BugHandler"),
    "ClearHandler": ("talkops.commands.core.clear", "ClearHandler"),
    "ForceClearHandler": ("talkops.commands.core.clear", "ForceClearHandler"),
    "CloudHandler": ("talkops.commands.core.cloud", "CloudHandler"),
    "CompactHandler": ("talkops.commands.core.compact", "CompactHandler"),
    "ConfigHandler": ("talkops.commands.core.config_cmd", "ConfigHandler"),
    "ContextHandler": ("talkops.commands.core.context", "ContextHandler"),
    "CostHandler": ("talkops.commands.core.cost", "CostHandler"),
    "DoctorHandler": ("talkops.commands.core.doctor", "DoctorHandler"),
    "EffortHandler": ("talkops.commands.core.effort", "EffortHandler"),
    "ExitHandler": ("talkops.commands.core.exit_cmd", "ExitHandler"),
    "FastHandler": ("talkops.commands.core.fast", "FastHandler"),
    "HelpHandler": ("talkops.commands.core.help_cmd", "HelpHandler"),
    "McpHandler": ("talkops.commands.core.mcp", "McpHandler"),
    "ModelHandler": ("talkops.commands.core.model", "ModelHandler"),
    "PermissionsHandler": ("talkops.commands.core.permissions", "PermissionsHandler"),
    "PluginsHandler": ("talkops.commands.core.plugins", "PluginsHandler"),
    "PoolHandler": ("talkops.commands.core.pool", "PoolHandler"),
    "ResumeHandler": ("talkops.commands.core.resume", "ResumeHandler"),
    "SkillsHandler": ("talkops.commands.core.skills", "SkillsHandler"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_IMPORTS:
        module_name, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_name)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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

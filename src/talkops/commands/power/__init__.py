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

"""Power user command handlers package."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from talkops.commands.power.agents import AgentsHandler
    from talkops.commands.power.btw import BtwHandler
    from talkops.commands.power.copy import CopyHandler
    from talkops.commands.power.goal import GoalHandler
    from talkops.commands.power.loop import LoopHandler
    from talkops.commands.power.memory import MemoryHandler
    from talkops.commands.power.review import ReviewHandler
    from talkops.commands.power.rubric import RubricHandler
    from talkops.commands.power.runtime import (
        AutoUpdateHandler,
        InstallHandler,
        ReloadHandler,
        RestartHandler,
        UpdateHandler,
    )
    from talkops.commands.power.skill_creator import SkillCreatorHandler
    from talkops.commands.power.skill_invoke import SkillInvokeHandler
    from talkops.commands.power.tasks import TasksHandler
    from talkops.commands.power.trace import TraceHandler
    from talkops.commands.power.ui_toggles import (
        NotificationsHandler,
        ScrollbarHandler,
        TimestampsHandler,
    )
    from talkops.commands.power.version import VersionHandler

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "AgentsHandler": ("talkops.commands.power.agents", "AgentsHandler"),
    "BtwHandler": ("talkops.commands.power.btw", "BtwHandler"),
    "CopyHandler": ("talkops.commands.power.copy", "CopyHandler"),
    "GoalHandler": ("talkops.commands.power.goal", "GoalHandler"),
    "LoopHandler": ("talkops.commands.power.loop", "LoopHandler"),
    "MemoryHandler": ("talkops.commands.power.memory", "MemoryHandler"),
    "ReviewHandler": ("talkops.commands.power.review", "ReviewHandler"),
    "RubricHandler": ("talkops.commands.power.rubric", "RubricHandler"),
    "AutoUpdateHandler": ("talkops.commands.power.runtime", "AutoUpdateHandler"),
    "InstallHandler": ("talkops.commands.power.runtime", "InstallHandler"),
    "ReloadHandler": ("talkops.commands.power.runtime", "ReloadHandler"),
    "RestartHandler": ("talkops.commands.power.runtime", "RestartHandler"),
    "UpdateHandler": ("talkops.commands.power.runtime", "UpdateHandler"),
    "SkillCreatorHandler": ("talkops.commands.power.skill_creator", "SkillCreatorHandler"),
    "SkillInvokeHandler": ("talkops.commands.power.skill_invoke", "SkillInvokeHandler"),
    "TasksHandler": ("talkops.commands.power.tasks", "TasksHandler"),
    "TraceHandler": ("talkops.commands.power.trace", "TraceHandler"),
    "NotificationsHandler": ("talkops.commands.power.ui_toggles", "NotificationsHandler"),
    "ScrollbarHandler": ("talkops.commands.power.ui_toggles", "ScrollbarHandler"),
    "TimestampsHandler": ("talkops.commands.power.ui_toggles", "TimestampsHandler"),
    "VersionHandler": ("talkops.commands.power.version", "VersionHandler"),
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
    "AgentsHandler",
    "AutoUpdateHandler",
    "BtwHandler",
    "CopyHandler",
    "GoalHandler",
    "InstallHandler",
    "LoopHandler",
    "MemoryHandler",
    "NotificationsHandler",
    "ReloadHandler",
    "RestartHandler",
    "ReviewHandler",
    "RubricHandler",
    "ScrollbarHandler",
    "SkillCreatorHandler",
    "SkillInvokeHandler",
    "TasksHandler",
    "TimestampsHandler",
    "TraceHandler",
    "UpdateHandler",
    "VersionHandler",
]

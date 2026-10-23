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

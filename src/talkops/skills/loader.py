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

"""Compatibility shim for talkops.skills.loader -> talkops.skills.load."""

from __future__ import annotations

from talkops.skills.load import (
    ExtendedSkillMetadata,
    SkillMetadata,
    _parse_skill_file,
    get_skill_by_name,
    get_skill_content_by_name,
    list_skills,
    load_skill_content,
)

__all__ = [
    "ExtendedSkillMetadata",
    "SkillMetadata",
    "_parse_skill_file",
    "get_skill_by_name",
    "get_skill_content_by_name",
    "list_skills",
    "load_skill_content",
]

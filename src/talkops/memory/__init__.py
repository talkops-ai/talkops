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

"""Persistent memory, onboarding wizard, and storage for talkops."""

from talkops.memory.branch import BranchMemoryEntry, BranchMemoryStore
from talkops.memory.guard import ManagedMemoryGuardMiddleware
from talkops.memory.onboarding import (
    ONBOARDING_NAME_MEMORY_END,
    ONBOARDING_NAME_MEMORY_START,
    extract_onboarding_name_block,
    run_onboarding_if_needed,
    strip_onboarding_name_markers,
)
from talkops.memory.registry import MemoryRegistry
from talkops.memory.store import MemoryEntry, MemoryStore

__all__ = [
    "MemoryRegistry",
    "ManagedMemoryGuardMiddleware",
    "MemoryStore",
    "MemoryEntry",
    "BranchMemoryStore",
    "BranchMemoryEntry",
    "run_onboarding_if_needed",
    "extract_onboarding_name_block",
    "strip_onboarding_name_markers",
    "ONBOARDING_NAME_MEMORY_START",
    "ONBOARDING_NAME_MEMORY_END",
]

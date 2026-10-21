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

"""Skills module for TalkOps.

Public CLI API:
- execute_skills_command: Execute skills subcommands (list/create/info/delete/trust)
- setup_skills_parser: Setup argparse configuration for skills commands

Public runtime & loader API:
- list_skills: Discover available skills across built-in, plugin, user, project, and Claude sources
- load_skill_content: Read SKILL.md content with containment validation
- build_skill_invocation_envelope: Wrap skill instructions with structured invocation metadata
- discover_skills_and_roots: Discover skills and pre-resolved containment roots
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from talkops.skills.commands import (
    execute_skills_command,
    list_skills_command,
    setup_skills_parser,
    trust_skill_command,
)
from talkops.skills.merge import merge_skill
from talkops.skills.trust import (
    RevokeResult,
    SkillTrustStore,
    clear_trusted_skill_dirs,
    is_skill_dir_trusted,
    list_trusted_skill_dir_entries,
    list_trusted_skill_dirs,
    load_trusted_skill_dirs,
    revoke_skill_dir_trust,
    trust_skill_dir,
)

if TYPE_CHECKING:
    from talkops.skills.invocation import (
        MAX_SKILL_CONTENT_CHARS,
        STATIC_SKILL_ALIASES,
        SkillInvocationEnvelope,
        build_skill_invocation_envelope,
        discover_skills_and_roots,
        format_skill_slash_name,
        match_discovered_skill,
        parse_skill_command,
    )
    from talkops.skills.load import (
        ExtendedSkillMetadata,
        SkillMetadata,
        get_skill_by_name,
        get_skill_content_by_name,
        list_skills,
        load_skill_content,
    )
    from talkops.skills.registry import (
        SkillRegistry,
        SkillSource,
        get_skill_registry,
    )
    from talkops.skills.sources import (
        CodeSkillSource,
        DirectorySkillSource,
        PluginSkillSource,
        get_skill_sources,
    )

_LAZY_IMPORTS: dict[str, str] = {
    "MAX_SKILL_CONTENT_CHARS": "talkops.skills.invocation",
    "STATIC_SKILL_ALIASES": "talkops.skills.invocation",
    "SkillInvocationEnvelope": "talkops.skills.invocation",
    "build_skill_invocation_envelope": "talkops.skills.invocation",
    "discover_skills_and_roots": "talkops.skills.invocation",
    "format_skill_slash_name": "talkops.skills.invocation",
    "match_discovered_skill": "talkops.skills.invocation",
    "parse_skill_command": "talkops.skills.invocation",
    "ExtendedSkillMetadata": "talkops.skills.load",
    "SkillMetadata": "talkops.skills.load",
    "get_skill_by_name": "talkops.skills.load",
    "get_skill_content_by_name": "talkops.skills.load",
    "list_skills": "talkops.skills.load",
    "load_skill_content": "talkops.skills.load",
    "SkillRegistry": "talkops.skills.registry",
    "SkillSource": "talkops.skills.registry",
    "get_skill_registry": "talkops.skills.registry",
    "CodeSkillSource": "talkops.skills.sources",
    "DirectorySkillSource": "talkops.skills.sources",
    "PluginSkillSource": "talkops.skills.sources",
    "get_skill_sources": "talkops.skills.sources",
}


def __getattr__(name: str) -> Any:
    mod_name = _LAZY_IMPORTS.get(name)
    if mod_name is not None:
        mod = importlib.import_module(mod_name)
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "MAX_SKILL_CONTENT_CHARS",
    "STATIC_SKILL_ALIASES",
    "ExtendedSkillMetadata",
    "RevokeResult",
    "SkillInvocationEnvelope",
    "SkillMetadata",
    "SkillRegistry",
    "SkillSource",
    "SkillTrustStore",
    "build_skill_invocation_envelope",
    "clear_trusted_skill_dirs",
    "discover_skills_and_roots",
    "execute_skills_command",
    "format_skill_slash_name",
    "get_skill_by_name",
    "get_skill_content_by_name",
    "get_skill_registry",
    "get_skill_sources",
    "is_skill_dir_trusted",
    "list_skills",
    "list_skills_command",
    "list_trusted_skill_dir_entries",
    "list_trusted_skill_dirs",
    "load_skill_content",
    "load_trusted_skill_dirs",
    "match_discovered_skill",
    "merge_skill",
    "parse_skill_command",
    "revoke_skill_dir_trust",
    "setup_skills_parser",
    "trust_skill_command",
    "trust_skill_dir",
]

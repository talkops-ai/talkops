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

"""Skills middleware adapter supporting namespaced plugin skills."""

from __future__ import annotations

import asyncio
import fnmatch
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, cast

from deepagents.backends.protocol import FileInfo, LsResult
from deepagents.backends.utils import to_posix_path
from deepagents.middleware import skills as sdk_skills
from deepagents.middleware.skills import SkillsMiddleware
from langchain_core.messages import AIMessage

from talkops.middleware.registry import register_middleware
from talkops.plugins.adapters.skills import (
    SkillNamespace,
    namespaced_skill_name,
)

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol
    from langchain_core.runnables import RunnableConfig
    from langgraph.runtime import Runtime

    from talkops.skills.registry import SkillSourceTuple
    from talkops.skills.sources import CodeSkillSource

from talkops.utils.logger import get_logger

logger = get_logger(__name__)

_PLUGIN_SKILL_SOURCE_LENGTH = 3
_SKILL_FILE = "SKILL.md"


def _entries(ls_result: object) -> list[FileInfo]:
    if isinstance(ls_result, LsResult):
        return list(ls_result.entries or [])
    if isinstance(ls_result, list):
        return cast(list[FileInfo], ls_result)
    return []


def _child_dirs(entries: list[FileInfo], root: str) -> list[tuple[str, str]]:
    root_posix = PurePosixPath(to_posix_path(root))
    dirs: list[tuple[str, str]] = []
    for entry in entries:
        if not entry.get("is_dir"):
            continue
        path = entry["path"]
        name = PurePosixPath(to_posix_path(path)).name
        if PurePosixPath(to_posix_path(path)) == root_posix:
            continue
        dirs.append((name, path))
    return dirs


def _has_skill_file(entries: list[FileInfo], root: str) -> bool:
    root_posix = PurePosixPath(to_posix_path(root))
    for entry in entries:
        path = PurePosixPath(to_posix_path(entry["path"]))
        if path.name == _SKILL_FILE and path.parent == root_posix:
            return True
    return False


def _skill_md_path(skill_dir: str) -> str:
    return str(PurePosixPath(to_posix_path(skill_dir)) / _SKILL_FILE)


def _namespace_skill(
    skill: sdk_skills.SkillMetadata,
    namespace: SkillNamespace,
    subfolders: tuple[str, ...],
) -> sdk_skills.SkillMetadata:
    return cast(
        sdk_skills.SkillMetadata,
        {
            **skill,
            "name": namespaced_skill_name(namespace, skill["name"], subfolders),
        },
    )


def discover_skill_dirs(
    backend: BackendProtocol,
    source_path: str,
) -> list[tuple[str, tuple[str, ...]]]:
    """Recursively search for directories containing a SKILL.md definition under source_path.

    Args:
        backend: Storage backend providing directory listing capabilities.
        source_path: Root filesystem path to search.

    Returns:
        List of tuples mapping resolved skill directory path to relative path segments.
    """
    found: list[tuple[str, tuple[str, ...]]] = []
    source_root = Path(source_path).resolve()
    visited: set[Path] = set()
    stack: list[tuple[str, tuple[str, ...]]] = [(str(source_root), ())]
    while stack:
        current, path_segments = stack.pop()
        try:
            resolved = Path(current).resolve()
        except (OSError, RuntimeError):
            logger.warning("Could not resolve plugin skill directory %s", current)
            continue
        if not resolved.is_relative_to(source_root) or resolved in visited:
            continue
        visited.add(resolved)
        resolved_path = str(resolved)
        entries = _entries(backend.ls(resolved_path))
        if _has_skill_file(entries, resolved_path):
            found.append((resolved_path, path_segments[:-1]))
            continue
        for name, path in _child_dirs(entries, resolved_path):
            stack.append((path, (*path_segments, name)))
    return found


def load_namespaced_skills(
    backend: BackendProtocol,
    source_path: str,
    namespace: SkillNamespace,
) -> list[sdk_skills.SkillMetadata]:
    """Discover and parse all skill definitions within a source root under a given namespace.

    Args:
        backend: Storage backend providing file reading and listing.
        source_path: Root directory path containing skills.
        namespace: Skill namespace identifier for prefixing skill names.

    Returns:
        List of loaded and namespaced SkillMetadata objects.
    """
    skill_dirs = discover_skill_dirs(backend, source_path)
    if not skill_dirs:
        return []
    paths = [_skill_md_path(skill_dir) for skill_dir, _ in skill_dirs]
    responses = backend.download_files(paths)
    skills: list[sdk_skills.SkillMetadata] = []
    for (skill_dir, segments), path, response in zip(skill_dirs, paths, responses, strict=True):
        skill = sdk_skills._skill_metadata_from_response(response, skill_dir, path)
        if skill is not None:
            skills.append(_namespace_skill(skill, namespace, segments))
    return skills


CRITERIA_SKILLS_SYSTEM_PROMPT = """## Skills System & Domain Capabilities

TalkOps operates with a skills library providing domain knowledge and specialized cloud operations procedures. Use these capabilities to understand what criteria and verification targets are achievable:
{skills_locations}{skills_load_warnings}
**Available Skills:**

{skills_list}"""

_COMPACT_SKILLS_THRESHOLD = 0
_MAX_INLINE_SKILL_DESC_CHARS = 200


def _compact_skill_description(description: str, max_chars: int = _MAX_INLINE_SKILL_DESC_CHARS) -> str:
    """Return a concise progressive-disclosure summary of a skill description."""
    desc = " ".join(description.strip().split())
    if len(desc) <= max_chars:
        return desc
    first_sentence = desc.split(". ", 1)[0]
    if not first_sentence.endswith("."):
        first_sentence += "."
    if len(first_sentence) <= max_chars:
        return first_sentence
    return desc[: max_chars - 3].rstrip() + "..."


TALKOPS_SKILLS_SYSTEM_PROMPT = """## Skills System

You have access to a skills library that provides verified cloud operations playbooks, IaC procedures, and domain knowledge.

{skills_locations}{skills_load_warnings}

**Available Skills:**

{skills_list}

**Cost-Hierarchy of Levers & Progressive Disclosure:**

Follow a strict **Cost-Hierarchy of Levers** — prefer the cheapest, most deterministic lever that resolves the task:

1. **Skill Playbooks First (Progressive Disclosure)**: Check if the operator's task matches a skill above. Read its `SKILL.md` with `read_file(..., limit=1000)` before guessing CLI flags, IAM condition keys, or writing ad-hoc scripts. Reading a skill also unlocks any skill-gated tools declared in `metadata.include_tools`.
2. **Persistent Memory & Experience (`AGENTS.md`)**: Consult recorded environment conventions and prior causal lessons (`Cue -> Lesson -> Evidence`) before repeating past failure modes.
3. **Deterministic Read-Only Inspection**: Use `read_file`, `glob`, `grep`, and read-only cloud/MCP queries to verify live state on disk or in the cloud before mutating resources.
4. **Specialized Subagent Delegation (`task` / `js_eval`)**: When a task spans multiple cloud regions, accounts, or requires a domain-specific plugin subagent, fan out work via `task` or `js_eval` (`Promise.all`) and pass file paths/ARNs as locators rather than pasting large payloads."""

TALKOPS_SUBAGENT_SKILLS_SYSTEM_PROMPT = """## Skills System

You have access to a skills library that provides verified cloud operations playbooks, IaC procedures, and domain knowledge.

{skills_locations}{skills_load_warnings}

**Available Skills:**

{skills_list}

**Cost-Hierarchy of Levers & Progressive Disclosure:**
1. **Skill Playbooks First (Progressive Disclosure)**: When your task matches a skill above, read its `SKILL.md` with `read_file(..., limit=1000)` before guessing CLI flags, IAM condition keys, or writing ad-hoc scripts. Reading a skill also unlocks any skill-gated tools declared in `metadata.include_tools`.
2. **Deterministic Read-Only Inspection**: Use `read_file`, `glob`, `grep`, and read-only cloud/MCP queries to verify live state on disk or in the cloud before mutating resources."""


@register_middleware(name="skills")
class PluginSkillsMiddleware(SkillsMiddleware):
    """Load namespaced plugin skills with optional whitelist filtering for subagent scoping."""

    def __init__(
        self,
        *,
        backend: BackendProtocol | None = None,
        sources: Sequence[CodeSkillSource | SkillSourceTuple | tuple[str, ...]] | None = None,
        skill_sources: Sequence[Any] | None = None,
        system_prompt: str | None = None,
        allowed_skills: Sequence[str] | None = None,
        include_subagent_skills: bool = False,
        subagents: Sequence[Any] | None = None,
        planning_mode: bool = False,
        is_subagent: bool = False,
        project_root: Path | str | None = None,
        tools: Sequence[Any] | Callable[..., Any] | None = None,
    ) -> None:
        """Initialize PluginSkillsMiddleware with backend, sources, and filtering options.

        Args:
            backend: Storage backend instance.
            sources: Custom skill root sources.
            skill_sources: Structured skill source definitions.
            system_prompt: Custom system prompt template for skill injection.
            allowed_skills: Optional whitelist of skill names permitted for execution.
            include_subagent_skills: Whether to inherit subagent-specific skills.
            subagents: Sequence of subagents whose skills to discover.
            planning_mode: Whether planning mode is active.
            is_subagent: Whether this middleware is attached to an isolated subagent.
            project_root: Optional project root path for skill discovery.
            tools: Optional skill-gated tools or SkillToolResolver callable
                disclosed when a skill with `metadata.include_tools` is read or pinned.
        """
        self._planning_mode = planning_mode
        self._is_subagent = is_subagent
        self._project_root = Path(project_root) if project_root else None
        if system_prompt is None:
            if planning_mode:
                system_prompt = CRITERIA_SKILLS_SYSTEM_PROMPT
            elif is_subagent:
                system_prompt = TALKOPS_SUBAGENT_SKILLS_SYSTEM_PROMPT
            else:
                system_prompt = TALKOPS_SKILLS_SYSTEM_PROMPT

        if backend is None:
            from deepagents.backends.filesystem import FilesystemBackend

            backend = FilesystemBackend(virtual_mode=False)

        self._dynamic_sources = sources is None and skill_sources is None
        self._include_subagent_skills = include_subagent_skills
        self._subagents = subagents
        if sources is None:
            if skill_sources is not None:
                sources = [(str(getattr(s, "path", s)), getattr(s, "name", str(s))) for s in skill_sources]
            else:
                from talkops.skills.sources import get_skill_sources

                sources = get_skill_sources(
                    project_root=self._project_root,
                    include_subagent_skills=include_subagent_skills,
                    subagents=subagents,
                )

        sdk_sources = [(source[0], source[1]) for source in sources]
        super().__init__(
            backend=backend,
            sources=sdk_sources,
            system_prompt=system_prompt,
            tools=tools,
        )
        self._namespaces = tuple(
            source[2] if len(source) == _PLUGIN_SKILL_SOURCE_LENGTH else None for source in sources
        )
        self._allowed_skills = tuple(allowed_skills) if allowed_skills is not None else None
        self._cached_live_skills: list[sdk_skills.SkillMetadata] | None = None

    def _format_skills_locations(self) -> str:
        """Format skills locations for display in system prompt."""
        if self._planning_mode:
            return ""
        return super()._format_skills_locations()

    def _format_skills_load_warnings(self, errors: list[str]) -> str:
        """Format skills load warnings for display in system prompt."""
        if self._planning_mode:
            return ""
        return super()._format_skills_load_warnings(errors)

    def _format_skills_list(self, skills: list[sdk_skills.SkillMetadata]) -> str:
        """Format skills metadata cleanly with progressive-disclosure summaries."""
        if self._planning_mode:
            if not skills:
                return "(No specialized skills currently loaded.)"
            lines: list[str] = []
            for skill in skills:
                name = skill.get("name", "")
                desc = _compact_skill_description(skill.get("description", ""))
                lines.append(f"- **{name}**: {desc}")
            return "\n".join(lines)

        if len(skills) > _COMPACT_SKILLS_THRESHOLD:
            single_root = self.sources[0].rstrip("/") if len(self.sources) == 1 and self.sources[0] else None
            all_under_single_root = bool(
                single_root
                and all(str(s.get("path", "")).startswith(f"{single_root}/") for s in skills)
            )
            lines = []
            if all_under_single_root:
                lines.append(
                    f"*(Read `{single_root}/<skill-name>/SKILL.md` with `read_file` for full instructions)*\n"
                )
            for skill in skills:
                name = skill.get("name", "")
                desc = _compact_skill_description(skill.get("description", ""))
                lines.append(f"- **{name}**: {desc}")
                allowed_tools = skill.get("allowed_tools")
                if allowed_tools:
                    lines.append(f"  -> Allowed tools: {', '.join(allowed_tools)}")
                if not all_under_single_root:
                    lines.append(f"  -> Read `{skill.get('path', '')}` for full instructions")
            return "\n".join(lines)

        return super()._format_skills_list(skills)

    def _is_skill_allowed(self, skill_name: str) -> bool:
        if self._allowed_skills is None:
            return True
        for pattern in self._allowed_skills:
            if fnmatch.fnmatch(skill_name, pattern) or fnmatch.fnmatch(skill_name.lower(), pattern.lower()):
                return True
            if ":" in pattern:
                prefix, base_pat = pattern.split(":", 1)
                if ":" in skill_name:
                    skill_prefix, skill_base = skill_name.split(":", 1)
                    short_prefix = prefix.split("@", 1)[0]
                    short_skill_prefix = skill_prefix.split("@", 1)[0]
                    if (
                        skill_prefix.lower() != prefix.lower()
                        and short_skill_prefix.lower() != short_prefix.lower()
                    ):
                        continue
                    target_name = skill_base
                else:
                    target_name = skill_name
                if (
                    base_pat == "*"
                    or fnmatch.fnmatch(target_name, base_pat)
                    or fnmatch.fnmatch(target_name.lower(), base_pat.lower())
                ):
                    return True
        return False

    def _get_live_skills(self) -> tuple[list[sdk_skills.SkillMetadata], list[str]]:
        if self._dynamic_sources:
            from talkops.skills.sources import get_skill_sources

            live_sources = get_skill_sources(
                project_root=self._project_root,
                include_subagent_skills=self._include_subagent_skills,
                subagents=self._subagents,
            )
            self.sources = [s[0] for s in live_sources]
            self.source_labels = [s[1] for s in live_sources]
            self._namespaces = tuple(s[2] if len(s) == _PLUGIN_SKILL_SOURCE_LENGTH else None for s in live_sources)

        backend = self._backend
        all_skills: dict[str, sdk_skills.SkillMetadata] = {}
        errors: list[str] = []

        active_sources: list[tuple[str, str, str | None]] = []
        for source_path, source_label, namespace in zip(
            self.sources, self.source_labels, self._namespaces, strict=True
        ):
            if namespace is None:
                source_skills, source_error = sdk_skills._list_skills_with_errors(backend, source_path)
                if source_error is not None:
                    errors.append(source_error)
            else:
                source_skills = load_namespaced_skills(backend, source_path, namespace)

            source_has_skills = False
            for skill in source_skills:
                skill_name = skill["name"]
                if self._is_skill_allowed(skill_name):
                    all_skills[skill_name] = skill
                    source_has_skills = True

            if source_has_skills:
                active_sources.append((source_path, source_label, namespace))

        if active_sources:
            self.sources = [s[0] for s in active_sources]
            self.source_labels = [s[1] for s in active_sources]
            self._namespaces = tuple(s[2] for s in active_sources)

        return list(all_skills.values()), errors

    def before_agent(
        self,
        state: sdk_skills.SkillsState,
        runtime: Runtime,
        config: RunnableConfig,
    ) -> sdk_skills.SkillsStateUpdate | None:
        """Discover live skills and prepare state updates before agent execution.

        Args:
            state: Current agent state.
            runtime: Active execution runtime.
            config: Runnable configuration for the invocation.

        Returns:
            SkillsStateUpdate if changes were detected, or None.
        """
        live_skills, errors = self._get_live_skills()
        self._cached_live_skills = live_skills
        current_skills = state.get("skills_metadata") or []
        current_names = [s["name"] for s in current_skills]
        live_names = [s["name"] for s in live_skills]

        if current_names == live_names and state.get("skills_metadata") is not None:
            return None

        update = sdk_skills.SkillsStateUpdate(skills_metadata=live_skills)
        if errors:
            logger.warning("Skills load errors: %s", errors)
            update["skills_load_errors"] = errors
        return update

    async def abefore_agent(
        self,
        state: sdk_skills.SkillsState,
        runtime: Runtime,
        config: RunnableConfig,
    ) -> sdk_skills.SkillsStateUpdate | None:
        """Asynchronously discover live skills and prepare state updates before agent execution.

        Args:
            state: Current agent state.
            runtime: Active execution runtime.
            config: Runnable configuration for the invocation.

        Returns:
            SkillsStateUpdate if changes were detected, or None.
        """
        return await asyncio.to_thread(self.before_agent, state, runtime, config)

    def modify_request(self, request: Any) -> Any:
        """Filter out stale skill metadata before formatting the system prompt.

        Args:
            request: Model request to modify.

        Returns:
            Modified request with pruned skill metadata.
        """
        live_skills = getattr(self, "_cached_live_skills", None)
        if live_skills is None:
            if hasattr(request, "state") and isinstance(request.state, dict) and request.state.get("skills_metadata"):
                live_skills = request.state["skills_metadata"]
            else:
                live_skills, _ = self._get_live_skills()
            self._cached_live_skills = live_skills

        live_names = {s["name"] for s in live_skills}

        if hasattr(request, "state") and isinstance(request.state, dict):
            # Prune any stale or uninstalled skills from request state before formatting system prompt
            existing = request.state.get("skills_metadata") or []
            request.state["skills_metadata"] = [s for s in existing if s.get("name") in live_names]
            if not request.state["skills_metadata"]:
                request.state["skills_metadata"] = live_skills

        return super().modify_request(request)

    def after_agent(
        self,
        state: sdk_skills.SkillsState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        """Reset cached `skills_metadata` when the agent creates or edits a SKILL.md file."""
        messages = state.get("messages") or []
        for msg in reversed(messages):
            if not isinstance(msg, AIMessage):
                continue
            for tc in getattr(msg, "tool_calls", None) or ():
                if tc.get("name") in ("write_file", "edit_file", "delete_file"):
                    args = tc.get("args") or {}
                    target = str(args.get("file_path") or args.get("path") or "")
                    if "/skills/" in target or target.endswith("SKILL.md"):
                        self._cached_live_skills = None
                        return {"skills_metadata": None}
            break
        return None

    async def aafter_agent(
        self,
        state: sdk_skills.SkillsState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        """Async reset of cached `skills_metadata` when a skill file is created or edited."""
        return self.after_agent(state, runtime)


__all__ = [
    "PluginSkillsMiddleware",
    "TALKOPS_SKILLS_SYSTEM_PROMPT",
    "discover_skill_dirs",
    "load_namespaced_skills",
]

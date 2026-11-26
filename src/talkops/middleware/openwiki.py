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

"""OpenWiki context, tool-gating, and grounding middleware for TalkOps agents."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NotRequired

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.types import Command

from talkops.middleware.registry import register_middleware
from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langgraph.runtime import Runtime

logger = get_logger(__name__)

# Retrieval tools exposed during normal coding turns (read-only, model-free)
BASE_RETRIEVAL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "openwiki_search",
        "openwiki_read",
        "openwiki_list_workspaces",
        "openwiki_list_wikis",
    }
)

RETRIEVAL_TOOL_NAMES: frozenset[str] = BASE_RETRIEVAL_TOOL_NAMES

# Compilation state-machine tools (gated behind /openwiki lifecycle turns)
BASE_LIFECYCLE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "openwiki_begin",
        "openwiki_submit_plan",
        "openwiki_next_page",
        "openwiki_inspect_page_claims",
        "openwiki_submit_page",
        "openwiki_finish",
    }
)

LIFECYCLE_TOOL_NAMES: frozenset[str] = BASE_LIFECYCLE_TOOL_NAMES

MUTATING_FS_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "edit_file",
        "delete",
        "delete_file",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "rm",
    }
)

EDIT_FS_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "edit_file",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
    }
)

DISCOVERY_FS_TOOLS: frozenset[str] = frozenset(
    {
        "glob",
        "grep",
        "grep_search",
        "ls",
    }
)

WIKI_INDEXED_GLOB_TOOLS: frozenset[str] = frozenset(
    {
        "glob",
        "ls",
    }
)

PROTECTED_OPENWIKI_RELATIVE_PATHS: tuple[str, ...] = (
    "openwiki/.claims",
    "openwiki/.run.json",
    "openwiki/.last-update.json",
)


def _replace_openwiki_value(_existing: Any, new: Any) -> Any:
    """Reducer that allows parallel openwiki_read/openwiki_search tool calls in the same step."""
    return new


class OpenWikiState(AgentState):
    """State schema channels managed by OpenWikiContextMiddleware."""

    openwiki_digest: Annotated[NotRequired[dict[str, Any] | None], _replace_openwiki_value]
    openwiki_grounded: Annotated[NotRequired[bool], _replace_openwiki_value]
    openwiki_lifecycle_active: Annotated[NotRequired[bool], _replace_openwiki_value]


def ensure_claude_agents_symlink_safe(repo_root: str | Path) -> bool:
    """Prevent symlink pointer corruption between CLAUDE.md and AGENTS.md.

    OpenWiki wraps its managed block with ``<!-- OPENWIKI:START -->`` in ``AGENTS.md``
    and ``<!-- OPENWIKI:CLAUDE:START -->`` in ``CLAUDE.md``. If ``CLAUDE.md`` is a
    POSIX symlink to ``AGENTS.md``, updating both files corrupts the marker pairs on
    the shared inode (OpenWiki Issues #719 and #725).

    Args:
        repo_root: Repository root directory.

    Returns:
        True if a symlink was converted into an ``@AGENTS.md`` regular file, False otherwise.
    """
    root = Path(repo_root).expanduser().resolve()
    claude_md = root / "CLAUDE.md"
    agents_md = root / "AGENTS.md"
    if not claude_md.is_symlink():
        return False
    try:
        resolved_target = claude_md.resolve()
    except OSError:
        return False
    if resolved_target == agents_md.resolve():
        claude_md.unlink()
        claude_md.write_text("@AGENTS.md\n", encoding="utf-8")
        return True
    return False


@register_middleware(name="openwiki_context")
class OpenWikiContextMiddleware(AgentMiddleware[OpenWikiState, Any]):
    """Manages OpenWiki context lifecycle, drift detection, tool gating, and grounding.

    Hooks into the LangChain / DeepAgents middleware pipeline:
    1. ``before_agent`` / ``abefore_agent``: Computes a compact ``openwiki_digest`` once
       per session (propagated to subagents) containing ``quickstart.md`` routing,
       ``INSTRUCTIONS.md`` conventions, and Git commit drift status against
       ``openwiki/.last-update.json``.
    2. ``wrap_model_call`` / ``awrap_model_call``: Injects the OpenWiki routing digest
       into the ``ModelRequest`` system message and hides the 6 compilation state-machine
       tools (``openwiki_begin``, etc.) unless a lifecycle turn is active.
    3. ``wrap_tool_call`` / ``awrap_tool_call``:
       - Blocks direct agent mutations to ``openwiki/.claims/``, ``openwiki/.run.json``,
         and ``openwiki/.last-update.json``.
       - Records ``openwiki_grounded=True`` in LangGraph state via ``Command(update=...)``
         whenever ``openwiki_search`` or ``openwiki_read`` is called.
       - Optionally enforces pre-edit architectural grounding before ``write_file`` /
         ``edit_file`` calls when ``enforce_pre_edit_grounding=True``.
    """

    state_schema = OpenWikiState

    def __init__(
        self,
        repo_root: str | Path | None = None,
        *,
        enforce_pre_edit_grounding: bool = False,
        include_lifecycle_tools: bool = False,
        is_subagent: bool = False,
        max_quickstart_chars: int = 3500,
        max_instructions_chars: int = 2500,
    ) -> None:
        """Initialize OpenWikiContextMiddleware and bind native OpenWiki LangChain tools."""
        super().__init__()
        self.repo_root = Path(repo_root or Path.cwd()).expanduser().resolve()
        self.openwiki_dir = self.repo_root / "openwiki"
        self.enforce_pre_edit_grounding = enforce_pre_edit_grounding
        self.include_lifecycle_tools = include_lifecycle_tools
        self.is_subagent = is_subagent
        self.max_quickstart_chars = max_quickstart_chars
        self.max_instructions_chars = max_instructions_chars

        from talkops.tools.openwiki_tools import build_openwiki_tools

        self.tools = [] if self.is_subagent else build_openwiki_tools(self.repo_root)
        self._cleanup_legacy_mcp_artifacts()

    @staticmethod
    def _cleanup_legacy_mcp_artifacts() -> None:
        """Remove any leftover ~/.talkops/mcp/openwiki schema cache from legacy runs."""
        import shutil

        legacy_mcp_dir = Path.home() / ".talkops" / "mcp" / "openwiki"
        if legacy_mcp_dir.exists():
            shutil.rmtree(legacy_mcp_dir, ignore_errors=True)

    def _get_git_head(self) -> str | None:
        """Resolve the current short Git commit hash for drift comparison."""
        try:
            res = subprocess.run(
                ["git", "rev-parse", "--short=12", "HEAD"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            logger.debug("Could not resolve git HEAD for OpenWiki drift check", exc_info=True)
        return None

    def _has_compiled_wiki_on_disk(self) -> bool:
        """Return True if a compiled OpenWiki (`quickstart.md` or `index.md`) exists on disk."""
        return bool(
            self.openwiki_dir.is_dir()
            and (
                (self.openwiki_dir / "quickstart.md").is_file()
                or (self.openwiki_dir / "index.md").is_file()
            )
        )

    def _build_digest(self) -> dict[str, Any]:
        """Build the compact OpenWiki session digest and live codebase delta from disk."""
        if not self._has_compiled_wiki_on_disk():
            return {"has_wiki": False}

        quickstart_path = self.openwiki_dir / "quickstart.md"
        instructions_path = self.openwiki_dir / "INSTRUCTIONS.md"
        last_update_path = self.openwiki_dir / ".last-update.json"

        quickstart_routing = ""
        if quickstart_path.exists():
            try:
                quickstart_routing = quickstart_path.read_text(encoding="utf-8")[
                    : self.max_quickstart_chars
                ]
            except OSError:
                quickstart_routing = ""

        repo_conventions = ""
        if instructions_path.exists():
            try:
                repo_conventions = instructions_path.read_text(encoding="utf-8")[
                    : self.max_instructions_chars
                ]
            except OSError:
                repo_conventions = ""

        from talkops.tools.openwiki_tools import compute_openwiki_codebase_delta

        delta = compute_openwiki_codebase_delta(self.repo_root)
        recorded_head = str(delta.get("recorded_head") or "")
        current_head = self._get_git_head() or str(delta.get("current_head") or "")
        changed_files: list[str] = list(delta.get("changed_files") or [])
        affected_pages: list[str] = list(delta.get("affected_pages") or [])
        recent_commits: list[str] = list(delta.get("recent_commits") or [])

        drift_notice = ""
        if last_update_path.exists():
            try:
                meta = json.loads(last_update_path.read_text(encoding="utf-8"))
                raw_last_commit = str(
                    meta.get("lastCommit") or meta.get("gitHead") or ""
                ).strip()
                if raw_last_commit and not recorded_head:
                    recorded_head = raw_last_commit[:12]
            except (OSError, ValueError, TypeError):
                drift_notice = "OpenWiki `.last-update.json` could not be parsed."

        head_drifted = bool(
            recorded_head
            and current_head
            and not current_head.startswith(recorded_head[:7])
            and not recorded_head.startswith(current_head[:7])
        )

        if not drift_notice and (head_drifted or changed_files or affected_pages):
            notice_lines: list[str] = []
            if head_drifted:
                notice_lines.append(
                    f"WARNING: OpenWiki was compiled at commit `{recorded_head}`, "
                    f"but current Git HEAD is `{current_head}`."
                )
            elif changed_files:
                notice_lines.append(
                    "WARNING: Uncommitted working-tree source changes detected since "
                    f"the last OpenWiki build (`{recorded_head or current_head or 'HEAD'}`)."
                )
            if recent_commits:
                commits_preview = "; ".join(f"`{c}`" for c in recent_commits[:5])
                notice_lines.append(f"- Recent commits since wiki build: {commits_preview}")
            if changed_files:
                files_preview = ", ".join(f"`{f}`" for f in changed_files[:15])
                if len(changed_files) > 15:
                    files_preview += f" (+{len(changed_files) - 15} more)"
                notice_lines.append(
                    f"- Modified/new source files since wiki build: {files_preview}"
                )
            if affected_pages:
                pages_preview = ", ".join(f"`{p}`" for p in affected_pages)
                notice_lines.append(
                    f"- Affected wiki pages with drifted claims or modified files: {pages_preview}"
                )
            notice_lines.append(
                "Cross-check recent file changes against live source code (`read_file`) "
                "as authoritative if discrepancies appear, or run `/openwiki update` to synchronize."
            )
            drift_notice = "\n".join(notice_lines)

        return {
            "has_wiki": True,
            "quickstart_routing": quickstart_routing,
            "repo_conventions": repo_conventions,
            "drift_notice": drift_notice,
            "recorded_head": recorded_head,
            "current_head": current_head,
            "changed_files": changed_files,
            "affected_pages": affected_pages,
        }

    def _needs_digest_refresh(self, existing: dict[str, Any] | None) -> bool:
        """Return True if the session ``openwiki_digest`` needs to be computed or refreshed."""
        if existing is None or not isinstance(existing, dict):
            return True
        disk_has_wiki = self._has_compiled_wiki_on_disk()
        if bool(existing.get("has_wiki", False)) != disk_has_wiki:
            return True
        if disk_has_wiki:
            if (self.openwiki_dir / "quickstart.md").is_file() and not existing.get(
                "quickstart_routing"
            ):
                return True
            live_head = self._get_git_head() or ""
            if live_head and existing.get("current_head") != live_head:
                return True
        return False

    def before_agent(
        self,
        state: OpenWikiState,
        runtime: Runtime[Any],  # noqa: ARG002
    ) -> dict[str, Any] | None:
        """Compute/refresh the OpenWiki digest and reset per-turn grounding (sync)."""
        existing = state.get("openwiki_digest")
        needs_refresh = self._needs_digest_refresh(existing)
        digest = self._build_digest() if needs_refresh else existing
        has_wiki = bool((digest or {}).get("has_wiki", False))
        return {
            "openwiki_digest": digest,
            "openwiki_grounded": not has_wiki,
        }

    async def abefore_agent(
        self,
        state: OpenWikiState,
        runtime: Runtime[Any],  # noqa: ARG002
    ) -> dict[str, Any] | None:
        """Compute/refresh the OpenWiki digest and reset per-turn grounding (async)."""
        existing = state.get("openwiki_digest")
        needs_refresh = self._needs_digest_refresh(existing)
        digest = await asyncio.to_thread(self._build_digest) if needs_refresh else existing
        has_wiki = bool((digest or {}).get("has_wiki", False))
        return {
            "openwiki_digest": digest,
            "openwiki_grounded": not has_wiki,
        }

    def _is_subagent_request(self, request: ModelRequest) -> bool:
        """Return True if ``request`` is executing inside a subagent (via ``task`` or ``js_eval``)."""
        if self.is_subagent:
            return True
        state = getattr(request, "state", None) or {}
        if isinstance(state, dict) and state.get("_deepagents_forked_context"):
            return True
        runtime = getattr(request, "runtime", None)
        config = getattr(runtime, "config", None) if runtime is not None else None
        if isinstance(config, dict):
            configurable = config.get("configurable")
            if isinstance(configurable, dict) and configurable.get("ls_agent_type") == "subagent":
                return True
            metadata = config.get("metadata")
            if isinstance(metadata, dict) and metadata.get("ls_agent_type") == "subagent":
                return True
        return False

    def _is_lifecycle_turn(self, request: ModelRequest) -> bool:
        """Return True when the 6 OpenWiki compilation tools should be exposed."""
        if self._is_subagent_request(request):
            return False
        if self.include_lifecycle_tools:
            return True
        state = getattr(request, "state", None) or {}
        if state.get("openwiki_lifecycle_active"):
            return True
        messages = getattr(request, "messages", None) or []
        for msg in reversed(messages[-15:]):
            content = getattr(msg, "content", "")
            text = content if isinstance(content, str) else str(content)
            lowered = text.lower()
            if (
                "/openwiki" in lowered
                or "/skill:openwiki" in lowered
                or "openwiki_begin" in lowered
                or "openwiki_submit_plan" in lowered
                or "openwiki_next_page" in lowered
                or "openwiki_inspect_page_claims" in lowered
                or "openwiki_submit_page" in lowered
                or "openwiki_finish" in lowered
                or "openwiki/skill.md" in lowered
            ):
                return True
        return False

    @staticmethod
    def _clean_repo_conventions(raw: str) -> str:
        """Strip default OpenWiki meta-boilerplate from INSTRUCTIONS.md while keeping concrete rules."""
        cleaned = re.sub(
            r"# Repository Conventions & Verification Rules\s*",
            "",
            raw,
        )
        cleaned = re.sub(
            r"## Purpose\nThis file \(`openwiki/INSTRUCTIONS\.md`\) defines standing conventions, verification commands,\nand architectural invariants read by both OpenWiki during wiki compilation and TalkOps\nagents at the start of every coding session\.\s*",
            "",
            cleaned,
        )
        cleaned = re.sub(
            r"## Architectural Notes\n(?:- Keep `openwiki/quickstart\.md` concise so agents can route directly to subsystem pages\.\n)?(?:- Ground architectural claims in concrete repository-relative file paths and component/symbol names\.\n)?(?:- Verify changes using the project's native build, test, or infrastructure validation tooling before completion\.\n?)?\s*",
            "",
            cleaned,
        )
        # Demote remaining H2/H3 headings inside INSTRUCTIONS.md to H4 so they nest cleanly under
        # `### Standing Repository Conventions (openwiki/INSTRUCTIONS.md)`.
        cleaned = re.sub(r"(?m)^#{2,3}\s+", "#### ", cleaned)
        return cleaned.strip()

    @staticmethod
    def _clean_quickstart_routing(raw: str) -> str:
        """Strip YAML frontmatter and redundant top-level H1 from openwiki/quickstart.md before prompt injection."""
        cleaned = re.sub(r"\A---\n.*?\n---\s*", "", raw.strip(), flags=re.DOTALL)
        cleaned = re.sub(r"\A# Architecture Routing Table(?: & Quickstart)?\s*", "", cleaned)
        return cleaned.strip()

    @staticmethod
    def _format_digest_injection(
        digest: dict[str, Any],
        *,
        is_grounded: bool = False,
        is_subagent: bool = False,
    ) -> str:
        """Format the system prompt injection block from the cached OpenWiki digest."""
        sections = ["## TalkOps.ai Durable Repository Context (OpenWiki)"]
        if digest.get("repo_conventions"):
            conventions = OpenWikiContextMiddleware._clean_repo_conventions(
                str(digest["repo_conventions"])
            )
            if conventions:
                sections.append(
                    "### Standing Repository Conventions (openwiki/INSTRUCTIONS.md)\n"
                    f"{conventions}"
                )
        if digest.get("quickstart_routing"):
            routing_text = OpenWikiContextMiddleware._clean_quickstart_routing(
                str(digest["quickstart_routing"])
            )
            if is_subagent:
                routing_text = re.sub(
                    r"via `openwiki_read[^`]*` or `openwiki_search[^`]*`",
                    "by inspecting the repository source files directly",
                    routing_text,
                )
            if routing_text:
                sections.append(
                    "### Architecture Routing Table (openwiki/quickstart.md)\n"
                    f"{routing_text}"
                )
        if digest.get("drift_notice"):
            sections.append(f"### Wiki Freshness Notice\n{digest['drift_notice']}")
        if is_subagent:
            sections.append(
                "### Subagent Source Grounding Rule\n"
                "Treat live repository source files (`read_file`, `grep`, `glob`) and tests "
                "as the authoritative source of truth—especially for any modified files listed "
                "in the Wiki Freshness Notice above."
            )
        else:
            sections.append(
                "### OpenWiki Tool Routing Rule\n"
                "Use `openwiki_search` and `openwiki_read(slug, section=...)` for just-in-time "
                "architectural discovery (returns `verified_source_files` and drift checks); "
                "treat live source code (`read_file`, `grep`, `glob`) and tests as authoritative "
                "and stop querying OpenWiki once grounded."
            )
        return "\n\n".join(sections)

    def _get_modified_request(self, request: ModelRequest) -> ModelRequest:
        """Filter OpenWiki tools based on repo initialization and subagent context, and inject the digest block."""
        overrides: dict[str, Any] = {}
        state = getattr(request, "state", None) or {}
        digest = state.get("openwiki_digest")
        has_wiki = bool(
            (isinstance(digest, dict) and digest.get("has_wiki"))
            or (digest is None and self._has_compiled_wiki_on_disk())
        )
        has_workspace = (self.repo_root / ".openwiki-workspace.json").is_file()
        is_grounded = bool(state.get("openwiki_grounded", False))
        is_subagent = self._is_subagent_request(request)
        is_lifecycle = self._is_lifecycle_turn(request)

        if request.tools:
            blocked_names: set[str] = set()
            if is_subagent:
                # Never expose any openwiki_* tools to subagents invoked via task or js_eval
                blocked_names.update(LIFECYCLE_TOOL_NAMES)
                blocked_names.update(RETRIEVAL_TOOL_NAMES)
            elif not is_lifecycle:
                blocked_names.update(LIFECYCLE_TOOL_NAMES)
                if not has_wiki and not has_workspace:
                    # Hide all openwiki_* tools on normal coding turns when the repo has no OpenWiki
                    blocked_names.update(RETRIEVAL_TOOL_NAMES)

            if blocked_names:
                filtered_tools = []
                for tool_obj in request.tools:
                    name = (
                        tool_obj.get("name", "")
                        if isinstance(tool_obj, dict)
                        else getattr(tool_obj, "name", "")
                    )
                    if name not in blocked_names:
                        filtered_tools.append(tool_obj)
                if len(filtered_tools) != len(request.tools):
                    overrides["tools"] = filtered_tools

        if isinstance(digest, dict) and digest.get("has_wiki"):
            injection = self._format_digest_injection(
                digest,
                is_grounded=is_grounded,
                is_subagent=is_subagent,
            )
            if request.system_message is not None:
                existing_blocks = list(request.system_message.content_blocks)
                new_content = [
                    *existing_blocks,
                    {"type": "text", "text": f"\n\n{injection}"},
                ]
                overrides["system_message"] = SystemMessage(content=new_content)
            else:
                system_prompt = getattr(request, "system_prompt", "") or ""
                combined = f"{system_prompt}\n\n{injection}" if system_prompt else injection
                overrides["system_message"] = SystemMessage(content=combined)

        if not overrides:
            return request
        return request.override(**overrides)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject OpenWiki digest and filter compilation tools (sync)."""
        return handler(self._get_modified_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Inject OpenWiki digest and filter compilation tools (async)."""
        return await handler(self._get_modified_request(request))

    @staticmethod
    def _extract_target_path(tool_args: dict[str, Any]) -> str:
        """Extract the file path argument from a filesystem tool call."""
        raw = (
            tool_args.get("file_path")
            or tool_args.get("path")
            or tool_args.get("target")
            or tool_args.get("TargetFile")
            or tool_args.get("target_file")
            or tool_args.get("file")
            or ""
        )
        return raw if isinstance(raw, str) else ""

    def _resolve_repo_path(self, raw_path: str) -> Path | None:
        """Resolve a tool path argument against ``self.repo_root``."""
        if not raw_path:
            return None
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.repo_root / candidate
        try:
            return candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            return None

    def _is_protected_openwiki_metadata(self, resolved: Path) -> bool:
        """Return True if ``resolved`` targets protected OpenWiki state/claim files."""
        for rel in PROTECTED_OPENWIKI_RELATIVE_PATHS:
            protected = (self.repo_root / rel).resolve()
            if resolved == protected or resolved.is_relative_to(protected):
                return True
        return False

    def _is_inside_openwiki(self, resolved: Path) -> bool:
        """Return True if ``resolved`` is inside ``<repo_root>/openwiki``."""
        openwiki_resolved = self.openwiki_dir.resolve()
        return resolved == openwiki_resolved or resolved.is_relative_to(openwiki_resolved)

    @staticmethod
    def _mark_grounded_result(result: ToolMessage | Command[Any]) -> Command[Any]:
        """Wrap or augment a tool result so ``openwiki_grounded=True`` is persisted in state."""
        if isinstance(result, Command):
            existing_update = dict(result.update) if isinstance(result.update, dict) else {}
            existing_update["openwiki_grounded"] = True
            return Command(
                graph=result.graph,
                update=existing_update,
                resume=result.resume,
                goto=result.goto,
            )
        return Command(
            update={
                "openwiki_grounded": True,
                "messages": [result],
            }
        )

    def _mark_finished_result(self, result: ToolMessage | Command[Any]) -> Command[Any]:
        """Refresh ``openwiki_digest`` and exit lifecycle mode after ``openwiki_finish``."""
        new_digest = self._build_digest()
        if isinstance(result, Command):
            existing_update = dict(result.update) if isinstance(result.update, dict) else {}
            existing_update["openwiki_digest"] = new_digest
            existing_update["openwiki_grounded"] = False
            existing_update["openwiki_lifecycle_active"] = False
            return Command(
                graph=result.graph,
                update=existing_update,
                resume=result.resume,
                goto=result.goto,
            )
        return Command(
            update={
                "openwiki_digest": new_digest,
                "openwiki_grounded": False,
                "openwiki_lifecycle_active": False,
                "messages": [result],
            }
        )

    def _pre_tool_check(self, request: ToolCallRequest) -> ToolMessage | None:
        """Validate protected OpenWiki paths and optional pre-edit grounding guardrail."""
        tool_call = getattr(request, "tool_call", None)
        if not isinstance(tool_call, dict):
            return None

        tool_name = str(tool_call.get("name", "") or "")
        tool_args = tool_call.get("args") or {}
        if not isinstance(tool_args, dict):
            tool_args = {}
        tool_call_id = str(tool_call.get("id", "") or "")

        if tool_name in MUTATING_FS_TOOLS:
            raw_path = self._extract_target_path(tool_args)
            resolved = self._resolve_repo_path(raw_path)
            if resolved is not None and self._is_protected_openwiki_metadata(resolved):
                return ToolMessage(
                    content=(
                        f"[OpenWiki Guardrail] Blocked direct modification of `{raw_path}`. "
                        "Files inside `openwiki/.claims/`, `openwiki/.run.json`, and "
                        "`openwiki/.last-update.json` are machine-managed by the OpenWiki "
                        "state machine and must only be updated via `openwiki_submit_page` "
                        "or `openwiki_finish`."
                    ),
                    name=tool_name,
                    tool_call_id=tool_call_id,
                    status="error",
                )

        if self.enforce_pre_edit_grounding and tool_name in EDIT_FS_TOOLS:
            state = getattr(request, "state", None) or {}
            has_wiki = bool((state.get("openwiki_digest") or {}).get("has_wiki", False))
            is_grounded = bool(state.get("openwiki_grounded", False))
            raw_path = self._extract_target_path(tool_args)
            resolved = self._resolve_repo_path(raw_path)
            editing_wiki_itself = resolved is not None and self._is_inside_openwiki(resolved)
            if has_wiki and not is_grounded and not editing_wiki_itself:
                return ToolMessage(
                    content=(
                        f"[OpenWiki Guardrail] Blocked edit to `{raw_path or 'unknown'}`. "
                        "You have not queried the repository's OpenWiki knowledge base in "
                        "this session. Call `openwiki_search` or `openwiki_read` on the "
                        "relevant subsystem before mutating source files."
                    ),
                    name=tool_name,
                    tool_call_id=tool_call_id,
                    status="error",
                )

        return None

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """Enforce OpenWiki file protection and track grounding state (sync)."""
        blocked = self._pre_tool_check(request)
        if blocked is not None:
            return blocked

        tool_call = getattr(request, "tool_call", None) or {}
        tool_name = str(tool_call.get("name", "") or "")
        result = handler(request)
        if tool_name == "openwiki_finish":
            return self._mark_finished_result(result)
        if tool_name in RETRIEVAL_TOOL_NAMES:
            return self._mark_grounded_result(result)
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Enforce OpenWiki file protection and track grounding state (async)."""
        blocked = self._pre_tool_check(request)
        if blocked is not None:
            return blocked

        tool_call = getattr(request, "tool_call", None) or {}
        tool_name = str(tool_call.get("name", "") or "")
        result = await handler(request)
        if tool_name == "openwiki_finish":
            return await asyncio.to_thread(self._mark_finished_result, result)
        if tool_name in RETRIEVAL_TOOL_NAMES:
            return self._mark_grounded_result(result)
        return result


async def build_talkops_coding_agent(
    repo_root: str | Path,
    *,
    include_lifecycle_tools: bool = False,
    enforce_pre_edit_grounding: bool = False,
    model: str = "anthropic:claude-sonnet-4-6",
) -> Any:
    """Standalone factory helper wiring native OpenWiki LangChain tools and middleware into a Deep Agent."""
    from deepagents import create_deep_agent
    from deepagents.backends import FilesystemBackend

    root_path = Path(repo_root).expanduser().resolve()
    return create_deep_agent(
        model=model,
        backend=FilesystemBackend(root_dir=str(root_path)),
        memory=["AGENTS.md"],
        skills=["src/talkops/built_in_skills/"],
        middleware=[
            OpenWikiContextMiddleware(
                repo_root=root_path,
                enforce_pre_edit_grounding=enforce_pre_edit_grounding,
                include_lifecycle_tools=include_lifecycle_tools,
            ),
        ],
    )


__all__ = [
    "BASE_LIFECYCLE_TOOL_NAMES",
    "BASE_RETRIEVAL_TOOL_NAMES",
    "LIFECYCLE_TOOL_NAMES",
    "OpenWikiContextMiddleware",
    "OpenWikiState",
    "PROTECTED_OPENWIKI_RELATIVE_PATHS",
    "RETRIEVAL_TOOL_NAMES",
    "build_talkops_coding_agent",
    "ensure_claude_agents_symlink_safe",
]

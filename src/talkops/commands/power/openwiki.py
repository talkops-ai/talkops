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

"""OpenWiki slash command — manage and query the durable repository knowledge base."""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

from talkops.commands._base import BaseCommandHandler, CommandContext, CommandResult
from talkops.commands._types import BypassTier, CommandCategory, SafetyLevel
from talkops.middleware.openwiki import ensure_claude_agents_symlink_safe
from talkops.tools.openwiki_tools import (
    bootstrap_openwiki_scaffold,
    compile_openwiki_deterministic,
    inspect_openwiki_status,
    parse_wiki_page,
    upsert_agents_md_openwiki_pointer,
)

if TYPE_CHECKING:
    from talkops.ui.textual_app import TalkOpsApp


def _resolve_repo_root(app: TalkOpsApp | Any) -> Path:
    """Resolve the active project root or working directory from the app."""
    cwd = getattr(app, "_cwd", None) if app is not None else None
    if cwd:
        return Path(cwd).expanduser().resolve()
    return Path.cwd().resolve()


async def _mount_system_notice(app: TalkOpsApp | Any, text: str) -> None:
    """Mount an inline system notice in the TUI (or test mock)."""
    if app is None:
        return
    if hasattr(app, "_mount_message"):
        from talkops.ui.widgets.messages import SystemMessage

        await app._mount_message(SystemMessage(text))
    elif hasattr(app, "_mount_system"):
        await app._mount_system(text)


async def _command_output(
    app: TalkOpsApp | Any,
    text: str,
    *,
    success: bool = True,
) -> CommandResult:
    """Return a CommandResult whose message is mounted by TalkOpsApp (and notify test mocks)."""
    if app is not None and not hasattr(app, "_mount_message") and hasattr(app, "_mount_system"):
        await app._mount_system(text)
    return CommandResult(
        success=success,
        message=text,
        mount_as_app_message=True,
    )


async def _invalidate_openwiki_session_state(
    app: TalkOpsApp | Any,
    *,
    lifecycle_active: bool = False,
) -> None:
    """Invalidate cached OpenWiki digest and skills metadata in LangGraph state."""
    if app is None:
        return
    update = {
        "openwiki_digest": None,
        "skills_metadata": None,
        "openwiki_lifecycle_active": lifecycle_active,
    }
    thread_id = getattr(app, "_agent_thread_id", None) or getattr(app, "_lc_thread_id", None)
    if not thread_id:
        return
    config = {"configurable": {"thread_id": thread_id}}

    # 1. Server-backed adapter client (matches GoalController.aupdate_thread_state)
    adapter = getattr(app, "_adapter", None)
    if adapter is not None and getattr(adapter, "connected", False):
        client = getattr(adapter, "_client", None)
        if client is not None and hasattr(client, "aupdate_state"):
            try:
                if hasattr(client, "aensure_thread"):
                    await client.aensure_thread(config)
                    await client.aupdate_state(config, update, as_node="model")
                else:
                    await client.aupdate_state(config, update)
                return
            except Exception:
                pass

    # 2. Direct in-process agent graph fallback
    agent = getattr(app, "_agent", None)
    if agent is not None and hasattr(agent, "aupdate_state"):
        try:
            await agent.aupdate_state(config, update)
        except Exception:
            pass


def format_openwiki_status_markdown(status: dict[str, object]) -> str:
    """Format the dictionary returned by ``inspect_openwiki_status`` as Markdown."""
    initialized = bool(status.get("initialized"))
    repo_root = status.get("repo_root", "")
    page_count = status.get("page_count", 0)
    pages = status.get("pages") or []
    claim_files = status.get("claim_files", 0)
    last_commit = status.get("last_commit") or "none"
    current_head = status.get("current_head") or "unknown"
    is_drifted = bool(status.get("is_drifted"))
    has_quickstart = bool(status.get("has_quickstart"))
    has_instructions = bool(status.get("has_instructions"))
    has_ignore = bool(status.get("has_openwikiignore"))
    active_run = bool(status.get("active_run"))

    state_badge = "Initialized" if initialized else "Not Initialized"
    if initialized and is_drifted:
        state_badge = "Drifted (Update Recommended)"

    lines = [
        "### OpenWiki Repository Knowledge Base",
        "",
        f"- **Status:** {state_badge}",
        f"- **Repository Root:** `{repo_root}`",
        f"- **Compiled Pages ({page_count}):** "
        + (", ".join(f"`{p}`" for p in pages) if isinstance(pages, list) and pages else "_none_"),
        f"- **Grounded Claim Ledgers (`.claims/*.json`):** {claim_files}",
        f"- **Compiled Commit (`lastCommit`):** `{last_commit}` (current HEAD: `{current_head}`)",
        f"- **Routing Table (`quickstart.md`):** {'present' if has_quickstart else 'missing'}",
        f"- **Standing Conventions (`INSTRUCTIONS.md`):** {'present' if has_instructions else 'missing'}",
        f"- **Ignore Rules (`.openwikiignore`):** {'present' if has_ignore else 'missing'}",
    ]
    if active_run:
        lines.append("- **Active Compilation Run (`.run.json`):** in progress")
    if is_drifted:
        lines.extend(
            [
                "",
                "> **Drift Detected:** Git `HEAD` has advanced since the last OpenWiki build. "
                "Run `/openwiki update` to incrementally re-verify Grounded Claims and refresh pages.",
            ]
        )
    lines.extend(
        [
            "",
            "**Subcommands:**",
            "- `/openwiki init [--force] [brief]` — Scaffold & compile `openwiki/` knowledge base",
            "- `/openwiki update [--force] [brief]` — Incrementally update drifted pages & claims",
            "- `/openwiki ask <question>` — Query repository architecture using `openwiki_search` & `openwiki_read`",
            "- `/openwiki brief <instructions>` — Add standing conventions to `openwiki/INSTRUCTIONS.md`",
            "- `/openwiki link` — Heal `CLAUDE.md` symlinks and repair `<!-- OPENWIKI:START -->` pointers",
        ]
    )
    return "\n".join(lines)


class OpenWikiHandler(BaseCommandHandler):
    """Manage and query the repository's OpenWiki durable Markdown knowledge base."""

    @property
    def name(self) -> str:
        """Return the primary slash command name."""
        return "/openwiki"

    @property
    def aliases(self) -> tuple[str, ...]:
        """Return alternative slash command aliases."""
        return ("/wiki", "/skill:openwiki")

    @property
    def description(self) -> str:
        """Return short command description."""
        return "Compile, update, or query the OpenWiki codebase knowledge base"

    @property
    def category(self) -> CommandCategory:
        """Return the command category classification."""
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        """Return the safety level required for this command."""
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        """Return the execution queue bypass tier for this command."""
        return BypassTier.QUEUED

    @property
    def model_facing_description(self) -> str | None:
        """Return the description exposed to the LLM for command recommendations."""
        return (
            "Recommend `/openwiki init` or `/openwiki update` when the user wants to "
            "compile or refresh the repository's durable OpenWiki architectural knowledge base."
        )

    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Dispatch ``/openwiki`` subcommands."""
        app = ctx.app
        repo_root = _resolve_repo_root(app)
        raw_args = ctx.args.strip()

        if not raw_args or raw_args.lower() == "status":
            status = inspect_openwiki_status(repo_root)
            return await _command_output(app, format_openwiki_status_markdown(status))

        try:
            tokens = shlex.split(raw_args)
        except ValueError:
            tokens = raw_args.split()

        subcmd = tokens[0].lower()
        rest_tokens = tokens[1:]
        rest_text = raw_args[len(tokens[0]) :].strip()

        if subcmd in {"help", "-h", "--help"}:
            status = inspect_openwiki_status(repo_root)
            return await _command_output(app, format_openwiki_status_markdown(status))

        if subcmd in {"init", "update"}:
            return await self._handle_init_or_update(
                app,
                repo_root=repo_root,
                mode=subcmd,
                tokens=rest_tokens,
            )

        if subcmd == "ask":
            if not rest_text:
                return await _command_output(
                    app,
                    "Usage: `/openwiki ask <architectural question>`",
                    success=False,
                )
            return await self._handle_ask(app, repo_root=repo_root, question=rest_text)

        if subcmd == "brief":
            if not rest_text:
                return await _command_output(
                    app,
                    "Usage: `/openwiki brief <standing convention or build/test instruction>`",
                    success=False,
                )
            return await self._handle_brief(app, repo_root=repo_root, note=rest_text)

        if subcmd == "link":
            return await self._handle_link(app, repo_root=repo_root)

        # Default fallback: treat arbitrary text after /openwiki as an architectural query
        return await self._handle_ask(app, repo_root=repo_root, question=raw_args)

    async def _handle_init_or_update(
        self,
        app: TalkOpsApp | Any,
        *,
        repo_root: Path,
        mode: str,
        tokens: list[str],
    ) -> CommandResult:
        """Execute `/openwiki init` or `/openwiki update` using native LangChain OpenWiki tools."""
        force = False
        brief_parts: list[str] = []

        idx = 0
        while idx < len(tokens):
            tok = tokens[idx]
            if tok in {"--force", "-f"}:
                force = True
            elif tok in {"--brief", "-b"} and idx + 1 < len(tokens):
                idx += 1
                brief_parts.append(tokens[idx])
            elif not tok.startswith("--"):
                brief_parts.append(tok)
            idx += 1

        brief = " ".join(brief_parts).strip()
        scaffold = bootstrap_openwiki_scaffold(repo_root)
        if scaffold.get("symlink_healed"):
            await _mount_system_notice(
                app,
                "Converted `CLAUDE.md -> AGENTS.md` symlink into `@AGENTS.md` pointer file "
                "to prevent OpenWiki marker collision.",
            )
        created = scaffold.get("created_files") or []
        if created:
            await _mount_system_notice(
                app,
                "Bootstrapped OpenWiki scaffold: " + ", ".join(f"`{c}`" for c in created),
            )

        await _invalidate_openwiki_session_state(app, lifecycle_active=True)

        # Compile baseline OpenWiki structure & claims deterministically first so
        # openwiki/index.md, openwiki/quickstart.md, and .claims/*.json exist immediately
        try:
            baseline = await asyncio.to_thread(
                compile_openwiki_deterministic,
                repo_root,
                mode=mode,  # type: ignore[arg-type]
                brief=brief,
                force=force,
            )
            if baseline.get("status") == "up_to_date":
                return await _command_output(
                    app,
                    f"OpenWiki is already up to date at commit `{baseline.get('currentHead', '')}`.",
                )
            compiled_pages = baseline.get("compiledPages") or []
            if compiled_pages:
                await _mount_system_notice(
                    app,
                    f"Compiled baseline OpenWiki ({len(compiled_pages)} pages, "
                    f"{baseline.get('totalClaims', 0)} Grounded Claims). "
                    "Launching agent skill to enrich architectural pages...",
                )
        except Exception:
            pass

        # Invoke the built-in `openwiki` skill using native LangChain OpenWiki tools
        action_desc = (
            "Initialize and compile the repository OpenWiki knowledge base (`mode='init'`)"
            if mode == "init"
            else "Incrementally update the repository OpenWiki knowledge base (`mode='update'`)"
        )
        prompt_parts = [
            f"{action_desc} at `{repo_root}`.",
            f"Call the native LangChain tool `openwiki_begin` with `mode='{mode}'`"
            + (" and `force=True`" if force else "")
            + (f" and `brief={brief!r}`." if brief else "."),
            "Follow the 6-step OpenWiki tool loop (`openwiki_begin` -> "
            "`openwiki_submit_plan` -> `openwiki_next_page` -> "
            "`openwiki_inspect_page_claims` -> `openwiki_submit_page` -> `openwiki_finish`). "
            "Do NOT run `openwiki` or `talkops openwiki` in the shell.",
        ]
        prompt_text = " ".join(prompt_parts)
        rewritten = f"/skill:openwiki {prompt_text}"
        if app is not None and hasattr(app, "_handle_skill_command"):
            await app._handle_skill_command(rewritten)
        elif app is not None and hasattr(app, "_handle_user_message"):
            await app._handle_user_message(prompt_text)
        return CommandResult(success=True, message=None, mount_as_app_message=False)

    async def _handle_ask(
        self,
        app: TalkOpsApp | Any,
        *,
        repo_root: Path,
        question: str,
    ) -> CommandResult:
        """Route an architectural question through OpenWiki retrieval tools."""
        status = inspect_openwiki_status(repo_root)
        if not status.get("initialized"):
            await _mount_system_notice(
                app,
                "Note: `openwiki/` is not initialized yet in this repository. "
                "Run `/openwiki init` to compile durable architectural pages, or the agent "
                "will answer by inspecting source files directly.",
            )
        prompt = (
            f"[OpenWiki Query] Answer the following repository question using "
            f"`openwiki_search` and `openwiki_read` first (checking `openwiki/` at `{repo_root}`), "
            f"and cite the relevant wiki pages and source files:\n\n{question}"
        )
        if app is not None and hasattr(app, "_handle_user_message"):
            await app._handle_user_message(prompt)
        return CommandResult(success=True, message=None, mount_as_app_message=False)

    async def _handle_brief(
        self,
        app: TalkOpsApp | Any,
        *,
        repo_root: Path,
        note: str,
    ) -> CommandResult:
        """Append standing repository guidance to ``openwiki/INSTRUCTIONS.md``."""
        bootstrap_openwiki_scaffold(repo_root, create_ignore=False, create_instructions=True)
        instructions_path = repo_root / "openwiki" / "INSTRUCTIONS.md"
        existing = (
            instructions_path.read_text(encoding="utf-8").rstrip()
            if instructions_path.is_file()
            else "# Repository Conventions & Verification Rules"
        )
        updated = f"{existing}\n- {note.strip()}\n"
        instructions_path.write_text(updated, encoding="utf-8")
        await _invalidate_openwiki_session_state(app, lifecycle_active=False)
        return await _command_output(
            app,
            f"Added standing instruction to `openwiki/INSTRUCTIONS.md`:\n> {note.strip()}\n\n"
            "Session `openwiki_digest` cache invalidated so the next turn picks up this convention.",
        )

    async def _handle_link(
        self,
        app: TalkOpsApp | Any,
        *,
        repo_root: Path,
    ) -> CommandResult:
        """Heal CLAUDE.md symlinks and update <!-- OPENWIKI:START --> block in AGENTS.md."""
        healed = ensure_claude_agents_symlink_safe(repo_root)
        openwiki_dir = repo_root / "openwiki"
        if openwiki_dir.is_dir():
            subsystem_pages = [
                parse_wiki_page(p)
                for p in sorted(openwiki_dir.glob("*.md"))
                if p.name.lower() not in {"instructions.md", "index.md", "quickstart.md"}
            ]
            upsert_agents_md_openwiki_pointer(repo_root, subsystem_pages)
        msg = (
            "Converted `CLAUDE.md -> AGENTS.md` symlink into `@AGENTS.md` pointer file and "
            "updated `<!-- OPENWIKI:START -->` block in `AGENTS.md`."
            if healed
            else "Updated `<!-- OPENWIKI:START -->` pointer block in `AGENTS.md`."
        )
        return await _command_output(app, msg)


__all__ = [
    "OpenWikiHandler",
    "format_openwiki_status_markdown",
]

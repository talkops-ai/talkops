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

"""Unit tests for OpenWiki middleware, slash command, CLI subcommand, and memory guard."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain.agents.middleware.types import ModelRequest, ToolCallRequest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.types import Command

from talkops.cli.main import build_parser, main
from talkops.commands import CommandContext, CommandRouter
from talkops.commands.power.openwiki import OpenWikiHandler
from talkops.memory.guard import (
    ONBOARDING_NAME_MEMORY_END,
    ONBOARDING_NAME_MEMORY_START,
    OPENWIKI_MEMORY_END,
    OPENWIKI_MEMORY_START,
    ManagedMemoryGuardMiddleware,
)
from talkops.middleware.openwiki import (
    OpenWikiContextMiddleware,
    ensure_claude_agents_symlink_safe,
)
from talkops.tools.openwiki_tools import (
    bootstrap_openwiki_scaffold,
    inspect_openwiki_status,
)
from talkops.ui.command_registry import get_command


def _make_tool_request(
    name: str,
    args: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    call_id: str = "call_ow_1",
) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args, "id": call_id},
        tool=None,
        state=state or {},
        runtime=MagicMock(),
    )


def test_ensure_claude_agents_symlink_safe(tmp_path: Path) -> None:
    """Verify CLAUDE.md -> AGENTS.md symlink is replaced with @AGENTS.md regular file."""
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text("# Team Memory\n", encoding="utf-8")
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.symlink_to(agents_md)

    assert claude_md.is_symlink()
    healed = ensure_claude_agents_symlink_safe(tmp_path)
    assert healed is True
    assert not claude_md.is_symlink()
    assert claude_md.read_text(encoding="utf-8") == "@AGENTS.md\n"
    # Repeat call is a no-op
    assert ensure_claude_agents_symlink_safe(tmp_path) is False


@pytest.mark.asyncio
async def test_openwiki_middleware_digest_and_drift(tmp_path: Path) -> None:
    """Verify before_agent builds digest with quickstart, instructions, and commit drift."""
    mw_empty = OpenWikiContextMiddleware(repo_root=tmp_path)
    empty_state = await mw_empty.abefore_agent({}, MagicMock())
    assert empty_state == {
        "openwiki_digest": {"has_wiki": False},
        "openwiki_grounded": True,
    }

    ow_dir = tmp_path / "openwiki"
    ow_dir.mkdir()
    (ow_dir / "quickstart.md").write_text("# Quickstart\n- See [[architecture]]\n", encoding="utf-8")
    (ow_dir / "INSTRUCTIONS.md").write_text("# Rules\n- Run pytest\n", encoding="utf-8")
    (ow_dir / ".last-update.json").write_text(
        json.dumps({"lastCommit": "1111111aaaa", "updatedAt": "2026-11-06T00:00:00Z"}),
        encoding="utf-8",
    )

    mw = OpenWikiContextMiddleware(repo_root=tmp_path)
    with patch.object(mw, "_get_git_head", return_value="2222222bbbb"):
        update = await mw.abefore_agent({}, MagicMock())

    assert update is not None
    assert update["openwiki_grounded"] is False
    digest = update["openwiki_digest"]
    assert digest["has_wiki"] is True
    assert "[[architecture]]" in digest["quickstart_routing"]
    assert "Run pytest" in digest["repo_conventions"]
    assert "1111111aaaa" in digest["drift_notice"]
    assert "2222222bbbb" in digest["drift_notice"]


@pytest.mark.asyncio
async def test_openwiki_middleware_model_call_injection_and_tool_gating(tmp_path: Path) -> None:
    """Verify awrap_model_call hides all OpenWiki tools on uninitialized repos and keeps fs tools on initialized repos."""
    mock_search_tool = SimpleNamespace(name="openwiki_search")
    mock_read_tool = SimpleNamespace(name="openwiki_read")
    mock_begin_tool = SimpleNamespace(name="openwiki_begin")
    mock_submit_tool = SimpleNamespace(name="openwiki_submit_page")
    mock_glob_tool = SimpleNamespace(name="glob")
    mock_grep_tool = SimpleNamespace(name="grep")

    captured_requests: list[ModelRequest] = []

    async def fake_handler(req: ModelRequest) -> Any:
        captured_requests.append(req)
        return MagicMock()

    # 1. Uninitialized repo (has_wiki=False), even with a stale .run.json on disk:
    #    All openwiki_* tools (retrieval + lifecycle) must be hidden on normal coding turns!
    ow_dir = tmp_path / "openwiki"
    ow_dir.mkdir()
    (ow_dir / ".run.json").write_text('{"phase": "planning"}', encoding="utf-8")

    mw_uninit = OpenWikiContextMiddleware(repo_root=tmp_path)
    uninit_state = mw_uninit.before_agent({}, MagicMock())
    assert uninit_state == {
        "openwiki_digest": {"has_wiki": False},
        "openwiki_grounded": True,
    }
    uninit_req = ModelRequest(
        model=MagicMock(),
        messages=[HumanMessage(content="Fix a bug in the router")],
        system_message=SystemMessage(content="Base system prompt"),
        tools=[
            mock_search_tool,
            mock_read_tool,
            mock_begin_tool,
            mock_submit_tool,
            mock_glob_tool,
            mock_grep_tool,
        ],
        state=uninit_state,
        runtime=MagicMock(),
    )
    await mw_uninit.awrap_model_call(uninit_req, fake_handler)
    assert len(captured_requests) == 1
    uninit_tool_names = [getattr(t, "name", "") for t in captured_requests[0].tools]
    assert uninit_tool_names == ["glob", "grep"]

    # 2. Initialized repo (has_wiki=True):
    #    Retrieval tools (openwiki_search, openwiki_read) AND filesystem tools (glob, grep)
    #    are available on normal turns, while lifecycle compilation tools stay hidden.
    (ow_dir / "quickstart.md").write_text("Route to [[middleware]]", encoding="utf-8")
    (ow_dir / "INSTRUCTIONS.md").write_text("Always run ruff", encoding="utf-8")

    mw = OpenWikiContextMiddleware(repo_root=tmp_path)
    state_update = mw.before_agent({}, MagicMock())
    assert state_update is not None

    request = ModelRequest(
        model=MagicMock(),
        messages=[HumanMessage(content="Fix a bug in the router")],
        system_message=SystemMessage(content="Base system prompt"),
        tools=[
            mock_search_tool,
            mock_read_tool,
            mock_begin_tool,
            mock_submit_tool,
            mock_glob_tool,
            mock_grep_tool,
        ],
        state=state_update,
        runtime=MagicMock(),
    )

    captured_requests.clear()
    await mw.awrap_model_call(request, fake_handler)
    assert len(captured_requests) == 1
    modified = captured_requests[0]

    tool_names = [getattr(t, "name", "") for t in modified.tools]
    assert tool_names == ["openwiki_search", "openwiki_read", "glob", "grep"]

    # System message should include the OpenWiki routing digest
    assert modified.system_message is not None
    full_sys_text = str(modified.system_message.content)
    assert "TalkOps.ai Durable Repository Context (OpenWiki)" in full_sys_text
    assert "Route to [[middleware]]" in full_sys_text
    assert "Always run ruff" in full_sys_text

    # When lifecycle turn is active, compilation tools remain exposed
    lifecycle_req = request.override(
        state={**state_update, "openwiki_lifecycle_active": True},
    )
    captured_requests.clear()
    await mw.awrap_model_call(lifecycle_req, fake_handler)
    lifecycle_tool_names = [getattr(t, "name", "") for t in captured_requests[0].tools]
    assert "openwiki_begin" in lifecycle_tool_names
    assert "openwiki_submit_page" in lifecycle_tool_names


@pytest.mark.asyncio
async def test_openwiki_middleware_tool_protection_and_grounding(tmp_path: Path) -> None:
    """Verify awrap_tool_call blocks protected files, marks grounding via Command, and enforces pre-edit guardrail."""
    ow_dir = tmp_path / "openwiki"
    claims_dir = ow_dir / ".claims"
    claims_dir.mkdir(parents=True)

    mw = OpenWikiContextMiddleware(repo_root=tmp_path, enforce_pre_edit_grounding=True)
    base_state = {
        "openwiki_digest": {"has_wiki": True},
        "openwiki_grounded": False,
    }

    async def ok_handler(req: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content="ok",
            name=req.tool_call["name"],
            tool_call_id=req.tool_call["id"],
        )

    # 1. Direct edits to openwiki/.claims/*.json or .last-update.json are blocked
    for protected_rel in (
        "openwiki/.claims/auth.json",
        "openwiki/.run.json",
        "openwiki/.last-update.json",
    ):
        req_protected = _make_tool_request(
            "write_file",
            {"file_path": str(tmp_path / protected_rel), "content": "{}"},
            state=base_state,
        )
        res_protected = await mw.awrap_tool_call(req_protected, ok_handler)
        assert isinstance(res_protected, ToolMessage)
        assert res_protected.status == "error"
        assert "Blocked direct modification" in str(res_protected.content)

    # 2. Pre-edit grounding blocks source edits before openwiki_search / openwiki_read
    req_edit_ungrounded = _make_tool_request(
        "edit_file",
        {"file_path": str(tmp_path / "src" / "app.py")},
        state=base_state,
    )
    res_ungrounded = await mw.awrap_tool_call(req_edit_ungrounded, ok_handler)
    assert isinstance(res_ungrounded, ToolMessage)
    assert res_ungrounded.status == "error"
    assert "You have not queried the repository's OpenWiki" in str(res_ungrounded.content)

    # 3. Calling openwiki_search returns a Command updating openwiki_grounded=True
    req_search = _make_tool_request(
        "openwiki_search",
        {"query": "router"},
        state=base_state,
    )
    res_search = await mw.awrap_tool_call(req_search, ok_handler)
    assert isinstance(res_search, Command)
    assert res_search.update is not None
    assert res_search.update["openwiki_grounded"] is True
    assert len(res_search.update["messages"]) == 1

    # 4. Once grounded, editing source files succeeds
    grounded_state = {**base_state, "openwiki_grounded": True}
    req_edit_grounded = _make_tool_request(
        "edit_file",
        {"file_path": str(tmp_path / "src" / "app.py")},
        state=grounded_state,
    )
    res_grounded = await mw.awrap_tool_call(req_edit_grounded, ok_handler)
    assert isinstance(res_grounded, ToolMessage)
    assert res_grounded.content == "ok"


def test_memory_guard_protects_openwiki_and_onboarding_blocks(tmp_path: Path) -> None:
    """Verify ManagedMemoryGuardMiddleware protects <!-- OPENWIKI:START --> blocks in AGENTS.md."""
    agents_md = tmp_path / "AGENTS.md"
    onboarding_block = (
        f"{ONBOARDING_NAME_MEMORY_START}\n"
        "- The user's name is Alice.\n"
        f"{ONBOARDING_NAME_MEMORY_END}"
    )
    openwiki_block = (
        f"{OPENWIKI_MEMORY_START}\n"
        "Consult `openwiki/index.md` and `openwiki/quickstart.md`.\n"
        f"{OPENWIKI_MEMORY_END}"
    )
    initial_content = (
        f"# Project Memory\n\n{onboarding_block}\n\n"
        f"## Notes\n- Keep tests green\n\n{openwiki_block}\n"
    )
    agents_md.write_text(initial_content, encoding="utf-8")

    guard = ManagedMemoryGuardMiddleware(guarded_paths=[agents_md])

    # Agent tries to tamper with the OPENWIKI block while adding a legitimate note
    tampered_content = (
        f"# Project Memory\n\n{onboarding_block}\n\n"
        "## Notes\n- Keep tests green\n- Use uv for python\n\n"
        f"{OPENWIKI_MEMORY_START}\nTAMPERED POINTER\n{OPENWIKI_MEMORY_END}\n"
    )
    req = _make_tool_request("write_file", {"file_path": str(agents_md)})

    def mutating_handler(_: ToolCallRequest) -> ToolMessage:
        agents_md.write_text(tampered_content, encoding="utf-8")
        return ToolMessage(content="written", name="write_file", tool_call_id="call_ow_1")

    result = guard.wrap_tool_call(req, mutating_handler)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"

    restored_disk = agents_md.read_text(encoding="utf-8")
    assert openwiki_block in restored_disk
    assert onboarding_block in restored_disk
    assert "- Use uv for python" in restored_disk
    assert "TAMPERED POINTER" not in restored_disk


@pytest.mark.asyncio
async def test_openwiki_slash_command_and_router(tmp_path: Path) -> None:
    """Verify /openwiki is registered in COMMAND_DESCRIPTORS and handles status, brief, init, and ask."""
    descriptor = get_command("/openwiki")
    assert descriptor is not None
    assert descriptor.name == "/openwiki"
    assert "/wiki" in descriptor.aliases

    router = CommandRouter()
    router.auto_discover()
    handler = router.get_handler("/openwiki")
    assert isinstance(handler, OpenWikiHandler)
    assert router.get_handler("/wiki") is handler

    class DummyApp:
        def __init__(self, cwd: Path) -> None:
            self._cwd = str(cwd)
            self._lc_thread_id = "thread-1"
            self._agent = SimpleNamespace(aupdate_state=AsyncMock())
            self.mounted_messages: list[str] = []
            self.user_messages: list[str] = []
            self.skill_commands: list[str] = []

        async def _mount_system(self, text: str) -> None:
            self.mounted_messages.append(text)

        async def _handle_user_message(self, prompt: str) -> None:
            self.user_messages.append(prompt)

        async def _handle_skill_command(self, cmd: str) -> None:
            self.skill_commands.append(cmd)

    app = DummyApp(tmp_path)

    # 1. /openwiki status
    ctx_status = CommandContext(
        app=app,
        session=None,
        agent=None,
        settings=None,
        raw_command="/openwiki status",
        args="status",
        thread_id="thread-1",
    )
    res_status = await handler.execute(ctx_status)
    assert res_status.success is True
    assert any("OpenWiki Repository Knowledge Base" in m for m in app.mounted_messages)

    # 2. /openwiki brief <note>
    app.mounted_messages.clear()
    ctx_brief = CommandContext(
        app=app,
        session=None,
        agent=None,
        settings=None,
        raw_command="/openwiki brief Always run pytest tests/unit_tests",
        args="brief Always run pytest tests/unit_tests",
        thread_id="thread-1",
    )
    res_brief = await handler.execute(ctx_brief)
    assert res_brief.success is True
    instructions_file = tmp_path / "openwiki" / "INSTRUCTIONS.md"
    assert instructions_file.is_file()
    assert "Always run pytest tests/unit_tests" in instructions_file.read_text(encoding="utf-8")
    app._agent.aupdate_state.assert_awaited()

    # 3. /openwiki init (Host-Driven MCP skill invocation)
    ctx_init = CommandContext(
        app=app,
        session=None,
        agent=None,
        settings=None,
        raw_command="/openwiki init Focus on middleware",
        args="init Focus on middleware",
        thread_id="thread-1",
    )
    res_init = await handler.execute(ctx_init)
    assert res_init.success is True
    assert len(app.skill_commands) == 1
    assert app.skill_commands[0].startswith("/skill:openwiki ")
    assert "openwiki_begin" in app.skill_commands[0]
    assert (tmp_path / ".openwikiignore").is_file()

    # 4. /openwiki ask <question>
    ctx_ask = CommandContext(
        app=app,
        session=None,
        agent=None,
        settings=None,
        raw_command="/openwiki ask How does middleware registration work?",
        args="ask How does middleware registration work?",
        thread_id="thread-1",
    )
    res_ask = await handler.execute(ctx_ask)
    assert res_ask.success is True
    assert len(app.user_messages) == 1
    assert "How does middleware registration work?" in app.user_messages[0]

    # 5. Real TalkOpsApp-shaped object (only _mount_message, no _mount_system)
    class RealTalkOpsAppStub:
        def __init__(self, cwd: Path) -> None:
            self._cwd = str(cwd)
            self._agent_thread_id = "thread-real"
            self._agent = SimpleNamespace(aupdate_state=AsyncMock())
            self.mounted_widgets: list[Any] = []
            self.skill_commands: list[str] = []

        async def _mount_message(self, widget: Any) -> None:
            self.mounted_widgets.append(widget)

        async def _handle_skill_command(self, cmd: str) -> None:
            self.skill_commands.append(cmd)

    real_stub = RealTalkOpsAppStub(tmp_path / "subrepo")
    (tmp_path / "subrepo").mkdir()
    ctx_real_init = CommandContext(
        app=real_stub,
        session=None,
        agent=None,
        settings=None,
        raw_command="/openwiki init",
        args="init",
        thread_id="thread-real",
    )
    res_real_init = await handler.execute(ctx_real_init)
    assert res_real_init.success is True
    assert len(real_stub.skill_commands) == 1
    assert real_stub.skill_commands[0].startswith("/skill:openwiki ")
    assert len(real_stub.mounted_widgets) >= 1

    ctx_real_status = CommandContext(
        app=real_stub,
        session=None,
        agent=None,
        settings=None,
        raw_command="/openwiki status",
        args="status",
        thread_id="thread-real",
    )
    res_real_status = await handler.execute(ctx_real_status)
    assert res_real_status.success is True
    assert res_real_status.mount_as_app_message is True
    assert res_real_status.message is not None
    assert "OpenWiki Repository Knowledge Base" in res_real_status.message


def test_openwiki_cli_subcommand(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify `talkops openwiki` CLI parser and subcommands."""
    parser = build_parser()
    parsed = parser.parse_args(["openwiki", "--cwd", str(tmp_path), "status", "--json"])
    assert parsed.subcommand == "openwiki"
    assert parsed.openwiki_action == "status"

    rc = main(["openwiki", "--cwd", str(tmp_path), "status", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["initialized"] is False
    assert payload["repo_root"] == str(tmp_path.resolve())

    rc_brief = main(
        ["openwiki", "--cwd", str(tmp_path), "brief", "Use ruff for linting", "--json"]
    )
    assert rc_brief == 0
    brief_out = json.loads(capsys.readouterr().out)
    assert brief_out["status"] == "ok"
    assert "Use ruff for linting" in (tmp_path / "openwiki" / "INSTRUCTIONS.md").read_text(
        encoding="utf-8"
    )


def test_openwiki_scaffold_and_builtin_skill_valid(tmp_path: Path) -> None:
    """Verify OpenWiki scaffold helper and built-in openwiki SKILL.md validation."""
    scaffold = bootstrap_openwiki_scaffold(tmp_path)
    assert (tmp_path / ".openwikiignore").is_file()
    assert (tmp_path / "openwiki" / "INSTRUCTIONS.md").is_file()
    status = inspect_openwiki_status(tmp_path)
    assert status["has_instructions"] is True
    assert status["has_openwikiignore"] is True
    assert scaffold["repo_root"] == str(tmp_path.resolve())

    # Validate built-in openwiki/SKILL.md with quick_validate.py
    repo_root = Path(__file__).parent.parent.parent
    validate_script = (
        repo_root
        / "src"
        / "talkops"
        / "built_in_skills"
        / "skill-creator"
        / "scripts"
        / "quick_validate.py"
    )
    openwiki_skill_dir = repo_root / "src" / "talkops" / "built_in_skills" / "openwiki"
    spec = importlib.util.spec_from_file_location("quick_validate", validate_script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    is_valid, msg = mod.validate_skill(openwiki_skill_dir)
    assert is_valid is True, msg


def test_openwiki_native_langchain_tools_lifecycle(tmp_path: Path) -> None:
    """Verify OpenWikiContextMiddleware binds all 10 native LangChain tools and executes the 6-step state machine."""
    src_dir = tmp_path / "src" / "demo"
    src_dir.mkdir(parents=True)
    router_py = src_dir / "router.py"
    router_py.write_text(
        '"""Demo router module."""\n\nclass RequestRouter:\n    def route(self, path: str) -> str:\n        return path\n',
        encoding="utf-8",
    )

    mw = OpenWikiContextMiddleware(repo_root=tmp_path)
    tools_by_name = {t.name: t for t in mw.tools}
    assert set(tools_by_name.keys()) == {
        "openwiki_search",
        "openwiki_read",
        "openwiki_list_wikis",
        "openwiki_list_workspaces",
        "openwiki_begin",
        "openwiki_submit_plan",
        "openwiki_next_page",
        "openwiki_inspect_page_claims",
        "openwiki_submit_page",
        "openwiki_finish",
    }

    # Step 1: openwiki_begin
    begin_res = json.loads(tools_by_name["openwiki_begin"].invoke({"mode": "init", "brief": "Focus on router"}))
    assert begin_res["status"] == "ready_for_plan"
    assert (tmp_path / "openwiki" / ".run.json").is_file()

    # Step 2: openwiki_submit_plan
    plan_res = json.loads(
        tools_by_name["openwiki_submit_plan"].invoke(
            {
                "pages": [
                    {
                        "slug": "routing-engine",
                        "title": "Routing Engine",
                        "description": "Core request routing architecture.",
                        "source_files": ["src/demo/router.py"],
                    }
                ]
            }
        )
    )
    assert plan_res["status"] == "plan_accepted"
    assert plan_res["plannedSlugs"] == ["routing-engine"]

    # Step 3: openwiki_next_page
    next_res = json.loads(tools_by_name["openwiki_next_page"].invoke({}))
    assert next_res["done"] is False
    assert next_res["pageBrief"]["slug"] == "routing-engine"

    # Step 4 & 5: openwiki_submit_page with Grounded Claims
    submit_res = json.loads(
        tools_by_name["openwiki_submit_page"].invoke(
            {
                "slug": "routing-engine",
                "title": "Routing Engine",
                "description": "Core request routing architecture.",
                "markdown": "## Overview\nThe `RequestRouter` dispatches incoming requests.\n\n## Dispatch Flow\nSee `src/demo/router.py`.",
                "claims": [
                    {"type": "file_exists", "path": "src/demo/router.py"},
                    {"type": "file_lines", "path": "src/demo/router.py", "startLine": 1, "endLine": 5},
                    {"type": "symbol", "path": "src/demo/router.py", "symbol": "RequestRouter"},
                ],
            }
        )
    )
    assert submit_res["status"] == "page_saved"
    assert submit_res["verifiedClaims"] == 3
    assert submit_res["remainingPlannedPages"] == 0

    # Verify claims via openwiki_inspect_page_claims
    claims_res = json.loads(
        tools_by_name["openwiki_inspect_page_claims"].invoke({"slug": "routing-engine"})
    )
    assert claims_res["hasExistingClaims"] is True
    assert claims_res["summary"]["verified"] == 3

    # Step 6: openwiki_finish
    finish_raw = tools_by_name["openwiki_finish"].invoke({"quickstart_markdown": ""})
    finish_data = json.loads(finish_raw)
    assert finish_data["status"] == "completed"
    assert (tmp_path / "openwiki" / "index.md").is_file()
    assert (tmp_path / "openwiki" / "quickstart.md").is_file()
    assert (tmp_path / "openwiki" / ".last-update.json").is_file()
    assert not (tmp_path / "openwiki" / ".run.json").exists()
    assert OPENWIKI_MEMORY_START in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")

    # Query via openwiki_search and openwiki_read
    search_raw = tools_by_name["openwiki_search"].invoke({"query": "RequestRouter dispatch"})
    search_data = json.loads(search_raw)
    assert search_data["results"][0]["slug"] == "routing-engine"

    read_raw = tools_by_name["openwiki_read"].invoke(
        {"slug": "routing-engine", "section": "Dispatch Flow"}
    )
    read_data = json.loads(read_raw)
    assert read_data["selected_section"] == "Dispatch Flow"
    assert "src/demo/router.py" in read_data["content"]
    assert str((tmp_path / "src" / "demo" / "router.py").resolve()) in read_data["verified_source_files"]


def test_openwiki_parallel_tool_calls_state_reducer() -> None:
    """Verify parallel openwiki_read tool calls in the same LangGraph step merge openwiki_grounded cleanly."""
    from langgraph.graph import START, StateGraph

    from talkops.middleware.openwiki import OpenWikiState

    builder = StateGraph(OpenWikiState)
    builder.add_node(
        "read_a",
        lambda _: Command(
            update={
                "openwiki_grounded": True,
                "messages": [ToolMessage(content="a", name="openwiki_read", tool_call_id="1")],
            }
        ),
    )
    builder.add_node(
        "read_b",
        lambda _: Command(
            update={
                "openwiki_grounded": True,
                "messages": [ToolMessage(content="b", name="openwiki_read", tool_call_id="2")],
            }
        ),
    )
    builder.add_edge(START, "read_a")
    builder.add_edge(START, "read_b")
    graph = builder.compile()
    result = graph.invoke({"messages": []})
    assert result["openwiki_grounded"] is True
    assert len(result["messages"]) == 2


@pytest.mark.asyncio
async def test_openwiki_subagent_and_js_eval_isolation(tmp_path: Path) -> None:
    """Verify subagents (via task or js_eval) and js_eval PTC never see any openwiki_* tools."""
    from talkops.agent.factory import _resolve_ptc_option, _subagent_cli_middleware
    from talkops.middleware.skills import PluginSkillsMiddleware
    from talkops.tools.openwiki_tools import build_openwiki_tools

    ow_dir = tmp_path / "openwiki"
    ow_dir.mkdir()
    (ow_dir / "quickstart.md").write_text(
        "Use this table via `openwiki_read(slug, section=...)` or `openwiki_search(query)`.\n"
        "- [[routing-engine]]",
        encoding="utf-8",
    )
    (ow_dir / "INSTRUCTIONS.md").write_text("- Always run pytest", encoding="utf-8")

    # 1. Subagent OpenWikiContextMiddleware(is_subagent=True) binds zero tools
    #    and strips any inherited openwiki_* tools even when the repo has an initialized wiki
    mw_sub = OpenWikiContextMiddleware(repo_root=tmp_path, is_subagent=True)
    assert mw_sub.tools == []

    state_update = mw_sub.before_agent({}, MagicMock())
    assert state_update is not None
    assert state_update["openwiki_digest"]["has_wiki"] is True

    all_ow_tools = build_openwiki_tools(tmp_path)
    fs_tools = [
        SimpleNamespace(name="read_file"),
        SimpleNamespace(name="glob"),
        SimpleNamespace(name="grep"),
    ]
    sub_req = ModelRequest(
        model=MagicMock(),
        messages=[HumanMessage(content="/openwiki init investigate routing")],
        system_message=SystemMessage(content="Subagent prompt"),
        tools=[*all_ow_tools, *fs_tools],
        state={**state_update, "openwiki_lifecycle_active": True},
        runtime=MagicMock(),
    )
    captured: list[ModelRequest] = []

    async def fake_handler(req: ModelRequest) -> Any:
        captured.append(req)
        return MagicMock()

    await mw_sub.awrap_model_call(sub_req, fake_handler)
    assert len(captured) == 1
    sub_tool_names = [getattr(t, "name", "") for t in captured[0].tools]
    assert sub_tool_names == ["read_file", "glob", "grep"]
    sub_sys_text = str(captured[0].system_message.content)
    assert "Subagent Source Grounding Rule" in sub_sys_text
    assert "openwiki_read" not in sub_sys_text
    assert "openwiki_search" not in sub_sys_text

    # 2. Even a non-subagent OpenWikiContextMiddleware strips openwiki_* tools when
    #    invoked with a subagent runtime config (e.g. forked subagent or task/js_eval dispatch)
    mw_parent = OpenWikiContextMiddleware(repo_root=tmp_path, is_subagent=False)
    fork_runtime = SimpleNamespace(config={"configurable": {"ls_agent_type": "subagent"}})
    fork_req = sub_req.override(runtime=fork_runtime)
    captured.clear()
    await mw_parent.awrap_model_call(fork_req, fake_handler)
    assert [getattr(t, "name", "") for t in captured[0].tools] == ["read_file", "glob", "grep"]

    # 3. _subagent_cli_middleware configures OpenWikiContextMiddleware(is_subagent=True)
    #    and PluginSkillsMiddleware hides the built-in 'openwiki' skill from subagents
    sub_mws = _subagent_cli_middleware(
        has_explicit_model=True,
        assistant_id="talkops",
        subagent_name="general-purpose",
        worktree_root=tmp_path,
        include_telemetry=False,
    )
    ow_mws = [m for m in sub_mws if isinstance(m, OpenWikiContextMiddleware)]
    assert len(ow_mws) == 1
    assert ow_mws[0].is_subagent is True
    assert ow_mws[0].tools == []

    skill_mws = [m for m in sub_mws if isinstance(m, PluginSkillsMiddleware)]
    assert len(skill_mws) == 1
    assert skill_mws[0]._is_skill_allowed("openwiki") is False

    # 4. _resolve_ptc_option never exposes openwiki_* tools to js_eval PTC (tools.*)
    ptc_all = _resolve_ptc_option(
        "all",
        tools=[*all_ow_tools, *fs_tools],
        acknowledge_unsafe=True,
        auto_approve=True,
    )
    assert ptc_all == ["glob", "grep", "read_file"]

    ptc_explicit = _resolve_ptc_option(
        ["read_file", "openwiki_search", "openwiki_read"],
        tools=[*all_ow_tools, *fs_tools],
        acknowledge_unsafe=True,
        auto_approve=True,
    )
    assert ptc_explicit == ["read_file"]


def test_openwiki_fresh_session_codebase_delta_and_claim_drift(tmp_path: Path) -> None:
    """Verify fresh session detects committed and working-tree changes and affected wiki pages."""
    import subprocess as sp

    from talkops.tools.openwiki_tools import (
        build_openwiki_tools,
        compile_openwiki_deterministic,
        compute_openwiki_codebase_delta,
    )

    # Initialize a real Git repo in tmp_path
    sp.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    sp.run(["git", "config", "user.email", "test@talkops.ai"], cwd=tmp_path, check=True)
    sp.run(["git", "config", "user.name", "TalkOps Test"], cwd=tmp_path, check=True)

    pkg_dir = tmp_path / "src" / "talkops" / "agent"
    pkg_dir.mkdir(parents=True)
    router_file = pkg_dir / "router.py"
    router_file.write_text(
        '"""Agent router."""\n\nclass RequestRouter:\n    def dispatch(self) -> str:\n        return "v1"\n',
        encoding="utf-8",
    )
    sp.run(["git", "add", "."], cwd=tmp_path, check=True)
    sp.run(["git", "commit", "-m", "Initial commit"], cwd=tmp_path, check=True, capture_output=True)

    # Compile OpenWiki at commit 1
    compile_res = compile_openwiki_deterministic(tmp_path, mode="init")
    assert compile_res["status"] == "completed"

    # Commit a change to router.py AND create an uncommitted working-tree file
    router_file.write_text(
        '"""Agent router v2."""\n\nclass RenamedRouter:\n    def dispatch_v2(self) -> str:\n        return "v2"\n',
        encoding="utf-8",
    )
    sp.run(["git", "add", "src/talkops/agent/router.py"], cwd=tmp_path, check=True)
    sp.run(
        ["git", "commit", "-m", "Refactor router to RenamedRouter"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    uncommitted_file = pkg_dir / "new_helper.py"
    uncommitted_file.write_text("def helper() -> bool:\n    return True\n", encoding="utf-8")

    # Verify live codebase delta computation
    delta = compute_openwiki_codebase_delta(tmp_path)
    assert delta["head_drifted"] is True
    assert "src/talkops/agent/router.py" in delta["committed_changed_files"]
    assert "src/talkops/agent/new_helper.py" in delta["working_tree_changed_files"]
    assert "src/talkops/agent/router.py" in delta["changed_files"]
    assert "src/talkops/agent/new_helper.py" in delta["changed_files"]
    assert any("Refactor router to RenamedRouter" in c for c in delta["recent_commits"])
    assert "agent" in delta["affected_pages"]

    # On a fresh session, before_agent populates openwiki_digest with the full delta
    mw = OpenWikiContextMiddleware(repo_root=tmp_path)
    fresh_state = mw.before_agent({}, MagicMock())
    assert fresh_state is not None
    digest = fresh_state["openwiki_digest"]
    assert digest["has_wiki"] is True
    assert "src/talkops/agent/router.py" in digest["changed_files"]
    assert "src/talkops/agent/new_helper.py" in digest["changed_files"]
    assert "agent" in digest["affected_pages"]
    assert "src/talkops/agent/router.py" in digest["drift_notice"]
    assert "`agent`" in digest["drift_notice"]

    # Calling openwiki_read('agent') surfaces modified_source_files, drifted_claims, and freshness_warning
    tools_by_name = {t.name: t for t in build_openwiki_tools(tmp_path)}
    read_payload = json.loads(tools_by_name["openwiki_read"].invoke({"slug": "agent"}))
    assert "src/talkops/agent/router.py" in read_payload["modified_source_files"]
    assert len(read_payload["drifted_claims"]) >= 1
    assert "freshness_warning" in read_payload




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

"""Unit tests for TalkOps middleware stack and repository bounds security."""
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from talkops.middleware._repository_bounds import (
    RepositoryBounds,
)
from talkops.middleware.ask_user import AskUserMiddleware
from talkops.middleware.registry import MiddlewareRegistry


def test_middleware_registry_discovery():
    """Verify middleware registry discovery."""
    reg = MiddlewareRegistry.ensure_discovered()
    registered = reg.list_registered()

    # Core Terminal & Cloud DevOps middlewares must be registered
    assert "ask_user" in registered
    assert "auto_mode_hitl" in registered
    assert "cost_tracking" in registered
    assert "local_context" in registered
    assert "mcp_tool" in registered
    assert "model_retry" in registered
    assert "resume_state" in registered
    assert "skills" in registered
    assert "subagents" in registered
    assert "webhook_sre" in registered

    # Web-specific baggage must NOT be registered
    assert "a2ui_buffer" not in registered
    assert "hitl_middleware" not in registered
    assert "human_in_the_loop" not in registered
    assert "hitl_helpers" not in registered
    assert "skill_shortcut" not in registered


def test_middleware_build_stack():
    """Verify middleware build stack."""
    reg = MiddlewareRegistry.ensure_discovered()
    # Build stack with exclude
    stack = reg.build_stack(exclude={"ask_user", "skills", "model_retry"})
    classes = [type(m).__name__ for m in stack]
    assert "AskUserMiddleware" not in classes
    assert "CodeModelRetryMiddleware" not in classes


def test_middleware_build_single():
    """Verify middleware build single."""
    reg = MiddlewareRegistry.ensure_discovered()
    mcp_mw = reg.build_middleware("mcp_tool")
    assert type(mcp_mw).__name__ == "MCPToolMiddleware"

    retry_mw = reg.build_middleware("model_retry", max_retries=5)
    assert type(retry_mw).__name__ == "CodeModelRetryMiddleware"
    assert getattr(retry_mw, "max_retries", None) == 5

    with pytest.raises(KeyError, match="not registered"):
        reg.build_middleware("non_existent_middleware")


def test_repository_bounds_safe_path():
    """Verify repository bounds safe path."""
    none_backend: Any = None
    bounds = RepositoryBounds(backend=none_backend, root="/workspace/project")
    assert bounds.root == "/workspace/project"

    # Valid child paths
    assert bounds.safe_path("/workspace/project/src/main.py") is True
    assert bounds.safe_path("/workspace/project/sub/dir/file.txt") is True

    # Path traversal attempts
    assert bounds.safe_path("/workspace/project/../etc/passwd") is False
    assert bounds.safe_path("/etc/passwd") is False
    assert bounds.safe_path("~/credentials") is False
    assert bounds.safe_path("relative/path") is False


def test_repository_bounds_safe_pattern():
    """Verify repository bounds safe pattern."""
    assert RepositoryBounds.safe_pattern("*.py") is True
    assert RepositoryBounds.safe_pattern("src/**/*.ts") is True
    assert RepositoryBounds.safe_pattern("../**/*.py") is False
    assert RepositoryBounds.safe_pattern("..") is False


def test_ask_user_middleware_instantiation():
    """Verify ask user middleware instantiation."""
    middleware = AskUserMiddleware()
    assert middleware is not None


def test_webhook_sre_prompt_excludes_coding_engine_and_preserves_footer():
    """Verify webhook sre prompt excludes coding engine and preserves footer."""
    from talkops.prompts import (
        MODEL_IDENTITY_RE,
        build_model_identity_section,
        get_base_system_prompt,
        get_webhook_sre_system_prompt,
    )

    base_prompt = get_base_system_prompt(
        interactive=False,
        cwd="/workspace/ops",
        model_name="gpt-5.6-luna",
        model_provider="openai-chat",
        cloud_provider="aws",
    )
    assert "Engine 1: Platform Engineering & DevOps Coding Specialist" in base_prompt
    assert "Platform Engineering & DevOps Coding Discipline" in base_prompt

    sre_prompt = get_webhook_sre_system_prompt(
        interactive=False,
        cwd="/workspace/ops",
        model_name="gpt-5.6-luna",
        model_provider="openai-chat",
        cloud_provider="aws",
        webhook_provider="alertmanager",
        webhook_event_type="alertmanager.firing",
        webhook_incident_id="inc-1234",
        source_addon="webhooks",
    )
    assert "Autonomous SRE & Cloud Incident Root Cause Analysis (RCA) Orchestrator" in sre_prompt
    assert "Standardized Incident RCA Deliverable Protocol" in sre_prompt
    assert "Adaptive Output Granularity Contract" in sre_prompt
    assert "query_service_blast_radius" in sre_prompt
    assert "Engine 1: Platform Engineering & DevOps Coding Specialist" not in sre_prompt
    assert "Platform Engineering & DevOps Coding Discipline" not in sre_prompt
    assert "### Skills Directory" not in sre_prompt
    assert "### Model Identity" in sre_prompt
    assert "### Current Working Directory" in sre_prompt
    assert "### Tool Approval & Security Policy" in sre_prompt
    assert "### Active Webhook / Incident Channel Context" in sre_prompt
    assert "`alertmanager`" in sre_prompt
    assert "### Cloud Platform Context: AWS" in sre_prompt

    # Verify ChatOps channel mode (e.g. Slack) differentiates human channel messages from automated webhooks
    slack_sre_prompt = get_webhook_sre_system_prompt(
        interactive=False,
        cwd="/workspace/ops",
        model_name="gpt-5.6-luna",
        model_provider="openai-chat",
        cloud_provider="aws",
        source_addon="channels",
        channel="slack",
        has_webhook_alert_prompt=False,
    )
    assert "Centralized SRE & Incident ChatOps mode (`slack`)" in slack_sre_prompt
    assert "skip `write_todos` for greetings, capability questions" in slack_sre_prompt

    # Verify ConfigurableModelMiddleware's MODEL_IDENTITY_RE still patches the SRE prompt cleanly
    new_identity = build_model_identity_section("claude-sonnet-4-6", provider="anthropic")
    patched = MODEL_IDENTITY_RE.sub(new_identity, sre_prompt, count=1)
    assert "claude-sonnet-4-6" in patched


def test_webhook_sre_detection_and_system_message_swap():
    """Verify webhook sre detection and system message swap."""
    from deepagents.middleware._utils import append_to_system_message
    from langchain.agents.middleware.types import ModelRequest, ModelResponse

    from talkops.agent.config import CLIContextSchema
    from talkops.middleware.unified_system_message import unify_system_message
    from talkops.middleware.webhook_sre import (
        WebhookSREMiddleware,
        is_webhook_sre_turn,
    )
    from talkops.prompts import get_base_system_prompt

    mock_model = MagicMock(spec=BaseChatModel)
    base_prompt = get_base_system_prompt(
        interactive=False,
        cwd="/workspace/ops",
        model_name="gpt-5.6-luna",
        model_provider="openai-chat",
    )

    # Simulate core deepagents middlewares (FilesystemMiddleware + SummarizationMiddleware)
    sys_msg = SystemMessage(content=base_prompt)
    sys_msg = append_to_system_message(sys_msg, "## Shell paths vs. virtual paths\nHost path mappings...")
    sys_msg = append_to_system_message(sys_msg, "<conversation_transcript>\nTranscript info\n</conversation_transcript>")

    mw = WebhookSREMiddleware(
        cwd="/workspace/ops",
        model_name="gpt-5.6-luna",
        model_provider="openai-chat",
        cloud_provider="aws",
    )

    # 1. Normal interactive TUI turn -> should NOT swap prompt
    normal_ctx = CLIContextSchema.from_payload({"thread_id": "t-normal", "interactive": True})
    normal_runtime: Any = SimpleNamespace(context=normal_ctx, config={"configurable": {"thread_id": "t-normal"}})
    assert is_webhook_sre_turn({"messages": [HumanMessage(content="Write a Terraform module")]}, normal_runtime) is False

    captured_normal: list[ModelRequest] = []

    def _normal_handler(req: ModelRequest) -> ModelResponse:
        captured_normal.append(req)
        return ModelResponse(result=[AIMessage(content="ok")])

    req_normal = ModelRequest(
        model=mock_model,
        messages=[HumanMessage(content="Write a Terraform module")],
        system_message=sys_msg,
        state={"messages": [HumanMessage(content="Write a Terraform module")]},
        runtime=normal_runtime,
    )
    mw.wrap_model_call(req_normal, _normal_handler)
    assert len(captured_normal) == 1
    assert "Engine 1: Platform Engineering & DevOps Coding Specialist" in (captured_normal[0].system_prompt or "")

    # 2. Webhook / incident channel turn -> MUST swap base prompt to SRE/RCA while preserving all downstream blocks
    webhook_ctx = CLIContextSchema.from_payload(
        {
            "thread_id": "t-webhook",
            "interactive": False,
            "source_addon": "webhooks",
            "webhook_provider": "alertmanager",
            "webhook_event_type": "alertmanager.firing",
            "webhook_incident_id": "inc-oom-1",
        }
    )
    webhook_runtime: Any = SimpleNamespace(
        context=webhook_ctx,
        config={
            "configurable": {"thread_id": "t-webhook"},
            "metadata": {"owner": "webhook", "source_addon": "webhooks"},
        },
    )
    state_update: Any = mw.before_agent({"messages": []}, webhook_runtime)
    assert state_update is not None
    assert state_update.get("_webhook_sre_mode") is True

    captured_webhook: list[ModelRequest] = []

    def _webhook_handler(req: ModelRequest) -> ModelResponse:
        # Simulate downstream middlewares appending their sections after WebhookSREMiddleware
        msg = append_to_system_message(req.system_message, "<agent_memory>\nMemory\n</agent_memory>")
        msg = append_to_system_message(msg, "## Skills System\n- eks-skill")
        msg = append_to_system_message(msg, "<custom_commands>\n- deep-check\n</custom_commands>")
        msg = append_to_system_message(msg, "### Interpreter\nAn `js_eval` tool is available.")
        msg = append_to_system_message(msg, "## Local Context\nAWS Operational Environment")
        msg = append_to_system_message(msg, "## Non-Blocking Background Command Execution")
        msg = append_to_system_message(msg, "## Subagent Delegation & Orchestration Architecture")
        unified = unify_system_message(msg)
        captured_webhook.append(req.override(system_message=unified))
        return ModelResponse(result=[AIMessage(content="rca")])

    req_webhook = ModelRequest(
        model=mock_model,
        messages=[HumanMessage(content="[WEBHOOK ALERT] Provider: ALERTMANAGER | Event: alertmanager.firing")],
        system_message=sys_msg,
        state=state_update,
        runtime=webhook_runtime,
    )
    mw.wrap_model_call(req_webhook, _webhook_handler)
    assert len(captured_webhook) == 1
    final_prompt = captured_webhook[0].system_prompt or ""

    # Base prompt is now SRE/RCA (no coding engine)
    assert "Autonomous SRE & Cloud Incident Root Cause Analysis (RCA) Orchestrator" in final_prompt
    assert "Engine 1: Platform Engineering & DevOps Coding Specialist" not in final_prompt
    assert "Platform Engineering & DevOps Coding Discipline" not in final_prompt

    # All core & downstream middleware sections are preserved in exact order
    expected_ordered_markers = [
        "# TalkOps — Autonomous SRE & Cloud Incident Root Cause Analysis (RCA) Orchestrator",
        "### Model Identity",
        "### Active Webhook / Incident Channel Context",
        "### Cloud Platform Context: AWS",
        "## Shell paths vs. virtual paths",
        "<conversation_transcript>",
        "<agent_memory>",
        "## Skills System",
        "<custom_commands>",
        "### Interpreter",
        "## Local Context",
        "## Non-Blocking Background Command Execution",
        "## Subagent Delegation & Orchestration Architecture",
    ]
    positions = [final_prompt.index(marker) for marker in expected_ordered_markers]
    assert positions == sorted(positions)
    assert "### Skills Directory" not in final_prompt


def test_cross_middleware_deduplication_and_list_content_blocks(tmp_path) -> None:
    """Verify unify_system_message deduplicates Skills/CWD/AWS/OpenWiki and list-block middlewares inject cleanly."""
    from deepagents.middleware._utils import append_to_system_message
    from langchain.agents.middleware.types import ModelRequest, ModelResponse

    from talkops.middleware.background_commands import BackgroundCommandMiddleware
    from talkops.middleware.context_authority import ContextAuthorityMiddleware
    from talkops.middleware.unified_system_message import unify_system_message
    from talkops.prompts import get_webhook_sre_system_prompt

    mock_model = MagicMock(spec=BaseChatModel)
    sre_prompt = get_webhook_sre_system_prompt(
        cwd=str(tmp_path),
        model_name="gemini-3.1-pro",
        model_provider="google",
        cloud_provider="aws",
        source_addon="channels",
        channel="slack",
    )

    # Start with multi-block SystemMessage (matching FilesystemMiddleware + SummarizationMiddleware)
    msg = SystemMessage(content=sre_prompt)
    msg = append_to_system_message(msg, "## Shell paths vs. virtual paths\nHost shell info")
    assert isinstance(msg.content, list)

    # 1. ContextAuthorityMiddleware MUST inject <context_authority> even when SystemMessage.content is list[dict]
    auth_mw = ContextAuthorityMiddleware(enable_tools=False)
    captured_after_auth: list[ModelRequest] = []

    def _auth_handler(req: ModelRequest) -> ModelResponse:
        captured_after_auth.append(req)
        return ModelResponse(result=[AIMessage(content="ok")])

    req_auth = ModelRequest(
        model=mock_model,
        messages=[HumanMessage(content="check checkout-api")],
        system_message=msg,
        state={
            "context_authority_bundle": {
                "tier1_execution_profile": "hitl-gated",
                "tier2_preflight": {},
                "tier4_blast_radius": {},
                "tier5_calibrated_incidents": [],
                "tier5_memory_rules": [],
            }
        },
    )
    auth_mw.wrap_model_call(req_auth, _auth_handler)
    msg_after_auth = captured_after_auth[0].system_message
    assert msg_after_auth is not None

    # 2. Simulate Memory (with duplicate OpenWiki + headless guidelines), Skills, Interpreter, LocalContext, and OpenWiki
    msg_chain = append_to_system_message(
        msg_after_auth,
        "<agent_memory>\n/Users/op/.talkops/AGENTS.md\n\n## User Preferences\n- Concise\n\n"
        "/repo/AGENTS.md\n\n## OpenWiki Durable Repository Knowledge Base\n- [`openwiki/k8s.md`](openwiki/k8s.md)\n</agent_memory>\n\n"
        "<memory_guidelines>\n**Working autonomously:**\n- No operator is available to answer follow-up questions.\n</memory_guidelines>",
    )
    msg_chain = append_to_system_message(msg_chain, "## Skills System\n- **remember**: save memory")
    from talkops.middleware.unified_system_message import compact_interpreter_prompt

    msg_chain = append_to_system_message(
        msg_chain,
        compact_interpreter_prompt(
            "## Code Execution & Programmatic Tool Calling\n\n### Interpreter\n\nAn `js_eval` tool is available.\n\n"
            "### API Reference — `tools` namespace\n\n/** doc */\n```typescript\ntools.readFile(input: { file_path: string; }): Promise<unknown>\n```\n"
        ),
    )
    msg_chain = append_to_system_message(
        msg_chain,
        f"## Local Context\n\n**Current Directory**: `{tmp_path}`\n\n**AWS Operational Environment**:\n- Active Profile: `prod`",
    )
    msg_chain = append_to_system_message(
        msg_chain,
        "## TalkOps.ai Durable Repository Context (OpenWiki)\n\n### Architecture Routing Table (openwiki/quickstart.md)\n| k8s |",
    )
    assert isinstance(msg_chain.content, list)

    # 3. BackgroundCommandMiddleware MUST inject instructions even when SystemMessage.content is list[dict]
    bg_mw = BackgroundCommandMiddleware(working_dir=tmp_path)
    captured_after_bg: list[ModelRequest] = []

    def _bg_handler(req: ModelRequest) -> ModelResponse:
        captured_after_bg.append(req)
        return ModelResponse(result=[AIMessage(content="ok")])

    req_bg = ModelRequest(
        model=mock_model,
        messages=[HumanMessage(content="check checkout-api")],
        system_message=msg_chain,
        state={},
    )
    bg_mw.wrap_model_call(req_bg, _bg_handler)
    unified = unify_system_message(captured_after_bg[0].system_message)
    assert unified is not None
    final_text = str(unified.content)

    assert "</context_authority>" in final_text
    assert "[Tier 1 · Policy Ceiling] Execution Trust Profile: hitl-gated" in final_text
    assert "[Live Environment Precedence]" in final_text
    assert "`<agent_memory>`." in final_text
    assert "## Runtime Identity, Governance & Channel Context" in final_text
    assert "\n---\n" not in final_text
    assert "No operator is available" not in final_text
    assert "Channel Collaboration" in final_text
    assert "A `js_eval` tool is available." in final_text
    assert "Invoke `execute`, `call_mcp_tool`" in final_text
    assert "/tmp/talkops_" in final_text
    assert "## Non-Blocking Background Command Execution" in final_text
    # Deduplicated sections
    assert "### Skills Directory" not in final_text
    assert "### Current Working Directory" not in final_text
    assert "**Current Directory**:" in final_text
    assert "### Cloud Platform Context: AWS" not in final_text
    assert "**AWS Operational Environment**:" in final_text
    assert "## OpenWiki Durable Repository Knowledge Base" not in final_text
    assert "## TalkOps.ai Durable Repository Context (OpenWiki)" in final_text
    assert "## User Preferences" in final_text


def test_local_shell_backend_glob_and_grep_prune_venv_and_pycache(tmp_path) -> None:
    """Verify LocalShellBackend.glob and _python_search prune .venv/__pycache__ unless explicitly targeted."""
    from talkops.backend.local import LocalShellBackend

    # Create real project files alongside heavy .venv and __pycache__ trees
    src_file = tmp_path / "k8s" / "prod-payments-deployment.yaml"
    src_file.parent.mkdir(parents=True)
    src_file.write_text("image: payments-checkout-api:v2.14.0\n", encoding="utf-8")

    venv_file = tmp_path / ".venv" / "lib" / "site-packages" / "payments_helper.py"
    venv_file.parent.mkdir(parents=True)
    venv_file.write_text("VERSION = 'payments-checkout-api:v2.14.0'\n", encoding="utf-8")

    pycache_file = tmp_path / "src" / "__pycache__" / "payments.cpython-314.pyc"
    pycache_file.parent.mkdir(parents=True)
    pycache_file.write_bytes(b"payments-checkout-api")

    backend = LocalShellBackend(root_dir=tmp_path)

    # Default glob should prune .venv and __pycache__
    glob_res = backend.glob("**/*payment*", path=str(tmp_path))
    assert not glob_res.truncated
    matched_paths = [m["path"] for m in (glob_res.matches or [])]
    assert str(src_file) in matched_paths
    assert all(".venv" not in p and "__pycache__" not in p for p in matched_paths)

    # Explicit .venv glob should still descend into .venv when requested
    explicit_venv_glob = backend.glob(".venv/**/*payment*", path=str(tmp_path))
    explicit_paths = [m["path"] for m in (explicit_venv_glob.matches or [])]
    assert str(venv_file) in explicit_paths

    # Python grep fallback should also prune .venv and __pycache__
    matches, truncated, err = backend._python_search("payments-checkout-api", tmp_path, None)
    assert not truncated
    assert err is None
    assert str(src_file) in matches
    assert str(venv_file) not in matches


def test_compact_interpreter_prompt_shrinks_quickjs_sections() -> None:
    """Verify compact_interpreter_prompt compacts verbose QuickJS task essay and PTC JSDoc blocks."""
    from talkops.middleware.unified_system_message import (
        compact_interpreter_prompt,
        unify_system_message,
    )

    verbose_prompt = (
        "### Interpreter\n\n"
        "An `js_eval` tool is available. It runs JavaScript in a persistent REPL.\n"
        "- Timeout: 30s per call. Memory: 64 MB total.\n\n"
        "### Dispatching Subagents with `task`\n\n"
        "#### The primitive\n\n"
        + ("Verbose generic SQL injection example in /src/a.ts. " * 120)
        + "\n### API Reference — `tools` namespace\n\n"
        "Example shape — substitute real tool names:\n\n"
        "```typescript\nconst users = await tools.findUsers({ name: \"Ada\" });\n```\n\n"
        "```typescript\n"
        "/** Reads a file from the filesystem. */\n"
        "tools.readFile(input: {\n"
        "  /**\n *Absolute path to the file to read.\n */ file_path: string;\n"
        "  /**\n *Line number.\n */ offset?: number;\n"
        "}): Promise<{ content: string; tool_call_id: string }>\n"
        "```\n\n"
        "## Local Context\n"
        "Current Directory: `/workspace`"
    )

    compacted = compact_interpreter_prompt(verbose_prompt)
    assert len(compacted) < len(verbose_prompt) // 3
    assert "### Interpreter" in compacted
    assert "QuickJS VM CPU time per call" in compacted
    assert "### Dispatching Subagents with `task`" in compacted
    assert "subagentType" in compacted
    assert "responseSchema" in compacted
    assert "return results;" not in compacted
    assert "results; // bare final expression" in compacted
    assert "Verbose generic SQL injection example" not in compacted
    assert "Example shape — substitute real tool names:" not in compacted
    assert "tools.readFile(input: { file_path: string; offset?: number; }): Promise<unknown>" in compacted
    assert "## Local Context" in compacted

    # Idempotent and works through unify_system_message
    unified = unify_system_message(SystemMessage(content=verbose_prompt))
    assert unified is not None
    assert unified.content == compacted
    assert compact_interpreter_prompt(compacted) == compacted


def test_regular_tui_session_excludes_all_sre_workflow_tools_and_middlewares(tmp_path, monkeypatch) -> None:
    """Verify regular TUI / CLI sessions never attach SRE workflow tools or SRE middlewares."""
    from unittest.mock import patch

    from talkops.agent.factory import create_talkops_agent

    for var in (
        "TALKOPS_SERVER_PROFILE",
        "TALKOPS_PROFILE",
        "TALKOPS_ADDONS",
        "TALKOPS_CENTRAL_SERVER",
    ):
        monkeypatch.delenv(var, raising=False)

    sre_only_tools = {
        "record_hypothesis_verdict",
        "query_service_blast_radius",
        "list_recent_infrastructure_changes",
        "search_incident_memory",
        "record_operational_memory_rule",
        "artifact_query",
    }
    sre_only_middlewares = {
        "WebhookSREMiddleware",
        "ContextAuthorityMiddleware",
        "AlertStormDrainMiddleware",
        "ToolPayloadShaperMiddleware",
    }

    mock_model = MagicMock(spec=BaseChatModel)
    mock_model.bind_tools = MagicMock(return_value=mock_model)
    mock_model.profile = {}

    # 1. Regular TUI session (interactive=True, no server profile) -> ZERO SRE tools or middlewares
    with patch("talkops.agent.factory.create_deep_agent", return_value=MagicMock()) as mock_create:
        create_talkops_agent(
            model=mock_model,
            cwd=tmp_path,
            interactive=True,
            auto_approve=False,
            no_subagents=True,
            _defer_mcp_preload=True,
        )
        call_kw = mock_create.call_args.kwargs
        mw_list = call_kw["middleware"]
        mw_class_names = {type(m).__name__ for m in mw_list}
        assert not (mw_class_names & sre_only_middlewares), f"Leaked SRE middlewares in TUI: {mw_class_names & sre_only_middlewares}"

        attached_tool_names = {getattr(t, "name", "") for t in call_kw["tools"]}
        for m in mw_list:
            for t in getattr(m, "tools", None) or ():
                attached_tool_names.add(getattr(t, "name", ""))
        assert not (attached_tool_names & sre_only_tools), f"Leaked SRE tools in TUI: {attached_tool_names & sre_only_tools}"

    # 2. Central server `--profile sre` -> SRE tools and middlewares ARE attached
    monkeypatch.setenv("TALKOPS_CENTRAL_SERVER", "1")
    monkeypatch.setenv("TALKOPS_SERVER_PROFILE", "sre")
    with patch("talkops.agent.factory.create_deep_agent", return_value=MagicMock()) as mock_create_sre:
        create_talkops_agent(
            model=mock_model,
            cwd=tmp_path,
            interactive=False,
            auto_approve=True,
            no_subagents=True,
            _defer_mcp_preload=True,
        )
        sre_kw = mock_create_sre.call_args.kwargs
        sre_mw_list = sre_kw["middleware"]
        sre_mw_names = {type(m).__name__ for m in sre_mw_list}
        assert sre_only_middlewares.issubset(sre_mw_names)

        sre_tool_names = {getattr(t, "name", "") for t in sre_kw["tools"]}
        for m in sre_mw_list:
            for t in getattr(m, "tools", None) or ():
                sre_tool_names.add(getattr(t, "name", ""))
        assert sre_only_tools.issubset(sre_tool_names)




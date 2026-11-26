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

"""Unit tests for TalkOps browser session lifecycle, TalkOpsBrowserMiddleware, and /browser command."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from talkops.browser.session import (
    BrowserConnectionConfig,
    BrowserConnectionError,
    BrowserSessionManager,
    discover_devtools_active_port,
    parse_devtools_active_port_file,
)
from talkops.commands._base import CommandContext
from talkops.commands.power.browser import (
    BROWSER_SUBAGENT_DIRECTIVE,
    BrowserHandler,
)
from talkops.commands.registry import CommandRegistry
from talkops.middleware.browser import (
    CURRENT_SNAPSHOT_TAG,
    TalkOpsBrowserMiddleware,
    format_and_offload_snapshot,
    prune_stale_snapshots_in_messages,
    validate_browser_navigation_urls,
)
from talkops.middleware.jev_model_router import _resolve_active_capabilities
from talkops.security.url_validation import UrlValidationError

# ─────────────────────────────────────────────────────────────────────────────
# 1. Point 3: DevToolsActivePort & 3-Tier BrowserSessionManager Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_devtools_active_port_valid(tmp_path: Path) -> None:
    """Verify parse devtools active port valid."""
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9222\n/devtools/browser/abc-123-def\n", encoding="utf-8")

    ws_url = parse_devtools_active_port_file(port_file)
    assert ws_url == "ws://127.0.0.1:9222/devtools/browser/abc-123-def"


def test_parse_devtools_active_port_errors(tmp_path: Path) -> None:
    """Verify parse devtools active port errors."""
    missing = tmp_path / "DevToolsActivePort"
    with pytest.raises(BrowserConnectionError, match="chrome://inspect/#remote-debugging"):
        parse_devtools_active_port_file(missing)

    missing.write_text("9222\n", encoding="utf-8")
    with pytest.raises(BrowserConnectionError, match="Invalid DevToolsActivePort"):
        parse_devtools_active_port_file(missing)

    missing.write_text("not_a_port\n/devtools/browser/123\n", encoding="utf-8")
    with pytest.raises(BrowserConnectionError, match="Invalid port"):
        parse_devtools_active_port_file(missing)

    missing.write_text("70000\n/devtools/browser/123\n", encoding="utf-8")
    with pytest.raises(BrowserConnectionError, match="Port out of range"):
        parse_devtools_active_port_file(missing)


def test_discover_devtools_active_port_finds_explicit_dir(tmp_path: Path) -> None:
    """Verify discover devtools active port finds explicit dir."""
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9333\n/devtools/browser/xyz-999\n", encoding="utf-8")

    discovered = discover_devtools_active_port(user_data_dir=tmp_path)
    assert discovered is not None
    ws_url, path = discovered
    assert ws_url == "ws://127.0.0.1:9333/devtools/browser/xyz-999"
    assert path == port_file


@pytest.mark.asyncio
async def test_session_manager_tier2_auto_connect_and_non_destructive_close(
    tmp_path: Path,
) -> None:
    """Verify session manager tier2 auto connect and non destructive close."""
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9222\n/devtools/browser/live-user-chrome\n", encoding="utf-8")

    cfg = BrowserConnectionConfig(
        mode="auto",
        user_data_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
    )
    manager = BrowserSessionManager(config=cfg)

    mock_browser = MagicMock()
    mock_browser.disconnect = AsyncMock()
    mock_browser.close = AsyncMock()

    mock_stagehand = MagicMock()
    mock_facade = MagicMock()
    mock_facade.__aenter__ = AsyncMock(return_value=mock_facade)
    mock_facade.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("stagehand.browser.local_browser.connect", new=AsyncMock(return_value=mock_browser)) as mock_connect,
        patch("stagehand.Stagehand.create", new=AsyncMock(return_value=mock_stagehand)),
        patch("stagehand_middleware.facade.tools.StagehandFacadeTools", return_value=mock_facade),
    ):
        facade = await manager.get_facade()
        assert facade is mock_facade
        assert manager.is_connected is True
        assert manager.connection_tier == "tier2_auto_connect"
        assert manager.connected_endpoint == "ws://127.0.0.1:9222/devtools/browser/live-user-chrome"
        mock_connect.assert_awaited_once_with(
            cdp_url="ws://127.0.0.1:9222/devtools/browser/live-user-chrome"
        )

        # Closing a Tier 2 (live user Chrome) session MUST call disconnect(), NOT close()!
        await manager.close()
        mock_browser.disconnect.assert_awaited_once()
        mock_browser.close.assert_not_awaited()
        assert manager.is_connected is False


@pytest.mark.asyncio
async def test_session_manager_tier3_persistent_fallback(tmp_path: Path) -> None:
    """Verify session manager tier3 persistent fallback."""
    empty_chrome_dir = tmp_path / "no-devtools-port"
    empty_chrome_dir.mkdir()
    profile_dir = tmp_path / "persistent-profile"

    cfg = BrowserConnectionConfig(
        mode="persistent",
        user_data_dir=profile_dir,
        artifacts_dir=tmp_path / "artifacts",
    )
    manager = BrowserSessionManager(config=cfg)

    mock_browser = MagicMock()
    mock_browser.disconnect = AsyncMock()
    mock_browser.close = AsyncMock()
    mock_stagehand = MagicMock()
    mock_stagehand.close = AsyncMock()
    mock_facade = MagicMock()

    with (
        patch("stagehand.browser.local_browser.launch", new=AsyncMock(return_value=mock_browser)) as mock_launch,
        patch("stagehand.Stagehand.create", new=AsyncMock(return_value=mock_stagehand)),
        patch("stagehand_middleware.facade.tools.StagehandFacadeTools", return_value=mock_facade),
    ):
        await manager.get_facade()
        assert manager.connection_tier == "tier3_persistent"
        assert Path(manager.connected_endpoint or "") == profile_dir.resolve()
        mock_launch.assert_awaited_once()
        call_kwargs = mock_launch.await_args.kwargs
        assert call_kwargs["preserve_user_data_dir"] is True
        assert call_kwargs["keep_alive"] is True

        await manager.close()
        mock_browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_manager_strict_auto_connect_raises_troubleshooting_error(
    tmp_path: Path,
) -> None:
    """Verify session manager strict auto connect raises troubleshooting error."""
    cfg = BrowserConnectionConfig(
        mode="auto_connect",
        user_data_dir=tmp_path / "empty_dir",
    )
    manager = BrowserSessionManager(config=cfg)
    with (
        patch("talkops.browser.session.default_chrome_user_data_dirs", return_value=[]),
        pytest.raises(BrowserConnectionError, match="chrome://inspect/#remote-debugging"),
    ):
        await manager.get_facade()


# ─────────────────────────────────────────────────────────────────────────────
# 2. Point 1: TalkOpsBrowserMiddleware (4 Tools + Snapshot Offloading + Pruning)
# ─────────────────────────────────────────────────────────────────────────────


def test_format_and_offload_snapshot_under_and_over_8kb(tmp_path: Path) -> None:
    # 1. Under 8KB
    """Verify format and offload snapshot under and over 8kb."""
    small_snap = "- button 'Sign in' [ref=12]\n- textbox 'Email' [ref=13]"
    formatted, snap_path, full_path = format_and_offload_snapshot(
        small_snap,
        artifacts_dir=tmp_path,
        max_bytes=8192,
    )
    assert full_path is None
    assert snap_path.is_file()
    assert f"<{CURRENT_SNAPSHOT_TAG}" in formatted
    assert "[ref=12]" in formatted
    assert "Snapshot truncated" not in formatted

    # 2. Over 8KB (e.g. 12KB accessibility tree)
    large_snap = "\n".join(f"- link 'Item {i}' [ref={i}]" for i in range(600))
    assert len(large_snap.encode("utf-8")) > 8192

    formatted_large, snap_path_large, full_path_large = format_and_offload_snapshot(
        large_snap,
        artifacts_dir=tmp_path,
        max_bytes=8192,
    )
    assert full_path_large is not None
    assert full_path_large.is_file()
    assert full_path_large.read_text(encoding="utf-8") == large_snap
    assert snap_path_large.is_file()
    assert "Snapshot truncated because it exceeded 8KB" in formatted_large
    assert str(full_path_large) in formatted_large
    # Head + Tail preservation: both top of page (Item 0) and bottom of page (Item 599) stay in context
    assert "Item 0" in formatted_large
    assert "Item 599" in formatted_large


def test_prune_stale_snapshots_keeps_only_latest_snapshot() -> None:
    """Verify prune stale snapshots keeps only latest snapshot."""
    msg1 = HumanMessage(content="Open page")
    snap1 = ToolMessage(
        content='<current_snapshot path="/tmp/snap_1.txt">\n- button [ref=1]\n</current_snapshot>',
        tool_call_id="tc_1",
    )
    msg2 = AIMessage(content="Clicked button 1")
    snap2 = ToolMessage(
        content='<current_snapshot path="/tmp/snap_2.txt">\n- button [ref=2]\n</current_snapshot>',
        tool_call_id="tc_2",
    )

    pruned = prune_stale_snapshots_in_messages([msg1, snap1, msg2, snap2])
    assert len(pruned) == 4
    # First snapshot is compacted to a 1-line pointer
    assert "Previous page snapshot superseded" in str(pruned[1].content)
    assert "/tmp/snap_1.txt" in str(pruned[1].content)
    assert "[ref=1]" not in str(pruned[1].content)
    # Latest snapshot remains intact
    assert "[ref=2]" in str(pruned[3].content)


def test_validate_browser_navigation_urls() -> None:
    # Localhost allowed by default for local web-app testing
    """Verify validate browser navigation urls."""
    validate_browser_navigation_urls(
        actions=[{"type": "goto", "url": "http://localhost:3000/dashboard"}],
        allow_localhost=True,
        validate_ssrf=True,
    )

    # Disallowed scheme blocked
    with pytest.raises(UrlValidationError, match="scheme not allowed"):
        validate_browser_navigation_urls(
            actions=[{"type": "goto", "url": "file:///etc/passwd"}],
        )

    # Cloud metadata IP blocked by SSRF guard
    with pytest.raises(UrlValidationError, match="blocked address"):
        validate_browser_navigation_urls(
            code="await page.goto('http://169.254.169.254/latest/meta-data/')",
            allow_localhost=True,
            validate_ssrf=True,
        )


@pytest.mark.asyncio
async def test_middleware_four_tools_execution(tmp_path: Path) -> None:
    """Verify middleware four tools execution."""
    mock_facade = MagicMock()
    mock_facade.snapshot = AsyncMock(return_value="- heading 'Dashboard' [ref=1]\n- button 'Deploy' [ref=2]")
    mock_facade.run = AsyncMock(return_value="Executed 1 action: click ref=2")
    png_bytes = b"\x89PNG\r\n\x1a\nfake-image"
    mock_facade.screenshot = AsyncMock(
        return_value={
            "data": base64.b64encode(png_bytes).decode("ascii"),
            "mimeType": "image/png",
        }
    )

    cfg = BrowserConnectionConfig(artifacts_dir=tmp_path / "browser_artifacts")
    mock_manager = MagicMock(spec=BrowserSessionManager)
    mock_manager.config = cfg
    mock_manager.get_facade = AsyncMock(return_value=mock_facade)
    mock_manager.close = AsyncMock()

    mw = TalkOpsBrowserMiddleware(session_manager=mock_manager, validate_ssrf=False)
    tool_by_name = {t.name: t for t in mw.tools}
    assert set(tool_by_name.keys()) == {"run", "snapshot", "screenshot", "browser_handoff"}

    # 1. Test `snapshot`
    snap_out = await tool_by_name["snapshot"].ainvoke({"includeIframes": True})
    assert "<current_snapshot" in snap_out
    assert "[ref=2]" in snap_out

    # 2. Test `run` with declarative actions
    run_out = await tool_by_name["run"].ainvoke(
        {"actions": [{"type": "click", "ref": 2}]}
    )
    assert "Executed 1 action" in run_out

    # 3. Test `run` syntax error rejection via tree-sitter
    bad_js_out = await tool_by_name["run"].ainvoke({"code": "await page.click(;;;"})
    assert "Invalid JavaScript syntax" in str(bad_js_out)

    # 4. Test `screenshot` persistence + multimodal output
    shot_out = await tool_by_name["screenshot"].ainvoke({"type": "png"})
    assert isinstance(shot_out, list)
    assert shot_out[0]["type"] == "image"
    assert shot_out[0]["mime_type"] == "image/png"
    assert "Screenshot saved to" in shot_out[1]["text"]
    saved_files = list((tmp_path / "browser_artifacts" / "screenshots").glob("*.png"))
    assert len(saved_files) == 1
    assert saved_files[0].read_bytes() == png_bytes

    # 5. Test `browser_handoff` interrupt & automatic post-handoff snapshot
    with patch(
        "talkops.middleware.browser.interrupt",
        return_value={"status": "answered", "answers": ["completed"]},
    ) as mock_interrupt:
        cmd = await tool_by_name["browser_handoff"].ainvoke(
            {
                "name": "browser_handoff",
                "args": {
                    "reason": "Please complete Okta 2FA login",
                    "url": "https://company.okta.com",
                },
                "id": "tc_handoff_1",
                "type": "tool_call",
            }
        )
        mock_interrupt.assert_called_once()
        msgs = cmd.update["messages"]
        assert len(msgs) == 1
        assert isinstance(msgs[0], ToolMessage)
        assert "User confirmed browser handoff completed" in msgs[0].content
        assert "Updated page snapshot after handoff" in msgs[0].content
        assert "[ref=1]" in msgs[0].content


# ─────────────────────────────────────────────────────────────────────────────
# 3. Point 2: /browser Command & Jev Capabilities Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_browser_command_handler_and_registry() -> None:
    """Verify browser command handler and registry."""
    BrowserSessionManager.reset_instance()
    handler = BrowserHandler()
    assert handler.name == "/browser"
    assert "/web" in handler.aliases
    assert handler.model_facing_description is not None

    # 1. Status subcommand
    ctx_status = CommandContext(app=None, raw_command="/browser status", args="status")
    res_status = await handler.execute(ctx_status)
    assert res_status.success is True
    assert "TalkOps Browser Agent" in (res_status.message or "")

    # 2. Mode switch subcommand
    ctx_mode = CommandContext(
        app=None,
        raw_command="/browser mode auto_connect",
        args="mode auto_connect",
    )
    res_mode = await handler.execute(ctx_mode)
    assert res_mode.success is True
    assert BrowserSessionManager.get_instance().config.mode == "auto_connect"

    # 3. Task delegation with <SUBAGENT> directive
    mock_app = MagicMock()
    mock_app.send_agent_message = AsyncMock()
    ctx_task = CommandContext(
        app=mock_app,
        raw_command="/browser Check Grafana alerts on localhost:3000",
        args="Check Grafana alerts on localhost:3000",
    )
    res_task = await handler.execute(ctx_task)
    assert res_task.success is True
    assert res_task.mount_as_app_message is False
    mock_app.send_agent_message.assert_awaited_once()
    sent_prompt = mock_app.send_agent_message.await_args.args[0]
    assert BROWSER_SUBAGENT_DIRECTIVE in sent_prompt
    assert "Check Grafana alerts on localhost:3000" in sent_prompt

    # 4. CommandRegistry auto-discovery finds /browser and /web
    CommandRegistry.reset()
    reg = CommandRegistry.get_instance()
    assert reg.get_handler("/browser") is not None
    assert reg.get_handler("/web") is not None

    # 5. UI command_registry includes /browser with /web attached as canonical alias
    from talkops.ui.command_registry import get_all_entries, get_command

    ui_cmd = get_command("/browser")
    assert ui_cmd is not None
    assert ui_cmd.name == "/browser"
    assert ui_cmd.description == "Invoke a browser agent for web tasks"
    entries_by_name = {entry.name: entry for entry in get_all_entries()}
    assert "/browser" in entries_by_name
    assert "/web" not in entries_by_name
    assert "/web" in entries_by_name["/browser"].aliases


def test_jev_model_router_browser_capabilities() -> None:
    """Verify jev model router browser capabilities."""
    orch_caps = _resolve_active_capabilities(
        is_subagent=False,
        available_subagents=["general-purpose", "browser"],
    )
    assert "browser_web_automation" in orch_caps

    sub_caps = _resolve_active_capabilities(
        is_subagent=True,
        subagent_name="browser",
        subagent_meta={"tools": ["snapshot", "run", "screenshot", "browser_handoff"]},
    )
    assert "browser_web_automation" in sub_caps
    assert "multimodal_dom_and_visual_inspection" in sub_caps


def test_browser_subagent_telemetry_and_scoped_middleware() -> None:
    """Verify browser subagent telemetry and scoped middleware."""
    from talkops.agent.factory import _subagent_cli_middleware
    from talkops.middleware.skills import PluginSkillsMiddleware
    from talkops.middleware.subagent_telemetry import _format_tool_activity
    from talkops.middleware.tool_filter import ToolFilterMiddleware

    assert (
        _format_tool_activity(
            "run",
            {"code": "await page.goto('https://nudgebee.com/discover/ai-agents-cloudops');"},
        )
        == "browser: goto https://nudgebee.com/discover/ai-agents-cloudops"
    )
    assert (
        _format_tool_activity(
            "run",
            {"actions": [{"op": "click", "id": "0-34"}, {"op": "fill", "id": "0-42"}]},
        )
        == "browser: click [0-34], fill [0-42]"
    )
    assert _format_tool_activity("snapshot", {}) == "browser: snapshot page"
    assert _format_tool_activity("screenshot", {}) == "browser: capture screenshot"
    assert (
        _format_tool_activity("browser_handoff", {"reason": "Solve Cloudflare Turnstile"})
        == "browser handoff: Solve Cloudflare Turnstile"
    )

    mw_list = _subagent_cli_middleware(
        has_explicit_model=False,
        assistant_id="talkops",
        subagent_name="browser",
        allowed_tools=[
            "snapshot",
            "run",
            "screenshot",
            "browser_handoff",
            "read_file",
            "write_file",
            "edit_file",
            "ls",
            "glob",
            "grep",
            "ask_user",
        ],
        allowed_skills=[],
        skill_sources_override=(),
    )
    assert any(isinstance(mw, ToolFilterMiddleware) for mw in mw_list)
    assert not any(isinstance(mw, PluginSkillsMiddleware) for mw in mw_list)


@pytest.mark.asyncio
async def test_destructive_browser_action_hitl_approval_gate(tmp_path: Path) -> None:
    """Verify destructive browser action hitl approval gate."""
    from talkops.middleware.browser import (
        detect_destructive_browser_actions,
        parse_snapshot_element_labels,
    )

    sample_snapshot = (
        "[0-2] RootWebArea: SaaS Admin Console\n"
        "  [0-10] button: Save Changes\n"
        "  [0-11] button: Remove filter\n"
        "  [0-12] button: Close modal\n"
        "  [0-61] button: Delete Application\n"
        "  [0-75] menuitem: Revoke API Token\n"
    )
    labels = parse_snapshot_element_labels(sample_snapshot)
    assert labels["0-61"] == "button: Delete Application"
    assert labels["0-75"] == "menuitem: Revoke API Token"

    # Benign clicks do not trigger destructive gate
    assert (
        detect_destructive_browser_actions(
            actions=[
                {"op": "click", "id": "0-10"},
                {"op": "click", "id": "0-11"},
                {"op": "click", "id": "0-12"},
            ],
            snapshot_elements=labels,
        )
        == []
    )

    # Destructive clicks and JS DELETE calls are detected
    detected = detect_destructive_browser_actions(
        actions=[{"op": "click", "id": "0-61"}],
        snapshot_elements=labels,
    )
    assert len(detected) == 1
    assert "Delete Application" in detected[0]

    detected_js = detect_destructive_browser_actions(
        code="await fetch('/api/v1/apps/prod', { method: 'DELETE' });",
    )
    assert len(detected_js) == 1
    assert "DELETE" in detected_js[0]

    # End-to-end HITL gate in TalkOpsBrowserMiddleware.run
    cfg = BrowserConnectionConfig(mode="persistent", artifacts_dir=tmp_path)
    mock_facade = MagicMock()
    mock_facade.snapshot = AsyncMock(return_value=sample_snapshot)
    mock_facade.run = AsyncMock(return_value="Executed")
    mock_manager = MagicMock(spec=BrowserSessionManager)
    mock_manager.config = cfg
    mock_manager.get_facade = AsyncMock(return_value=mock_facade)

    mw = TalkOpsBrowserMiddleware(session_manager=mock_manager, max_snapshot_bytes=8192)
    tools_by_name = {t.name: t for t in mw.tools}

    # 1. Capture snapshot so middleware caches element labels
    await tools_by_name["snapshot"].ainvoke({"includeIframes": True})

    # 2. Safe click executes without interrupt
    with patch("talkops.middleware.browser.interrupt") as mock_interrupt:
        res_safe = await tools_by_name["run"].ainvoke(
            {"actions": [{"op": "click", "id": "0-10"}]}
        )
        assert res_safe == "Executed"
        mock_interrupt.assert_not_called()

    # 3. Destructive click triggers interrupt; when rejected, facade.run is NOT called
    mock_facade.run.reset_mock()
    with patch(
        "talkops.middleware.browser.interrupt",
        return_value={"status": "ok", "answers": ["reject"]},
    ) as mock_interrupt:
        res_rejected = await tools_by_name["run"].ainvoke(
            {"actions": [{"op": "click", "id": "0-61"}]}
        )
        mock_interrupt.assert_called_once()
        mock_facade.run.assert_not_awaited()
        assert "Action blocked: User rejected destructive browser action" in res_rejected

    # 4. Destructive click when approved proceeds to facade.run
    mock_facade.run.reset_mock()
    with patch(
        "talkops.middleware.browser.interrupt",
        return_value={"status": "ok", "answers": ["approve"]},
    ) as mock_interrupt:
        res_approved = await tools_by_name["run"].ainvoke(
            {"actions": [{"op": "click", "id": "0-61"}]}
        )
        mock_interrupt.assert_called_once()
        mock_facade.run.assert_awaited_once()
        assert res_approved == "Executed"


def test_browser_devops_sre_prompt_and_blast_radius_gates() -> None:
    """Verify DevOps/SRE-first browser prompt playbooks and production blast-radius gates."""
    from talkops.middleware.browser import (
        BROWSER_SAFETY_AND_HANDOFF_SECTION,
        BROWSER_SUBAGENT_SYSTEM_PROMPT,
        CHROME_DEFAULT_PROFILE_SECTION,
        detect_destructive_browser_actions,
        parse_snapshot_element_labels,
    )

    # 1. Top-level `#` -> `##` -> `###` Markdown hierarchy + DevOps/SRE playbooks + universal fallback
    assert BROWSER_SUBAGENT_SYSTEM_PROMPT.startswith(
        "# TalkOps Browser — Autonomous DevOps, SRE & Web Automation Subagent\n\nYou are TalkOps Browser,"
    )
    assert "\n## Autonomous Web & Dashboard Operations Architecture\n" in BROWSER_SUBAGENT_SYSTEM_PROMPT
    assert "\n### 1. Core Browser Loop" in BROWSER_SUBAGENT_SYSTEM_PROMPT
    assert "DevOps, CloudOps & SRE UI Playbooks" in BROWSER_SUBAGENT_SYSTEM_PROMPT
    assert "Grafana" in BROWSER_SUBAGENT_SYSTEM_PROMPT
    assert "Datadog" in BROWSER_SUBAGENT_SYSTEM_PROMPT
    assert "Argo CD" in BROWSER_SUBAGENT_SYSTEM_PROMPT
    assert "time-range picker" in BROWSER_SUBAGENT_SYSTEM_PROMPT
    assert "Region selector" in BROWSER_SUBAGENT_SYSTEM_PROMPT
    assert "General SaaS & Any Other Web UI" in BROWSER_SUBAGENT_SYSTEM_PROMPT

    # 2. Tool boundary accuracy: no hallucinated `grep_search` or shell `execute` instructions
    assert "grep_search" not in BROWSER_SUBAGENT_SYSTEM_PROMPT
    assert "\n## Subagent Execution & Anti-Recursion Discipline\n" in BROWSER_SUBAGENT_SYSTEM_PROMPT

    # 3. Middleware system_prompt_section carries headed 4-tool surface, SSO & blast-radius guidance
    mw = TalkOpsBrowserMiddleware(session_manager=MagicMock(spec=BrowserSessionManager))
    prompt_sec = mw.system_prompt_section
    assert prompt_sec.startswith("## Browser Tool Surface (Stagehand Playwright Facade)\n\n")
    assert "through four tools:" in prompt_sec
    assert "through exactly three tools:" not in prompt_sec
    assert "- browser_handoff:" in prompt_sec
    assert CHROME_DEFAULT_PROFILE_SECTION in prompt_sec
    assert BROWSER_SAFETY_AND_HANDOFF_SECTION in prompt_sec
    assert "AWS IAM Identity Center" in prompt_sec
    assert "Drain Node" in prompt_sec

    # 4. DevOps-specific destructive vs benign dashboard labels
    devops_snapshot = (
        "[0-1] RootWebArea: Argo CD & Cluster Console\n"
        "  [0-10] button: Clear selection\n"
        "  [0-11] button: Reset zoom\n"
        "  [0-20] button: Drain Node\n"
        "  [0-21] button: Prune Resources\n"
        "  [0-22] button: Force Sync\n"
        "  [0-23] menuitem: Rotate Secret\n"
        "  [0-24] menuitem: Invalidate Token\n"
    )
    labels = parse_snapshot_element_labels(devops_snapshot)
    assert (
        detect_destructive_browser_actions(
            actions=[
                {"op": "click", "id": "0-10"},
                {"op": "click", "id": "0-11"},
            ],
            snapshot_elements=labels,
        )
        == []
    )
    for target_id in ("0-20", "0-21", "0-22", "0-23", "0-24"):
        reasons = detect_destructive_browser_actions(
            actions=[{"op": "click", "id": target_id}],
            snapshot_elements=labels,
        )
        assert len(reasons) == 1


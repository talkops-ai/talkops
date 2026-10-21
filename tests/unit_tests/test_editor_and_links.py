"""Unit tests for external editor opening and Markdown link handling."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from talkops.editor import (
    TERMINAL_EDITORS,
    extract_file_path,
    open_file_in_editor,
    resolve_editor,
)
from talkops.ui.widgets._links import (
    autolink_file_paths,
    open_link_or_file_async,
)


def test_resolve_editor_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify resolve_editor checks TALKOPS_EDITOR, VISUAL, then EDITOR."""
    monkeypatch.delenv("TALKOPS_EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)

    # All unset -> platform default
    default_cmd = resolve_editor()
    assert default_cmd is not None
    assert len(default_cmd) >= 1

    # EDITOR set
    monkeypatch.setenv("EDITOR", "nano")
    assert resolve_editor() == ["nano"]

    # VISUAL overrides EDITOR
    monkeypatch.setenv("VISUAL", "vim")
    assert resolve_editor() == ["vim"]

    # TALKOPS_EDITOR overrides VISUAL
    monkeypatch.setenv("TALKOPS_EDITOR", "cursor")
    assert resolve_editor() == ["cursor"]


def test_autolink_file_paths() -> None:
    """Verify autolink_file_paths wraps bare file paths without altering code or punctuation."""
    # Simple path with trailing period
    text = "Report generated at /Users/test/report.md."
    linked = autolink_file_paths(text)
    assert linked == "Report generated at [/Users/test/report.md](file:///Users/test/report.md)."

    # Path in parentheses
    text_paren = "See (/var/log/audit.log) for details"
    assert (
        autolink_file_paths(text_paren)
        == "See ([/var/log/audit.log](file:///var/log/audit.log)) for details"
    )

    # Relative and tilde paths
    text_rel = "Output written to ./out/result.json and ~/notes.txt."
    assert (
        autolink_file_paths(text_rel)
        == "Output written to [./out/result.json](file://./out/result.json) and [~/notes.txt](file://~/notes.txt)."
    )

    # Preserves code blocks
    code_block = "```\n/etc/hosts\n```"
    assert autolink_file_paths(code_block) == code_block

    # Preserves inline code
    inline_code = "Use `cat /etc/passwd` to view"
    assert autolink_file_paths(inline_code) == inline_code

    # Preserves existing markdown links
    existing_link = "Download [report](/Users/test/report.md) now"
    assert autolink_file_paths(existing_link) == existing_link

    # Does not link math or command flags
    math_text = "1 / 2 and /help or and/or"
    assert autolink_file_paths(math_text) == math_text

    # Does not link slash-separated technical terms or bare URLs
    slash_terms = "Core microservices, backend services (ECS Fargate/EKS/EC2), CI/CD/Pipeline"
    assert autolink_file_paths(slash_terms) == slash_terms

    bare_url = "Docs at https://docs.talkops.dev/guide/getting-started."
    assert autolink_file_paths(bare_url) == bare_url


def test_assistant_markdown_parses_file_links_and_table_tooltips_safely() -> None:
    """Verify AssistantMarkdown renders file:// links and table cell tooltips never crash with MarkupError."""
    import talkops._textual_patches  # noqa: F401
    from textual.widgets._markdown import MarkdownTable, MarkdownTableContent
    from textual.widgets._tooltip import Tooltip
    from talkops.ui.widgets.chat_messages import _assistant_markdown_class

    md_cls = _assistant_markdown_class()
    md = md_cls()

    # 1. file:// links are parsed as links instead of raw literal markdown syntax
    blocks = md._build_from_source(
        "Report generated at [/Users/test/report.md](file:///Users/test/report.md)."
    )
    assert blocks[0]._content.plain == "Report generated at /Users/test/report.md."
    assert len(blocks[0]._content.spans) == 1

    # 2. Table cells with bracket-slash text use Content tooltips and Tooltip.update handles raw [/...] safely
    table_md = (
        "| Layer | Services |\n"
        "|---|---|\n"
        "| Compute | Core microservices, backend services (ECS Fargate[/EKS/EC2](file:///EKS/EC2)) |\n"
        "| Regex | Pattern `[/EKS/EC2]` or literal [/EKS/EC2] tag |\n"
    )
    table_blocks = md._build_from_source(table_md)
    table = table_blocks[0]
    assert isinstance(table, MarkdownTable)
    headers, rows = table._get_headers_and_rows()
    table_content = MarkdownTableContent(headers, rows)
    cells = list(table_content.compose())

    tooltip = Tooltip()
    for cell_widget in cells:
        assert cell_widget.tooltip is not None
        tooltip.update(cell_widget.tooltip)

    # Direct raw string with unmatched closing tag also must not raise MarkupError
    tooltip.update("Core microservices, backend services (ECS Fargate[/EKS/EC2](file:///EKS/EC2))")



def test_open_file_in_editor_nonexistent(tmp_path: Path) -> None:
    """Verify non-existent file returns False and optionally notifies."""
    fake_path = tmp_path / "does_not_exist.txt"
    app = MagicMock()

    result = open_file_in_editor(str(fake_path), app=app)
    assert result is False
    app.notify.assert_called_once()
    assert "File not found" in app.notify.call_args[0][0]


def test_open_file_in_editor_terminal_editor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify terminal editor suspends app and calls subprocess.run."""
    sample = tmp_path / "report.md"
    sample.write_text("# Test", encoding="utf-8")

    monkeypatch.setenv("EDITOR", "nvim")
    monkeypatch.delenv("TALKOPS_EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)

    app = MagicMock()
    with patch("subprocess.run") as mock_run:
        result = open_file_in_editor(str(sample), line=10, app=app)
        assert result is True
        app.suspend.assert_called_once()
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "nvim"
        assert "+10" in args
        assert str(sample.resolve()) in args


def test_open_file_in_editor_gui_editor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify GUI editor launches non-blocking Popen with -g for line."""
    sample = tmp_path / "report.md"
    sample.write_text("# Test", encoding="utf-8")

    monkeypatch.setenv("TALKOPS_EDITOR", "cursor")
    app = MagicMock()

    with patch("subprocess.Popen") as mock_popen:
        result = open_file_in_editor(f"file://{sample}", line=42, column=5, app=app)
        assert result is True
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args[0] == "cursor"
        assert "-g" in args
        assert f"{sample.resolve()}:42:5" in args


def test_open_file_in_editor_trailing_punctuation(tmp_path: Path) -> None:
    """Verify trailing sentence punctuation is stripped when finding file."""
    sample = tmp_path / "report.md"
    sample.write_text("# Test", encoding="utf-8")

    # Pass path with a trailing period like in a sentence
    path_with_period = f"{sample}."
    app = MagicMock()

    with patch("subprocess.Popen") as mock_popen, patch("sys.platform", "darwin"):
        result = open_file_in_editor(path_with_period, app=app)
        assert result is True
        mock_popen.assert_called_once()


def test_open_file_in_editor_sandbox_and_line_anchors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify sandbox: URIs, #L anchors, and /mnt/data workspace fallback open the file."""
    sample = tmp_path / "aws_cost_audit_last_week.md"
    sample.write_text("# Cost Audit", encoding="utf-8")

    monkeypatch.setenv("TALKOPS_EDITOR", "cursor")
    app = MagicMock()
    app._cwd = tmp_path

    # 1. sandbox:/absolute/path (as emitted by OpenAI / GPT models)
    with patch("subprocess.Popen") as mock_popen:
        assert open_file_in_editor(f"sandbox:{sample}", app=app) is True
        mock_popen.assert_called_once()
        assert str(sample.resolve()) in mock_popen.call_args[0][0]

    # 2. file:///path#L15-L25 anchor
    with patch("subprocess.Popen") as mock_popen:
        assert open_file_in_editor(f"file://{sample}#L15-L25", app=app) is True
        mock_popen.assert_called_once()
        assert f"{sample.resolve()}:15" in mock_popen.call_args[0][0]

    # 3. sandbox:/mnt/data/<filename> fallback to workspace cwd
    with patch("subprocess.Popen") as mock_popen:
        assert (
            open_file_in_editor(
                "sandbox:/mnt/data/aws_cost_audit_last_week.md", app=app
            )
            is True
        )
        mock_popen.assert_called_once()
        assert str(sample.resolve()) in mock_popen.call_args[0][0]

    # 4. extract_file_path also handles sandbox: prefix
    assert extract_file_path(f"sandbox:{sample}") == str(sample)



@pytest.mark.asyncio
async def test_open_link_or_file_async_routing(tmp_path: Path) -> None:
    """Verify open_link_or_file_async routes web URLs to browser and files to editor."""
    app = MagicMock()

    with patch(
        "talkops.ui.widgets._links.open_checked_url_async", new_callable=AsyncMock
    ) as mock_web:
        mock_web.return_value = True
        # Web URL
        res = await open_link_or_file_async("https://docs.talkops.dev", app=app)
        assert res is True
        mock_web.assert_called_once_with(
            "https://docs.talkops.dev", app=app, notify_on_success=True
        )

    with patch("talkops.editor.open_file_in_editor") as mock_file:
        mock_file.return_value = True
        # File URL
        res = await open_link_or_file_async(
            "file:///Users/structbinary/report.md", app=app
        )
        assert res is True
        mock_file.assert_called_once_with(
            "file:///Users/structbinary/report.md", app=app
        )


def test_subagent_panel_phase_row_no_duplicate_time() -> None:
    """Verify subagent panel phase row does not duplicate elapsed time."""
    from talkops.ui.theme import DARK_COLORS
    from talkops.ui.widgets.subagent_panel import SubagentPanel, _Phase, _SubagentRecord

    panel = SubagentPanel()
    phase = _Phase(index=1, eval_id="eval-1")
    rec = _SubagentRecord(id="sub-1", label="agent: Task", status="done", duration_ms=6700)
    phase.add(rec)

    row = panel._phase_row(phase, selected=True, colors=DARK_COLORS)
    # The phase row must NOT include timing (e.g. 6.7s or s)
    assert "6.7s" not in row.plain
    assert "Phase 1 (1/1)" in row.plain


def test_extract_file_path(tmp_path: Path) -> None:
    """Verify extract_file_path detects existing files across various formats."""
    sample = tmp_path / "README.md"
    sample.write_text("# Readme", encoding="utf-8")

    # Plain string
    assert extract_file_path(str(sample)) == str(sample)

    # In sentence with trailing period
    assert extract_file_path(f"The path is {sample}.") == str(sample)

    # Code fence content
    assert extract_file_path(f"```\n{sample}\n```") == str(sample)

    # Inline code
    assert extract_file_path(f"`{sample}`") == str(sample)

    # Non-existent path returns None
    assert extract_file_path("/non/existent/path/file.txt") is None


@pytest.mark.asyncio
async def test_assistant_fence_click_opens_file(tmp_path: Path) -> None:
    """Verify clicking an AssistantFence containing a file path triggers editor opening."""
    from textual.app import App, ComposeResult
    from textual.widgets._markdown import MarkdownFence
    from talkops.ui.widgets.messages import AssistantMessage

    sample = tmp_path / "README.md"
    sample.write_text("# Readme", encoding="utf-8")

    content = f"Here is the path:\n```\n{sample}\n```"
    msg = AssistantMessage(content)

    class DummyApp(App[None]):
        def compose(self) -> ComposeResult:
            yield msg

    app = DummyApp()
    with patch("talkops.editor.open_file_in_editor") as mock_open:
        mock_open.return_value = True
        async with app.run_test() as pilot:
            await pilot.pause()
            # Wait for background markdown rendering worker to populate DOM and apply link styling
            fence = None
            for _ in range(50):
                fences = msg.query(MarkdownFence)
                if fences and "-is-file-link" in fences.first().classes:
                    fence = fences.first()
                    break
                await pilot.pause(0.05)
            if fence is None:
                fence = msg.query_one(MarkdownFence)
            assert "-is-file-link" in fence.classes
            assert fence.border_subtitle == "click to open"

            await pilot.click(fence)
            await pilot.pause()
            mock_open.assert_called_once_with(str(sample), app=app)


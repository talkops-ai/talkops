"""Unit tests for TalkOps enhanced CLI capabilities.

Covers:
- Positional prompt auto-detection and subcommand disambiguation
- Piped stdin ingestion, prompt merging, and 10MB cap
- CI/CD execution flags: --quiet, --no-stream, --json, --max-turns, --timeout
- Cloud operations flags: --read-only, --aws-profile, --aws-region, -S/--shell-allow-list
- Model parameters & profile overrides JSON parsing
- Deterministic exit codes (0, 1, 2, 124, 130)
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from talkops.approval_mode import ApprovalMode
from talkops.cli.main import (
    _apply_stdin_pipe,
    _resolve_approval_mode,
    _validate_args,
    build_parser,
    main,
    non_negative_int,
    positive_int,
    shell_allow_list_arg,
)
from talkops.cli.non_interactive import run_non_interactive


# ── 1. Argument Types & Validators ───────────────────────


def test_positive_int_valid():
    assert positive_int("1") == 1
    assert positive_int("100") == 100


def test_positive_int_invalid():
    with pytest.raises(argparse.ArgumentTypeError, match="must be greater than 0"):
        positive_int("0")
    with pytest.raises(argparse.ArgumentTypeError, match="must be greater than 0"):
        positive_int("-5")
    with pytest.raises(argparse.ArgumentTypeError, match="is not a valid integer"):
        positive_int("abc")


def test_non_negative_int_valid():
    assert non_negative_int("0") == 0
    assert non_negative_int("10") == 10


def test_non_negative_int_invalid():
    with pytest.raises(argparse.ArgumentTypeError, match="must be non-negative"):
        non_negative_int("-1")


def test_shell_allow_list_arg():
    parsed = shell_allow_list_arg("aws, kubectl, terraform ")
    assert parsed == ["aws", "kubectl", "terraform"]


# ── 2. Positional Prompt & Disambiguation ────────────────


def test_positional_prompt_detected(monkeypatch):
    """Positional prompt without -p flag should be recognized as non-interactive prompt."""
    mock_run = MagicMock(return_value=0)
    monkeypatch.setattr("talkops.cli.main.run_non_interactive", mock_run)

    ret = main(["Audit VPC peering connections"])
    assert ret == 0
    assert mock_run.called
    assert mock_run.call_args[0][0] == "Audit VPC peering connections"


def test_positional_prompt_with_flags(monkeypatch):
    """Positional prompt mixed with options should be parsed correctly."""
    mock_run = MagicMock(return_value=0)
    monkeypatch.setattr("talkops.cli.main.run_non_interactive", mock_run)

    ret = main(["Inspect EKS nodes", "--timeout", "120", "--read-only"])
    assert ret == 0
    assert mock_run.called
    args, kwargs = mock_run.call_args
    assert args[0] == "Inspect EKS nodes"
    assert kwargs["timeout"] == 120.0
    assert kwargs["read_only"] is True


def test_flags_before_positional_prompt(monkeypatch):
    """Flags placed before positional prompt should parse correctly."""
    mock_run = MagicMock(return_value=0)
    monkeypatch.setattr("talkops.cli.main.run_non_interactive", mock_run)

    ret = main(["--aws-region", "us-west-2", "Check CloudWatch alarms", "--max-turns", "5"])
    assert ret == 0
    assert mock_run.called
    args, kwargs = mock_run.call_args
    assert args[0] == "Check CloudWatch alarms"
    assert kwargs["aws_region"] == "us-west-2"
    assert kwargs["max_turns"] == 5


def test_subcommand_not_treated_as_positional_prompt(monkeypatch):
    """Registered subcommands like doctor or auth must not be converted into prompts."""
    mock_doctor = MagicMock(return_value=0)
    monkeypatch.setattr("talkops.cli.main.run_doctor_command", mock_doctor)

    ret = main(["doctor"])
    assert ret == 0
    assert mock_doctor.called


# ── 3. Stdin Detection & Ingestion ───────────────────────


def test_stdin_piping_into_prompt():
    """Piped stdin without prompt should become the prompt."""
    args = argparse.Namespace(prompt=None, initial_prompt=None, stdin=False)
    fake_stdin = io.StringIO("Critical alert: pod evicted in prod-namespace\n")
    fake_stdin.isatty = lambda: False  # type: ignore

    with patch("sys.stdin", fake_stdin):
        _apply_stdin_pipe(args)

    assert args.prompt == "Critical alert: pod evicted in prod-namespace"


def test_stdin_piping_prepended_to_existing_prompt():
    """Piped stdin with an existing prompt should be prepended."""
    args = argparse.Namespace(prompt="diagnose crashloop", initial_prompt=None, stdin=False)
    fake_stdin = io.StringIO("Events:\n  Warning  FailedScheduling  pod didn't fit")
    fake_stdin.isatty = lambda: False  # type: ignore

    with patch("sys.stdin", fake_stdin):
        _apply_stdin_pipe(args)

    expected = "Events:\n  Warning  FailedScheduling  pod didn't fit\n\ndiagnose crashloop"
    assert args.prompt == expected


def test_stdin_piping_oversized_exits(capsys):
    """Piped input exceeding 10MB should exit with error."""
    args = argparse.Namespace(prompt=None, initial_prompt=None, stdin=False)
    large_input = "a" * (10 * 1024 * 1024 + 50)
    fake_stdin = io.StringIO(large_input)
    fake_stdin.isatty = lambda: False  # type: ignore

    with patch("sys.stdin", fake_stdin), pytest.raises(SystemExit) as exc:
        _apply_stdin_pipe(args)

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "exceeds 10 MiB limit" in captured.err


# ── 4. Argument Compatibility & Validations ──────────────


def test_quiet_requires_prompt(capsys):
    ret = main(["--quiet"])
    assert ret == 2
    captured = capsys.readouterr()
    assert "--quiet" in captured.err
    assert "requires -p/--prompt" in captured.err


def test_no_stream_requires_prompt(capsys):
    ret = main(["--no-stream"])
    assert ret == 2
    captured = capsys.readouterr()
    assert "--no-stream" in captured.err
    assert "requires -p/--prompt" in captured.err


def test_max_turns_requires_prompt(capsys):
    ret = main(["--max-turns", "5"])
    assert ret == 2
    captured = capsys.readouterr()
    assert "--max-turns requires -p/--prompt" in captured.err


def test_timeout_requires_prompt(capsys):
    ret = main(["--timeout", "60"])
    assert ret == 2
    captured = capsys.readouterr()
    assert "--timeout requires -p/--prompt" in captured.err


def test_no_mcp_and_mcp_config_conflict(capsys):
    ret = main(["-p", "test", "--no-mcp", "--mcp-config", "mcp.json"])
    assert ret == 2
    captured = capsys.readouterr()
    assert "--no-mcp and --mcp-config are mutually exclusive" in captured.err


def test_goal_with_prompt_conflict(capsys):
    ret = main(["-p", "test prompt", "--goal", "interactive goal"])
    assert ret == 1
    captured = capsys.readouterr()
    assert "Cannot specify both --goal and --rubric/--prompt" in captured.err


# ── 5. Cloud-Specific & Security Flags ───────────────────


def test_cloud_flags_parsed():
    parser = build_parser()
    args = parser.parse_args([
        "-p", "Audit AWS infrastructure",
        "--read-only",
        "--aws-profile", "prod-eu",
        "--aws-region", "eu-central-1",
        "-S", "aws,kubectl,helm",
    ])
    assert args.read_only is True
    assert args.aws_profile == "prod-eu"
    assert args.aws_region == "eu-central-1"
    assert args.shell_allow_list == ["aws", "kubectl", "helm"]


def test_approval_mode_resolution():
    args_smart = argparse.Namespace(smart=True, approval_mode=None, prompt="test")
    assert _resolve_approval_mode(args_smart) == ApprovalMode.SMART

    args_auto = argparse.Namespace(smart=False, approval_mode="auto", prompt="test")
    assert _resolve_approval_mode(args_auto) == ApprovalMode.AUTO

    args_manual = argparse.Namespace(smart=False, approval_mode="manual", prompt="test")
    assert _resolve_approval_mode(args_manual) == ApprovalMode.MANUAL

    # Headless without explicit mode defaults to AUTO
    args_headless = argparse.Namespace(smart=False, auto_approve=False, approval_mode=None, prompt="test")
    assert _resolve_approval_mode(args_headless) == ApprovalMode.AUTO

    # Interactive TUI without explicit mode defaults to MANUAL
    args_interactive = argparse.Namespace(smart=False, auto_approve=False, approval_mode=None, prompt=None)
    assert _resolve_approval_mode(args_interactive) == ApprovalMode.MANUAL


# ── 6. Model Parameters & Profile Override JSON ──────────


def test_model_params_json_valid(monkeypatch):
    mock_run = MagicMock(return_value=0)
    monkeypatch.setattr("talkops.cli.main.run_non_interactive", mock_run)

    ret = main(["-p", "hello", "--model-params", '{"temperature": 0.2, "max_tokens": 2048}'])
    assert ret == 0
    assert mock_run.called
    kwargs = mock_run.call_args[1]
    assert kwargs["model_params"] == {"temperature": 0.2, "max_tokens": 2048}


def test_model_params_json_invalid(capsys):
    ret = main(["-p", "hello", "--model-params", "{invalid-json}"])
    assert ret == 1
    captured = capsys.readouterr()
    assert "not valid JSON" in captured.err


# ── 7. Non-Interactive Runner Unit Behavior ──────────────


def test_run_non_interactive_timeout():
    """Wall-clock timeout in run_non_interactive should return exit code 124."""
    with patch("talkops.cli.non_interactive._run_headless") as mock_headless:
        # Simulate long-running coro that wait_for will cancel
        async def slow_headless(*args: Any, **kwargs: Any) -> int:
            await asyncio.sleep(10.0)
            return 0

        mock_headless.side_effect = slow_headless
        ret = run_non_interactive("Task that times out", timeout=0.01, quiet=True)
        assert ret == 124


def test_run_non_interactive_keyboard_interrupt():
    """KeyboardInterrupt in run_non_interactive should return exit code 130."""
    with patch("talkops.cli.non_interactive._run_headless") as mock_headless:
        mock_headless.side_effect = KeyboardInterrupt
        ret = run_non_interactive("Task cancelled by user", quiet=True)
        assert ret == 130


@pytest.mark.asyncio
async def test_run_headless_quiet_mode_and_turn_limit():
    """Quiet mode should direct diagnostics away from stdout and turn limits return 124."""
    from talkops.cli.non_interactive import _run_headless

    # Create a mock agent that yields more turns than max_turns
    mock_agent = MagicMock()

    async def mock_astream(*args: Any, **kwargs: Any):
        yield ((), "messages", ("Analyzing logs...", {}))
        yield ((), "updates", {"messages": [MagicMock(name="aws ec2 describe-instances")]})
        yield ((), "updates", {"messages": [MagicMock(name="aws ec2 describe-instances")]})
        yield ((), "updates", {"messages": [MagicMock(name="aws ec2 describe-instances")]})

    mock_agent.astream = mock_astream

    with patch("talkops.cli.non_interactive.server_session") as mock_session:
        mock_session.return_value.__enter__.return_value = mock_agent

        # Run with max_turns=2
        exit_code = await _run_headless(
            "Analyze instances",
            quiet=True,
            max_turns=2,
        )
        assert exit_code == 124


@pytest.mark.asyncio
async def test_run_headless_json_output(capsys):
    """JSON output mode emits machine-readable envelope with metrics."""
    from talkops.cli.non_interactive import _run_headless

    mock_agent = MagicMock()

    async def mock_astream(*args: Any, **kwargs: Any):
        yield ((), "messages", ("All 12 instances are healthy.", {}))

    mock_agent.astream = mock_astream

    with patch("talkops.cli.non_interactive.server_session") as mock_session:
        mock_session.return_value.__enter__.return_value = mock_agent

        exit_code = await _run_headless(
            "Check cluster status",
            json_output=True,
        )
        assert exit_code == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["schema_version"] == 1
    assert payload["command"] == "run"
    assert payload["data"]["status"] == "success"
    assert payload["data"]["content"] == "All 12 instances are healthy."


# ── 8. Subcommand JSON Envelopes & Operations ────────────


def test_auth_json_envelope(capsys, monkeypatch):
    """auth command with --json flag emits structured envelope."""
    fake_ctx = MagicMock(
        is_authenticated=True,
        account_id="123456789012",
        arn="arn:aws:iam::123456789012:user/devops",
        user_id="AIDAEXAMPLE",
        region="us-east-1",
        profile="default",
        error=None,
    )
    monkeypatch.setattr("talkops.cli.commands.auth.get_aws_context", lambda: fake_ctx)

    ret = main(["auth", "--json"])
    assert ret == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["schema_version"] == 1
    assert payload["command"] == "auth"
    assert payload["data"]["authenticated"] is True
    assert payload["data"]["account_id"] == "123456789012"


def test_doctor_json_envelope(capsys, monkeypatch):
    """doctor command with --json flag emits structured environment metrics."""
    fake_ctx = MagicMock(
        is_authenticated=True,
        account_id="123456789012",
        region="us-east-1",
        profile="default",
        error=None,
    )
    monkeypatch.setattr("talkops.cli.commands.doctor.get_aws_context", lambda: fake_ctx)

    ret = main(["doctor", "--json"])
    assert ret == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["schema_version"] == 1
    assert payload["command"] == "doctor"
    assert payload["data"]["aws_sts"]["authenticated"] is True
    assert "tools" in payload["data"]
    assert "python" in payload["data"]


def test_threads_list_json_envelope(capsys, monkeypatch):
    """threads list --json emits list of thread metadata."""
    async def fake_list_threads(**kwargs):
        return [
            {
                "thread_id": "thread_abc123",
                "cloud_badge": "AWS",
                "message_count": 4,
                "initial_prompt": "Audit S3 buckets",
                "updated_at": "2026-09-30T10:00:00Z",
            }
        ]

    monkeypatch.setattr("talkops.cli.commands.threads.list_threads", fake_list_threads)

    ret = main(["threads", "list", "--json"])
    assert ret == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["schema_version"] == 1
    assert payload["command"] == "threads list"
    assert len(payload["data"]) == 1
    assert payload["data"][0]["thread_id"] == "thread_abc123"


def test_threads_delete_command(capsys, monkeypatch):
    """threads delete command deletes target thread and outputs envelope."""
    mock_del = AsyncMock(return_value=True)
    monkeypatch.setattr("talkops.state.session.delete_thread", mock_del)

    ret = main(["threads", "delete", "thread_to_delete_123", "--json"])
    assert ret == 0
    assert mock_del.called
    assert mock_del.call_args[0][0] == "thread_to_delete_123"
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["schema_version"] == 1
    assert payload["command"] == "threads delete"
    assert payload["data"]["deleted"] is True


# ── 9. Thread ID Normalization & UUID Validity ───────────


def test_normalize_thread_id():
    import uuid
    from talkops.ui.remote_client import normalize_thread_id

    # 1. Valid UUID string remains unchanged
    valid_uuid = str(uuid.uuid4())
    assert normalize_thread_id(valid_uuid) == valid_uuid

    # 2. None generates a valid UUID string
    generated = normalize_thread_id(None)
    uuid.UUID(generated)  # does not raise

    # 3. Arbitrary non-UUID string becomes a valid deterministic UUIDv5
    named_id = "my-custom-devops-session"
    normalized = normalize_thread_id(named_id)
    assert normalized != named_id
    uuid.UUID(normalized)  # does not raise
    # Repeatability check
    assert normalize_thread_id(named_id) == normalized


@pytest.mark.asyncio
async def test_run_headless_valid_uuid_passed_to_agent():
    """Headless runner must ensure effective_thread is a valid UUID string."""
    import uuid
    from talkops.cli.non_interactive import _run_headless

    mock_agent = MagicMock()
    captured_config = {}

    async def mock_astream(*args: Any, **kwargs: Any):
        nonlocal captured_config
        captured_config = kwargs.get("config", {})
        yield ((), "messages", ("pong", {}))

    mock_agent.astream = mock_astream

    with patch("talkops.cli.non_interactive.server_session") as mock_session:
        mock_session.return_value.__enter__.return_value = mock_agent

        # Call with no thread_id specified
        await _run_headless("ping", quiet=True)
        tid = captured_config.get("configurable", {}).get("thread_id")
        assert tid is not None
        # Must be valid UUID (does not raise ValueError)
        uuid_obj = uuid.UUID(tid)
        assert str(uuid_obj) == tid


# ── 10. Regression Tests for CLI Audit Fixes (BUG-01 .. BUG-13) ──


@pytest.mark.asyncio
async def test_max_turns_ignores_middleware_updates_and_counts_model_turns():
    """BUG-01: Middleware updates (e.g. TodoListMiddleware) must not increment turn_count."""
    from talkops.cli.non_interactive import _run_headless

    mock_agent = MagicMock()

    async def mock_astream(*args: Any, **kwargs: Any):
        # Middleware pre-model steps
        yield ((), "updates", {"TodoListMiddleware.before_model": None})
        yield ((), "updates", {"SummarizationMiddleware.before_model": None})
        # Streamed AI message chunk
        chunk = MagicMock()
        chunk.content = "PONG"
        chunk.tool_call_chunks = []
        yield ((), "messages", (chunk, {}))
        # Model turn completion update
        ai_msg = MagicMock()
        ai_msg.content = "PONG"
        yield ((), "updates", {"model": {"messages": [ai_msg]}})
        # Post-model middleware update
        yield ((), "updates", {"TodoListMiddleware.after_model": {"messages": []}})

    mock_agent.astream = mock_astream

    with patch("talkops.cli.non_interactive.server_session") as mock_session:
        mock_session.return_value.__enter__.return_value = mock_agent

        exit_code = await _run_headless(
            "Reply with PONG",
            max_turns=1,
            quiet=True,
        )
        assert exit_code == 0


def test_mcp_json_output_not_wrapped_and_supports_flags(capsys, monkeypatch, tmp_path):
    """BUG-02 & BUG-09: talkops mcp --json outputs unwrapped JSON and honors --no-mcp / --mcp-config."""
    ret = main(["mcp", "list", "--no-mcp", "--json"])
    assert ret == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["mcp_servers"] == []

    # Custom --mcp-config with a very long command string should not be line-wrapped in JSON
    long_path = "/very/long/path/" + ("segment_" * 20) + "server.py"
    cfg_file = tmp_path / "custom_mcp.json"
    cfg_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "custom-long-server": {
                        "command": "python3",
                        "args": [long_path],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    ret2 = main(["mcp", "list", "--mcp-config", str(cfg_file), "--json"])
    assert ret2 == 0
    captured2 = capsys.readouterr()
    payload2 = json.loads(captured2.out)
    custom_entries = [
        s for s in payload2["mcp_servers"] if s["name"] == "custom-long-server"
    ]
    assert len(custom_entries) == 1
    assert long_path in custom_entries[0]["args"]


def test_default_model_space_separated_argument(monkeypatch):
    """BUG-03: talkops --default-model <MODEL> space-separated value is not rewritten to -p."""
    saved = []
    monkeypatch.setattr(
        "talkops.model.config.save_default_model",
        lambda spec: (saved.append(spec), True)[1],
    )
    assert main(["--default-model", "anthropic:claude-sonnet-4-5"]) == 0
    assert saved == ["anthropic:claude-sonnet-4-5"]



def test_plugin_subcommands_accept_json_flag(capsys):
    """BUG-04: talkops plugin list --json and marketplace list --json succeed."""
    ret = main(["plugin", "list", "--json"])
    assert ret == 0
    payload = json.loads(capsys.readouterr().out)
    assert "plugins" in payload

    ret_mp = main(["plugin", "marketplace", "list", "--json"])
    assert ret_mp == 0
    payload_mp = json.loads(capsys.readouterr().out)
    assert "marketplaces" in payload_mp


def test_threads_delete_nonexistent_json_returns_nonzero(capsys, monkeypatch):
    """BUG-04: threads delete <nonexistent> --json returns exit code 1."""
    monkeypatch.setattr("talkops.state.session.delete_thread", AsyncMock(return_value=False))
    ret = main(["threads", "delete", "missing_thread_xyz", "--json"])
    assert ret == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["deleted"] is False


def test_help_subcommand_and_topics(capsys):
    """BUG-05: talkops help and talkops help <subcmd> exit with 0."""
    assert main(["help"]) == 0
    assert main(["help", "skills"]) == 0
    assert main(["help", "mcp"]) == 0


def test_short_v_version_vs_verbose_flag(capsys, monkeypatch):
    """BUG-06: standalone -v prints version; -v with subcommand acts as --verbose."""
    assert main(["-v"]) == 0
    assert "talkops" in capsys.readouterr().out

    monkeypatch.setattr("talkops.cli.main.run_doctor_command", lambda fmt: 0)
    assert main(["doctor", "-v"]) == 0
    assert main(["doctor", "-V"]) == 0


def test_skills_list_project_excludes_plugin_skills(capsys, tmp_path):
    """BUG-07: skills list --project --json only returns project skills."""
    ret = main(["skills", "list", "--project", "--json"])
    assert ret == 0
    payload = json.loads(capsys.readouterr().out)
    for skill in payload["data"]:
        assert skill.get("source") == "project"


def test_auth_and_doctor_forward_aws_profile_and_region(monkeypatch):
    """BUG-08: auth and doctor accept --aws-profile and --aws-region and forward them."""
    captured_auth = {}
    captured_doc = {}

    def fake_auth(fmt, aws_profile=None, aws_region=None):
        captured_auth["profile"] = aws_profile
        captured_auth["region"] = aws_region
        return 0

    def fake_doc(fmt, aws_profile=None, aws_region=None):
        captured_doc["profile"] = aws_profile
        captured_doc["region"] = aws_region
        return 0

    monkeypatch.setattr("talkops.cli.main.run_auth_command", fake_auth)
    monkeypatch.setattr("talkops.cli.main.run_doctor_command", fake_doc)

    assert main(["auth", "--aws-profile", "my-prof", "--aws-region", "eu-west-1"]) == 0
    assert captured_auth == {"profile": "my-prof", "region": "eu-west-1"}

    assert main(["doctor", "--aws-profile", "my-prof", "--aws-region", "ap-south-1"]) == 0
    assert captured_doc == {"profile": "my-prof", "region": "ap-south-1"}


def test_plugin_enable_disable_nonexistent_returns_error(capsys):
    """BUG-10: plugin enable/disable on a nonexistent plugin returns exit code 1."""
    assert main(["plugin", "enable", "definitely-not-installed-xyz"]) == 1
    assert main(["plugin", "disable", "definitely-not-installed-xyz"]) == 1


def test_empty_rubric_string_rejected(capsys):
    """BUG-11: --rubric '' is rejected with exit code 1."""
    ret = main(["-p", "hello", "--rubric", ""])
    assert ret == 1
    captured = capsys.readouterr()
    assert "--rubric must not be empty" in (captured.err + captured.out)


def test_serve_conflicting_flags_rejected():
    """BUG-12: serve rejects conflicting approval flags and --no-mcp with --mcp-config."""
    with pytest.raises(SystemExit) as exc1:
        main(["serve", "--yolo", "--smart"])
    assert exc1.value.code == 2

    assert main(["serve", "--no-mcp", "--mcp-config", "foo.json"]) == 2



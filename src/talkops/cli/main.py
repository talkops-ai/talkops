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

"""Main CLI entry point and argument parser for TalkOps.

Routes between interactive TUI mode (default), subcommands (doctor, auth,
threads, mcp, plugin, skill), and non-interactive headless mode
(-p / -n / positional prompt / piped stdin).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, overload

from talkops._version import __version__
from talkops.approval_mode import ApprovalMode
from talkops.output import OutputFormat, add_json_output_arg
from talkops.utils.logger import get_logger

logger = get_logger(__name__)



def run_auth_command(*args: Any, **kwargs: Any) -> int:
    """Execute auth command."""
    from talkops.cli.commands.auth import run_auth_command as _run

    return _run(*args, **kwargs)


def run_doctor_command(*args: Any, **kwargs: Any) -> int:
    """Execute doctor command."""
    from talkops.cli.commands.doctor import run_doctor_command as _run

    return _run(*args, **kwargs)


def run_mcp_command(*args: Any, **kwargs: Any) -> int:
    """Execute mcp command."""
    from talkops.cli.commands.mcp import run_mcp_command as _run

    return _run(*args, **kwargs)


def run_serve_command(*args: Any, **kwargs: Any) -> int:
    """Execute serve command."""
    from talkops.cli.commands.serve import run_serve_command as _run

    return _run(*args, **kwargs)


def run_threads_command(*args: Any, **kwargs: Any) -> int:
    """Execute threads command."""
    from talkops.cli.commands.threads import run_threads_command as _run

    return _run(*args, **kwargs)


def setup_mcp_parser(subparsers: Any) -> None:
    """Execute setup mcp parser."""
    from talkops.cli.commands.mcp import setup_mcp_parser as _setup

    _setup(subparsers)


def setup_serve_parser(subparsers: Any) -> None:
    """Execute setup serve parser."""
    from talkops.cli.commands.serve import setup_serve_parser as _setup

    _setup(subparsers)


def setup_benchmark_parser(subparsers: Any) -> None:
    """Execute setup benchmark parser if benchmark module is installed."""
    try:
        from talkops.cli.commands.benchmark import (
            setup_benchmark_parser as _setup,
        )
    except ImportError:
        return

    _setup(subparsers)


def run_benchmark_command(*args: Any, **kwargs: Any) -> int:
    """Execute benchmark command if benchmark module is installed."""
    try:
        from talkops.cli.commands.benchmark import run_benchmark_command as _run
    except ImportError:
        print(
            "Error: Benchmark suite ('talkops.benchmarks') is not installed in this build.",
            file=sys.stderr,
        )
        return 1

    return _run(*args, **kwargs)


def run_interactive(*args: Any, **kwargs: Any) -> int:
    """Execute interactive."""
    from talkops.cli.interactive import run_interactive as _run

    return _run(*args, **kwargs)


def run_non_interactive(*args: Any, **kwargs: Any) -> int:
    """Execute non interactive."""
    from talkops.cli.non_interactive import run_non_interactive as _run

    return _run(*args, **kwargs)


# ── Argument Types ───────────────────────────────────────


def positive_int(value: str) -> int:
    """Validate positive integer arguments (> 0)."""
    try:
        ivalue = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid integer") from exc
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be greater than 0")
    return ivalue


def non_negative_int(value: str) -> int:
    """Validate non-negative integer arguments (>= 0)."""
    try:
        ivalue = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid integer") from exc
    if ivalue < 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be non-negative")
    return ivalue


def shell_allow_list_arg(value: str) -> list[str]:
    """Parse comma-separated list of allowed shell commands."""
    from talkops.config.settings import parse_shell_allow_list

    parsed = parse_shell_allow_list(value)
    return parsed or []


# ── Rubric Resolution ────────────────────────────────────


@overload
def _resolve_rubric_text(rubric: None) -> None: ...


@overload
def _resolve_rubric_text(rubric: str) -> str: ...


def _resolve_rubric_text(rubric: str | None) -> str | None:
    """Resolve the rubric from `--rubric` into one string.

    `--rubric` accepts literal text, or `@path` to read a file. File paths
    may be absolute, relative to the process working directory, or `~`-expanded.
    """
    if rubric is None:
        return None

    from talkops.state.goal_state_limits import validate_rubric

    if rubric.startswith("@"):

        path = rubric[1:]
        try:
            text = Path(path).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            msg = f"Could not read rubric file {path!r}: {exc}."
            raise ValueError(msg) from exc
        if not text.strip():
            msg = f"Rubric file {path!r} is empty."
            raise ValueError(msg)
        resolved = text.strip()
        validate_rubric(resolved)
        return resolved

    if not rubric.strip():
        msg = "--rubric must not be empty."
        raise ValueError(msg)
    resolved = rubric.strip()
    validate_rubric(resolved)
    return resolved


# ── Stdin detection & piping ─────────────────────────────


def _apply_stdin_pipe(args: argparse.Namespace) -> None:
    """Read piped stdin and merge it into the parsed CLI arguments."""
    explicit_stdin = getattr(args, "stdin", False)
    if sys.stdin is None:
        if explicit_stdin:
            print("Error: --stdin was passed but stdin is not available.", file=sys.stderr)
            sys.exit(1)
        return

    try:
        is_tty = sys.stdin.isatty()
    except (ValueError, OSError):
        if explicit_stdin:
            print("Error: --stdin was passed but stdin state could not be determined.", file=sys.stderr)
            sys.exit(1)
        return

    if is_tty:
        if explicit_stdin:
            print(
                "Error: --stdin was passed but stdin is a terminal. Pipe input or pass a prompt directly.",
                file=sys.stderr,
            )
            sys.exit(1)
        return

    max_stdin_bytes = 10 * 1024 * 1024  # 10 MiB
    try:
        stdin_text = sys.stdin.read(max_stdin_bytes + 1)
    except UnicodeDecodeError:
        print("Error: Could not read piped input — ensure the input is valid text", file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError) as exc:
        if not explicit_stdin or "pytest" in str(exc).lower():
            return
        print(f"Error: Failed to read piped input: {exc}", file=sys.stderr)
        sys.exit(1)

    if len(stdin_text) > max_stdin_bytes:
        print(f"Error: Piped input exceeds {max_stdin_bytes // (1024 * 1024)} MiB limit.", file=sys.stderr)
        sys.exit(1)

    stdin_text = stdin_text.strip()
    if not stdin_text:
        return

    if getattr(args, "prompt", None):
        args.prompt = f"{stdin_text}\n\n{args.prompt}"
    elif getattr(args, "initial_prompt", None) is not None:
        if args.initial_prompt:
            args.initial_prompt = f"{stdin_text}\n\n{args.initial_prompt}"
        else:
            args.initial_prompt = stdin_text
    elif getattr(args, "initial_skill", None) and not explicit_stdin:
        args.initial_prompt = stdin_text
    else:
        args.prompt = stdin_text

    # Restore terminal fd for interactive mode if possible
    try:
        tty_fd = os.open("/dev/tty", os.O_RDONLY)
        os.dup2(tty_fd, 0)
        os.close(tty_fd)
    except OSError:
        pass


# ── Approval Mode Resolution ─────────────────────────────


def _resolve_approval_mode(args: argparse.Namespace) -> ApprovalMode:
    """Resolve flags into typed ApprovalMode.

    Enforces that `ApprovalMode.YOLO` is only permitted in headless CLI flows (`args.prompt`),
    while interactive TUI flows are restricted to `manual`, `smart`, and `auto`.
    """
    from talkops.approval_mode import coerce_approval_mode

    is_headless = bool(getattr(args, "prompt", None))
    if getattr(args, "yolo", False):
        return ApprovalMode.YOLO if is_headless else ApprovalMode.MANUAL
    if getattr(args, "smart", False):
        return ApprovalMode.SMART
    if getattr(args, "auto_approve", False):
        return ApprovalMode.AUTO
    raw = getattr(args, "approval_mode", None)
    if raw:
        return coerce_approval_mode(raw, allow_yolo=is_headless)

    # Non-interactive / headless execution (-p / --prompt) defaults to AUTO for autonomous execution,
    # while interactive TUI defaults to MANUAL for human-in-the-loop safety.
    if is_headless:
        return ApprovalMode.AUTO
    return ApprovalMode.MANUAL


def _ensure_cli_authenticated(
    args: argparse.Namespace,
    *,
    output_format: OutputFormat = "text",
) -> int:
    """Enforce mandatory Control Plane token authentication before launching TUI or headless CLI.

    If no valid token or stored session (`~/.talkops/.state/auth.json`) exists:
    - In an interactive terminal (`isatty()`): automatically initiates Browser-Loopback
      PKCE SSO (`talkops auth login --sso`) against the Control Plane to acquire a token.
    - In non-interactive / headless mode (or if SSO fails): prints a 401 Unauthorized
      error and returns exit code 1.
    """
    if "PYTEST_CURRENT_TEST" in os.environ and os.environ.get("TALKOPS_ENFORCE_CLI_AUTH_IN_TESTS") != "1":
        return 0

    from talkops.auth.storage import get_credential_storage

    storage = get_credential_storage()
    stored = storage.load_session()
    prev_email = (stored.email if stored and stored.email else None)
    prev_cp_url = (stored.control_plane_url if stored and stored.control_plane_url else "")

    explicit_key = (
        getattr(args, "api_key", None)
        or os.environ.get("TALKOPS_API_KEY", "").strip()
        or os.environ.get("TALKOPS_SERVER_API_KEY", "").strip()
    )

    if stored is not None and stored.is_access_token_valid() and (
        not explicit_key or explicit_key == stored.api_key or explicit_key == stored.access_token
    ):
        args.api_key = stored.access_token
        if not getattr(args, "user_id", None) and stored.user_id:
            args.user_id = stored.user_id
        return 0

    candidate_key = explicit_key or (stored.api_key if stored else "")
    cp_url_cfg = (
        os.environ.get("TALKOPS_CONTROL_PLANE_URL", "").strip()
        or prev_cp_url
    )
    srv_url_cfg = (
        getattr(args, "server_url", None)
        or os.environ.get("TALKOPS_SERVER_URL", "").strip()
        or (stored.server_url if stored and stored.server_url else "")
    )
    if srv_url_cfg.startswith("inprocess://"):
        srv_url_cfg = ""

    if candidate_key:
        if cp_url_cfg or srv_url_cfg:
            from talkops.state.remote_session import (
                exchange_api_key_for_temporary_token,
            )

            try:
                temp_jwt = asyncio.run(
                    exchange_api_key_for_temporary_token(
                        srv_url_cfg or None,
                        api_key=candidate_key,
                        user_id=(
                            getattr(args, "user_id", None)
                            or (stored.user_id if stored else None)
                        ),
                        control_plane_url=cp_url_cfg or None,
                        force_refresh=True,
                        raise_on_unauthorized=True,
                    )
                )
                if temp_jwt:
                    refreshed_sess = storage.load_session()
                    args.api_key = temp_jwt
                    if not getattr(args, "user_id", None):
                        if refreshed_sess and refreshed_sess.user_id:
                            args.user_id = refreshed_sess.user_id
                        elif stored and stored.user_id:
                            args.user_id = stored.user_id
                    return 0
            except PermissionError:
                # Stored or env API key was explicitly rejected (401/403 revoked/invalid) by Control Plane.
                # Clear the stale session and fall through to interactive Browser SSO re-authentication.
                try:
                    storage.clear_session(clear_dotenv=True)
                except Exception:
                    pass
                os.environ.pop("TALKOPS_API_KEY", None)
                os.environ.pop("TALKOPS_SERVER_API_KEY", None)
                args.api_key = None
                stored = None
                explicit_key = ""
                candidate_key = ""

        if candidate_key:
            args.api_key = candidate_key
            if stored and not getattr(args, "user_id", None) and stored.user_id:
                args.user_id = stored.user_id
            return 0

    try:
        is_interactive_tty = bool(
            sys.stdin is not None
            and sys.stdin.isatty()
            and sys.stdout is not None
            and sys.stdout.isatty()
        )
    except Exception:
        is_interactive_tty = False

    if is_interactive_tty and os.environ.get("TALKOPS_AUTO_SSO_LOGIN", "1") != "0":
        from talkops._constants import DEFAULT_CONTROL_PLANE_URL
        from talkops.cli.commands.auth import run_auth_login_command

        cp_url = cp_url_cfg or DEFAULT_CONTROL_PLANE_URL
        srv_url = srv_url_cfg
        print(
            f"\n[TalkOps] Control Plane authentication required (mandatory token mode).\n"
            f"[TalkOps] Redirecting to Browser SSO ({cp_url})...\n",
            file=sys.stderr,
        )
        login_args = argparse.Namespace(
            sso_provider="sso",
            control_plane_url=cp_url,
            server_url=srv_url,
            api_key=None,
            user_id=getattr(args, "user_id", None),
            login_hint=prev_email,
            auth_code=None,
            no_browser=False,
            auto_approve=False,
        )
        rc = run_auth_login_command(login_args, output_format=output_format)
        if rc == 0:
            refreshed = storage.load_session()
            if refreshed is not None and (refreshed.is_access_token_valid() or bool(refreshed.api_key)):
                args.api_key = (
                    refreshed.access_token if refreshed.is_access_token_valid() else refreshed.api_key
                )
                if not getattr(args, "user_id", None) and refreshed.user_id:
                    args.user_id = refreshed.user_id
                return 0

    print(
        "Error: 401 Unauthorized — Control Plane token required. "
        "Run 'talkops auth login --sso' or 'talkops auth login --api-key <oc_live_...>' first.",
        file=sys.stderr,
    )
    return 1


# ── Parser Builder ───────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the comprehensive TalkOps command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="talkops",
        description="TalkOps — Autonomous Cloud Operations & DevOps Coding Agent.",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    # Prompt & Headless flags
    parser.add_argument(
        "-p",
        "--prompt",
        "-n",
        "--non-interactive",
        dest="prompt",
        type=str,
        default=None,
        help="Run non-interactively (headless) with the given prompt.",
    )

    # Interactive Initial Prompts & Seeding
    parser.add_argument(
        "-m",
        "--message",
        "--initial-prompt",
        dest="initial_prompt",
        type=str,
        default=None,
        help="Initial prompt to auto-submit when interactive session starts.",
    )
    parser.add_argument(
        "-s",
        "--skill",
        dest="initial_skill",
        type=str,
        default=None,
        help="Invoke a skill when interactive or headless session starts.",
    )
    parser.add_argument(
        "-a",
        "--agent",
        dest="agent",
        type=str,
        default=None,
        help="Start session with or run prompt using a named specialist agent.",
    )
    parser.add_argument(
        "--startup-cmd",
        dest="startup_cmd",
        type=str,
        default=None,
        help="Shell command executed at startup before the first prompt.",
    )

    # Output formatting
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=False,
        help="Clean output for piping stdout — only agent's final text goes to stdout. Requires headless mode.",
    )
    parser.add_argument(
        "--no-stream",
        dest="no_stream",
        action="store_true",
        default=False,
        help="Buffer response and write to stdout at once instead of streaming chunks. Requires headless mode.",
    )
    add_json_output_arg(parser, default="text")

    # CI/CD Execution Limits
    parser.add_argument(
        "--max-turns",
        dest="max_turns",
        type=positive_int,
        default=None,
        help="Maximum agentic turns before stopping (>= 1). Requires headless mode.",
    )
    parser.add_argument(
        "--timeout",
        dest="timeout",
        type=positive_int,
        default=None,
        help="Hard wall-clock timeout in seconds (>= 1). Exits 124 on expiry. Requires headless mode.",
    )
    parser.add_argument(
        "--recursion-limit",
        dest="recursion_limit",
        type=positive_int,
        default=None,
        help="Override main agent's LangGraph recursion_limit (graph step budget).",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        default=False,
        help="Read input from stdin explicitly.",
    )

    # Cloud & Environment Overrides
    parser.add_argument(
        "--read-only",
        "--dry-run",
        dest="read_only",
        action="store_true",
        default=False,
        help="Enforce read-only cloud operations safety mode (blocks all mutating AWS/K8s/Terraform calls).",
    )
    parser.add_argument(
        "--aws-profile",
        dest="aws_profile",
        type=str,
        default=None,
        help="AWS profile override for this session (e.g. staging, prod).",
    )
    parser.add_argument(
        "--aws-region",
        dest="aws_region",
        type=str,
        default=None,
        help="AWS region override for this session (e.g. us-east-1, eu-west-1).",
    )
    parser.add_argument(
        "-S",
        "--shell-allow-list",
        dest="shell_allow_list",
        type=shell_allow_list_arg,
        default=None,
        help="Comma-separated list of allowed shell commands ('recommended', 'all', or comma-separated names).",
    )

    # Model & Config
    parser.add_argument(
        "-M",
        "--model",
        dest="model",
        type=str,
        default=None,
        help="Model specification to use (e.g. anthropic:claude-3-5-sonnet, openai:gpt-4o, bedrock:anthropic.claude-3-5-sonnet).",
    )
    parser.add_argument(
        "--model-params",
        dest="model_params",
        type=str,
        default=None,
        help="Extra kwargs to pass to model as JSON string (e.g. '{\"temperature\": 0.0}').",
    )
    parser.add_argument(
        "--profile-override",
        dest="profile_override",
        type=str,
        default=None,
        help="Override model profile fields as JSON string.",
    )
    parser.add_argument(
        "--default-model",
        dest="default_model",
        nargs="?",
        const="__SHOW__",
        default=None,
        help="Set or show the default model for future launches.",
    )
    parser.add_argument(
        "--clear-default-model",
        dest="clear_default_model",
        action="store_true",
        default=False,
        help="Clear configured default model.",
    )

    # Approvals
    approval_group = parser.add_mutually_exclusive_group()
    approval_group.add_argument(
        "--approval-mode",
        dest="approval_mode",
        type=str,
        choices=["manual", "smart", "auto", "yolo"],
        default=None,
        help="Tool approval mode: 'manual', 'smart' (Jev System One gate), 'auto' (LLM classifier), or 'yolo' (headless/webhook only).",
    )
    approval_group.add_argument(
        "-y",
        "--auto-approve",
        dest="auto_approve",
        action="store_true",
        default=False,
        help="Enable classic classifier-backed Auto mode.",
    )
    approval_group.add_argument(
        "--smart",
        dest="smart",
        action="store_true",
        default=False,
        help="Enable Jev-powered Smart approval mode (<100ms System One safety gate).",
    )
    approval_group.add_argument(
        "--yolo",
        dest="yolo",
        action="store_true",
        default=False,
        help="Enable unrestricted YOLO mode without human prompts or classifier gating (requires -p/--prompt or `talkops serve`).",
    )

    # MCP
    parser.add_argument(
        "--mcp-config",
        dest="mcp_config",
        type=str,
        default=None,
        help="Path to MCP servers JSON configuration file.",
    )
    parser.add_argument(
        "--no-mcp",
        dest="no_mcp",
        action="store_true",
        default=False,
        help="Disable all MCP tool loading.",
    )
    parser.add_argument(
        "--trust-project-mcp",
        "--trust-workspace-mcp",
        dest="trust_project_mcp",
        action="store_true",
        default=False,
        help="Skip interactive confirmation prompts for project-level MCP servers.",
    )

    # Session & State
    parser.add_argument(
        "-r",
        "--resume",
        "--resume-thread",
        dest="resume_thread",
        type=str,
        default=None,
        help="Resume an existing session thread ID.",
    )
    parser.add_argument(
        "--server-url",
        dest="server_url",
        type=str,
        default=None,
        help="Connect to a remote TalkOps central server URL instead of spawning a local server (or set TALKOPS_SERVER_URL).",
    )
    parser.add_argument(
        "--api-key",
        dest="api_key",
        type=str,
        default=None,
        help="Bearer token or API key for authenticating with a remote TalkOps server (or set TALKOPS_SERVER_API_KEY).",
    )
    parser.add_argument(
        "--user-id",
        dest="user_id",
        type=str,
        default=None,
        help="Explicit user identity for multi-user activity tracking (or set TALKOPS_USER_ID).",
    )
    parser.add_argument(
        "--cwd",
        "--workspace",
        dest="cwd",
        type=str,
        default=None,
        help="Working directory path for this execution.",
    )
    parser.add_argument(
        "--goal",
        type=str,
        default=None,
        help="Initial goal objective to establish (for interactive TUI mode).",
    )
    parser.add_argument(
        "--rubric",
        type=str,
        default=None,
        help="Acceptance criteria to grade against (literal text or @path); used with headless mode.",
    )
    parser.add_argument(
        "--rubric-model",
        type=str,
        default=None,
        help="Model specification for the rubric grader agent.",
    )
    parser.add_argument(
        "--rubric-max-iterations",
        type=positive_int,
        default=None,
        help="Maximum evaluation iterations for the rubric grader.",
    )
    parser.add_argument(
        "--trajectory-file",
        dest="trajectory_file",
        type=str,
        default=None,
        help="Write step-wise JSONL execution trajectory and benchmark telemetry to file.",
    )
    parser.add_argument(
        "--no-subagents",
        dest="no_subagents",
        action="store_true",
        default=False,
        help="Disable subagent delegation (Single-Agent vs. Multi-Agent ablation mode).",
    )
    parser.add_argument(
        "--tnr",
        dest="tnr",
        action="store_true",
        default=False,
        help="Enable Transactional Non-Regression (TNR) safety lock and mandatory undo rollback.",
    )
    parser.add_argument(
        "-V",
        "--verbose",
        dest="verbose",
        action="store_true",
        default=False,
        help="Enable verbose logging.",
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error", "critical"],
        type=str.lower,
        default=None,
        help="Set logging level (default: info, or debug with -V/--verbose).",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default=None,
        help="Directory for log files (default: /tmp/talkops_logs).",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="Log file path or filename (default: talkops.log in log directory).",
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="subcommand", help="Operational subcommands")
    doctor_parser = subparsers.add_parser("doctor", help="Run system & cloud environment health checks.")
    add_json_output_arg(doctor_parser)
    doctor_parser.add_argument(
        "--aws-profile",
        dest="aws_profile",
        type=str,
        default=argparse.SUPPRESS,
        help="AWS profile override for health checks.",
    )
    doctor_parser.add_argument(
        "--aws-region",
        dest="aws_region",
        type=str,
        default=argparse.SUPPRESS,
        help="AWS region override for health checks.",
    )

    from talkops.cli.commands.auth import setup_auth_parser

    setup_auth_parser(subparsers, add_output_args=add_json_output_arg)

    def _add_remote_server_args(target_parser: argparse.ArgumentParser) -> None:
        target_parser.add_argument(
            "--server-url",
            dest="server_url",
            type=str,
            default=argparse.SUPPRESS,
            help="Remote TalkOps server URL.",
        )
        target_parser.add_argument(
            "--api-key",
            dest="api_key",
            type=str,
            default=argparse.SUPPRESS,
            help="Bearer token or API key for remote TalkOps server.",
        )
        target_parser.add_argument(
            "--user-id",
            dest="user_id",
            type=str,
            default=argparse.SUPPRESS,
            help="Explicit user identity for thread operations.",
        )

    threads_parser = subparsers.add_parser("threads", help="Manage session threads.")
    add_json_output_arg(threads_parser)
    _add_remote_server_args(threads_parser)
    threads_sub = threads_parser.add_subparsers(dest="threads_command")
    threads_list = threads_sub.add_parser("list", aliases=["ls"], help="List saved session threads.")
    add_json_output_arg(threads_list)
    _add_remote_server_args(threads_list)
    threads_del = threads_sub.add_parser("delete", help="Delete a session thread.")
    threads_del.add_argument("thread_id", help="Thread ID to delete.")
    add_json_output_arg(threads_del)
    _add_remote_server_args(threads_del)

    setup_mcp_parser(subparsers)
    setup_serve_parser(subparsers)
    setup_benchmark_parser(subparsers)

    from talkops.plugins.commands_cli import setup_plugin_parser

    setup_plugin_parser(subparsers, add_output_args=add_json_output_arg)

    from talkops.addons.commands_cli import setup_addon_parser

    setup_addon_parser(subparsers, add_output_args=add_json_output_arg)

    from talkops.skills import setup_skills_parser

    setup_skills_parser(subparsers)

    from talkops.cli.commands.agents import setup_agents_parser

    setup_agents_parser(subparsers, add_output_args=add_json_output_arg)

    help_parser = subparsers.add_parser("help", help="Show help information.")
    help_parser.add_argument(
        "help_topic",
        nargs="?",
        default=None,
        help="Optional subcommand to show help for.",
    )

    return parser


# ── Argument Validation ──────────────────────────────────


def _validate_args(args: argparse.Namespace) -> int | None:
    """Validate argument compatibility and mutual exclusions."""
    # Goal and rubric flag validations
    has_rubric = getattr(args, "rubric", None) is not None
    if getattr(args, "goal", None) and has_rubric:
        print(
            "Error: Cannot specify both --goal and --rubric. Use --goal for interactive sessions, or --rubric with -p for headless verification.",
            file=sys.stderr,
        )
        return 1

    if getattr(args, "goal", None) and getattr(args, "prompt", None):
        print(
            "Error: Cannot specify both --goal and --rubric/--prompt. Use --goal for interactive sessions, or --rubric with -p for headless verification.",
            file=sys.stderr,
        )
        return 1

    if has_rubric and not getattr(args, "prompt", None):
        print("Error: --rubric requires -p/--prompt for headless verification.", file=sys.stderr)
        return 1

    if getattr(args, "rubric_model", None) and not has_rubric:
        print("Error: --rubric-model requires --rubric.", file=sys.stderr)
        return 1

    if getattr(args, "rubric_max_iterations", None) and not has_rubric:
        print("Error: --rubric-max-iterations requires --rubric.", file=sys.stderr)
        return 1

    if (getattr(args, "quiet", False) or getattr(args, "no_stream", False)) and not getattr(args, "prompt", None):
        flags = []
        if getattr(args, "quiet", False):
            flags.append("--quiet")
        if getattr(args, "no_stream", False):
            flags.append("--no-stream")
        flag = " and ".join(flags)
        print(
            f"Error: {flag} requires -p/--prompt, -n, positional prompt, or piped stdin.",
            file=sys.stderr,
        )
        return 2

    if getattr(args, "max_turns", None) is not None and not getattr(args, "prompt", None):
        print(
            "Error: --max-turns requires -p/--prompt, -n, positional prompt, or piped stdin.",
            file=sys.stderr,
        )
        return 2

    if getattr(args, "timeout", None) is not None and not getattr(args, "prompt", None):
        print(
            "Error: --timeout requires -p/--prompt, -n, positional prompt, or piped stdin.",
            file=sys.stderr,
        )
        return 2

    if getattr(args, "no_mcp", False) and getattr(args, "mcp_config", None):
        print("Error: --no-mcp and --mcp-config are mutually exclusive.", file=sys.stderr)
        return 2

    raw_mode = str(getattr(args, "approval_mode", "") or "").strip().lower()
    if (getattr(args, "yolo", False) or raw_mode == "yolo") and not getattr(args, "prompt", None):
        print(
            "Error: 'yolo' approval mode is only supported in CLI headless mode (-p/--prompt) "
            "and webhook server mode (`talkops serve`). Interactive sessions support: manual, smart, auto.",
            file=sys.stderr,
        )
        return 2

    return None


# ── Main Entrypoint ──────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Main CLI entrypoint function."""
    raw_args = list(sys.argv[1:]) if argv is None else list(argv)

    # 1. Fast-path for --version
    if len(raw_args) == 1 and raw_args[0] in {"-v", "--version"}:
        print(f"talkops {__version__}")
        return 0

    parser = build_parser()

    known_subcommands = {
        "doctor",
        "auth",
        "threads",
        "mcp",
        "serve",
        "webhook",
        "webhooks",
        "benchmark",
        "benchmarks",
        "plugin",
        "plugins",
        "addon",
        "addons",
        "skill",
        "skills",
        "agent",
        "agents",
        "help",
    }

    # Hoist global logging flags to the front so they work seamlessly
    # across both root invocations and subcommands (e.g. talkops doctor --verbose)
    hoisted_args: list[str] = []
    remaining_args: list[str] = []
    it = iter(raw_args)
    for arg in it:
        if arg in {"-v", "-V", "--verbose"}:
            hoisted_args.append("--verbose")
        elif arg.startswith(("--log-level=", "--log-dir=", "--log-file=")):
            hoisted_args.append(arg)
        elif arg in {"--log-level", "--log-dir", "--log-file"}:
            hoisted_args.append(arg)
            val = next(it, None)
            if val is not None:
                hoisted_args.append(val)
        else:
            remaining_args.append(arg)
    reordered_args = hoisted_args + remaining_args

    # Identify flags with value arguments vs flags without
    flags_with_values: set[str] = {
        opt for act in parser._actions if act.nargs not in (0, "?") for opt in act.option_strings
    }
    optional_val_flags: set[str] = {
        opt for act in parser._actions if act.nargs == "?" for opt in act.option_strings
    }

    # Find the first positional token to check if it's a subcommand or a prompt
    first_pos = None
    idx = 0
    while idx < len(reordered_args):
        token = reordered_args[idx]
        if token in flags_with_values:
            idx += 2
            continue
        if token in optional_val_flags:
            if (
                idx + 1 < len(reordered_args)
                and not reordered_args[idx + 1].startswith("-")
                and reordered_args[idx + 1] not in known_subcommands
            ):
                idx += 2
            else:
                idx += 1
            continue
        if not token.startswith("-"):
            first_pos = token
            break
        idx += 1

    is_subcommand = first_pos in known_subcommands
    if is_subcommand:
        args = parser.parse_args(reordered_args)
    else:
        # Preprocess positional prompt if -p/--prompt not already present
        has_prompt_flag = any(a in {"-p", "--prompt", "-n", "--non-interactive"} for a in reordered_args)
        if first_pos is not None and not has_prompt_flag:
            processed_args: list[str] = []
            prompt_injected = False
            b_idx = 0
            while b_idx < len(reordered_args):
                token = reordered_args[b_idx]
                if token in flags_with_values:
                    processed_args.append(token)
                    if b_idx + 1 < len(reordered_args):
                        processed_args.append(reordered_args[b_idx + 1])
                    b_idx += 2
                elif token in optional_val_flags:
                    processed_args.append(token)
                    if (
                        b_idx + 1 < len(reordered_args)
                        and not reordered_args[b_idx + 1].startswith("-")
                        and reordered_args[b_idx + 1] not in known_subcommands
                    ):
                        processed_args.append(reordered_args[b_idx + 1])
                        b_idx += 2
                    else:
                        b_idx += 1
                elif not token.startswith("-") and token == first_pos and not prompt_injected:
                    processed_args.extend(["-p", token])
                    prompt_injected = True
                    b_idx += 1
                else:
                    processed_args.append(token)
                    b_idx += 1
            args = parser.parse_args(processed_args)
        else:
            args = parser.parse_args(reordered_args)

    # 2. Configure system logging based on CLI flags and environment
    from talkops.utils.logger import configure_logging

    log_level = "DEBUG" if getattr(args, "verbose", False) else getattr(args, "log_level", None)
    log_to_console = (
        True
        if getattr(args, "verbose", False)
        else (False if getattr(args, "json_output", False) or getattr(args, "quiet", False) else None)
    )

    configure_logging(
        level=log_level,
        log_to_console=log_to_console,
        log_dir=getattr(args, "log_dir", None),
        log_file=getattr(args, "log_file", None),
    )

    # 3. Ingest piped stdin
    _apply_stdin_pipe(args)

    # 4. Handle subcommands
    raw_fmt = getattr(args, "output_format", "text")
    output_format: OutputFormat = "json" if raw_fmt == "json" else "text"

    if args.subcommand == "help":
        topic = getattr(args, "help_topic", None)
        if topic:
            subparsers_actions = [
                act for act in parser._actions if isinstance(act, argparse._SubParsersAction)
            ]
            if subparsers_actions and topic in subparsers_actions[0].choices:
                subparsers_actions[0].choices[topic].print_help()
                return 0
        parser.print_help()
        return 0
    elif args.subcommand == "doctor":
        aws_prof = getattr(args, "aws_profile", None)
        aws_reg = getattr(args, "aws_region", None)
        if aws_prof or aws_reg:
            return run_doctor_command(output_format, aws_profile=aws_prof, aws_region=aws_reg)
        return run_doctor_command(output_format)
    elif args.subcommand == "auth":
        auth_cmd = getattr(args, "auth_command", None)
        if auth_cmd in (None, "aws"):
            aws_prof = getattr(args, "aws_profile", None)
            aws_reg = getattr(args, "aws_region", None)
            if aws_prof or aws_reg:
                return run_auth_command(output_format, aws_profile=aws_prof, aws_region=aws_reg)
            return run_auth_command(output_format)
        from talkops.cli.commands.auth import dispatch_auth_subcommand

        return dispatch_auth_subcommand(args, output_format=output_format)
    elif args.subcommand == "threads":
        srv_url = getattr(args, "server_url", None)
        srv_key = getattr(args, "api_key", None)
        usr_id = getattr(args, "user_id", None)
        subcmd = getattr(args, "threads_command", None)
        if subcmd == "delete":
            # Delete thread command
            from talkops.state.remote_session import set_remote_client_context
            from talkops.state.session import delete_thread, set_current_user_id

            if srv_url:
                set_remote_client_context(srv_url, api_key=srv_key, user_id=usr_id)
            if usr_id:
                set_current_user_id(usr_id)

            try:
                deleted = asyncio.run(delete_thread(args.thread_id, user_id=usr_id))
                if output_format == "json":
                    from talkops.output import write_json

                    write_json("threads delete", {"thread_id": args.thread_id, "deleted": deleted})
                    return 0 if deleted else 1
                else:
                    if deleted:
                        print(f"Deleted thread: {args.thread_id}")
                    else:
                        print(f"Thread not found: {args.thread_id}", file=sys.stderr)
                        return 1
                return 0
            except Exception as e:
                print(f"Error deleting thread {args.thread_id}: {e}", file=sys.stderr)
                return 1
        if srv_url or srv_key or usr_id:
            return run_threads_command(
                output_format,
                server_url=srv_url,
                api_key=srv_key,
                user_id=usr_id,
            )
        return run_threads_command(output_format)
    elif args.subcommand == "mcp":
        return run_mcp_command(args)
    elif args.subcommand in {"serve", "webhook", "webhooks"}:
        return run_serve_command(args)
    elif args.subcommand in {"benchmark", "benchmarks"}:
        return run_benchmark_command(args)
    elif args.subcommand in {"plugin", "plugins"}:
        from talkops.plugins.commands_cli import execute_plugin_command

        try:
            execute_plugin_command(args)
            return 0
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1
    elif args.subcommand in {"addon", "addons"}:
        from talkops.addons.commands_cli import execute_addon_command

        try:
            execute_addon_command(args)
            return 0
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1
    elif args.subcommand in {"skill", "skills"}:
        from talkops.skills import execute_skills_command

        try:
            execute_skills_command(args)
            return 0
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1
    elif args.subcommand in {"agent", "agents"}:
        from talkops.cli.commands.agents import run_agents_command

        return run_agents_command(args, output_format=output_format)

    # 4. Handle default model configuration
    if getattr(args, "clear_default_model", False):
        from talkops.model.config import clear_default_model

        if clear_default_model():
            print("Default model cleared.")
            return 0
        else:
            print("Error: Could not clear default model.", file=sys.stderr)
            return 1

    if getattr(args, "default_model", None) is not None:
        from talkops.model.config import load_default_model, save_default_model
        from talkops.model.factory import normalize_model_spec

        if args.default_model == "__SHOW__":
            curr = load_default_model()
            if curr:
                print(f"Default model: {curr}")
            else:
                print("No default model set.")
            return 0

        norm = normalize_model_spec(args.default_model)
        if save_default_model(norm):
            print(f"Default model set to {norm}")
            return 0
        else:
            print("Error: Could not save default model.", file=sys.stderr)
            return 1

    # 5. Parse model kwargs and profile JSON if provided
    model_params: dict[str, Any] | None = None
    if getattr(args, "model_params", None):
        try:
            model_params = json.loads(args.model_params)
            if not isinstance(model_params, dict):
                print("Error: --model-params must be a JSON object.", file=sys.stderr)
                return 1
        except json.JSONDecodeError as exc:
            print(f"Error: --model-params is not valid JSON: {exc}", file=sys.stderr)
            return 1

    profile_override: dict[str, Any] | None = None
    if getattr(args, "profile_override", None):
        try:
            profile_override = json.loads(args.profile_override)
            if not isinstance(profile_override, dict):
                print("Error: --profile-override must be a JSON object.", file=sys.stderr)
                return 1
        except json.JSONDecodeError as exc:
            print(f"Error: --profile-override is not valid JSON: {exc}", file=sys.stderr)
            return 1

    # 6. Validate argument combinations
    val_err = _validate_args(args)
    if val_err is not None:
        return val_err

    # 7. Resolve Rubric text if provided
    resolved_rubric = None
    if args.rubric is not None:
        try:
            resolved_rubric = _resolve_rubric_text(args.rubric)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    # 8. Resolve Approval Mode
    approval_mode = _resolve_approval_mode(args)

    # 8b. Mandatory Control Plane Token Authentication Gate (applies to both local TUI/CLI and remote server)
    auth_rc = _ensure_cli_authenticated(args, output_format=output_format)
    if auth_rc != 0:
        return auth_rc

    # 9. Headless / Non-interactive execution mode
    effective_server_url = getattr(args, "server_url", None) or os.environ.get("TALKOPS_SERVER_URL")
    if args.prompt:
        model = args.model
        if not model:
            try:
                from talkops.model.factory import _get_default_model_spec

                model = _get_default_model_spec()
            except Exception as e:
                if not effective_server_url:
                    print(f"Error: {e}", file=sys.stderr)
                    return 1
                model = None

        json_out = getattr(args, "output_format", "text") == "json"

        from talkops.project_utils import ProjectContext

        project_context = (
            ProjectContext.from_user_cwd(Path(args.cwd))
            if args.cwd
            else ProjectContext.from_user_cwd(Path.cwd())
        )
        effective_cwd = str(project_context.user_cwd)
        non_interactive_kwargs: dict[str, Any] = {
            "model": model,
            "model_params": model_params,
            "profile_override": profile_override,
            "approval_mode": approval_mode,
            "shell_allow_list": args.shell_allow_list,
            "read_only": args.read_only,
            "aws_profile": args.aws_profile,
            "aws_region": args.aws_region,
            "quiet": args.quiet,
            "no_stream": args.no_stream,
            "json_output": json_out,
            "max_turns": args.max_turns,
            "timeout": float(args.timeout) if args.timeout is not None else None,
            "rubric": resolved_rubric,
            "rubric_model": args.rubric_model,
            "rubric_max_iterations": args.rubric_max_iterations,
            "recursion_limit": args.recursion_limit,
            "initial_skill": args.initial_skill,
            "startup_cmd": args.startup_cmd,
            "mcp_config_path": args.mcp_config,
            "no_mcp": args.no_mcp,
            "trust_project_mcp": args.trust_project_mcp,
            "thread_id": args.resume_thread,
            "cwd": effective_cwd,
        }
        if getattr(args, "server_url", None):
            non_interactive_kwargs["server_url"] = args.server_url
        if getattr(args, "api_key", None):
            non_interactive_kwargs["api_key"] = args.api_key
        if getattr(args, "user_id", None):
            non_interactive_kwargs["user_id"] = args.user_id
        if getattr(args, "trajectory_file", None):
            non_interactive_kwargs["trajectory_file"] = args.trajectory_file
        if getattr(args, "no_subagents", False):
            non_interactive_kwargs["no_subagents"] = True
        if getattr(args, "tnr", False):
            non_interactive_kwargs["tnr"] = True
        if getattr(args, "agent", None):
            non_interactive_kwargs["agent"] = args.agent
        return run_non_interactive(args.prompt, **non_interactive_kwargs)

    # 10. Interactive TUI mode
    from talkops.project_utils import ProjectContext

    project_context = (
        ProjectContext.from_user_cwd(Path(args.cwd))
        if args.cwd
        else ProjectContext.from_user_cwd(Path.cwd())
    )
    effective_cwd = str(project_context.user_cwd)
    interactive_kwargs: dict[str, Any] = {
        "model": args.model,
        "approval_mode": approval_mode,
        "resume_thread": args.resume_thread,
        "cwd": effective_cwd,
        "goal": args.goal,
        "initial_prompt": args.initial_prompt,
        "initial_skill": args.initial_skill,
        "startup_cmd": args.startup_cmd,
        "read_only": args.read_only,
        "aws_profile": args.aws_profile,
        "aws_region": args.aws_region,
        "shell_allow_list": args.shell_allow_list,
        "mcp_config_path": args.mcp_config,
        "no_mcp": args.no_mcp,
        "trust_project_mcp": args.trust_project_mcp,
    }
    if getattr(args, "server_url", None):
        interactive_kwargs["server_url"] = args.server_url
    if getattr(args, "api_key", None):
        interactive_kwargs["api_key"] = args.api_key
    if getattr(args, "user_id", None):
        interactive_kwargs["user_id"] = args.user_id
    if getattr(args, "agent", None):
        interactive_kwargs["agent"] = args.agent
    return run_interactive(**interactive_kwargs)


cli_main = main

if __name__ == "__main__":
    sys.exit(main())

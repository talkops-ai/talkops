"""Central webhook server CLI command (`talkops serve` / `talkops webhooks`)."""

from __future__ import annotations

import argparse
import contextlib
import os
from pathlib import Path
import sys
import time
from typing import TYPE_CHECKING, Any, TextIO

from colorama import Fore, Style
from rich.console import Console
from rich.table import Table

from talkops.config.settings import get_settings
from talkops.output import add_json_output_arg, write_json
from talkops.project_utils import ProjectContext
from talkops.server import ServerConfig, ServerProcess
from talkops.utils.logger import configure_logging
from talkops.webhooks.models import WebhookConfig
from talkops.webhooks.providers import create_default_registry

DEFAULT_WEBHOOK_PORT = 2024

DEFAULT_WEBHOOK_HOST = "0.0.0.0"

_LEVEL_BADGE_COLORS: dict[str, str] = {
    "[DEBUG]": f"{Fore.LIGHTBLACK_EX}[DEBUG]{Style.RESET_ALL}",
    "[INFO]": f"{Fore.BLUE}[INFO]{Style.RESET_ALL}",
    "[WARNING]": f"{Fore.YELLOW}[WARNING]{Style.RESET_ALL}",
    "[ERROR]": f"{Fore.RED}[ERROR]{Style.RESET_ALL}",
    "[CRITICAL]": f"{Fore.LIGHTRED_EX}[CRITICAL]{Style.RESET_ALL}",
}


def _colorize_log_line(line: str, *, use_color: bool = True) -> str:
    """Apply ANSI color to leading [LEVEL] tags for live console streaming."""
    if not use_color:
        return line
    for tag, colored in _LEVEL_BADGE_COLORS.items():
        if line.startswith(tag):
            return colored + line[len(tag) :]
    return line


def _drain_live_logs(
    log_reader: TextIO | None,
    *,
    stream: TextIO | None = None,
    quiet: bool = False,
) -> None:
    """Read and print any newly appended lines from the server log file."""
    if log_reader is None or quiet:
        return
    out = stream or sys.stdout
    use_color = bool(getattr(out, "isatty", lambda: False)())
    with contextlib.suppress(Exception):
        while True:
            line = log_reader.readline()
            if not line:
                break
            out.write(_colorize_log_line(line, use_color=use_color))
        out.flush()


def setup_serve_parser(subparsers: Any) -> None:
    """Register `talkops serve` (and `webhooks` alias) subcommand parser."""
    serve_parser = subparsers.add_parser(
        "serve",
        aliases=["webhook", "webhooks"],
        help="Start TalkOps in central server & webhook mode (TUI/CLI remote clients + SNS, PagerDuty, Alertmanager, CloudEvents, Jenkins, GitHub).",
    )
    serve_parser.add_argument(
        "action",
        nargs="?",
        default="start",
        choices=["start", "list", "ls", "addons"],
        help="Action to perform: 'start' (default) launches the central server; 'list' prints webhook endpoints; 'addons' lists server addons.",
    )
    serve_parser.add_argument(
        "--profile",
        dest="profile",
        type=str,
        default=argparse.SUPPRESS,
        help="Server addon profile preset ('default', 'api-only', 'webhook-worker', 'enterprise-full').",
    )
    serve_parser.add_argument(
        "--addons",
        dest="addons",
        type=str,
        default=argparse.SUPPRESS,
        help="Comma-separated list of server addons to enable (e.g. 'chatops,telemetry-audit' or 'all').",
    )
    serve_parser.add_argument(
        "--disable-addons",
        dest="disable_addons",
        type=str,
        default=argparse.SUPPRESS,
        help="Comma-separated list of server addons to disable (e.g. 'webhooks').",
    )
    serve_parser.add_argument(
        "--custom-addon",
        dest="custom_addons",
        action="append",
        default=argparse.SUPPRESS,
        help="Dynamic custom addon spec ('pkg.mod:AddonClass' or 'path/to/addon.py:AddonClass'). Can be specified multiple times.",
    )
    serve_parser.add_argument(
        "--addons-config",
        dest="addons_config",
        type=str,
        default=argparse.SUPPRESS,
        help="Path to declarative addons.json / addons.yaml configuration file for external & custom addons.",
    )
    serve_parser.add_argument(
        "--list-addons",
        action="store_true",
        dest="list_addons",
        help="List all discovered Open-Source, Enterprise, and Custom server addons and exit.",
    )
    serve_parser.add_argument(
        "--host",
        type=str,
        default=None,
        help=f"Host interface to bind (default: configured webhooks.host or {DEFAULT_WEBHOOK_HOST}).",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Port to listen on (default: configured webhooks.port or {DEFAULT_WEBHOOK_PORT}).",
    )
    serve_parser.add_argument(
        "--webhook-port",
        dest="webhook_port",
        type=int,
        default=argparse.SUPPRESS,
        help="Optional dedicated port for inbound webhook ingestion (/webhooks/{provider}). If omitted, webhooks are served on --port.",
    )
    serve_parser.add_argument(
        "--webhook-host",
        dest="webhook_host",
        type=str,
        default=argparse.SUPPRESS,
        help="Optional bind host for the dedicated --webhook-port listener.",
    )
    serve_parser.add_argument(
        "--postgres-uri",
        dest="postgres_uri",
        type=str,
        default=argparse.SUPPRESS,
        help="PostgreSQL connection URI for multi-user checkpoint & store persistence (or set TALKOPS_POSTGRES_URI).",
    )
    serve_parser.add_argument(
        "--auth-mode",
        dest="auth_mode",
        choices=["token"],
        default=argparse.SUPPRESS,
        help="Authentication mode for central server: 'token' (mandatory Bearer token / X-API-Key / JWT).",
    )
    serve_parser.add_argument(
        "--workspaces-dir",
        dest="workspaces_dir",
        type=str,
        default=argparse.SUPPRESS,
        help="Directory for server workspace isolation.",
    )
    serve_parser.add_argument(
        "--list",
        action="store_true",
        dest="list_only",
        help="List all supported webhook providers and exit without starting the server.",
    )
    add_json_output_arg(serve_parser)
    serve_parser.add_argument(
        "--output-format",
        dest="output_format",
        choices=["text", "json"],
        default=argparse.SUPPRESS,
        help="Output format for provider listing.",
    )
    serve_approval_group = serve_parser.add_mutually_exclusive_group()
    serve_approval_group.add_argument(
        "--approval-mode",
        dest="approval_mode",
        type=str,
        choices=["manual", "smart", "auto", "yolo"],
        default=argparse.SUPPRESS,
        help="Tool approval mode for webhook runs: 'smart' (default Jev gate), 'auto' (LLM classifier), 'yolo' (unrestricted), or 'manual'.",
    )
    serve_approval_group.add_argument(
        "-y",
        "--auto-approve",
        dest="auto_approve",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Enable classifier-backed Auto tool approval mode.",
    )
    serve_approval_group.add_argument(
        "--smart",
        dest="smart",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Enable Jev-powered Smart approval mode (<100ms System One safety gate).",
    )
    serve_approval_group.add_argument(
        "--yolo",
        dest="yolo",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Enable unrestricted YOLO mode for webhook runs without classifier or human gating.",
    )
    serve_parser.add_argument(
        "--read-only",
        "--dry-run",
        dest="read_only",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Enforce read-only cloud operations safety mode (blocks mutating AWS/K8s/Terraform calls).",
    )
    from talkops.config.settings import parse_shell_allow_list

    serve_parser.add_argument(
        "-S",
        "--shell-allow-list",
        dest="shell_allow_list",
        type=lambda v: parse_shell_allow_list(v) or [],
        default=argparse.SUPPRESS,
        help="Comma-separated list of allowed shell commands ('recommended', 'all', or comma-separated names).",
    )
    serve_parser.add_argument(
        "-M",
        "--model",
        dest="model",
        type=str,
        default=argparse.SUPPRESS,
        help="Model specification to use (e.g. anthropic:claude-sonnet-4-5, openai:gpt-4o).",
    )
    serve_parser.add_argument(
        "--aws-profile",
        dest="aws_profile",
        type=str,
        default=argparse.SUPPRESS,
        help="AWS profile override for webhook runs.",
    )
    serve_parser.add_argument(
        "--aws-region",
        dest="aws_region",
        type=str,
        default=argparse.SUPPRESS,
        help="AWS region override for webhook runs.",
    )
    serve_parser.add_argument(
        "--cwd",
        dest="cwd",
        type=str,
        default=argparse.SUPPRESS,
        help="Working directory path for the webhook server.",
    )
    serve_parser.add_argument(
        "--mcp-config",
        dest="mcp_config",
        type=str,
        default=argparse.SUPPRESS,
        help="Path to MCP servers JSON configuration file.",
    )
    serve_parser.add_argument(
        "--no-mcp",
        dest="no_mcp",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Disable all MCP tool loading.",
    )
    serve_parser.add_argument(
        "--trust-project-mcp",
        dest="trust_project_mcp",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Trust project-level MCP servers in unattended webhook mode.",
    )
    serve_parser.add_argument(
        "--log-level",
        dest="log_level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        type=str.upper,
        default=argparse.SUPPRESS,
        help="Server log level for webhook operations (default: INFO).",
    )
    serve_parser.add_argument(
        "-V",
        "--verbose",
        dest="verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Enable verbose DEBUG logging for webhook operations.",
    )
    serve_parser.add_argument(
        "-q",
        "--quiet",
        dest="quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Suppress live log streaming to stdout while the webhook server runs.",
    )


def _render_providers_table(
    console: Console,
    base_url: str,
    webhook_cfg: WebhookConfig,
) -> None:
    registry = create_default_registry()
    table = Table(
        title=f"TalkOps Central Webhook Endpoints ({base_url})",
        border_style="cyan",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Provider", style="bold cyan")
    table.add_column("Webhook Endpoint URL", style="green")
    table.add_column("Aliases", style="dim")
    table.add_column("Auth Status", justify="center")
    table.add_column("Supported Integrations")

    for item in registry.list_providers():
        provider = item["provider"]
        secret = webhook_cfg.get_provider_secret(provider)
        auth_badge = "[green]HMAC/Token[/green]" if secret else "[yellow]Open (VPC)[/yellow]"
        aliases_str = ", ".join(item["aliases"]) if item["aliases"] else "-"
        table.add_row(
            provider,
            f"{base_url}{item['endpoint']}",
            aliases_str,
            auth_badge,
            item["description"],
        )

    console.print(table)


def _resolve_serve_approval_mode(args: argparse.Namespace, settings: Any) -> str:
    """Resolve approval mode for `talkops serve` (supports manual, smart, auto, yolo)."""
    raw_approval = getattr(args, "approval_mode", None)
    if getattr(args, "yolo", False) or (raw_approval and str(raw_approval).strip().lower() in {"yolo", "y"}):
        return "yolo"
    if getattr(args, "auto_approve", False) or (raw_approval and str(raw_approval).strip().lower() in {"auto", "a"}):
        return "auto"
    if getattr(args, "smart", False) or (raw_approval and str(raw_approval).strip().lower() in {"smart", "s", "jev"}):
        return "smart"
    if raw_approval and str(raw_approval).strip().lower() in {"manual", "m", "always"}:
        return "manual"

    cfg_mode = str(getattr(settings, "approval_mode", "") or "").strip().lower()
    if cfg_mode in {"yolo", "auto", "smart"}:
        return cfg_mode

    # Unattended central webhook mode defaults to 'smart' (Jev safety gate)
    return "smart"


def _resolve_serve_log_level(args: argparse.Namespace) -> str:
    """Resolve log level for `talkops serve` (defaults to INFO)."""
    if getattr(args, "verbose", False):
        return "DEBUG"
    raw_level = getattr(args, "log_level", None)
    if raw_level and str(raw_level).strip().upper() in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        return str(raw_level).strip().upper()
    return "INFO"


def _render_addons_table(
    console: Console,
    profile: str,
    addons_list: list[dict[str, Any]],
) -> None:
    table = Table(
        title=f"TalkOps Server Addons (profile: {profile})",
        border_style="cyan",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Addon Name", style="bold cyan")
    table.add_column("Status", justify="center")
    table.add_column("Tier", justify="center")
    table.add_column("Source", style="dim")
    table.add_column("Description")

    for item in addons_list:
        enabled = item.get("enabled", False)
        status_badge = "[green]enabled[/green]" if enabled else "[dim]disabled[/dim]"
        tier = str(item.get("tier") or "oss")
        tier_badge = (
            f"[cyan]{tier}[/cyan]"
            if tier == "oss"
            else (f"[magenta]{tier}[/magenta]" if tier == "enterprise" else f"[yellow]{tier}[/yellow]")
        )
        table.add_row(
            str(item.get("name", "")),
            status_badge,
            tier_badge,
            str(item.get("source", "builtin")),
            str(item.get("description", "")),
        )
    console.print(table)


def run_serve_command(args: argparse.Namespace) -> int:
    """Execute the `talkops serve` / `talkops webhooks` command."""
    if getattr(args, "no_mcp", False) and getattr(args, "mcp_config", None):
        print("Error: --no-mcp and --mcp-config are mutually exclusive.", file=sys.stderr)
        return 2
    console = Console()
    settings = get_settings()
    action = getattr(args, "action", "start")
    list_only = getattr(args, "list_only", False) or action in {"list", "ls"}
    list_addons = getattr(args, "list_addons", False) or action == "addons"
    host = getattr(args, "host", None) or settings.webhook_host or DEFAULT_WEBHOOK_HOST
    raw_port = getattr(args, "port", None)
    port = int(raw_port if raw_port is not None else (settings.webhook_port or DEFAULT_WEBHOOK_PORT))
    webhook_port = getattr(args, "webhook_port", None)
    webhook_host = getattr(args, "webhook_host", None) or host
    output_format = getattr(args, "output_format", "text")

    raw_profile = getattr(args, "profile", None)
    raw_addons_str = getattr(args, "addons", None)
    cli_addons = [s.strip() for s in str(raw_addons_str).split(",") if s.strip()] if raw_addons_str else None
    raw_disable_str = getattr(args, "disable_addons", None)
    cli_disable = [s.strip() for s in str(raw_disable_str).split(",") if s.strip()] if raw_disable_str else None
    raw_custom_list = getattr(args, "custom_addons", None)
    cli_custom: list[str] = []
    if isinstance(raw_custom_list, list):
        for entry in raw_custom_list:
            cli_custom.extend(s.strip() for s in str(entry).split(",") if s.strip())
    raw_addons_config = getattr(args, "addons_config", None)

    cwd = getattr(args, "cwd", None)
    project_context = (
        ProjectContext.from_user_cwd(Path(cwd))
        if cwd
        else ProjectContext.from_user_cwd(Path.cwd())
    )

    if list_addons:
        from talkops.addons.manager import AddonManager

        mgr = AddonManager(
            profile=raw_profile,
            enabled_addons=cli_addons,
            disabled_addons=cli_disable,
            custom_addon_specs=cli_custom or None,
            addons_config_path=raw_addons_config,
            webhook_port=webhook_port,
            main_port=port,
            host=host,
            project_root=project_context.project_root,
        )
        active = mgr.resolve_and_validate()
        active_names = {a.name for a in active}
        addons_summary = [
            {
                "name": name,
                "enabled": name in active_names,
                "tier": addon.tier,
                "version": getattr(addon, "version", "1.0.0"),
                "source": getattr(addon, "source", "builtin"),
                "description": addon.description,
            }
            for name, addon in sorted(mgr.discovered_addons.items())
        ]
        if output_format == "json":
            write_json(
                "serve addons",
                {
                    "profile": mgr.profile,
                    "active_addons": [a.name for a in active],
                    "addons": addons_summary,
                },
            )
            return 0
        console.print()
        _render_addons_table(console, mgr.profile, addons_summary)
        console.print()
        return 0

    webhook_cfg = WebhookConfig.from_settings(settings)
    registry = create_default_registry()
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    display_wh_host = "127.0.0.1" if webhook_host == "0.0.0.0" else webhook_host
    base_url = f"http://{display_host}:{port}"
    webhook_base_url = f"http://{display_wh_host}:{webhook_port}" if webhook_port else base_url

    if list_only:
        providers = registry.list_providers()
        if output_format == "json":
            write_json(
                "serve list",
                {
                    "base_url": base_url,
                    "webhook_base_url": webhook_base_url,
                    "providers": [
                        {
                            **p,
                            "url": f"{webhook_base_url}{p['endpoint']}",
                            "secret_configured": bool(webhook_cfg.get_provider_secret(p["provider"])),
                        }
                        for p in providers
                    ],
                },
            )
            return 0
        console.print()
        _render_providers_table(console, webhook_base_url, webhook_cfg)
        console.print()
        return 0

    model = getattr(args, "model", None) or settings.model_name
    if not model:
        try:
            from talkops.model.factory import _get_default_model_spec

            model = _get_default_model_spec()
        except Exception:
            model = None

    effective_approval = _resolve_serve_approval_mode(args, settings)
    read_only = bool(getattr(args, "read_only", False))
    server_log_level = _resolve_serve_log_level(args)
    quiet = bool(getattr(args, "quiet", False))

    # Ensure active logging level is propagated to child server subprocess via environment
    configure_logging(level=server_log_level)
    os.environ["TALKOPS_CENTRAL_SERVER"] = "1"

    server_config = ServerConfig.from_cli_args(
        project_context=project_context,
        model=model,
        interactive=False,
        auto_approve=(effective_approval == "auto"),
        approval_mode=effective_approval,
        read_only=read_only,
        aws_profile=getattr(args, "aws_profile", None) or settings.aws_profile,
        aws_region=getattr(args, "aws_region", None) or settings.aws_region,
        shell_allow_list=getattr(args, "shell_allow_list", None) or settings.shell_allow_list,
        mcp_config_path=getattr(args, "mcp_config", None),
        no_mcp=bool(getattr(args, "no_mcp", False)),
        trust_project_mcp=bool(getattr(args, "trust_project_mcp", False)),
        server_log_level=server_log_level,
        postgres_uri=getattr(args, "postgres_uri", None),
        auth_mode=getattr(args, "auth_mode", None),
        webhook_port=webhook_port,
        webhook_host=webhook_host if webhook_port is not None else None,
        workspaces_dir=getattr(args, "workspaces_dir", None),
        profile=raw_profile,
        addons=cli_addons,
        disable_addons=cli_disable,
        custom_addons=cli_custom or None,
        addons_config=raw_addons_config,
    )

    proc = ServerProcess(server_config, host=host, port=port)
    storage_backend = "postgres" if server_config.postgres_uri else "sqlite"
    console.print(
        f"\n[bold cyan]Starting TalkOps Central Server[/bold cyan] "
        f"(host=[bold]{host}[/bold], port=[bold]{proc.port}[/bold], "
        f"webhook_port=[bold]{proc.webhook_port or proc.port}[/bold], "
        f"storage=[bold]{storage_backend}[/bold], auth=[bold]{server_config.auth_mode}[/bold], "
        f"approval_mode=[bold]{effective_approval}[/bold], read_only=[bold]{read_only}[/bold], "
        f"log_level=[bold]{server_log_level}[/bold])...\n"
    )

    log_reader: TextIO | None = None
    try:
        url = proc.start()
        active_wh_url = proc.webhook_url or url
        _render_providers_table(console, active_wh_url, webhook_cfg)
        console.print(
            f"\n[bold green]● Ready![/bold green] Central Server API: [bold]{url}[/bold] | "
            f"Webhooks: [bold]{active_wh_url}/webhooks/{{provider}}[/bold] "
            "(Press Ctrl+C to stop)"
        )
        if isinstance(getattr(proc, "log_file", None), Path):
            console.print(
                f"[dim]  Log file: {proc.log_file} (streaming live webhook & agent logs below)[/dim]\n"
            )
            with contextlib.suppress(Exception):
                proc.log_file.parent.mkdir(parents=True, exist_ok=True)
                proc.log_file.touch(exist_ok=True)
                log_reader = open(proc.log_file, encoding="utf-8", errors="replace")
                log_reader.seek(0, os.SEEK_END)
        else:
            console.print()

        while proc.process is not None and proc.process.poll() is None:
            _drain_live_logs(log_reader, quiet=quiet)
            time.sleep(0.15)
        _drain_live_logs(log_reader, quiet=quiet)
        return proc.process.returncode if proc.process and proc.process.returncode is not None else 0
    except KeyboardInterrupt:
        _drain_live_logs(log_reader, quiet=quiet)
        console.print("\n[yellow]Shutting down TalkOps Server...[/yellow]")
        return 0
    finally:
        if log_reader is not None:
            with contextlib.suppress(Exception):
                log_reader.close()
        proc.stop()


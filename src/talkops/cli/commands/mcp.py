"""MCP management CLI command for talkops."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Any

from rich.console import Console
from rich.table import Table


def setup_mcp_parser(subparsers: Any) -> None:
    """Setup parser for `talkops mcp`."""
    mcp_parser = subparsers.add_parser(
        "mcp",
        help="List, probe, and inspect configured Model Context Protocol (MCP) servers.",
    )
    mcp_parser.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "ls", "status", "probe"],
        help="Action to perform (default: list).",
    )
    mcp_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output in JSON format.",
    )
    mcp_parser.add_argument(
        "--output-format",
        dest="output_format",
        choices=["text", "json"],
        default=argparse.SUPPRESS,
        help="Output format (default: text).",
    )
    mcp_parser.add_argument(
        "--probe",
        action="store_true",
        help="Actively probe configured servers to inspect tools and health status.",
    )
    mcp_parser.add_argument(
        "--mcp-config",
        dest="mcp_config",
        type=str,
        default=argparse.SUPPRESS,
        help="Path to MCP servers JSON configuration file.",
    )
    mcp_parser.add_argument(
        "--no-mcp",
        dest="no_mcp",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Disable all MCP tool loading.",
    )
    mcp_parser.add_argument(
        "--cwd",
        dest="cwd",
        type=str,
        default=argparse.SUPPRESS,
        help="Working directory path for MCP discovery.",
    )


def run_mcp_command(args: argparse.Namespace | None = None) -> int:
    """Execute the `talkops mcp` CLI command."""
    from talkops.mcp.discovery import MCPDiscovery, _load_mcp_json, _parse_server_config
    from talkops.mcp.preload import preload_mcp_metadata

    console = Console()

    action = getattr(args, "action", "list") if args else "list"
    json_output = bool(
        args
        and (
            getattr(args, "json_output", False)
            or getattr(args, "output_format", "text") == "json"
        )
    )
    do_probe = getattr(args, "probe", False) if args else False
    no_mcp = bool(getattr(args, "no_mcp", False)) if args else False
    mcp_config = getattr(args, "mcp_config", None) if args else None
    cwd = getattr(args, "cwd", None) if args else None

    if no_mcp and mcp_config:
        print("Error: --no-mcp and --mcp-config are mutually exclusive.", file=sys.stderr)
        return 2

    if action in ("status", "probe"):
        do_probe = True

    if no_mcp:
        configs: dict[str, Any] = {}
    else:
        project_root = Path(cwd).expanduser().resolve() if cwd else None
        discovery = MCPDiscovery()
        configs = dict(discovery.discover(project_root=project_root))
        if mcp_config:
            cfg_path = Path(mcp_config).expanduser().resolve()
            for name, raw_cfg in _load_mcp_json(cfg_path).items():
                if isinstance(raw_cfg, dict):
                    clean_cfg = _parse_server_config(name, raw_cfg, "cli")
                    if name not in configs:
                        configs[name] = clean_cfg
                    else:
                        configs[name] = {**configs[name], **clean_cfg}

    if not configs:
        if json_output:
            sys.stdout.write(json.dumps({"mcp_servers": [], "total_servers": 0}, indent=2) + "\n")
            sys.stdout.flush()
        else:
            console.print("\n[yellow]No MCP servers configured.[/yellow]")
            console.print("Add servers in [bold].mcp.json[/bold] or [bold]~/.talkops/.mcp.json[/bold].\n")
        return 0

    server_infos = None
    if do_probe:
        if json_output:
            server_infos = asyncio.run(preload_mcp_metadata(configs))
        else:
            with console.status("[cyan]Probing MCP servers...[/cyan]"):
                server_infos = asyncio.run(preload_mcp_metadata(configs))

    if json_output:
        if server_infos:
            payload = {
                "mcp_servers": [s.to_dict() for s in server_infos],
                "total_servers": len(server_infos),
                "total_tools": sum(s.tool_count for s in server_infos),
            }
        else:
            payload = {
                "mcp_servers": [
                    {"name": k, **v} for k, v in configs.items()
                ],
                "total_servers": len(configs),
            }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        sys.stdout.flush()
        return 0

    console.print("\n[bold cyan]Configured MCP Servers[/bold cyan]\n")
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Status", width=8, justify="center")
    table.add_column("Server Name", style="bold")
    table.add_column("Transport", style="dim")
    table.add_column("Command / URL")
    table.add_column("Source", style="dim")
    table.add_column("Tools", justify="right")

    if server_infos:
        for srv in server_infos:
            if srv.status == "ok":
                status_glyph = "[bold green]● OK[/bold green]"
            elif srv.status == "disabled":
                status_glyph = "[dim]○ OFF[/dim]"
            elif srv.status == "unauthenticated":
                status_glyph = "[bold yellow]▲ AUTH[/bold yellow]"
            else:
                status_glyph = "[bold red]✗ ERR[/bold red]"

            cmd_or_url = srv.url or f"{srv.command} {' '.join(srv.args or [])}".strip()
            table.add_row(
                status_glyph,
                srv.name,
                srv.transport,
                cmd_or_url or "-",
                srv.source or "unknown",
                str(srv.tool_count),
            )
    else:
        for name, cfg in configs.items():
            enabled = cfg.get("enabled", True)
            status_glyph = "[green]● ON[/green]" if enabled else "[dim]○ OFF[/dim]"
            transport = cfg.get("transport") or ("http" if cfg.get("url") else "stdio")
            cmd_or_url = cfg.get("url") or f"{cfg.get('command', '')} {' '.join(cfg.get('args') or [])}".strip()
            source = cfg.get("source", "project")
            table.add_row(
                status_glyph,
                name,
                transport,
                cmd_or_url or "-",
                source,
                "-",
            )

    console.print(table)
    if not do_probe:
        console.print("\n[dim]Tip: Run `talkops mcp probe` to test live server connections and tool schemas.[/dim]\n")
    else:
        console.print("")
    return 0

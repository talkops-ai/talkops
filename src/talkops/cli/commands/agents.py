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

"""CLI ``talkops agents`` (and ``talkops agent``) subcommand implementation."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

from talkops.commands.power.agents import _clean_short_name
from talkops.output import OutputFormat, write_json
from talkops.project_utils import ProjectContext
from talkops.subagents.loader import list_subagents


def setup_agents_parser(
    subparsers: Any,
    *,
    add_output_args: Callable[[argparse.ArgumentParser], None] | None = None,
) -> argparse.ArgumentParser:
    """Register the ``agents`` (and ``agent``) CLI subcommand."""
    agents_parser = subparsers.add_parser(
        "agents",
        aliases=["agent"],
        help="List and inspect available user, project, and plugin specialist agents.",
    )
    if add_output_args is not None:
        add_output_args(agents_parser)
    agents_parser.add_argument(
        "--cwd",
        "--workspace",
        dest="cwd",
        type=str,
        default=argparse.SUPPRESS,
        help="Working directory path for agent discovery.",
    )
    agents_sub = agents_parser.add_subparsers(dest="agents_command")
    list_parser = agents_sub.add_parser(
        "list",
        aliases=["ls"],
        help="List all discovered specialist agents.",
    )
    if add_output_args is not None:
        add_output_args(list_parser)
    list_parser.add_argument(
        "--cwd",
        "--workspace",
        dest="cwd",
        type=str,
        default=argparse.SUPPRESS,
        help="Working directory path for agent discovery.",
    )
    return agents_parser


def discover_cli_agents(cwd: str | Path | None = None) -> list[dict[str, Any]]:
    """Discover all user, shared, plugin, and project specialist agents for CLI output."""
    project_context = (
        ProjectContext.from_user_cwd(Path(cwd))
        if cwd is not None
        else ProjectContext.from_user_cwd(Path.cwd())
    )
    project_root = project_context.project_root or project_context.user_cwd
    metas = list_subagents(project_root=project_root, include_plugins=True)
    items: list[dict[str, Any]] = []
    for meta in metas:
        full_name = str(meta.get("name") or "").strip()
        if not full_name:
            continue
        short_name = _clean_short_name(full_name)
        source = str(meta.get("source") or "custom")
        plugin_id = str(meta.get("plugin_id") or "")
        if not plugin_id and source.startswith("plugin:"):
            plugin_id = source.split(":", 1)[1]
        description = str(meta.get("description") or "").strip()
        first_line = description.splitlines()[0].strip() if description else ""
        items.append(
            {
                "name": short_name,
                "full_name": full_name,
                "source": source,
                "plugin_id": plugin_id or None,
                "model": meta.get("model"),
                "description": first_line,
                "tools": list(meta.get("tools") or []),
                "skills": list(meta.get("skills") or []),
                "path": str(meta.get("path") or ""),
            }
        )
    items.sort(key=lambda item: (str(item.get("name") or "").lower(), str(item.get("full_name") or "").lower()))
    return items


def run_agents_command(
    args: argparse.Namespace,
    output_format: OutputFormat = "text",
) -> int:
    """Execute ``talkops agents`` / ``talkops agent``."""
    raw_fmt = getattr(args, "output_format", output_format)
    fmt: OutputFormat = "json" if raw_fmt == "json" else "text"
    cwd = getattr(args, "cwd", None)
    items = discover_cli_agents(cwd=cwd)

    if fmt == "json":
        write_json("agents", {"agents": items, "count": len(items)})
        return 0

    if not items:
        print("No specialist agents found.")
        print("Define agents in .agents/, .talkops/agents/, ~/.talkops/agents/, or install an agent plugin.")
        return 0

    print(f"Available Agents ({len(items)}):")
    for item in items:
        short = item["name"]
        full = item["full_name"]
        src = item["source"]
        desc = item["description"] or "No description provided."
        model = item.get("model")
        meta_tags = [src]
        if model:
            meta_tags.append(f"model: {model}")
        label = f"{short} ({full})" if full != short else short
        print(f"  - {label} [{' · '.join(meta_tags)}]")
        print(f"    {desc}")
    print("\nUsage: talkops -a <agent-name> [prompt]  or  /agent <agent-name> in interactive TUI")
    return 0


__all__ = [
    "discover_cli_agents",
    "run_agents_command",
    "setup_agents_parser",
]

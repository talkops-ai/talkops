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

"""CLI subcommands for OpenWiki repository knowledge base management."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from talkops.commands.power.openwiki import format_openwiki_status_markdown
from talkops.middleware.openwiki import ensure_claude_agents_symlink_safe
from talkops.output import add_json_output_arg
from talkops.tools.openwiki_tools import (
    bootstrap_openwiki_scaffold,
    compile_openwiki_deterministic,
    inspect_openwiki_status,
    parse_wiki_page,
    search_openwiki_pages,
    upsert_agents_md_openwiki_pointer,
)


def setup_openwiki_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Register the ``talkops openwiki`` CLI subcommand and its actions."""
    parser = subparsers.add_parser(
        "openwiki",
        aliases=["wiki"],
        help="Compile, update, or query the repository OpenWiki knowledge base.",
        description="Manage the durable OpenWiki repository knowledge base (openwiki/).",
    )
    add_json_output_arg(parser, default="text")
    parser.add_argument(
        "--cwd",
        dest="repo_root",
        type=str,
        default=None,
        help="Repository root directory (defaults to current working directory).",
    )

    ow_sub = parser.add_subparsers(dest="openwiki_action", metavar="[action]")

    # status
    status_p = ow_sub.add_parser("status", help="Show OpenWiki initialization and drift status.")
    add_json_output_arg(status_p, default="text")

    # init
    init_p = ow_sub.add_parser("init", help="Scaffold and initialize the OpenWiki knowledge base.")
    init_p.add_argument("--force", "-f", action="store_true", help="Force full recompilation.")
    init_p.add_argument(
        "--brief",
        "-b",
        type=str,
        default="",
        help="Focus instructions for wiki compilation.",
    )
    add_json_output_arg(init_p, default="text")

    # update
    update_p = ow_sub.add_parser(
        "update",
        help="Incrementally update drifted OpenWiki pages and Grounded Claims.",
    )
    update_p.add_argument("--force", "-f", action="store_true", help="Force full update.")
    update_p.add_argument(
        "--brief",
        "-b",
        type=str,
        default="",
        help="Focus instructions for the incremental update.",
    )
    add_json_output_arg(update_p, default="text")

    # ask
    ask_p = ow_sub.add_parser("ask", help="Query the OpenWiki knowledge base.")
    ask_p.add_argument("question", nargs="+", help="Architectural question to ask.")
    add_json_output_arg(ask_p, default="text")

    # brief
    brief_p = ow_sub.add_parser(
        "brief",
        help="Append a standing convention or verification command to openwiki/INSTRUCTIONS.md.",
    )
    brief_p.add_argument("instruction", nargs="+", help="Standing convention text.")
    add_json_output_arg(brief_p, default="text")

    # link
    link_p = ow_sub.add_parser(
        "link",
        help="Heal CLAUDE.md symlinks and repair <!-- OPENWIKI:START --> pointers in AGENTS.md.",
    )
    add_json_output_arg(link_p, default="text")


def run_openwiki_command(args: argparse.Namespace) -> int:
    """Execute ``talkops openwiki`` CLI subcommands."""
    repo_root = Path(getattr(args, "repo_root", None) or Path.cwd()).expanduser().resolve()
    action = getattr(args, "openwiki_action", None) or "status"
    output_format = getattr(args, "output_format", "text")

    if action == "status":
        status = inspect_openwiki_status(repo_root)
        if output_format == "json":
            print(json.dumps(status, indent=2))
        else:
            print(format_openwiki_status_markdown(status))
        return 0

    if action == "brief":
        note = " ".join(getattr(args, "instruction", []) or []).strip()
        if not note:
            print("Error: standing instruction text is required.")
            return 1
        bootstrap_openwiki_scaffold(repo_root, create_ignore=False, create_instructions=True)
        instructions_path = repo_root / "openwiki" / "INSTRUCTIONS.md"
        existing = (
            instructions_path.read_text(encoding="utf-8").rstrip()
            if instructions_path.is_file()
            else "# Repository Conventions & Verification Rules"
        )
        instructions_path.write_text(f"{existing}\n- {note}\n", encoding="utf-8")
        if output_format == "json":
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "instructions_path": str(instructions_path),
                        "added": note,
                    },
                    indent=2,
                )
            )
        else:
            print(f"Updated {instructions_path} with standing instruction:\n  - {note}")
        return 0

    if action in {"init", "update"}:
        scaffold = bootstrap_openwiki_scaffold(repo_root)
        force = bool(getattr(args, "force", False))
        brief = str(getattr(args, "brief", "") or "").strip()

        summary = compile_openwiki_deterministic(
            repo_root,
            mode=action,  # type: ignore[arg-type]
            brief=brief,
            force=force,
        )
        result_payload = {**scaffold, **summary}
        if output_format == "json":
            print(json.dumps(result_payload, indent=2))
        else:
            pages = summary.get("compiledPages") or []
            print(
                f"OpenWiki `{action}` completed in {repo_root} "
                f"({len(pages)} pages, {summary.get('totalClaims', 0)} Grounded Claims)."
            )
            if pages:
                print("Pages: " + ", ".join(str(p) for p in pages))
        return 0

    if action == "link":
        healed = ensure_claude_agents_symlink_safe(repo_root)
        openwiki_dir = repo_root / "openwiki"
        if openwiki_dir.is_dir():
            subsystem_pages = [
                parse_wiki_page(p)
                for p in sorted(openwiki_dir.glob("*.md"))
                if p.name.lower() not in {"instructions.md", "index.md", "quickstart.md"}
            ]
            upsert_agents_md_openwiki_pointer(repo_root, subsystem_pages)
        if output_format == "json":
            print(json.dumps({"symlink_healed": healed, "repo_root": str(repo_root)}, indent=2))
        else:
            msg = (
                "Converted CLAUDE.md -> AGENTS.md symlink to @AGENTS.md and updated AGENTS.md OpenWiki block."
                if healed
                else "Updated <!-- OPENWIKI:START --> pointer block in AGENTS.md."
            )
            print(msg)
        return 0

    if action == "ask":
        question = " ".join(getattr(args, "question", []) or []).strip()
        status = inspect_openwiki_status(repo_root)
        if not status.get("initialized"):
            print("Note: openwiki/ is not initialized yet. Run `talkops openwiki init` first.")
            return 1

        results = search_openwiki_pages(repo_root / "openwiki", question, max_results=5)
        if output_format == "json":
            print(json.dumps(results, indent=2))
        else:
            print(f"### OpenWiki Search Results for: {question}\n")
            for hit in results.get("results", []):
                print(
                    f"- **{hit['title']}** (`openwiki/{hit['slug']}.md`, score={hit['score']}): "
                    f"{hit['description']}"
                )
                for sec in hit.get("matched_sections", [])[:2]:
                    print(f"  - *{sec['heading']}*: {sec['snippet']}")
        return 0

    return 0


__all__ = [
    "run_openwiki_command",
    "setup_openwiki_parser",
]

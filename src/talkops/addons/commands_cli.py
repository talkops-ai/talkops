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

"""CLI parser and command executor for `talkops addon` (`talkops addons`)."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from talkops.addons.discovery import (
    add_addon_marketplace_source,
    install_addon,
    list_available_addons,
    refresh_addon_marketplace,
    refresh_all_addon_marketplaces,
    remove_addon_marketplace,
    set_installed_addon_enabled,
    uninstall_addon,
)
from talkops.addons.marketplace import AddonMarketplaceError
from talkops.addons.marketplace_models import AddonInstallScope
from talkops.addons.store import (
    AddonNotFoundError,
    load_addon_marketplace_records,
    load_installed_addons,
)


def _resolve_project_root(scope: str | None) -> Path | None:
    if scope in {None, "user", "global"}:
        return None
    from talkops.config.paths import find_project_root

    root = find_project_root()
    if root is None and scope in {"project", "local", "team"}:
        root = Path.cwd()
    return root


def setup_addon_parser(
    subparsers: Any,
    *,
    make_help_action: Callable[[Callable[[], None]], type[argparse.Action]] | None = None,
    add_output_args: Callable[[argparse.ArgumentParser], None] | None = None,
) -> argparse.ArgumentParser:
    """Set up the `addon` / `addons` CLI subcommand parser."""
    _ = make_help_action
    addon_parser = subparsers.add_parser(
        "addon",
        aliases=["addons"],
        help="Manage TalkOps Server Mode addons and addon marketplaces",
        description="Install, enable, disable, and manage server addons from central or custom `.talkops/marketplace.json` repositories.",
    )
    if add_output_args is not None:
        add_output_args(addon_parser)

    addon_sub = addon_parser.add_subparsers(dest="addon_command", metavar="SUBCOMMAND")

    # list
    list_p = addon_sub.add_parser("list", aliases=["ls"], help="List available and installed server addons")
    list_p.add_argument("--installed", action="store_true", help="Show only installed addons")
    if add_output_args is not None:
        add_output_args(list_p)

    # search
    search_p = addon_sub.add_parser("search", help="Search addons across registered marketplaces")
    search_p.add_argument("query", help="Search term (name, category, or description)")
    if add_output_args is not None:
        add_output_args(search_p)

    # install
    install_p = addon_sub.add_parser(
        "install",
        aliases=["add", "i"],
        help="Install a server addon from a marketplace and auto-install its Python dependencies",
    )
    install_p.add_argument("addon", help="Addon identifier (<name> or <name>@<marketplace>)")
    install_p.add_argument(
        "--scope",
        choices=("user", "project", "local"),
        default="user",
        help="Installation and enablement scope (default: user)",
    )
    install_p.add_argument(
        "--no-deps",
        action="store_true",
        help="Skip automatic installation of Python dependencies declared in .talkops/addon.json",
    )
    if add_output_args is not None:
        add_output_args(install_p)

    # uninstall
    uninstall_p = addon_sub.add_parser(
        "uninstall",
        aliases=["remove", "rm"],
        help="Uninstall an addon from local cache",
    )
    uninstall_p.add_argument("addon", help="Addon identifier (<name> or <name>@<marketplace>)")
    uninstall_p.add_argument(
        "--scope",
        choices=("user", "project", "local"),
        default="user",
        help="Scope to clear enablement from (default: user)",
    )
    if add_output_args is not None:
        add_output_args(uninstall_p)

    # enable
    enable_p = addon_sub.add_parser("enable", help="Enable an installed server addon")
    enable_p.add_argument("addon", help="Addon identifier (<name> or <name>@<marketplace>)")
    enable_p.add_argument(
        "--scope",
        choices=("user", "project", "local"),
        default="user",
        help="Enablement scope (default: user)",
    )
    if add_output_args is not None:
        add_output_args(enable_p)

    # disable
    disable_p = addon_sub.add_parser("disable", help="Disable an installed server addon")
    disable_p.add_argument("addon", help="Addon identifier (<name> or <name>@<marketplace>)")
    disable_p.add_argument(
        "--scope",
        choices=("user", "project", "local"),
        default="user",
        help="Enablement scope (default: user)",
    )
    if add_output_args is not None:
        add_output_args(disable_p)

    # refresh
    refresh_p = addon_sub.add_parser("refresh", help="Refresh addon marketplace catalogs")
    refresh_p.add_argument("name", nargs="?", default=None, help="Optional marketplace name to refresh")
    if add_output_args is not None:
        add_output_args(refresh_p)

    # marketplace
    mp_p = addon_sub.add_parser(
        "marketplace",
        aliases=["marketplaces", "mp"],
        help="Manage central or custom organization addon marketplaces",
    )
    if add_output_args is not None:
        add_output_args(mp_p)
    mp_sub = mp_p.add_subparsers(dest="marketplace_command", metavar="ACTION")

    mp_list = mp_sub.add_parser("list", aliases=["ls"], help="List registered addon marketplaces")
    if add_output_args is not None:
        add_output_args(mp_list)

    mp_add = mp_sub.add_parser("add", help="Add an addon marketplace (local path, GitHub owner/repo, Git URL, or HTTP URL)")
    mp_add.add_argument("source", help="Marketplace source path, owner/repo, or URL")
    if add_output_args is not None:
        add_output_args(mp_add)

    mp_rm = mp_sub.add_parser("remove", aliases=["rm", "delete"], help="Remove a registered addon marketplace")
    mp_rm.add_argument("name", help="Marketplace name to remove")
    if add_output_args is not None:
        add_output_args(mp_rm)

    return addon_parser


def execute_addon_command(args: argparse.Namespace) -> str | None:
    """Execute a parsed `talkops addon` CLI command."""
    cmd = getattr(args, "addon_command", None)
    output_format = getattr(args, "output_format", "text")
    if getattr(args, "json_output", False):
        output_format = "json"

    if cmd in {None, "list", "ls"}:
        only_installed = bool(getattr(args, "installed", False))
        rows = list_available_addons(project_root=_resolve_project_root("project"))
        if only_installed:
            rows = tuple(r for r in rows if r[3])

        if output_format == "json":
            payload = {
                "addons": [
                    {
                        "addon_id": aid,
                        "version": ver,
                        "description": desc,
                        "installed": is_inst,
                        "enabled": is_en,
                    }
                    for aid, ver, desc, is_inst, is_en in rows
                ]
            }
            text = json.dumps(payload, indent=2)
            print(text)
            return text

        if not rows:
            text = "No marketplace addons found. Use 'talkops addon marketplace add <repo-or-path>' or 'talkops addon install <name>'."
            print(text)
            return text

        lines = ["Server Addons (Marketplace & Installed):"]
        for aid, ver, desc, is_inst, is_en in rows:
            state = "enabled" if is_en else ("installed" if is_inst else "available")
            lines.append(f"  • {aid} (v{ver}) [{state}] — {desc}")
        text = "\n".join(lines)
        print(text)
        return text

    if cmd == "search":
        query = str(getattr(args, "query", "")).strip().lower()
        rows = [
            r
            for r in list_available_addons(project_root=_resolve_project_root("project"))
            if query in r[0].lower() or query in r[2].lower()
        ]
        if output_format == "json":
            text = json.dumps(
                {
                    "results": [
                        {
                            "addon_id": aid,
                            "version": ver,
                            "description": desc,
                            "installed": is_inst,
                            "enabled": is_en,
                        }
                        for aid, ver, desc, is_inst, is_en in rows
                    ]
                },
                indent=2,
            )
            print(text)
            return text

        if not rows:
            text = f"No addons matching {query!r}."
            print(text)
            return text
        lines = [f"Matching Addons ({len(rows)}):"]
        for aid, ver, desc, is_inst, is_en in rows:
            state = "enabled" if is_en else ("installed" if is_inst else "available")
            lines.append(f"  • {aid} (v{ver}) [{state}] — {desc}")
        text = "\n".join(lines)
        print(text)
        return text

    if cmd in {"install", "add", "i"}:
        addon_spec = str(getattr(args, "addon", "")).strip()
        scope = cast("AddonInstallScope", getattr(args, "scope", "user") or "user")
        no_deps = bool(getattr(args, "no_deps", False))
        try:
            instance = install_addon(
                addon_spec,
                scope=scope,
                project_root=_resolve_project_root(scope),
                auto_install_deps=not no_deps,
            )
        except (AddonMarketplaceError, AddonNotFoundError) as exc:
            text = f"Failed to install addon {addon_spec}: {exc}"
            print(text)
            raise SystemExit(1) from exc

        if output_format == "json":
            text = json.dumps(
                {
                    "addon_id": instance.addon_id,
                    "name": instance.name,
                    "version": instance.version,
                    "marketplace": instance.marketplace,
                    "install_path": str(instance.root_dir),
                    "entry_point": instance.manifest.entry_point,
                    "enabled": instance.enabled,
                    "status": "installed",
                },
                indent=2,
            )
            print(text)
            return text

        text = (
            f"Installed and enabled addon '{instance.addon_id}' (v{instance.version}) "
            f"at {instance.root_dir}."
        )
        print(text)
        return text

    if cmd in {"uninstall", "remove", "rm"}:
        addon_spec = str(getattr(args, "addon", "")).strip()
        scope = cast("AddonInstallScope", getattr(args, "scope", "user") or "user")
        try:
            removed_id = uninstall_addon(
                addon_spec,
                scope=scope,
                project_root=_resolve_project_root(scope),
            )
        except (AddonMarketplaceError, AddonNotFoundError) as exc:
            text = f"Failed to uninstall addon {addon_spec}: {exc}"
            print(text)
            raise SystemExit(1) from exc

        if output_format == "json":
            text = json.dumps({"addon_id": removed_id, "status": "uninstalled"}, indent=2)
            print(text)
            return text

        text = f"Uninstalled addon '{removed_id}'."
        print(text)
        return text

    if cmd in {"enable", "disable"}:
        addon_spec = str(getattr(args, "addon", "")).strip()
        scope = cast("AddonInstallScope", getattr(args, "scope", "user") or "user")
        target_state = cmd == "enable"
        try:
            resolved_id = set_installed_addon_enabled(
                addon_spec,
                target_state,
                scope=scope,
                project_root=_resolve_project_root(scope),
            )
        except (AddonMarketplaceError, AddonNotFoundError) as exc:
            text = f"Failed to {cmd} addon {addon_spec}: {exc}"
            print(text)
            raise SystemExit(1) from exc

        if output_format == "json":
            text = json.dumps(
                {"addon_id": resolved_id, "enabled": target_state, "scope": scope},
                indent=2,
            )
            print(text)
            return text

        state_word = "Enabled" if target_state else "Disabled"
        text = f"{state_word} addon '{resolved_id}' (scope: {scope})."
        print(text)
        return text

    if cmd == "refresh":
        target_name = getattr(args, "name", None)
        if target_name:
            try:
                mp = refresh_addon_marketplace(str(target_name))
            except Exception as exc:
                text = f"Failed to refresh addon marketplace {target_name}: {exc}"
                print(text)
                raise SystemExit(1) from exc
            if output_format == "json":
                text = json.dumps(
                    {
                        "name": mp.name,
                        "addon_count": len(mp.addons),
                        "status": "refreshed",
                    },
                    indent=2,
                )
                print(text)
                return text
            text = f"Refreshed addon marketplace '{mp.name}' ({len(mp.addons)} addon(s) available)."
            print(text)
            return text

        refreshed = refresh_all_addon_marketplaces()
        if output_format == "json":
            text = json.dumps(
                {
                    "marketplaces": [
                        {"name": k, "addon_count": len(v.addons), "status": "refreshed"}
                        for k, v in refreshed.items()
                    ]
                },
                indent=2,
            )
            print(text)
            return text
        lines = [f"Refreshed {len(refreshed)} addon marketplace(s):"]
        for k, v in refreshed.items():
            lines.append(f"  • {k}: {len(v.addons)} addon(s) available")
        text = "\n".join(lines)
        print(text)
        return text

    if cmd in {"marketplace", "marketplaces", "mp"}:
        mp_cmd = getattr(args, "marketplace_command", None)
        if mp_cmd in {None, "list", "ls"}:
            records = load_addon_marketplace_records()
            if output_format == "json":
                text = json.dumps(
                    {
                        "marketplaces": [
                            {
                                "name": rec.name,
                                "source_type": rec.source_type,
                                "source": rec.source,
                                "install_location": rec.install_location,
                                "addon_count": rec.addon_count,
                                "is_default": rec.is_default,
                            }
                            for rec in records.values()
                        ]
                    },
                    indent=2,
                )
                print(text)
                return text
            if not records:
                text = "No addon marketplaces registered yet."
                print(text)
                return text
            lines = ["Registered Addon Marketplaces:"]
            for rec in records.values():
                lines.append(
                    f"  • {rec.name} ({rec.source_type}: {rec.source}) — {rec.addon_count} addon(s)"
                )
            text = "\n".join(lines)
            print(text)
            return text

        if mp_cmd == "add":
            source_spec = str(getattr(args, "source", "")).strip()
            try:
                mp = add_addon_marketplace_source(source_spec)
            except Exception as exc:
                text = f"Failed to add addon marketplace {source_spec}: {exc}"
                print(text)
                raise SystemExit(1) from exc

            if output_format == "json":
                text = json.dumps(
                    {
                        "name": mp.name,
                        "addon_count": len(mp.addons),
                        "addons": [a.name for a in mp.addons],
                        "status": "added",
                    },
                    indent=2,
                )
                print(text)
                return text
            text = f"Added addon marketplace '{mp.name}' ({len(mp.addons)} addon(s) available)."
            print(text)
            return text

        if mp_cmd in {"remove", "rm", "delete"}:
            name = str(getattr(args, "name", "")).strip()
            try:
                remove_addon_marketplace(name)
            except Exception as exc:
                text = f"Failed to remove addon marketplace {name}: {exc}"
                print(text)
                raise SystemExit(1) from exc
            if output_format == "json":
                text = json.dumps({"name": name, "status": "removed"}, indent=2)
                print(text)
                return text
            text = f"Removed addon marketplace '{name}'."
            print(text)
            return text

    text = "Usage: talkops addon {list,search,install,uninstall,enable,disable,refresh,marketplace}"
    print(text)
    return text

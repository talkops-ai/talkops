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

"""Adapter from discovered plugins to TalkOps subagent metadata."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from talkops.plugins.models import PluginInstance
    from talkops.subagents.types import SubagentMetadata

from talkops.utils.logger import get_logger

logger = get_logger(__name__)


def _extract_plugin_mcp_server_names(meta: SubagentMetadata, plugin: PluginInstance) -> list[str]:
    """Extract plain-text MCP server names scoped to a plugin subagent without opening connections."""
    import json

    names: list[str] = []

    def _collect_from_mapping(raw: Mapping[str, Any]) -> None:
        inner = raw.get("mcpServers") if isinstance(raw.get("mcpServers"), Mapping) else raw
        for srv_name, srv_cfg in inner.items():
            if not isinstance(srv_name, str) or not srv_name.strip() or srv_name.startswith("$"):
                continue
            if isinstance(srv_cfg, Mapping) and srv_cfg.get("enabled") is False:
                continue
            if srv_name not in names:
                names.append(srv_name)

    mcp_cfg = meta.get("mcp_config")
    if isinstance(mcp_cfg, Mapping):
        _collect_from_mapping(mcp_cfg)

    mcp_files = meta.get("mcp_files") or [str(p) for p in plugin.inventory.mcp_files]
    for fp in mcp_files:
        try:
            path_obj = Path(fp)
            if path_obj.is_file():
                parsed = json.loads(path_obj.read_text(encoding="utf-8"))
                if isinstance(parsed, Mapping):
                    _collect_from_mapping(parsed)
        except Exception:
            continue

    if not names and plugin.manifest and isinstance(plugin.manifest.inline_mcp, Mapping):
        _collect_from_mapping(plugin.manifest.inline_mcp)

    return names


def _enrich_plugin_subagent(meta: SubagentMetadata, plugin: PluginInstance) -> SubagentMetadata:
    p_id = plugin.plugin_id
    raw_name = meta.get("name", "")

    if not raw_name or raw_name == plugin.name or p_id.startswith(f"{raw_name}@"):
        meta["name"] = p_id
    elif not raw_name.startswith(f"{p_id}:"):
        meta["name"] = f"{p_id}:{raw_name}"

    meta["source"] = f"plugin:{p_id}"
    meta["is_plugin"] = True
    meta["plugin_id"] = p_id
    meta["bundle_dir"] = str(plugin.root)

    if not meta.get("keywords") and plugin.manifest and plugin.manifest.keywords:
        meta["keywords"] = list(plugin.manifest.keywords)

    skills_dir = plugin.root / "skills"
    if skills_dir.is_dir():
        meta["skills_dir"] = str(skills_dir)
    elif plugin.inventory.skills:
        first_skill = plugin.inventory.skills[0]
        if first_skill.is_dir():
            meta["skills_dir"] = str(first_skill)
        elif first_skill.is_file():
            meta["skills_dir"] = str(first_skill.parent)

    # Attach default plugin skill wildcard if no skills explicitly set
    if not meta.get("skills"):
        meta["skills"] = [f"{p_id}:*"]

    # Attach plugin MCP files or inline config if subagent doesn't specify own
    if "mcp_config" not in meta and "mcp_files" not in meta:
        if plugin.inventory.mcp_files:
            meta["mcp_files"] = [str(p) for p in plugin.inventory.mcp_files]
        elif plugin.manifest and plugin.manifest.inline_mcp:
            meta["mcp_config"] = {"mcpServers": plugin.manifest.inline_mcp}

    if not meta.get("mcp_servers"):
        extracted_servers = _extract_plugin_mcp_server_names(meta, plugin)
        if extracted_servers:
            meta["mcp_servers"] = extracted_servers

    return meta


def get_subagent_bundle_dir(subagent_meta: SubagentMetadata | Mapping[str, Any]) -> Path | None:
    """Return the plugin or bundle root directory for a subagent."""
    if "bundle_dir" in subagent_meta and subagent_meta["bundle_dir"]:
        return Path(str(subagent_meta["bundle_dir"]))
    path = subagent_meta.get("path")
    if not path:
        return None
    p = Path(path)
    return p.parent.parent if p.parent.name == "agents" else p.parent


def get_subagent_skills_source(subagent_meta: SubagentMetadata | Mapping[str, Any]) -> tuple[str, str] | None:
    """Return the skill source tuple for a subagent's bundled skills, if any."""
    subagent_name = subagent_meta.get("name", "subagent")
    skills_dir_str = subagent_meta.get("skills_dir")
    if skills_dir_str and Path(skills_dir_str).is_dir():
        return (skills_dir_str, f"Subagent ({subagent_name})")
    bundle_dir = get_subagent_bundle_dir(subagent_meta)
    if bundle_dir and (bundle_dir / "skills").is_dir():
        return (str(bundle_dir / "skills"), f"Subagent ({subagent_name})")
    return None


def plugin_subagents(plugins: tuple[PluginInstance, ...] | list[PluginInstance]) -> list[SubagentMetadata]:
    """Discover and parse subagents from enabled plugins."""
    from talkops.subagents.loader import parse_subagent_file

    subagents: list[SubagentMetadata] = []

    for plugin in plugins:
        for agent_path in plugin.inventory.agents:
            if not agent_path.exists():
                continue
            if agent_path.is_dir():
                # Scan for .md files or AGENTS.md in subdirectory
                for entry in agent_path.iterdir():
                    if entry.is_file() and entry.suffix == ".md":
                        meta = parse_subagent_file(entry, fallback_name=entry.stem)
                        if meta:
                            subagents.append(_enrich_plugin_subagent(meta, plugin))
                    elif entry.is_dir():
                        sub_agents_file = entry / "AGENTS.md"
                        if not sub_agents_file.is_file():
                            named_md = entry / f"{entry.name}.md"
                            if named_md.is_file():
                                sub_agents_file = named_md
                            else:
                                md_files = sorted(entry.glob("*.md"))
                                if len(md_files) == 1:
                                    sub_agents_file = md_files[0]
                        if sub_agents_file.is_file():
                            meta = parse_subagent_file(sub_agents_file, fallback_name=entry.name)
                            if meta:
                                subagents.append(_enrich_plugin_subagent(meta, plugin))
            elif agent_path.is_file() and agent_path.suffix == ".md":
                meta = parse_subagent_file(agent_path, fallback_name=agent_path.stem)
                if meta:
                    subagents.append(_enrich_plugin_subagent(meta, plugin))

    return subagents


def discover_plugin_subagents(project_root: Path | None = None, store: Any = None) -> list[SubagentMetadata]:
    """Discover subagent definitions across all enabled marketplace and project plugins."""
    try:
        from talkops.plugins.discovery import discover_plugins

        plugins = discover_plugins(project_root=project_root, store=store).plugins
        return plugin_subagents(plugins)
    except Exception as exc:
        logger.warning("Could not discover plugin subagents: %s", exc)
        return []

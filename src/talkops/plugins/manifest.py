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

"""Plugin manifest parsing and component inventory building for TalkOps."""

from __future__ import annotations

import json
import re
from pathlib import Path, PureWindowsPath
from typing import Any

from talkops.plugins.models import (
    ComponentInventory,
    JsonObject,
    PluginManifest,
    PluginPoliciesSpec,
    PluginRubricsSpec,
    PluginWorkflowsSpec,
    UnsupportedComponent,
    normalize_execution_profile,
)
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

_MANIFEST_RELATIVE_PATHS = (
    Path(".claude-plugin") / "plugin.json",
    Path(".talkops-plugin") / "plugin.json",
    Path(".agents-plugin") / "plugin.json",
    Path(".codex-plugin") / "plugin.json",
    Path("plugin.json"),
)
_PATH_COMPONENT_FIELDS = {"skills", "mcpServers", "agents", "commands", "hooks"}
_UNSUPPORTED_COMPONENT_DIRS: tuple[UnsupportedComponent, ...] = ()
_NAME_RE = re.compile(r"^[^\s]+$")


class PluginManifestError(ValueError):
    """Raised when a plugin manifest is malformed enough to skip the plugin."""


def find_manifest_path(root: Path) -> Path | None:
    """Return the first supported manifest path under `root`, if present."""
    for rel in _MANIFEST_RELATIVE_PATHS:
        path = root / rel
        try:
            if path.is_file():
                return path
        except OSError:
            logger.warning("Could not inspect plugin manifest path %s", path)
    return None


def _validate_name(name: object, *, fallback: str | None = None, allow_at: bool = True) -> str:
    if isinstance(name, str) and name and _NAME_RE.fullmatch(name) and (allow_at or "@" not in name):
        return name
    if fallback and _NAME_RE.fullmatch(fallback) and (allow_at or "@" not in fallback):
        return fallback
    msg = f"Invalid plugin name: {name!r}"
    raise PluginManifestError(msg)


def _is_windows_absolute(path: str) -> bool:
    return bool(PureWindowsPath(path).drive or PureWindowsPath(path).root)


def _resolve_component_path(
    declaration: str,
    plugin_root: Path,
    field_name: str,
    warnings: list[str],
) -> Path | None:
    if not declaration.startswith("./"):
        warnings.append(f"ignoring {field_name}: path must start with './' relative to plugin root")
        return None
    relative = declaration[2:]
    if not relative:
        warnings.append(f"ignoring {field_name}: path must not be './'")
        return None
    path = Path(relative)
    if any(part == ".." for part in path.parts):
        warnings.append(f"ignoring {field_name}: path must not contain '..'")
        return None
    if path.is_absolute() or _is_windows_absolute(relative):
        warnings.append(f"ignoring {field_name}: path must stay within the plugin root")
        return None
    try:
        root_resolved = plugin_root.resolve()
        resolved = (plugin_root / path).resolve()
    except OSError as exc:
        warnings.append(f"ignoring {field_name}: could not resolve {declaration!r}: {exc}")
        return None
    if not resolved.is_relative_to(root_resolved):
        warnings.append(f"ignoring {field_name}: path escapes plugin root")
        return None
    return resolved


def _resolve_component_paths(
    declaration: object,
    plugin_root: Path,
    field_name: str,
    warnings: list[str],
) -> tuple[Path, ...]:
    raw_paths: list[str]
    if isinstance(declaration, str):
        raw_paths = [declaration]
    elif isinstance(declaration, list):
        raw_paths = [item for item in declaration if isinstance(item, str)]
        warnings.extend(
            f"ignoring {field_name}: expected path string, got {type(item).__name__}"
            for item in declaration
            if not isinstance(item, str)
        )
    else:
        warnings.append(f"ignoring {field_name}: expected path string or list of strings")
        return ()
    paths: list[Path] = []
    for raw_path in raw_paths:
        resolved = _resolve_component_path(raw_path, plugin_root, field_name, warnings)
        if resolved is not None:
            paths.append(resolved)
    return tuple(paths)


def _inline_mcp(value: object) -> JsonObject:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        merged: JsonObject = {}
        for item in value:
            if isinstance(item, dict):
                merged.update(item)
        return merged
    return {}


def load_manifest(
    root: Path, *, fallback_name: str | None = None
) -> tuple[PluginManifest | None, Path | None, tuple[str, ...]]:
    """Load a plugin manifest from known locations."""
    manifest_path = find_manifest_path(root)
    if manifest_path is None:
        return None, None, ()
    try:
        decoded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        msg = f"Invalid JSON syntax in {manifest_path}: {exc}"
        raise PluginManifestError(msg) from exc
    except OSError as exc:
        msg = f"Could not read plugin manifest {manifest_path}: {exc}"
        raise PluginManifestError(msg) from exc
    if not isinstance(decoded, dict):
        msg = f"Plugin manifest {manifest_path} must be a JSON object"
        raise PluginManifestError(msg)
    raw = decoded

    warnings: list[str] = []
    name = _validate_name(raw.get("name"), fallback=fallback_name)
    component_paths: dict[str, tuple[Path, ...]] = {}
    for field_name in _PATH_COMPONENT_FIELDS:
        declaration = raw.get(field_name)
        if declaration is None:
            continue
        if field_name in {"mcpServers", "hooks"} and isinstance(declaration, dict):
            continue
        paths = _resolve_component_paths(declaration, root, field_name, warnings)
        if paths:
            component_paths[field_name] = paths

    version_value = raw.get("version")
    version = version_value if isinstance(version_value, str) else "1.0.0"
    raw_schema_version = raw.get("schemaVersion") or raw.get("schema_version")
    schema_version = str(raw_schema_version).strip() if isinstance(raw_schema_version, (str, int, float)) else "1.0"
    display_name_value = raw.get("displayName") or raw.get("display_name")
    description_value = raw.get("description")
    author_value = raw.get("author")

    raw_envs = raw.get("environments")
    environments: tuple[str, ...] = ()
    if isinstance(raw_envs, list):
        environments = tuple(str(item).strip() for item in raw_envs if isinstance(item, str) and item.strip())

    # Resolve optional MCP schema lockfile (Plugin Manifest v2.1 — OWASP MCP03)
    mcp_lockfile: Path | None = None
    raw_lockfile = raw.get("mcpLockfile") or raw.get("mcp_lockfile")
    if isinstance(raw_lockfile, str):
        mcp_lockfile = _resolve_component_path(raw_lockfile, root, "mcpLockfile", warnings)
    elif raw_lockfile is not None:
        warnings.append("ignoring mcpLockfile: expected './' relative path string")
    elif (root / "mcp.lock.json").is_file():
        mcp_lockfile = (root / "mcp.lock.json").resolve()

    # Resolve typed workflows (preflight, postRemediation, scheduled)
    workflows_spec = PluginWorkflowsSpec()
    raw_workflows = raw.get("workflows")
    if isinstance(raw_workflows, dict):
        wf_pre = raw_workflows.get("preflight")
        wf_post = raw_workflows.get("postRemediation") or raw_workflows.get("post_remediation")
        wf_sched = raw_workflows.get("scheduled")
        workflows_spec = PluginWorkflowsSpec(
            preflight=_resolve_component_path(wf_pre, root, "workflows.preflight", warnings)
            if isinstance(wf_pre, str)
            else None,
            post_remediation=_resolve_component_path(wf_post, root, "workflows.postRemediation", warnings)
            if isinstance(wf_post, str)
            else None,
            scheduled=_resolve_component_path(wf_sched, root, "workflows.scheduled", warnings)
            if isinstance(wf_sched, str)
            else None,
        )
    elif raw_workflows is not None:
        warnings.append("ignoring workflows: expected JSON object with preflight/postRemediation/scheduled")

    # Resolve two-stage AutoQA rubrics (deterministicGates, scorecard)
    rubrics_spec = PluginRubricsSpec()
    raw_rubrics = raw.get("rubrics")
    if isinstance(raw_rubrics, dict):
        rub_gates = (
            raw_rubrics.get("deterministicGates")
            or raw_rubrics.get("deterministic_gates")
            or raw_rubrics.get("gates")
        )
        rub_card = raw_rubrics.get("scorecard")
        rubrics_spec = PluginRubricsSpec(
            deterministic_gates=_resolve_component_path(rub_gates, root, "rubrics.deterministicGates", warnings)
            if isinstance(rub_gates, str)
            else None,
            scorecard=_resolve_component_path(rub_card, root, "rubrics.scorecard", warnings)
            if isinstance(rub_card, str)
            else None,
        )
    elif raw_rubrics is not None:
        warnings.append("ignoring rubrics: expected JSON object with deterministicGates/scorecard")

    # Resolve graduated execution trust policies & scope boundaries
    policies_spec = PluginPoliciesSpec()
    raw_policies = raw.get("policies")
    if isinstance(raw_policies, dict):
        raw_prof = (
            raw_policies.get("defaultExecutionProfile")
            or raw_policies.get("default_execution_profile")
            or raw_policies.get("executionProfile")
            or raw_policies.get("execution_profile")
        )
        prof = normalize_execution_profile(raw_prof if isinstance(raw_prof, str) else None)
        meta_only_raw = raw_policies.get(
            "metadataOnlyDatastores",
            raw_policies.get("metadata_only_datastores", prof == "readonly-metadata"),
        )
        raw_egress = raw_policies.get("egressAllowlist") or raw_policies.get("egress_allowlist")
        egress_tuple: tuple[str, ...] = ()
        if isinstance(raw_egress, list):
            egress_tuple = tuple(str(h).strip() for h in raw_egress if isinstance(h, str) and h.strip())
        pol_scope = raw_policies.get("scopeGuards") or raw_policies.get("scope_guards")
        pol_check = raw_policies.get("companionChecklists") or raw_policies.get("companion_checklists")
        pol_esc = raw_policies.get("escalationRules") or raw_policies.get("escalation_rules")
        policies_spec = PluginPoliciesSpec(
            default_execution_profile=prof,
            metadata_only_datastores=bool(meta_only_raw) or prof == "readonly-metadata",
            egress_allowlist=egress_tuple,
            scope_guards=_resolve_component_path(pol_scope, root, "policies.scopeGuards", warnings)
            if isinstance(pol_scope, str)
            else None,
            companion_checklists=_resolve_component_path(pol_check, root, "policies.companionChecklists", warnings)
            if isinstance(pol_check, str)
            else None,
            escalation_rules=_resolve_component_path(pol_esc, root, "policies.escalationRules", warnings)
            if isinstance(pol_esc, str)
            else None,
        )
    elif raw_policies is not None:
        warnings.append("ignoring policies: expected JSON object")

    # Resolve synthetic & adversarial simulation suites
    simulations_paths: tuple[Path, ...] = ()
    raw_sims = raw.get("simulations")
    if raw_sims is not None:
        simulations_paths = _resolve_component_paths(raw_sims, root, "simulations", warnings)

    skills_dir = root / "skills" if (root / "skills").is_dir() else None
    agents_dir = root / "agents" if (root / "agents").is_dir() else None
    disabled = bool(raw.get("disabled", False)) if isinstance(raw.get("disabled"), bool) else False
    raw_hooks_cfg = raw.get("hooks") if isinstance(raw.get("hooks"), dict) else {}
    raw_keywords = raw.get("keywords")
    keywords = (
        tuple(str(k).strip() for k in raw_keywords if isinstance(k, str) and k.strip())
        if isinstance(raw_keywords, (list, tuple))
        else ()
    )

    manifest = PluginManifest(
        name=name,
        version=version,
        schema_version=schema_version,
        disabled=disabled,
        keywords=keywords,
        environments=environments,
        component_paths=component_paths,
        inline_mcp=_inline_mcp(raw.get("mcpServers")),
        hooks_config=dict(raw_hooks_cfg),
        mcp_lockfile=mcp_lockfile,
        workflows=workflows_spec,
        rubrics=rubrics_spec,
        policies=policies_spec,
        simulations_paths=simulations_paths,
        display_name=(display_name_value if isinstance(display_name_value, str) else None),
        description=(description_value if isinstance(description_value, str) else None),
        author=author_value,
        skills_dir=skills_dir,
        agents_dir=agents_dir,
    )
    return manifest, manifest_path, tuple(warnings)


def _existing_component_path(path: Path, plugin_root: Path) -> tuple[Path, ...]:
    try:
        if not path.exists():
            return ()
        resolved = path.resolve()
        if not resolved.is_relative_to(plugin_root.resolve()):
            logger.warning("Ignoring plugin component outside plugin root: %s", path)
            return ()
    except OSError:
        logger.warning("Could not inspect plugin component path %s", path)
        return ()
    else:
        return (resolved,)


def _unsupported_component_dirs(
    plugin_root: Path,
) -> tuple[UnsupportedComponent, ...]:
    found: list[UnsupportedComponent] = []
    for name in _UNSUPPORTED_COMPONENT_DIRS:
        path = plugin_root / name
        try:
            if path.is_dir():
                found.append(name)
        except OSError:
            logger.warning("Could not inspect plugin component path %s", path)
    return tuple(found)


def build_inventory(
    plugin_root: Path,
    manifest: PluginManifest | None,
    manifest_warnings: tuple[str, ...] = (),
) -> ComponentInventory:
    """Build component inventory for a plugin."""
    plugin_root = plugin_root.resolve()
    warnings = list(manifest_warnings)
    metadata_paths = manifest.component_paths if manifest else {}

    default_skills = _existing_component_path(plugin_root / "skills", plugin_root)
    root_skill = (
        ()
        if default_skills or (manifest and "skills" in manifest.component_paths)
        else _existing_component_path(plugin_root / "SKILL.md", plugin_root)
    )
    skills = (*default_skills, *metadata_paths.get("skills", ()), *root_skill)

    default_agents = _existing_component_path(plugin_root / "agents", plugin_root)
    agents = (*default_agents, *metadata_paths.get("agents", ()))

    default_commands = _existing_component_path(plugin_root / "commands", plugin_root)
    commands = (*default_commands, *metadata_paths.get("commands", ()))

    mcp_files = (
        *_existing_component_path(plugin_root / ".mcp.json", plugin_root),
        *_existing_component_path(plugin_root / "mcp.json", plugin_root),
        *metadata_paths.get("mcpServers", ()),
    )

    declared_hooks: list[Path] = []
    for hp in metadata_paths.get("hooks", ()):
        if hp.is_dir():
            declared_hooks.extend(_existing_component_path(hp / "hooks.json", plugin_root))
        elif hp.is_file():
            declared_hooks.extend(_existing_component_path(hp, plugin_root))
    hooks_files = (
        *_existing_component_path(plugin_root / "hooks" / "hooks.json", plugin_root),
        *_existing_component_path(plugin_root / "hooks.json", plugin_root),
        *declared_hooks,
    )

    default_sims = _existing_component_path(plugin_root / "simulations", plugin_root)
    declared_sims: list[Path] = []
    if manifest and manifest.simulations_paths:
        for sim_path in manifest.simulations_paths:
            declared_sims.extend(_existing_component_path(sim_path, plugin_root))
    simulations = tuple(declared_sims) if declared_sims else default_sims

    workflows_map: dict[str, Path] = {}
    if manifest:
        if manifest.workflows.preflight and manifest.workflows.preflight.exists():
            workflows_map["preflight"] = manifest.workflows.preflight.resolve()
        if manifest.workflows.post_remediation and manifest.workflows.post_remediation.exists():
            workflows_map["postRemediation"] = manifest.workflows.post_remediation.resolve()
        if manifest.workflows.scheduled and manifest.workflows.scheduled.exists():
            workflows_map["scheduled"] = manifest.workflows.scheduled.resolve()

    rubrics_map: dict[str, Path] = {}
    if manifest:
        if manifest.rubrics.deterministic_gates and manifest.rubrics.deterministic_gates.exists():
            rubrics_map["deterministicGates"] = manifest.rubrics.deterministic_gates.resolve()
        if manifest.rubrics.scorecard and manifest.rubrics.scorecard.exists():
            rubrics_map["scorecard"] = manifest.rubrics.scorecard.resolve()

    lockfile_resolved: Path | None = None
    if manifest and manifest.mcp_lockfile and manifest.mcp_lockfile.is_file():
        lockfile_resolved = manifest.mcp_lockfile.resolve()
    elif (plugin_root / "mcp.lock.json").is_file():
        lockfile_resolved = (plugin_root / "mcp.lock.json").resolve()

    unsupported = _unsupported_component_dirs(plugin_root)

    return ComponentInventory(
        skills=tuple(dict.fromkeys(skills)),
        mcp_files=tuple(dict.fromkeys(mcp_files)),
        agents=tuple(dict.fromkeys(agents)),
        commands=tuple(dict.fromkeys(commands)),
        hooks_files=tuple(dict.fromkeys(hooks_files)),
        simulations=tuple(dict.fromkeys(simulations)),
        workflows=workflows_map,
        rubrics=rubrics_map,
        mcp_lockfile=lockfile_resolved,
        unsupported=unsupported,
        warnings=tuple(warnings),
    )


def inspect_plugin_components(
    plugin_root: Path,
    manifest: PluginManifest | None = None,
) -> dict[str, Any]:
    """Inspect a plugin filesystem tree and return exact component names and metadata."""
    plugin_root = plugin_root.resolve()

    if manifest is None:
        try:
            manifest, _, _ = load_manifest(plugin_root, fallback_name=plugin_root.name)
        except Exception:
            manifest = None

    # 1. Skills
    skill_names: list[str] = []
    skills_dir = plugin_root / "skills"
    if skills_dir.is_dir():
        for item in sorted(skills_dir.iterdir()):
            if item.is_dir() and (item / "SKILL.md").is_file():
                skill_names.append(item.name)
            elif item.is_file() and item.suffix == ".md" and item.name != "SKILL.md":
                skill_names.append(item.stem)
        if not skill_names and (skills_dir / "SKILL.md").is_file():
            skill_names.append(plugin_root.name)
    elif (plugin_root / "SKILL.md").is_file():
        skill_names.append(plugin_root.name)

    # 2. MCP Servers
    mcp_server_names: list[str] = []
    for mcp_candidate in (plugin_root / ".mcp.json", plugin_root / "mcp.json"):
        if mcp_candidate.is_file():
            try:
                data = json.loads(mcp_candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    servers = data.get("mcpServers")
                    if not isinstance(servers, dict):
                        servers = data.get("mcp_servers")
                    if isinstance(servers, dict):
                        for k in servers:
                            if k not in mcp_server_names:
                                mcp_server_names.append(k)
                    elif "mcpServers" not in data and "mcp_servers" not in data:
                        for k, v in data.items():
                            if not str(k).startswith("$") and isinstance(v, dict) and k not in mcp_server_names:
                                mcp_server_names.append(k)
            except Exception:
                pass

    if manifest and manifest.inline_mcp:
        for k in manifest.inline_mcp:
            if k not in mcp_server_names:
                mcp_server_names.append(k)

    # 3. Commands & Agents
    command_names: list[str] = []
    commands_dir = plugin_root / "commands"
    if commands_dir.is_dir():
        for item in sorted(commands_dir.glob("*.md")):
            command_names.append(item.stem)

    agent_names: list[str] = []
    agents_dir = plugin_root / "agents"
    if agents_dir.is_dir():
        for item in sorted(agents_dir.iterdir()):
            if item.is_file() and item.suffix == ".md":
                agent_names.append(item.stem)
            elif item.is_dir() and (
                (item / "AGENTS.md").is_file()
                or (item / f"{item.name}.md").is_file()
                or any(item.glob("*.md"))
            ):
                agent_names.append(item.name)
    if manifest and manifest.component_paths.get("agents"):
        for agent_path in manifest.component_paths["agents"]:
            if agent_path.is_file() and agent_path.suffix == ".md":
                stem = (
                    agent_path.parent.name
                    if agent_path.name.upper() == "AGENTS.MD"
                    else agent_path.stem
                )
                if stem not in agent_names:
                    agent_names.append(stem)

    # 4. Author string normalization
    raw_author = manifest.author if manifest else None
    author_str = None
    if isinstance(raw_author, dict):
        author_str = raw_author.get("name")
    elif isinstance(raw_author, str):
        if raw_author.strip().startswith("{"):
            try:
                parsed = json.loads(raw_author)
                author_str = parsed.get("name") if isinstance(parsed, dict) else raw_author
            except Exception:
                author_str = raw_author
        else:
            author_str = raw_author

    return {
        "display_name": manifest.display_name if manifest and manifest.display_name else None,
        "description": manifest.description if manifest and manifest.description else None,
        "version": manifest.version if manifest and manifest.version else "1.0.0",
        "schema_version": manifest.schema_version if manifest else "1.0",
        "environments": list(manifest.environments) if manifest else [],
        "execution_profile": manifest.policies.default_execution_profile if manifest else "hitl-gated",
        "has_mcp_lockfile": bool(
            (manifest and manifest.mcp_lockfile and manifest.mcp_lockfile.is_file())
            or (plugin_root / "mcp.lock.json").is_file()
        ),
        "author": author_str,
        "skill_names": skill_names,
        "skill_count": len(skill_names),
        "mcp_server_names": mcp_server_names,
        "command_names": command_names,
        "agent_names": agent_names,
    }

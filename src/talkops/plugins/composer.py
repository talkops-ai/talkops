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

"""Deterministic multi-plugin policy, workflow, and rubric composition (Manifest v2.1).

Follows the Context Center resolution principle:
- Execution trust profiles compose via most-restrictive intersection
  (``readonly-metadata`` > ``gitops-pr`` > ``hitl-gated`` > ``autonomous-tnr``).
- ``metadata_only_datastores`` is enforced if ANY active plugin or server rule requires it.
- ``egress_allowlist`` composes via intersection across plugins that declare an explicit allowlist.
- Pre-flight workflows run concurrently across all matching plugins within a shared 400ms ceiling.
- AutoQA deterministic gates union across all active plugins, paired with the primary plugin's scorecard.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from talkops.plugins.models import (
    EXECUTION_PROFILE_RESTRICTIVENESS,
    ExecutionTrustProfile,
    PluginInstance,
    normalize_execution_profile,
)
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class ComposedPluginBundle:
    """Resolved execution contract across one or more active v1/v2.1 plugins."""

    plugin_ids: tuple[str, ...] = ()
    effective_execution_profile: ExecutionTrustProfile = "hitl-gated"
    metadata_only_datastores: bool = False
    egress_allowlist: tuple[str, ...] | None = None
    preflight_workflow_files: tuple[Path, ...] = ()
    post_remediation_workflow_files: tuple[Path, ...] = ()
    scheduled_workflow_files: tuple[Path, ...] = ()
    deterministic_gate_files: tuple[Path, ...] = ()
    primary_scorecard_file: Path | None = None
    companion_checklist_files: tuple[Path, ...] = ()
    scope_guard_rules: tuple[dict[str, Any], ...] = ()
    escalation_rules: tuple[dict[str, Any], ...] = ()
    mcp_lockfiles: dict[str, Path] = field(default_factory=dict)

    @property
    def execution_profile(self) -> ExecutionTrustProfile:
        """Convenience alias for ``effective_execution_profile``."""
        return self.effective_execution_profile


def _load_json_list_or_dict(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            return [item for item in loaded if isinstance(item, dict)]
        if isinstance(loaded, dict):
            rules = loaded.get("rules")
            if isinstance(rules, list):
                return [item for item in rules if isinstance(item, dict)]
            return [loaded]
    except Exception as exc:
        logger.warning("Failed to parse plugin policy JSON %s: %s", path, exc)
    return []


def _intersect_host_patterns(
    existing: set[str],
    incoming: Sequence[str],
) -> set[str]:
    """Intersect two wildcard hostname allowlists conservatively."""
    kept: set[str] = set()
    for pat_a in existing:
        for pat_b in incoming:
            if pat_a == pat_b or fnmatch.fnmatch(pat_a, pat_b):
                kept.add(pat_a)
            elif fnmatch.fnmatch(pat_b, pat_a):
                kept.add(pat_b)
    return kept


def compose_active_plugins(
    plugins: Sequence[PluginInstance],
    *,
    primary_plugin_name: str | None = None,
    server_profile_ceiling: str | None = None,
) -> ComposedPluginBundle:
    """Compose policies, workflows, rubrics, and lockfiles across active plugins."""
    enabled_plugins = [p for p in plugins if p.enabled]
    if not enabled_plugins and not server_profile_ceiling:
        return ComposedPluginBundle()

    # Start with least restrictive if plugins exist so the most restrictive active plugin wins;
    # if no plugins specify policies, fall back to hitl-gated.
    chosen_profile: ExecutionTrustProfile = "hitl-gated"
    saw_explicit_profile = False

    if server_profile_ceiling:
        chosen_profile = normalize_execution_profile(server_profile_ceiling)
        saw_explicit_profile = True

    metadata_only = chosen_profile == "readonly-metadata"
    egress_acc: set[str] | None = None

    preflight_files: list[Path] = []
    post_rem_files: list[Path] = []
    scheduled_files: list[Path] = []
    gate_files: list[Path] = []
    checklist_files: list[Path] = []
    scope_rules: list[dict[str, Any]] = []
    esc_rules: list[dict[str, Any]] = []
    lockfiles: dict[str, Path] = {}
    primary_scorecard: Path | None = None
    fallback_scorecard: Path | None = None

    for plugin in enabled_plugins:
        manifest = plugin.manifest
        plugin_key = plugin.plugin_id or plugin.name
        if plugin.inventory.mcp_lockfile and plugin.inventory.mcp_lockfile.is_file():
            lockfiles[plugin_key] = plugin.inventory.mcp_lockfile
        elif manifest and manifest.mcp_lockfile and manifest.mcp_lockfile.is_file():
            lockfiles[plugin_key] = manifest.mcp_lockfile

        if manifest is None:
            continue

        prof = manifest.policies.default_execution_profile
        if not saw_explicit_profile:
            chosen_profile = prof
            saw_explicit_profile = True
        elif EXECUTION_PROFILE_RESTRICTIVENESS[prof] > EXECUTION_PROFILE_RESTRICTIVENESS[chosen_profile]:
            chosen_profile = prof

        if manifest.policies.metadata_only_datastores or prof == "readonly-metadata":
            metadata_only = True

        if manifest.policies.egress_allowlist:
            if egress_acc is None:
                egress_acc = set(manifest.policies.egress_allowlist)
            else:
                egress_acc = _intersect_host_patterns(egress_acc, manifest.policies.egress_allowlist)

        if manifest.workflows.preflight and manifest.workflows.preflight.is_file():
            preflight_files.append(manifest.workflows.preflight)
        if manifest.workflows.post_remediation and manifest.workflows.post_remediation.is_file():
            post_rem_files.append(manifest.workflows.post_remediation)
        if manifest.workflows.scheduled and manifest.workflows.scheduled.is_file():
            scheduled_files.append(manifest.workflows.scheduled)

        if manifest.rubrics.deterministic_gates and manifest.rubrics.deterministic_gates.is_file():
            gate_files.append(manifest.rubrics.deterministic_gates)
        if manifest.rubrics.scorecard and manifest.rubrics.scorecard.is_file():
            if fallback_scorecard is None:
                fallback_scorecard = manifest.rubrics.scorecard
            if primary_plugin_name and primary_plugin_name.lower() in (
                plugin.name.lower(),
                plugin.plugin_id.lower(),
            ):
                primary_scorecard = manifest.rubrics.scorecard

        if manifest.policies.companion_checklists and manifest.policies.companion_checklists.is_file():
            checklist_files.append(manifest.policies.companion_checklists)

        scope_rules.extend(_load_json_list_or_dict(manifest.policies.scope_guards))
        esc_rules.extend(_load_json_list_or_dict(manifest.policies.escalation_rules))

    if chosen_profile == "readonly-metadata":
        metadata_only = True

    return ComposedPluginBundle(
        plugin_ids=tuple(p.plugin_id or p.name for p in enabled_plugins),
        effective_execution_profile=chosen_profile,
        metadata_only_datastores=metadata_only,
        egress_allowlist=tuple(sorted(egress_acc)) if egress_acc is not None else None,
        preflight_workflow_files=tuple(dict.fromkeys(preflight_files)),
        post_remediation_workflow_files=tuple(dict.fromkeys(post_rem_files)),
        scheduled_workflow_files=tuple(dict.fromkeys(scheduled_files)),
        deterministic_gate_files=tuple(dict.fromkeys(gate_files)),
        primary_scorecard_file=primary_scorecard or fallback_scorecard,
        companion_checklist_files=tuple(dict.fromkeys(checklist_files)),
        scope_guard_rules=tuple(scope_rules),
        escalation_rules=tuple(esc_rules),
        mcp_lockfiles=lockfiles,
    )


__all__ = [
    "ComposedPluginBundle",
    "compose_active_plugins",
]

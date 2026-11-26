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

"""``ContextAuthorityMiddleware`` — 5-Tier Context Center Precedence for TalkOps v2.

Enforces deterministic precedence so stale historical incidents or generic notes
never override live telemetry or active policy boundaries:
1. Tier 1 (Authoritative Ceiling): Execution Trust Profile & Scope Guards
2. Tier 2 (Live Ground Truth): Pre-Flight Tri-State Checklist Findings & Drained Storm Alerts
3. Tier 3 (Domain SOP): Active Plugin Manifest v2.1 Runbooks & Policies
4. Tier 4 (Structural Lineage): ``ContextGraph`` Blast Radius & Recent CI/CD/GitOps Changes
5. Tier 5 (Advisory Prior): Calibrated RRF Historical Incidents & TTL Memory Rules
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain_core.messages import SystemMessage

from talkops.context_graph.graph import ContextGraph
from talkops.memory.incident_store import CalibratedIncidentStore
from talkops.middleware.registry import register_middleware
from talkops.security.execution_profiles import resolve_runtime_execution_profile
from talkops.storage.factory import get_operational_store
from talkops.storage.protocol import OperationalStore


def render_context_authority_block(bundle: Mapping[str, Any]) -> str:
    """Render the 5-tier ``<context_authority>`` prompt block."""
    profile = bundle.get("tier1_execution_profile") or "hitl-gated"
    preflight = bundle.get("tier2_preflight") or {}
    blast = bundle.get("tier4_blast_radius") or {}
    prior_incidents = bundle.get("tier5_calibrated_incidents") or []
    memory_rules = bundle.get("tier5_memory_rules") or []

    lines = [
        "<context_authority>",
        "STRICT PRECEDENCE RULE: Higher tiers ALWAYS override lower tiers on any conflict.",
        "- [Tier 1 · Policy Ceiling] Execution Trust Profile: " + str(profile),
        "- [Live Environment Precedence] Authenticated targets in `## Local Context` (active cloud profile, account, region, cluster) ALWAYS override static defaults in `<agent_memory>`.",
    ]
    if preflight:
        counts = preflight.get("counts") or {}
        lines.append(
            f"- [Tier 2 · Live Pre-Flight Telemetry] Findings={counts.get('Finding', 0)}, "
            f"Normal={counts.get('Normal', 0)}, Inconclusive={counts.get('Inconclusive', 0)} "
            "(Note: Inconclusive != Normal; verify manually if needed)."
        )
        for item in list(preflight.get("items") or [])[:5]:
            if item.get("status") in ("Finding", "Inconclusive"):
                lines.append(
                    f"  * [{item.get('status')}] {item.get('title')}: {item.get('detail')}"
                )

    neighbors = list(blast.get("neighbors") or [])
    changes = list(blast.get("recent_changes") or [])
    if neighbors or changes:
        dep_str = ", ".join(
            f"{n.get('service_name') or n.get('node_id')} ({n.get('node_type')})"
            for n in neighbors[:6]
        ) or "none"
        lines.append(f"- [Tier 4 · Topology Blast Radius] Connected resources: {dep_str}")
        for chg in changes[:3]:
            lines.append(
                f"  * Recent Change [{chg.get('source')}] on {chg.get('service_name')} "
                f"({str(chg.get('commit_sha', ''))[:8]}): {chg.get('diff_summary')}"
            )

    if memory_rules:
        lines.append("- [Tier 5a · Calibrated Operational Rules (TTL-Active)]:")
        for r in memory_rules[:4]:
            lines.append(
                f"  * [{r.get('memory_type')} w={r.get('confidence_weight')}] {r.get('rule_text')}"
            )

    if prior_incidents:
        lines.append(
            "- [Tier 5b · Verified Historical Incidents (RRF Advisory — verify against Tier 2 live telemetry first)]:"
        )
        for inc in prior_incidents[:3]:
            lines.append(
                f"  * [{inc.get('incident_id')} | {inc.get('verification_status')} | w={inc.get('confidence_weight')}] "
                f"Root cause: {inc.get('primary_root_cause')} | Ruled out: {json.dumps(inc.get('ruled_out_hypotheses', []))[:160]}"
            )

    lines.append("</context_authority>")
    return "\n".join(lines)


@register_middleware("context_authority")
class ContextAuthorityMiddleware(AgentMiddleware):
    """Hydrates and injects the 5-Tier Context Authority bundle for SRE runs."""

    def __init__(
        self,
        *,
        store: OperationalStore | None = None,
        enable_tools: bool | None = None,
    ) -> None:
        """Initialize ContextAuthorityMiddleware and bind SRE operational tools when enabled."""
        super().__init__()
        self._store = store
        if enable_tools is None:
            from talkops.addons.config import is_sre_profile_active

            enable_tools = is_sre_profile_active()
        if enable_tools:
            from talkops.sre.tools import build_sre_operational_tools

            self.tools = build_sre_operational_tools(store_getter=self._get_store)
        else:
            self.tools = []

    def _get_store(self) -> OperationalStore | None:
        if self._store is not None:
            return self._store
        try:
            return get_operational_store()
        except Exception:
            return None

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Hydrate the 5-tier context authority bundle into state before model invocation."""
        if not isinstance(state, Mapping):
            return None
        if state.get("context_authority_bundle"):
            return None

        wh_meta = state.get("_webhook_metadata") if isinstance(state.get("_webhook_metadata"), Mapping) else {}
        is_sre_turn = bool(state.get("_webhook_sre_mode") or wh_meta)

        if not is_sre_turn:
            # For interactive/remote SRE sessions when operational store has active calibrated rules,
            # surface active global/service memory rules without forcing webhook mode.
            store = self._get_store()
            if store is None or not self.tools:
                return None
            try:
                rules = await store.list_active_memory_rules(service_name=None)
            except Exception:
                rules = []
            if not rules:
                return None
            profile = resolve_runtime_execution_profile(state)
            return {
                "context_authority_bundle": {
                    "tier1_execution_profile": profile,
                    "tier2_preflight": {},
                    "tier4_blast_radius": {},
                    "tier5_calibrated_incidents": [],
                    "tier5_memory_rules": [
                        {
                            "rule_id": r.rule_id,
                            "memory_type": r.memory_type,
                            "rule_text": r.rule_text,
                            "confidence_weight": round(r.confidence_weight, 2),
                        }
                        for r in rules[:5]
                    ],
                }
            }
        service_name = str(wh_meta.get("service_name") or "").strip()
        alert_title = str(wh_meta.get("title") or wh_meta.get("event_type") or service_name).strip()

        profile = resolve_runtime_execution_profile(state)
        bundle: dict[str, Any] = {
            "tier1_execution_profile": profile,
            "tier2_preflight": state.get("preflight_checklist_summary") or {},
            "tier4_blast_radius": {},
            "tier5_calibrated_incidents": [],
            "tier5_memory_rules": [],
        }

        store = self._get_store()
        if store is not None and (service_name or alert_title):
            try:
                graph = ContextGraph(store)
                inc_store = CalibratedIncidentStore(store)
                if service_name:
                    report = await graph.compute_blast_radius(service_name, max_depth=2)
                    bundle["tier4_blast_radius"] = report.to_dict()
                rules = await store.list_active_memory_rules(service_name=service_name or None)
                bundle["tier5_memory_rules"] = [
                    {
                        "rule_id": r.rule_id,
                        "memory_type": r.memory_type,
                        "rule_text": r.rule_text,
                        "confidence_weight": round(r.confidence_weight, 2),
                    }
                    for r in rules[:5]
                ]
                if alert_title:
                    hits = await inc_store.retrieve_similar(
                        f"{service_name} {alert_title}".strip(),
                        service_name=service_name or None,
                        top_k=3,
                    )
                    bundle["tier5_calibrated_incidents"] = [
                        {
                            "incident_id": h.incident_id,
                            "primary_root_cause": h.primary_root_cause,
                            "verification_status": h.verification_status,
                            "confidence_weight": round(h.confidence_weight, 2),
                            "ruled_out_hypotheses": h.ruled_out_hypotheses,
                        }
                        for h in hits
                    ]
            except Exception:
                pass

        return {"context_authority_bundle": bundle}

    def _inject_authority_system_message(self, request: ModelRequest) -> ModelRequest:
        state = getattr(request, "state", None)
        if not isinstance(state, Mapping):
            return request
        bundle = state.get("context_authority_bundle")
        if not isinstance(bundle, Mapping) or not bundle:
            return request

        block = render_context_authority_block(bundle)
        sys_msg = getattr(request, "system_message", None)
        if isinstance(sys_msg, SystemMessage):
            content = sys_msg.content
            if isinstance(content, str):
                if "</context_authority>" in content:
                    return request
                updated = SystemMessage(content=f"{content}\n\n{block}")
                if hasattr(request, "override"):
                    return request.override(system_message=updated)
            elif isinstance(content, list):
                flat_text = "".join(
                    b.get("text", "") if isinstance(b, Mapping) else str(b)
                    for b in content
                )
                if "</context_authority>" in flat_text:
                    return request
                from deepagents.middleware._utils import append_to_system_message

                updated = append_to_system_message(sys_msg, block)
                if hasattr(request, "override"):
                    return request.override(system_message=updated)
        return request

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> Any:
        """Inject `<context_authority>` into synchronous model requests."""
        return handler(self._inject_authority_system_message(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        """Inject `<context_authority>` into asynchronous model requests."""
        return await handler(self._inject_authority_system_message(request))

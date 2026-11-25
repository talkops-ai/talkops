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

"""LangChain operational tools exposing ``ContextGraph`` and ``CalibratedIncidentStore`` in ``talkops serve``.

Allows the SRE agent to organically discover service topology dependencies, blast radius,
recent CI/CD/GitOps/IaC changes, and calibrated RRF historical incident memory from natural
human prompts or autonomous webhook alerts without requiring hardcoded prompt instructions.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from typing import Annotated, Any, Literal

from langchain_core.tools import tool
from pydantic import Field

from talkops.context_graph.graph import ContextGraph
from talkops.memory.incident_store import CalibratedIncidentStore
from talkops.storage.factory import get_operational_store
from talkops.storage.protocol import MemoryRuleRecord, OperationalStore


def build_sre_operational_tools(
    *,
    store_getter: Callable[[], OperationalStore | None] | None = None,
) -> list[Any]:
    """Build operational tools backed by ``ContextGraph`` and ``CalibratedIncidentStore``."""

    def _resolve_store() -> OperationalStore | None:
        if store_getter is not None:
            return store_getter()
        try:
            return get_operational_store()
        except Exception:
            return None

    @tool
    async def query_service_blast_radius(
        service_or_node_id: Annotated[
            str,
            Field(
                description=(
                    "Service name or topology node ID to inspect (e.g., 'checkout-api', "
                    "'orders-rds', 'service:checkout-api', or 'rds:orders-rds')."
                )
            ),
        ],
        max_depth: Annotated[
            int,
            Field(default=2, ge=1, le=5, description="Maximum dependency traversal hops (1..5)."),
        ] = 2,
        change_window_sec: Annotated[
            float,
            Field(
                default=7200.0,
                ge=60.0,
                description="Lookback window in seconds for correlated deployments/CI/IaC changes.",
            ),
        ] = 7200.0,
    ) -> str:
        """Inspect upstream/downstream service topology dependencies (blast radius) and correlated recent deployments or config changes."""
        store = _resolve_store()
        if store is None:
            return json.dumps({"error": "Operational topology store is unavailable."})
        graph = ContextGraph(store)
        report = await graph.compute_blast_radius(
            service_or_node_id.strip(),
            max_depth=max_depth,
            change_window_sec=change_window_sec,
        )
        return json.dumps(report.to_dict(), indent=2, default=str)

    @tool
    async def list_recent_infrastructure_changes(
        service_name: Annotated[
            str,
            Field(
                default="",
                description="Service or application name (e.g. 'checkout-api'). Leave empty to check across known topology services.",
            ),
        ] = "",
        lookback_minutes: Annotated[
            int,
            Field(default=180, ge=1, le=10080, description="How far back in minutes to search for ArgoCD, GitHub, Jenkins, or Terraform changes."),
        ] = 180,
    ) -> str:
        """List recent CI/CD, GitOps (ArgoCD/Flux), GitHub, or Terraform changes deployed in the lookback window."""
        store = _resolve_store()
        if store is None:
            return json.dumps({"error": "Operational store is unavailable."})
        now_ts = time.time()
        since_ts = now_ts - (max(1, lookback_minutes) * 60.0)
        target = service_name.strip()
        if target:
            records = await store.list_recent_changes(target, since_ts=since_ts, until_ts=now_ts)
        else:
            records = await store.list_recent_changes("*", since_ts=since_ts, until_ts=now_ts)
        payload = [
            {
                "event_id": r.event_id,
                "service_name": r.service_name,
                "source": r.source,
                "environment": r.environment,
                "commit_sha": r.commit_sha,
                "pr_url": r.pr_url,
                "author": r.author,
                "diff_summary": r.diff_summary,
                "occurred_at": r.occurred_at,
            }
            for r in records
        ]
        return json.dumps({"service_filter": target or "*", "changes": payload}, indent=2, default=str)

    @tool
    async def search_incident_memory(
        search_text: Annotated[
            str,
            Field(description="Natural-language symptom, error signature, or service behavior to match against verified past incidents and operational rules."),
        ],
        service_name: Annotated[
            str,
            Field(default="", description="Optional service name filter (e.g. 'checkout-api')."),
        ] = "",
        top_k: Annotated[
            int,
            Field(default=3, ge=1, le=10, description="Maximum number of calibrated historical incidents to retrieve."),
        ] = 3,
    ) -> str:
        """Search verified historical incidents (Hybrid Vector + Lexical RRF) and active operational memory rules for similar failure patterns."""
        store = _resolve_store()
        if store is None:
            return json.dumps({"error": "Operational incident memory store is unavailable."})
        inc_store = CalibratedIncidentStore(store)
        svc = service_name.strip() or None
        hits = await inc_store.retrieve_similar(
            search_text.strip(),
            service_name=svc,
            verified_only=False,
            min_confidence=0.25,
            top_k=top_k,
        )
        rules = await store.list_active_memory_rules(service_name=svc)
        return json.dumps(
            {
                "active_memory_rules": [
                    {
                        "rule_id": r.rule_id,
                        "scope_service": r.scope_service,
                        "memory_type": r.memory_type,
                        "rule_text": r.rule_text,
                        "confidence_weight": round(r.confidence_weight, 3),
                        "source": r.source,
                    }
                    for r in rules[:6]
                ],
                "similar_incidents": [
                    {
                        "incident_id": h.incident_id,
                        "service_name": h.service_name,
                        "primary_root_cause": h.primary_root_cause,
                        "symptoms_text": h.symptoms_text,
                        "verification_status": h.verification_status,
                        "confidence_weight": round(h.confidence_weight, 3),
                        "ruled_out_hypotheses": h.ruled_out_hypotheses,
                        "remediation_action": h.remediation_action,
                    }
                    for h in hits
                ],
            },
            indent=2,
            default=str,
        )

    @tool
    async def record_operational_memory_rule(
        rule_text: Annotated[
            str,
            Field(description="Clear, actionable operational constraint, runbook lesson, or human operator correction to remember for future incidents."),
        ],
        scope_service: Annotated[
            str,
            Field(default="*", description="Target service name (e.g., 'checkout-api' or 'orders-rds'), or '*' for global."),
        ] = "*",
        memory_type: Annotated[
            Literal["episodic_constraint", "procedural_sequence"],
            Field(default="episodic_constraint", description="Whether this is a safety/diagnostic constraint or a multi-step debugging sequence."),
        ] = "episodic_constraint",
    ) -> str:
        """Persist a verified operational lesson, runbook rule, or human operator correction into calibrated operational memory."""
        store = _resolve_store()
        if store is None:
            return json.dumps({"error": "Operational store is unavailable."})
        inc_store = CalibratedIncidentStore(store)
        rec = MemoryRuleRecord(
            rule_id=f"rule-{uuid.uuid4().hex[:10]}",
            rule_text=rule_text.strip(),
            scope_service=scope_service.strip() or "*",
            memory_type=memory_type,
            source="chatops_correction",
            confidence_weight=0.95,
        )
        saved = await inc_store.upsert_rule(rec)
        return json.dumps(
            {
                "status": "recorded",
                "rule_id": saved.rule_id,
                "scope_service": saved.scope_service,
                "memory_type": saved.memory_type,
                "rule_text": saved.rule_text,
            }
        )

    tools_list = [
        query_service_blast_radius,
        list_recent_infrastructure_changes,
        search_incident_memory,
        record_operational_memory_rule,
    ]
    for t_obj in tools_list:
        t_obj.metadata = {"readOnlyHint": True}
    return tools_list


__all__ = ["build_sre_operational_tools"]

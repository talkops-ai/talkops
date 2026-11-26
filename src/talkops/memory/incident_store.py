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

"""Calibrated 3-Tier SRE Memory & Hybrid RRF Incident Store for TalkOps v2.

Prevents "Self-Reinforcing Hallucination Poisoning":
- Unverified LLM root-cause guesses stay ``verification_status="unverified"``
  (excluded from default ``verified_only=True`` RRF retrieval).
- Only incidents verified by durable post-remediation SLO checks (``slo_verified``),
  3-Stage Stratified AutoQA (``autoqa_verified``), or explicit human operator
  confirmation (``human_confirmed``) are promoted into calibrated retrieval.
- Regressed or overturned fixes drop to ``confidence_weight=0.0`` and optionally
  emit an ``episodic_constraint`` warning rule synced to LangGraph ``BaseStore`` with TTL.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from talkops.storage.factory import sync_rule_to_langgraph_store
from talkops.storage.protocol import (
    EmbedderFn,
    IncidentRecord,
    MemoryRuleRecord,
    OperationalStore,
    VerificationStatus,
    build_langchain_embedder,
)

_STATUS_DEFAULT_WEIGHTS: dict[VerificationStatus, float] = {
    "unverified": 0.25,
    "slo_verified": 0.85,
    "autoqa_verified": 0.90,
    "human_confirmed": 1.00,
    "regressed_rolled_back": 0.0,
    "rejected": 0.0,
}

DEFAULT_EPISODIC_TTL_SECONDS = 30 * 86400.0  # 30 days
DEFAULT_PROCEDURAL_TTL_SECONDS = 90 * 86400.0  # 90 days


class CalibratedIncidentStore:
    """Manages calibrated incident persistence, RRF retrieval, and TTL-decayed memory rules."""

    def __init__(
        self,
        store: OperationalStore,
        *,
        embedder: EmbedderFn | None = None,
        langgraph_store: Any | None = None,
    ) -> None:
        """Initialize CalibratedIncidentStore."""
        self.store = store
        self.embedder = embedder or build_langchain_embedder()
        self.langgraph_store = langgraph_store

    async def record_candidate_incident(
        self,
        *,
        thread_id: str,
        service_name: str,
        primary_root_cause: str,
        symptoms_text: str,
        environment: str = "production",
        tenant_id: str = "default",
        alert_fingerprint: str = "",
        contributing_factors: Sequence[str] = (),
        ruled_out_hypotheses: Sequence[Mapping[str, Any]] = (),
        remediation_action: Mapping[str, Any] | None = None,
        git_tree_hash: str = "",
        verification_status: VerificationStatus = "unverified",
        incident_id: str | None = None,
    ) -> IncidentRecord:
        """Persist an incident record with calibrated weight determined by ``verification_status``."""
        embed_input = f"{service_name} {alert_fingerprint} {symptoms_text} {primary_root_cause}".strip()
        vec = await self.embedder(embed_input)
        weight = _STATUS_DEFAULT_WEIGHTS.get(verification_status, 0.25)

        rec = IncidentRecord(
            incident_id=incident_id or f"inc-{uuid.uuid4().hex[:12]}",
            thread_id=thread_id,
            service_name=service_name,
            primary_root_cause=primary_root_cause,
            environment=environment,
            tenant_id=tenant_id,
            alert_fingerprint=alert_fingerprint,
            symptoms_text=symptoms_text,
            contributing_factors=list(contributing_factors),
            ruled_out_hypotheses=[dict(h) for h in ruled_out_hypotheses],
            remediation_action=dict(remediation_action or {}),
            verification_status=verification_status,
            confidence_weight=weight,
            git_tree_hash=git_tree_hash,
            embedding=vec,
        )
        await self.store.upsert_incident(rec)
        return rec

    async def promote_incident(
        self,
        incident: IncidentRecord,
        *,
        verification_status: Literal["slo_verified", "autoqa_verified", "human_confirmed"] | VerificationStatus,
    ) -> IncidentRecord:
        """Promote an incident after passing SLO verification, AutoQA, or human sign-off."""
        incident.verification_status = verification_status
        incident.confidence_weight = max(
            incident.confidence_weight,
            _STATUS_DEFAULT_WEIGHTS.get(verification_status, 0.85),
        )
        incident.updated_at = time.time()
        if not incident.embedding:
            incident.embedding = await self.embedder(
                f"{incident.service_name} {incident.symptoms_text} {incident.primary_root_cause}"
            )
        await self.store.upsert_incident(incident)
        return incident

    async def demote_regressed_incident(
        self,
        incident: IncidentRecord,
        *,
        regression_reason: str,
    ) -> tuple[IncidentRecord, MemoryRuleRecord]:
        """Zero out confidence on a rolled-back fix and emit a cautionary episodic constraint rule."""
        incident.verification_status = "regressed_rolled_back"
        incident.confidence_weight = 0.0
        incident.updated_at = time.time()
        await self.store.upsert_incident(incident)

        warning_rule = MemoryRuleRecord(
            rule_id=f"caution-{incident.incident_id}",
            scope_service=incident.service_name,
            tenant_id=incident.tenant_id,
            memory_type="episodic_constraint",
            rule_text=(
                f"CAUTION on {incident.service_name}: remediation {incident.remediation_action} "
                f"for '{incident.primary_root_cause}' regressed SLOs ({regression_reason}) and was rolled back."
            ),
            source="durable_verifier_rollback",
            confidence_weight=0.95,
            expires_at=time.time() + DEFAULT_EPISODIC_TTL_SECONDS,
        )
        await self.upsert_rule(warning_rule)
        return incident, warning_rule

    async def retrieve_similar(
        self,
        query_text: str,
        *,
        service_name: str | None = None,
        verified_only: bool = True,
        min_confidence: float = 0.35,
        top_k: int = 3,
        tenant_id: str = "default",
    ) -> list[IncidentRecord]:
        """Retrieve calibrated historical incidents via Hybrid Vector + Lexical RRF (`k=60`)."""
        q_vec = await self.embedder(query_text)
        return await self.store.search_incidents_rrf(
            query_text,
            query_embedding=q_vec,
            service_name=service_name,
            verified_only=verified_only,
            min_confidence=min_confidence,
            top_k=top_k,
            tenant_id=tenant_id,
        )

    async def upsert_rule(
        self,
        rule: MemoryRuleRecord,
    ) -> MemoryRuleRecord:
        """Persist a calibrated memory rule with default TTL and mirror into LangGraph ``BaseStore``."""
        if rule.expires_at is None:
            ttl = (
                DEFAULT_PROCEDURAL_TTL_SECONDS
                if rule.memory_type == "procedural_sequence"
                else DEFAULT_EPISODIC_TTL_SECONDS
            )
            rule.expires_at = time.time() + ttl
        await self.store.upsert_memory_rule(rule)
        await sync_rule_to_langgraph_store(self.langgraph_store, rule)
        return rule

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

"""Unified OperationalStore protocol, data models, RRF fusion, and LangChain embedder helpers."""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

VerificationStatus = Literal[
    "unverified",
    "slo_verified",
    "autoqa_verified",
    "human_confirmed",
    "regressed_rolled_back",
    "rejected",
]

DurableWorkflowPhase = Literal["grace_crash", "slo_verify", "scheduled_run"]
DurableWorkflowStatus = Literal[
    "pending",
    "running",
    "verified",
    "rolled_back",
    "failed",
    "cancelled",
]


@dataclass(slots=True)
class TopologyNode:
    """Generic multi-environment topology resource node."""

    node_id: str
    node_type: str
    service_name: str = ""
    environment: str = "production"
    tenant_id: str = "default"
    correlation_tags: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)
    baseline_stats: dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class TopologyEdge:
    """Directed relationship between two topology resource nodes."""

    src_id: str
    dst_id: str
    rel_type: str
    tenant_id: str = "default"
    lineage_source: str = "manual"
    last_seen_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class ChangeEventRecord:
    """Normalized CI/CD, GitOps, IaC, or host change event."""

    event_id: str
    service_name: str
    source: str
    environment: str = "production"
    tenant_id: str = "default"
    commit_sha: str = ""
    pr_url: str = ""
    author: str = ""
    diff_summary: str = ""
    git_tree_hash: str = ""
    occurred_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class IncidentRecord:
    """Calibrated episodic incident record with ruled-out hypotheses & RRF metadata."""

    incident_id: str
    thread_id: str
    service_name: str
    primary_root_cause: str
    environment: str = "production"
    tenant_id: str = "default"
    alert_fingerprint: str = ""
    symptoms_text: str = ""
    contributing_factors: list[str] = field(default_factory=list)
    ruled_out_hypotheses: list[dict[str, Any]] = field(default_factory=list)
    remediation_action: dict[str, Any] = field(default_factory=dict)
    verification_status: VerificationStatus = "unverified"
    confidence_weight: float = 1.0
    git_tree_hash: str = ""
    embedding: list[float] | None = None
    rrf_score: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class MemoryRuleRecord:
    """Calibrated episodic constraint or procedural debugging sequence."""

    rule_id: str
    rule_text: str
    scope_service: str = "*"
    tenant_id: str = "default"
    memory_type: Literal["episodic_constraint", "procedural_sequence"] = "episodic_constraint"
    source: str = "reflection_observer"
    confirmation_count: int = 1
    hit_count: int = 0
    helped_count: int = 0
    misled_count: int = 0
    confidence_weight: float = 1.0
    active: bool = True
    expires_at: float | None = None
    updated_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class CorrelatedAlertItem:
    """Sidecar Alert Inbox entry awaiting synchronous @before_model drain."""

    alert_row_id: str
    thread_id: str
    provider: str
    dedup_key: str
    normalized_event: dict[str, Any]
    service_name: str = ""
    environment: str = "production"
    tenant_id: str = "default"
    consumed: bool = False
    arrived_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class DurableWorkflowJob:
    """Restart-safe checkpointed post-remediation verification or cron job."""

    workflow_id: str
    thread_id: str
    workflow_type: Literal["post_remediation", "scheduled_review"]
    status: DurableWorkflowStatus = "pending"
    phase: DurableWorkflowPhase = "grace_crash"
    poll_interval_sec: float = 15.0
    next_poll_at: float = field(default_factory=time.time)
    started_at: float = field(default_factory=time.time)
    deadline_at: float = field(default_factory=lambda: time.time() + 300.0)
    consecutive_passes: int = 0
    spec_json: dict[str, Any] = field(default_factory=dict)
    tnr_undo_json: dict[str, Any] = field(default_factory=dict)
    sink_refs: dict[str, Any] = field(default_factory=dict)
    lease_owner: str | None = None
    lease_expires_at: float | None = None
    result_json: dict[str, Any] = field(default_factory=dict)
    tenant_id: str = "default"
    updated_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class AutoQAScoreRecord:
    """3-Stage Stratified AutoQA scorecard entry."""

    thread_id: str
    plugin_name: str
    stage0_passed: bool
    overall_score: float
    plugin_version: str = "default"
    stage0_violations: list[str] = field(default_factory=list)
    judge_tier: Literal["stage0_only", "fast", "frontier_calibrated"] = "fast"
    self_confidence: float = 1.0
    criterion_citations: dict[str, list[str]] = field(default_factory=dict)
    resolution_mode: Literal[
        "agent_mitigated",
        "agent_assisted",
        "gitops_pr_opened",
        "human_resolved",
        "unverified",
    ] = "agent_assisted"
    dispute_status: Literal["none", "disputed", "upheld", "overturned"] = "none"
    feedback_notes: str = ""
    tenant_id: str = "default"
    created_at: float = field(default_factory=time.time)


def compute_rrf_scores(
    vec_ranked_ids: Sequence[str],
    fts_ranked_ids: Sequence[str],
    *,
    k: int = 60,
    weights_by_id: Mapping[str, float] | None = None,
) -> list[tuple[str, float]]:
    """Compute Reciprocal Rank Fusion (RRF) scores across semantic and lexical rankings.

    Formula:
        S_RRF(d) = (1 / (k + rank_vec(d)) + 1 / (k + rank_fts(d))) * confidence_weight(d)
    """
    scores: dict[str, float] = {}
    for rank_idx, doc_id in enumerate(vec_ranked_ids, start=1):
        scores[doc_id] = scores.get(doc_id, 0.0) + (1.0 / (k + rank_idx))
    for rank_idx, doc_id in enumerate(fts_ranked_ids, start=1):
        scores[doc_id] = scores.get(doc_id, 0.0) + (1.0 / (k + rank_idx))

    if weights_by_id:
        for doc_id in list(scores.keys()):
            scores[doc_id] *= max(0.0, float(weights_by_id.get(doc_id, 1.0)))

    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


def cosine_similarity(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:
    """Compute cosine similarity between two equal-length float vectors."""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b, strict=False))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


_TOKEN_RE = re.compile(r"[a-z0-9_:\-\.]{2,}")


def deterministic_local_embedding(text: str, dims: int = 128) -> list[float]:
    """Fast, zero-network signed feature-hashing embedding fallback for offline/air-gapped OSS runs.

    When a LangChain ``Embeddings`` model (OpenAI, Ollama, Bedrock, Gemini, HuggingFace) is
    configured, ``EmbedderFn`` produces neural embeddings; when running completely offline
    with zero embedding API keys, feature hashing still enables cosine similarity alongside FTS5/tsvector!
    """
    vec = [0.0] * dims
    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        return vec
    for tok in tokens:
        digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "little") % dims
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0.0:
        vec = [v / norm for v in vec]
    return vec


EmbedderFn = Callable[[str], Awaitable[list[float]]]


def build_langchain_embedder(
    embedding_spec: str | Any | None = None,
    *,
    fallback_dims: int = 128,
) -> EmbedderFn:
    """Create an async embedder using ``langchain.embeddings.init_embeddings`` with offline fallback."""
    lc_embeddings: Any = None
    if embedding_spec is not None and not isinstance(embedding_spec, str):
        lc_embeddings = embedding_spec
    elif isinstance(embedding_spec, str) and embedding_spec.strip():
        try:
            from langchain.embeddings import init_embeddings

            lc_embeddings = init_embeddings(embedding_spec.strip())
        except Exception:
            lc_embeddings = None

    async def _embed(text: str) -> list[float]:
        if lc_embeddings is not None:
            try:
                if hasattr(lc_embeddings, "aembed_query"):
                    return list(await lc_embeddings.aembed_query(text))
                if hasattr(lc_embeddings, "embed_query"):
                    return list(lc_embeddings.embed_query(text))
            except Exception:
                pass
        return deterministic_local_embedding(text, dims=fallback_dims)

    return _embed


@runtime_checkable
class OperationalStore(Protocol):
    """Unified async storage interface across PostgreSQL and local WAL-mode SQLite."""

    async def setup(self) -> None:
        """Initialize backing schema and indexes."""
        ...

    async def aclose(self) -> None:
        """Close open database connections."""
        ...

    # Topology & Change Events
    async def upsert_topology_node(self, node: TopologyNode) -> None:
        """Insert or update a service topology node."""
        ...

    async def upsert_topology_edge(self, edge: TopologyEdge) -> None:
        """Insert or update a directed topology edge."""
        ...

    async def get_topology_neighbors(
        self,
        node_id: str,
        *,
        max_depth: int = 1,
        tenant_id: str = "default",
    ) -> list[TopologyNode]:
        """Return neighboring topology nodes within `max_depth`."""
        ...

    async def record_change_event(self, event: ChangeEventRecord) -> None:
        """Record an infrastructure or deployment change event."""
        ...

    async def list_recent_changes(
        self,
        service_name: str,
        *,
        since_ts: float,
        until_ts: float | None = None,
        tenant_id: str = "default",
    ) -> list[ChangeEventRecord]:
        """Return recent change events for `service_name`."""
        ...

    # Calibrated Incident Store (Hybrid Vector + Lexical RRF)
    async def upsert_incident(self, incident: IncidentRecord) -> None:
        """Insert or update an incident record."""
        ...

    async def search_incidents_rrf(
        self,
        query_text: str,
        *,
        query_embedding: Sequence[float] | None = None,
        service_name: str | None = None,
        verified_only: bool = True,
        min_confidence: float = 0.30,
        top_k: int = 3,
        rrf_k: int = 60,
        tenant_id: str = "default",
    ) -> list[IncidentRecord]:
        """Search historical incidents using hybrid RRF ranking."""
        ...

    async def apply_incident_git_decay(
        self,
        service_name: str,
        new_git_tree_hash: str,
        *,
        decay_factor: float = 0.65,
        tenant_id: str = "default",
    ) -> int:
        """Decay historical incident confidence after Git tree changes."""
        ...

    # Calibrated Memory Rules (Episodic & Procedural)
    async def upsert_memory_rule(self, rule: MemoryRuleRecord) -> None:
        """Insert or update an operational memory rule."""
        ...

    async def list_active_memory_rules(
        self,
        *,
        service_name: str | None = None,
        now_ts: float | None = None,
        tenant_id: str = "default",
    ) -> list[MemoryRuleRecord]:
        """Return active operational memory rules."""
        ...

    async def record_memory_rule_outcome(
        self,
        rule_id: str,
        *,
        helped: bool,
    ) -> None:
        """Record whether a memory rule helped resolve an incident."""
        ...

    # Sidecar Alert Inbox (Storm Drain)
    async def enqueue_correlated_alert(self, item: CorrelatedAlertItem) -> bool:
        """Enqueue a correlated alert into the sidecar storm-drain inbox."""
        ...

    async def drain_correlated_alerts(self, thread_id: str) -> list[CorrelatedAlertItem]:
        """Drain unconsumed correlated alerts for `thread_id`."""
        ...

    async def peek_unconsumed_alert_count(self, thread_id: str) -> int:
        """Return count of unconsumed correlated alerts for `thread_id`."""
        ...

    # Durable Workflows (Post-Remediation 2-Phase Verifier & Scheduled Reviews)
    async def enqueue_durable_workflow(self, job: DurableWorkflowJob) -> None:
        """Enqueue a durable workflow job."""
        ...

    async def claim_due_workflows(
        self,
        worker_id: str,
        *,
        now_ts: float | None = None,
        lease_seconds: float = 45.0,
        limit: int = 10,
    ) -> list[DurableWorkflowJob]:
        """Claim due workflow jobs with an exclusive worker lease."""
        ...

    async def update_durable_workflow(self, job: DurableWorkflowJob) -> None:
        """Update durable workflow job state."""
        ...

    async def get_durable_workflow(self, workflow_id: str) -> DurableWorkflowJob | None:
        """Return durable workflow job by ID if present."""
        ...

    # AutoQA Scores & Appeals
    async def save_autoqa_score(self, score: AutoQAScoreRecord) -> None:
        """Persist a stratified AutoQA score record."""
        ...

    async def get_autoqa_scores(
        self,
        *,
        thread_id: str | None = None,
        plugin_name: str | None = None,
        plugin_version: str | None = None,
        limit: int = 50,
        tenant_id: str = "default",
    ) -> list[AutoQAScoreRecord]:
        """Return stored AutoQA score records matching filters."""
        ...

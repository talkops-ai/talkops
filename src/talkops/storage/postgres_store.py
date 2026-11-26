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

"""PostgreSQL OperationalStore implementation.

Uses pgvector HNSW + tsvector GIN Reciprocal Rank Fusion (RRF) and FOR UPDATE SKIP LOCKED durable workflow leases.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from talkops.storage.protocol import (
    AutoQAScoreRecord,
    ChangeEventRecord,
    CorrelatedAlertItem,
    DurableWorkflowJob,
    IncidentRecord,
    MemoryRuleRecord,
    TopologyEdge,
    TopologyNode,
    deterministic_local_embedding,
)
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

_MIGRATION_PATH = Path(__file__).parent / "migrations" / "v001_talkops_v2.sql"


def _format_pgvector(vec: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(v):.6f}" for v in vec) + "]"


class PostgresOperationalStore:
    """Async PostgreSQL implementation of ``OperationalStore``."""

    def __init__(
        self,
        conn_uri: str,
        *,
        pool: AsyncConnectionPool[Any] | None = None,
        embedding_dims: int = 1536,
    ) -> None:
        """Initialize PostgresOperationalStore."""
        self._conn_uri = conn_uri
        self._pool = pool
        self._owns_pool = pool is None
        self._embedding_dims = embedding_dims
        self._initialized = False

    async def _ensure_pool(self) -> AsyncConnectionPool[Any]:
        if self._pool is None:
            self._pool = AsyncConnectionPool(
                conninfo=self._conn_uri,
                min_size=1,
                max_size=10,
                open=False,
                kwargs={"autocommit": True, "row_factory": dict_row},
            )
            await self._pool.open()
        return self._pool

    async def setup(self) -> None:
        """Apply canonical v001 PostgreSQL DDL migrations idempotently."""
        pool = await self._ensure_pool()
        ddl = _MIGRATION_PATH.read_text(encoding="utf-8")
        if self._embedding_dims != 1536:
            ddl = ddl.replace("vector(1536)", f"vector({self._embedding_dims})")
        async with pool.connection() as conn:
            await conn.execute(ddl)
        self._initialized = True

    async def aclose(self) -> None:
        """Close the connection pool if owned by this store."""
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
            self._pool = None
        self._initialized = False

    async def upsert_topology_node(self, node: TopologyNode) -> None:
        """Execute `upsert_topology_node`."""
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO talkops_topology_nodes (
                    tenant_id, node_id, node_type, service_name, environment,
                    correlation_tags, attributes, baseline_stats, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)
                ON CONFLICT (tenant_id, node_id) DO UPDATE SET
                    node_type = EXCLUDED.node_type,
                    service_name = EXCLUDED.service_name,
                    environment = EXCLUDED.environment,
                    correlation_tags = EXCLUDED.correlation_tags,
                    attributes = EXCLUDED.attributes,
                    baseline_stats = EXCLUDED.baseline_stats,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    node.tenant_id,
                    node.node_id,
                    node.node_type,
                    node.service_name or node.node_id,
                    node.environment,
                    json.dumps(node.correlation_tags),
                    json.dumps(node.attributes),
                    json.dumps(node.baseline_stats),
                    node.updated_at,
                ),
            )

    async def upsert_topology_edge(self, edge: TopologyEdge) -> None:
        """Execute `upsert_topology_edge`."""
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO talkops_topology_edges (
                    tenant_id, src_id, dst_id, rel_type, lineage_source, last_seen_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, src_id, dst_id, rel_type) DO UPDATE SET
                    lineage_source = EXCLUDED.lineage_source,
                    last_seen_at = EXCLUDED.last_seen_at
                """,
                (
                    edge.tenant_id,
                    edge.src_id,
                    edge.dst_id,
                    edge.rel_type,
                    edge.lineage_source,
                    edge.last_seen_at,
                ),
            )

    async def get_topology_neighbors(
        self,
        node_id: str,
        *,
        max_depth: int = 1,
        tenant_id: str = "default",
    ) -> list[TopologyNode]:
        """Return `get_topology_neighbors` result."""
        bounded_depth = max(1, min(int(max_depth), 4))
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """
                WITH RECURSIVE seeds(nid) AS (
                    SELECT %s::text
                    UNION
                    SELECT node_id
                    FROM talkops_topology_nodes
                    WHERE tenant_id = %s AND (node_id = %s OR LOWER(service_name) = LOWER(%s))
                ),
                walk(nid, depth) AS (
                    SELECT nid, 0 FROM seeds
                    UNION
                    SELECT CASE WHEN e.src_id = w.nid THEN e.dst_id ELSE e.src_id END,
                           w.depth + 1
                    FROM talkops_topology_edges e
                    JOIN walk w ON (e.src_id = w.nid OR e.dst_id = w.nid)
                    WHERE e.tenant_id = %s AND w.depth < %s
                )
                SELECT DISTINCT n.*
                FROM talkops_topology_nodes n
                JOIN walk w ON n.node_id = w.nid
                WHERE n.tenant_id = %s AND n.node_id NOT IN (SELECT nid FROM seeds)
                """,
                (node_id, tenant_id, node_id, node_id, tenant_id, bounded_depth, tenant_id),
            )
            rows = await cur.fetchall()
        return [
            TopologyNode(
                tenant_id=r["tenant_id"],
                node_id=r["node_id"],
                node_type=r["node_type"],
                service_name=r["service_name"],
                environment=r["environment"],
                correlation_tags=dict(r["correlation_tags"] or {}),
                attributes=dict(r["attributes"] or {}),
                baseline_stats=dict(r["baseline_stats"] or {}),
                updated_at=float(r["updated_at"]),
            )
            for r in rows
        ]

    async def record_change_event(self, event: ChangeEventRecord) -> None:
        """Execute `record_change_event`."""
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO talkops_change_events (
                    event_id, tenant_id, service_name, environment, source,
                    commit_sha, pr_url, author, diff_summary, git_tree_hash, occurred_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO UPDATE SET
                    diff_summary = EXCLUDED.diff_summary,
                    git_tree_hash = EXCLUDED.git_tree_hash,
                    occurred_at = EXCLUDED.occurred_at
                """,
                (
                    event.event_id,
                    event.tenant_id,
                    event.service_name,
                    event.environment,
                    event.source,
                    event.commit_sha,
                    event.pr_url,
                    event.author,
                    event.diff_summary,
                    event.git_tree_hash,
                    event.occurred_at,
                ),
            )

    async def list_recent_changes(
        self,
        service_name: str,
        *,
        since_ts: float,
        until_ts: float | None = None,
        tenant_id: str = "default",
    ) -> list[ChangeEventRecord]:
        """Return `list_recent_changes` result."""
        pool = await self._ensure_pool()
        upper = until_ts if until_ts is not None else (time.time() + 300.0)
        async with pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT * FROM talkops_change_events
                WHERE tenant_id = %s AND service_name = %s AND occurred_at >= %s AND occurred_at <= %s
                ORDER BY occurred_at DESC
                LIMIT 25
                """,
                (tenant_id, service_name, since_ts, upper),
            )
            rows = await cur.fetchall()
        return [
            ChangeEventRecord(
                event_id=r["event_id"],
                tenant_id=r["tenant_id"],
                service_name=r["service_name"],
                environment=r["environment"],
                source=r["source"],
                commit_sha=r["commit_sha"],
                pr_url=r["pr_url"],
                author=r["author"],
                diff_summary=r["diff_summary"],
                git_tree_hash=r["git_tree_hash"],
                occurred_at=float(r["occurred_at"]),
            )
            for r in rows
        ]

    async def upsert_incident(self, incident: IncidentRecord) -> None:
        """Execute `upsert_incident`."""
        pool = await self._ensure_pool()
        searchable = " ".join(
            p
            for p in (
                incident.service_name,
                incident.alert_fingerprint,
                incident.symptoms_text,
                incident.primary_root_cause,
            )
            if p
        )
        emb = (
            list(incident.embedding)
            if incident.embedding is not None
            else deterministic_local_embedding(searchable, dims=self._embedding_dims)
        )
        if len(emb) < self._embedding_dims:
            emb = emb + [0.0] * (self._embedding_dims - len(emb))
        elif len(emb) > self._embedding_dims:
            emb = emb[: self._embedding_dims]

        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO talkops_incidents (
                    incident_id, tenant_id, thread_id, service_name, environment,
                    alert_fingerprint, symptoms_text, primary_root_cause,
                    contributing_factors, ruled_out_hypotheses, remediation_action,
                    verification_status, confidence_weight, git_tree_hash,
                    embedding, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s::jsonb,
                    %s, %s, %s, %s::vector, %s, %s
                )
                ON CONFLICT (incident_id) DO UPDATE SET
                    primary_root_cause = EXCLUDED.primary_root_cause,
                    contributing_factors = EXCLUDED.contributing_factors,
                    ruled_out_hypotheses = EXCLUDED.ruled_out_hypotheses,
                    remediation_action = EXCLUDED.remediation_action,
                    verification_status = EXCLUDED.verification_status,
                    confidence_weight = EXCLUDED.confidence_weight,
                    git_tree_hash = EXCLUDED.git_tree_hash,
                    embedding = EXCLUDED.embedding,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    incident.incident_id,
                    incident.tenant_id,
                    incident.thread_id,
                    incident.service_name,
                    incident.environment,
                    incident.alert_fingerprint,
                    incident.symptoms_text,
                    incident.primary_root_cause,
                    json.dumps(incident.contributing_factors),
                    json.dumps(incident.ruled_out_hypotheses),
                    json.dumps(incident.remediation_action),
                    incident.verification_status,
                    incident.confidence_weight,
                    incident.git_tree_hash,
                    _format_pgvector(emb),
                    incident.created_at,
                    incident.updated_at,
                ),
            )

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
        """Execute `search_incidents_rrf`."""
        pool = await self._ensure_pool()
        q_emb = (
            list(query_embedding)
            if query_embedding is not None
            else deterministic_local_embedding(query_text, dims=self._embedding_dims)
        )
        if len(q_emb) < self._embedding_dims:
            q_emb = q_emb + [0.0] * (self._embedding_dims - len(q_emb))
        elif len(q_emb) > self._embedding_dims:
            q_emb = q_emb[: self._embedding_dims]

        ver_clause = (
            "AND verification_status IN ('slo_verified', 'autoqa_verified', 'human_confirmed')"
            if verified_only
            else ""
        )
        srv_clause = "AND service_name = %(service_name)s" if service_name else ""
        sql = f"""
        WITH vec_matches AS (
            SELECT incident_id,
                   ROW_NUMBER() OVER (ORDER BY embedding <=> %(q_vec)s::vector) AS rank_vec
            FROM talkops_incidents
            WHERE tenant_id = %(tenant_id)s
              AND confidence_weight >= %(min_conf)s
              {ver_clause}
              {srv_clause}
            ORDER BY embedding <=> %(q_vec)s::vector
            LIMIT 20
        ),
        fts_matches AS (
            SELECT incident_id,
                   ROW_NUMBER() OVER (
                       ORDER BY ts_rank_cd(tsv, websearch_to_tsquery('english', %(q_text)s)) DESC
                   ) AS rank_fts
            FROM talkops_incidents
            WHERE tenant_id = %(tenant_id)s
              AND confidence_weight >= %(min_conf)s
              {ver_clause}
              {srv_clause}
              AND tsv @@ websearch_to_tsquery('english', %(q_text)s)
            LIMIT 20
        )
        SELECT i.*,
               (
                   COALESCE(1.0 / (%(rrf_k)s + v.rank_vec), 0.0) +
                   COALESCE(1.0 / (%(rrf_k)s + f.rank_fts), 0.0)
               ) * i.confidence_weight AS rrf_score
        FROM talkops_incidents i
        LEFT JOIN vec_matches v ON i.incident_id = v.incident_id
        LEFT JOIN fts_matches f ON i.incident_id = f.incident_id
        WHERE v.incident_id IS NOT NULL OR f.incident_id IS NOT NULL
        ORDER BY rrf_score DESC
        LIMIT %(top_k)s;
        """
        async with pool.connection() as conn:
            cur = await conn.execute(
                sql,
                {
                    "q_vec": _format_pgvector(q_emb),
                    "q_text": query_text,
                    "tenant_id": tenant_id,
                    "min_conf": min_confidence,
                    "service_name": service_name,
                    "rrf_k": rrf_k,
                    "top_k": top_k,
                },
            )
            rows = await cur.fetchall()

        return [
            IncidentRecord(
                incident_id=r["incident_id"],
                tenant_id=r["tenant_id"],
                thread_id=r["thread_id"],
                service_name=r["service_name"],
                environment=r["environment"],
                alert_fingerprint=r["alert_fingerprint"],
                symptoms_text=r["symptoms_text"],
                primary_root_cause=r["primary_root_cause"],
                contributing_factors=list(r["contributing_factors"] or []),
                ruled_out_hypotheses=list(r["ruled_out_hypotheses"] or []),
                remediation_action=dict(r["remediation_action"] or {}),
                verification_status=cast("Any", r["verification_status"]),
                confidence_weight=float(r["confidence_weight"]),
                git_tree_hash=r["git_tree_hash"],
                rrf_score=float(r["rrf_score"]),
                created_at=float(r["created_at"]),
                updated_at=float(r["updated_at"]),
            )
            for r in rows
        ]

    async def apply_incident_git_decay(
        self,
        service_name: str,
        new_git_tree_hash: str,
        *,
        decay_factor: float = 0.65,
        tenant_id: str = "default",
    ) -> int:
        """Execute `apply_incident_git_decay`."""
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """
                UPDATE talkops_incidents
                SET confidence_weight = confidence_weight * %s,
                    updated_at = %s
                WHERE tenant_id = %s
                  AND service_name = %s
                  AND git_tree_hash <> ''
                  AND git_tree_hash <> %s
                """,
                (decay_factor, time.time(), tenant_id, service_name, new_git_tree_hash),
            )
            return int(cur.rowcount or 0)

    async def upsert_memory_rule(self, rule: MemoryRuleRecord) -> None:
        """Execute `upsert_memory_rule`."""
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO talkops_memory_rules (
                    rule_id, tenant_id, scope_service, memory_type, rule_text, source,
                    confirmation_count, hit_count, helped_count, misled_count,
                    confidence_weight, active, expires_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (rule_id) DO UPDATE SET
                    rule_text = EXCLUDED.rule_text,
                    confirmation_count = EXCLUDED.confirmation_count,
                    confidence_weight = EXCLUDED.confidence_weight,
                    active = EXCLUDED.active,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    rule.rule_id,
                    rule.tenant_id,
                    rule.scope_service,
                    rule.memory_type,
                    rule.rule_text,
                    rule.source,
                    rule.confirmation_count,
                    rule.hit_count,
                    rule.helped_count,
                    rule.misled_count,
                    rule.confidence_weight,
                    rule.active,
                    rule.expires_at,
                    rule.updated_at,
                ),
            )

    async def list_active_memory_rules(
        self,
        *,
        service_name: str | None = None,
        now_ts: float | None = None,
        tenant_id: str = "default",
    ) -> list[MemoryRuleRecord]:
        """Return `list_active_memory_rules` result."""
        pool = await self._ensure_pool()
        now = now_ts if now_ts is not None else time.time()
        async with pool.connection() as conn:
            if service_name:
                cur = await conn.execute(
                    """
                    SELECT * FROM talkops_memory_rules
                    WHERE tenant_id = %s AND active = TRUE
                      AND (expires_at IS NULL OR expires_at > %s)
                      AND (scope_service = '*' OR scope_service = %s)
                    ORDER BY confidence_weight DESC, updated_at DESC
                    LIMIT 25
                    """,
                    (tenant_id, now, service_name),
                )
            else:
                cur = await conn.execute(
                    """
                    SELECT * FROM talkops_memory_rules
                    WHERE tenant_id = %s AND active = TRUE
                      AND (expires_at IS NULL OR expires_at > %s)
                    ORDER BY confidence_weight DESC, updated_at DESC
                    LIMIT 25
                    """,
                    (tenant_id, now),
                )
            rows = await cur.fetchall()
        return [
            MemoryRuleRecord(
                rule_id=r["rule_id"],
                tenant_id=r["tenant_id"],
                scope_service=r["scope_service"],
                memory_type=cast("Any", r["memory_type"]),
                rule_text=r["rule_text"],
                source=r["source"],
                confirmation_count=int(r["confirmation_count"]),
                hit_count=int(r["hit_count"]),
                helped_count=int(r["helped_count"]),
                misled_count=int(r["misled_count"]),
                confidence_weight=float(r["confidence_weight"]),
                active=bool(r["active"]),
                expires_at=float(r["expires_at"]) if r["expires_at"] is not None else None,
                updated_at=float(r["updated_at"]),
            )
            for r in rows
        ]

    async def record_memory_rule_outcome(
        self,
        rule_id: str,
        *,
        helped: bool,
    ) -> None:
        """Execute `record_memory_rule_outcome`."""
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            if helped:
                await conn.execute(
                    """
                    UPDATE talkops_memory_rules
                    SET hit_count = hit_count + 1,
                        helped_count = helped_count + 1,
                        confidence_weight = LEAST(2.0, confidence_weight * 1.10),
                        updated_at = %s
                    WHERE rule_id = %s
                    """,
                    (time.time(), rule_id),
                )
            else:
                await conn.execute(
                    """
                    UPDATE talkops_memory_rules
                    SET hit_count = hit_count + 1,
                        misled_count = misled_count + 1,
                        confidence_weight = confidence_weight * 0.50,
                        active = CASE WHEN confidence_weight * 0.50 < 0.25 THEN FALSE ELSE active END,
                        updated_at = %s
                    WHERE rule_id = %s
                    """,
                    (time.time(), rule_id),
                )

    async def enqueue_correlated_alert(self, item: CorrelatedAlertItem) -> bool:
        """Execute `enqueue_correlated_alert`."""
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO talkops_correlated_alerts (
                    alert_row_id, thread_id, tenant_id, provider, dedup_key,
                    service_name, environment, normalized_event, consumed, arrived_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (thread_id, dedup_key) DO NOTHING
                """,
                (
                    item.alert_row_id,
                    item.thread_id,
                    item.tenant_id,
                    item.provider,
                    item.dedup_key,
                    item.service_name,
                    item.environment,
                    json.dumps(item.normalized_event),
                    item.consumed,
                    item.arrived_at,
                ),
            )
            return bool(cur.rowcount and cur.rowcount > 0)

    async def drain_correlated_alerts(self, thread_id: str) -> list[CorrelatedAlertItem]:
        """Execute `drain_correlated_alerts`."""
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """
                UPDATE talkops_correlated_alerts
                SET consumed = TRUE
                WHERE thread_id = %s AND consumed = FALSE
                RETURNING *
                """,
                (thread_id,),
            )
            rows = await cur.fetchall()
        rows_sorted = sorted(rows, key=lambda r: float(r["arrived_at"]))
        return [
            CorrelatedAlertItem(
                alert_row_id=r["alert_row_id"],
                thread_id=r["thread_id"],
                tenant_id=r["tenant_id"],
                provider=r["provider"],
                dedup_key=r["dedup_key"],
                service_name=r["service_name"],
                environment=r["environment"],
                normalized_event=dict(r["normalized_event"] or {}),
                consumed=True,
                arrived_at=float(r["arrived_at"]),
            )
            for r in rows_sorted
        ]

    async def peek_unconsumed_alert_count(self, thread_id: str) -> int:
        """Execute `peek_unconsumed_alert_count`."""
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) AS cnt FROM talkops_correlated_alerts WHERE thread_id = %s AND consumed = FALSE",
                (thread_id,),
            )
            row = await cur.fetchone()
        return int(row["cnt"]) if row else 0

    def _row_to_workflow(self, r: dict[str, Any]) -> DurableWorkflowJob:
        return DurableWorkflowJob(
            workflow_id=r["workflow_id"],
            thread_id=r["thread_id"],
            tenant_id=r["tenant_id"],
            workflow_type=cast("Any", r["workflow_type"]),
            status=cast("Any", r["status"]),
            phase=cast("Any", r["phase"]),
            poll_interval_sec=float(r["poll_interval_sec"]),
            next_poll_at=float(r["next_poll_at"]),
            started_at=float(r["started_at"]),
            deadline_at=float(r["deadline_at"]),
            consecutive_passes=int(r["consecutive_passes"]),
            spec_json=dict(r["spec_json"] or {}),
            tnr_undo_json=dict(r["tnr_undo_json"] or {}),
            sink_refs=dict(r["sink_refs"] or {}),
            lease_owner=r["lease_owner"],
            lease_expires_at=float(r["lease_expires_at"])
            if r["lease_expires_at"] is not None
            else None,
            result_json=dict(r["result_json"] or {}),
            updated_at=float(r["updated_at"]),
        )

    async def enqueue_durable_workflow(self, job: DurableWorkflowJob) -> None:
        """Execute `enqueue_durable_workflow`."""
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO talkops_durable_workflows (
                    workflow_id, thread_id, tenant_id, workflow_type, status, phase,
                    poll_interval_sec, next_poll_at, started_at, deadline_at,
                    consecutive_passes, spec_json, tnr_undo_json, sink_refs,
                    lease_owner, lease_expires_at, result_json, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb, %s
                )
                ON CONFLICT (workflow_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    phase = EXCLUDED.phase,
                    poll_interval_sec = EXCLUDED.poll_interval_sec,
                    next_poll_at = EXCLUDED.next_poll_at,
                    consecutive_passes = EXCLUDED.consecutive_passes,
                    spec_json = EXCLUDED.spec_json,
                    tnr_undo_json = EXCLUDED.tnr_undo_json,
                    sink_refs = EXCLUDED.sink_refs,
                    lease_owner = EXCLUDED.lease_owner,
                    lease_expires_at = EXCLUDED.lease_expires_at,
                    result_json = EXCLUDED.result_json,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    job.workflow_id,
                    job.thread_id,
                    job.tenant_id,
                    job.workflow_type,
                    job.status,
                    job.phase,
                    job.poll_interval_sec,
                    job.next_poll_at,
                    job.started_at,
                    job.deadline_at,
                    job.consecutive_passes,
                    json.dumps(job.spec_json),
                    json.dumps(job.tnr_undo_json),
                    json.dumps(job.sink_refs),
                    job.lease_owner,
                    job.lease_expires_at,
                    json.dumps(job.result_json),
                    job.updated_at,
                ),
            )

    async def claim_due_workflows(
        self,
        worker_id: str,
        *,
        now_ts: float | None = None,
        lease_seconds: float = 45.0,
        limit: int = 10,
    ) -> list[DurableWorkflowJob]:
        """Execute `claim_due_workflows`."""
        pool = await self._ensure_pool()
        now = now_ts if now_ts is not None else time.time()
        lease_until = now + lease_seconds
        async with pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """
                WITH claimed AS (
                    SELECT workflow_id
                    FROM talkops_durable_workflows
                    WHERE status IN ('pending', 'running')
                      AND next_poll_at <= %s
                      AND (lease_expires_at IS NULL OR lease_expires_at <= %s)
                    ORDER BY next_poll_at ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE talkops_durable_workflows w
                SET status = 'running',
                    lease_owner = %s,
                    lease_expires_at = %s,
                    updated_at = %s
                FROM claimed c
                WHERE w.workflow_id = c.workflow_id
                RETURNING w.*
                """,
                (now, now, limit, worker_id, lease_until, now),
            )
            rows = await cur.fetchall()
        return [self._row_to_workflow(r) for r in rows]

    async def update_durable_workflow(self, job: DurableWorkflowJob) -> None:
        """Execute `update_durable_workflow`."""
        job.updated_at = time.time()
        await self.enqueue_durable_workflow(job)

    async def get_durable_workflow(self, workflow_id: str) -> DurableWorkflowJob | None:
        """Return `get_durable_workflow` result."""
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM talkops_durable_workflows WHERE workflow_id = %s",
                (workflow_id,),
            )
            row = await cur.fetchone()
        return self._row_to_workflow(row) if row is not None else None

    async def save_autoqa_score(self, score: AutoQAScoreRecord) -> None:
        """Execute `save_autoqa_score`."""
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO talkops_autoqa_scores (
                    thread_id, tenant_id, plugin_name, plugin_version,
                    stage0_passed, stage0_violations, judge_tier, self_confidence,
                    overall_score, criterion_citations, resolution_mode,
                    dispute_status, feedback_notes, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s, %s, %s
                )
                ON CONFLICT (thread_id) DO UPDATE SET
                    plugin_name = EXCLUDED.plugin_name,
                    plugin_version = EXCLUDED.plugin_version,
                    stage0_passed = EXCLUDED.stage0_passed,
                    stage0_violations = EXCLUDED.stage0_violations,
                    judge_tier = EXCLUDED.judge_tier,
                    self_confidence = EXCLUDED.self_confidence,
                    overall_score = EXCLUDED.overall_score,
                    criterion_citations = EXCLUDED.criterion_citations,
                    resolution_mode = EXCLUDED.resolution_mode,
                    dispute_status = EXCLUDED.dispute_status,
                    feedback_notes = EXCLUDED.feedback_notes
                """,
                (
                    score.thread_id,
                    score.tenant_id,
                    score.plugin_name,
                    score.plugin_version,
                    score.stage0_passed,
                    json.dumps(score.stage0_violations),
                    score.judge_tier,
                    score.self_confidence,
                    score.overall_score,
                    json.dumps(score.criterion_citations),
                    score.resolution_mode,
                    score.dispute_status,
                    score.feedback_notes,
                    score.created_at,
                ),
            )

    async def get_autoqa_scores(
        self,
        *,
        thread_id: str | None = None,
        plugin_name: str | None = None,
        plugin_version: str | None = None,
        limit: int = 50,
        tenant_id: str = "default",
    ) -> list[AutoQAScoreRecord]:
        """Return `get_autoqa_scores` result."""
        pool = await self._ensure_pool()
        where = ["tenant_id = %s"]
        params: list[Any] = [tenant_id]
        if thread_id:
            where.append("thread_id = %s")
            params.append(thread_id)
        if plugin_name:
            where.append("plugin_name = %s")
            params.append(plugin_name)
        if plugin_version:
            where.append("plugin_version = %s")
            params.append(plugin_version)
        params.append(limit)
        async with pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM talkops_autoqa_scores WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT %s",
                tuple(params),
            )
            rows = await cur.fetchall()
        return [
            AutoQAScoreRecord(
                thread_id=r["thread_id"],
                tenant_id=r["tenant_id"],
                plugin_name=r["plugin_name"],
                plugin_version=r["plugin_version"],
                stage0_passed=bool(r["stage0_passed"]),
                stage0_violations=list(r["stage0_violations"] or []),
                judge_tier=cast("Any", r["judge_tier"]),
                self_confidence=float(r["self_confidence"]),
                overall_score=float(r["overall_score"]),
                criterion_citations=dict(r["criterion_citations"] or {}),
                resolution_mode=cast("Any", r["resolution_mode"]),
                dispute_status=cast("Any", r["dispute_status"]),
                feedback_notes=r["feedback_notes"],
                created_at=float(r["created_at"]),
            )
            for r in rows
        ]

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

"""Zero-config WAL-mode SQLite OperationalStore with FTS5 + Vector RRF hybrid search.

Used automatically in standalone TUI/CLI mode or single-node self-hosted deployments
when ``TALKOPS_POSTGRES_URI`` is unset, preventing concurrent flat-JSONL corruption.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import aiosqlite

from talkops.storage.protocol import (
    AutoQAScoreRecord,
    ChangeEventRecord,
    CorrelatedAlertItem,
    DurableWorkflowJob,
    IncidentRecord,
    MemoryRuleRecord,
    TopologyEdge,
    TopologyNode,
    compute_rrf_scores,
    cosine_similarity,
    deterministic_local_embedding,
)
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

_FTS_SAFE_RE = re.compile(r"[a-zA-Z0-9_]+")


def _to_fts5_match_query(raw: str) -> str:
    """Convert arbitrary alert/error text into a safe OR-joined SQLite FTS5 query."""
    tokens = _FTS_SAFE_RE.findall(raw)
    if not tokens:
        return ""
    unique = list(dict.fromkeys(tok for tok in tokens if len(tok) >= 2))[:24]
    return " OR ".join(f'"{tok}"' for tok in unique)


class SqliteOperationalStore:
    """Async WAL-mode SQLite implementation of ``OperationalStore``."""

    def __init__(self, db_path: str | Path) -> None:
        """Initialize SqliteOperationalStore."""
        self._db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._initialized = False

    @property
    def db_path(self) -> Path:
        """Return underlying SQLite database path."""
        return self._db_path

    @staticmethod
    def _conn_worker_thread(conn: Any) -> threading.Thread | None:
        worker = getattr(conn, "_thread", None)
        if isinstance(worker, threading.Thread):
            return worker
        if isinstance(conn, threading.Thread):
            return conn
        return None

    def _is_conn_alive(self) -> bool:
        conn = self._conn
        if conn is None:
            return False
        if not getattr(conn, "_running", False) or getattr(conn, "_connection", None) is None:
            return False
        worker = self._conn_worker_thread(conn)
        return bool(worker is not None and worker.is_alive())

    async def _ensure_conn(self) -> aiosqlite.Connection:
        if self._conn is not None and self._initialized and self._is_conn_alive():
            return self._conn
        async with self._lock:
            if self._conn is not None and not self._is_conn_alive():
                with contextlib.suppress(Exception):
                    self._conn.stop()
                self._conn = None
                self._initialized = False
            if self._conn is None:
                if str(self._db_path) != ":memory:":
                    self._db_path.parent.mkdir(parents=True, exist_ok=True)
                pending_conn = aiosqlite.connect(str(self._db_path))
                if isinstance(pending_conn, threading.Thread):
                    pending_conn.daemon = True
                worker = getattr(pending_conn, "_thread", None)
                if worker is not None:
                    worker.daemon = True
                conn = await pending_conn
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA journal_mode=WAL;")
                await conn.execute("PRAGMA synchronous=NORMAL;")
                await conn.execute("PRAGMA busy_timeout=5000;")
                await conn.execute("PRAGMA foreign_keys=ON;")
                self._conn = conn
            if not self._initialized:
                await self._create_schema(self._conn)
                self._initialized = True
            return self._conn

    async def setup(self) -> None:
        """Initialize WAL mode, tables, and FTS5 virtual indexes."""
        await self._ensure_conn()

    async def aclose(self) -> None:
        """Close the SQLite connection cleanly."""
        async with self._lock:
            conn = self._conn
            self._conn = None
            self._initialized = False
            if conn is not None:
                with contextlib.suppress(Exception):
                    await conn.close()
                worker = self._conn_worker_thread(conn)
                if worker is not None and worker.is_alive():
                    with contextlib.suppress(RuntimeError):
                        await asyncio.to_thread(worker.join, 2.0)

    def close(self) -> None:
        """Synchronously stop the SQLite connection and join its worker thread."""
        conn = self._conn
        self._conn = None
        self._initialized = False
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.stop()
            worker = self._conn_worker_thread(conn)
            if (
                worker is not None
                and worker.is_alive()
                and worker is not threading.current_thread()
            ):
                with contextlib.suppress(RuntimeError):
                    worker.join(timeout=2.0)

    async def _create_schema(self, conn: aiosqlite.Connection) -> None:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS talkops_topology_nodes (
                tenant_id TEXT NOT NULL DEFAULT 'default',
                node_id TEXT NOT NULL,
                node_type TEXT NOT NULL,
                service_name TEXT NOT NULL DEFAULT '',
                environment TEXT NOT NULL DEFAULT 'production',
                correlation_tags TEXT NOT NULL DEFAULT '{}',
                attributes TEXT NOT NULL DEFAULT '{}',
                baseline_stats TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL,
                PRIMARY KEY (tenant_id, node_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sqlite_topo_nodes_service
                ON talkops_topology_nodes (tenant_id, service_name);

            CREATE TABLE IF NOT EXISTS talkops_topology_edges (
                tenant_id TEXT NOT NULL DEFAULT 'default',
                src_id TEXT NOT NULL,
                dst_id TEXT NOT NULL,
                rel_type TEXT NOT NULL,
                lineage_source TEXT NOT NULL DEFAULT 'manual',
                last_seen_at REAL NOT NULL,
                PRIMARY KEY (tenant_id, src_id, dst_id, rel_type)
            );
            CREATE INDEX IF NOT EXISTS idx_sqlite_topo_edges_dst
                ON talkops_topology_edges (tenant_id, dst_id);

            CREATE TABLE IF NOT EXISTS talkops_change_events (
                event_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                service_name TEXT NOT NULL,
                environment TEXT NOT NULL DEFAULT 'production',
                source TEXT NOT NULL,
                commit_sha TEXT NOT NULL DEFAULT '',
                pr_url TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                diff_summary TEXT NOT NULL DEFAULT '',
                git_tree_hash TEXT NOT NULL DEFAULT '',
                occurred_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sqlite_changes_srv_time
                ON talkops_change_events (tenant_id, service_name, occurred_at DESC);

            CREATE TABLE IF NOT EXISTS talkops_incidents (
                incident_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                thread_id TEXT NOT NULL,
                service_name TEXT NOT NULL,
                environment TEXT NOT NULL DEFAULT 'production',
                alert_fingerprint TEXT NOT NULL DEFAULT '',
                symptoms_text TEXT NOT NULL DEFAULT '',
                primary_root_cause TEXT NOT NULL,
                contributing_factors TEXT NOT NULL DEFAULT '[]',
                ruled_out_hypotheses TEXT NOT NULL DEFAULT '[]',
                remediation_action TEXT NOT NULL DEFAULT '{}',
                verification_status TEXT NOT NULL DEFAULT 'unverified',
                confidence_weight REAL NOT NULL DEFAULT 1.0,
                git_tree_hash TEXT NOT NULL DEFAULT '',
                embedding_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sqlite_incidents_srv
                ON talkops_incidents (tenant_id, service_name, verification_status);

            CREATE VIRTUAL TABLE IF NOT EXISTS talkops_incidents_fts USING fts5(
                incident_id UNINDEXED,
                tenant_id UNINDEXED,
                searchable_text
            );

            CREATE TABLE IF NOT EXISTS talkops_memory_rules (
                rule_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                scope_service TEXT NOT NULL DEFAULT '*',
                memory_type TEXT NOT NULL DEFAULT 'episodic_constraint',
                rule_text TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'reflection_observer',
                confirmation_count INTEGER NOT NULL DEFAULT 1,
                hit_count INTEGER NOT NULL DEFAULT 0,
                helped_count INTEGER NOT NULL DEFAULT 0,
                misled_count INTEGER NOT NULL DEFAULT 0,
                confidence_weight REAL NOT NULL DEFAULT 1.0,
                active INTEGER NOT NULL DEFAULT 1,
                expires_at REAL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sqlite_rules_scope
                ON talkops_memory_rules (tenant_id, scope_service, active);

            CREATE TABLE IF NOT EXISTS talkops_correlated_alerts (
                alert_row_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                provider TEXT NOT NULL,
                dedup_key TEXT NOT NULL,
                service_name TEXT NOT NULL DEFAULT '',
                environment TEXT NOT NULL DEFAULT 'production',
                normalized_event TEXT NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0,
                arrived_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sqlite_corr_inbox
                ON talkops_correlated_alerts (thread_id, consumed, arrived_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sqlite_corr_dedup
                ON talkops_correlated_alerts (thread_id, dedup_key);

            CREATE TABLE IF NOT EXISTS talkops_durable_workflows (
                workflow_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                workflow_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                phase TEXT NOT NULL DEFAULT 'grace_crash',
                poll_interval_sec REAL NOT NULL DEFAULT 15.0,
                next_poll_at REAL NOT NULL,
                started_at REAL NOT NULL,
                deadline_at REAL NOT NULL,
                consecutive_passes INTEGER NOT NULL DEFAULT 0,
                spec_json TEXT NOT NULL DEFAULT '{}',
                tnr_undo_json TEXT NOT NULL DEFAULT '{}',
                sink_refs TEXT NOT NULL DEFAULT '{}',
                lease_owner TEXT,
                lease_expires_at REAL,
                result_json TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sqlite_workflows_due
                ON talkops_durable_workflows (status, next_poll_at);

            CREATE TABLE IF NOT EXISTS talkops_autoqa_scores (
                thread_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                plugin_name TEXT NOT NULL DEFAULT 'core',
                plugin_version TEXT NOT NULL DEFAULT 'default',
                stage0_passed INTEGER NOT NULL,
                stage0_violations TEXT NOT NULL DEFAULT '[]',
                judge_tier TEXT NOT NULL DEFAULT 'fast',
                self_confidence REAL NOT NULL DEFAULT 1.0,
                overall_score REAL NOT NULL,
                criterion_citations TEXT NOT NULL DEFAULT '{}',
                resolution_mode TEXT NOT NULL DEFAULT 'agent_assisted',
                dispute_status TEXT NOT NULL DEFAULT 'none',
                feedback_notes TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sqlite_autoqa_plugin
                ON talkops_autoqa_scores (tenant_id, plugin_name, created_at DESC);
            """
        )
        await conn.commit()

    # ── Topology & Change Events ─────────────────────────────────────────────

    async def upsert_topology_node(self, node: TopologyNode) -> None:
        """Execute `upsert_topology_node`."""
        conn = await self._ensure_conn()
        await conn.execute(
            """
            INSERT INTO talkops_topology_nodes (
                tenant_id, node_id, node_type, service_name, environment,
                correlation_tags, attributes, baseline_stats, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, node_id) DO UPDATE SET
                node_type = excluded.node_type,
                service_name = excluded.service_name,
                environment = excluded.environment,
                correlation_tags = excluded.correlation_tags,
                attributes = excluded.attributes,
                baseline_stats = excluded.baseline_stats,
                updated_at = excluded.updated_at
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
        await conn.commit()

    async def upsert_topology_edge(self, edge: TopologyEdge) -> None:
        """Execute `upsert_topology_edge`."""
        conn = await self._ensure_conn()
        await conn.execute(
            """
            INSERT INTO talkops_topology_edges (
                tenant_id, src_id, dst_id, rel_type, lineage_source, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, src_id, dst_id, rel_type) DO UPDATE SET
                lineage_source = excluded.lineage_source,
                last_seen_at = excluded.last_seen_at
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
        await conn.commit()

    async def get_topology_neighbors(
        self,
        node_id: str,
        *,
        max_depth: int = 1,
        tenant_id: str = "default",
    ) -> list[TopologyNode]:
        """Return `get_topology_neighbors` result."""
        bounded_depth = max(1, min(int(max_depth), 4))
        conn = await self._ensure_conn()
        cursor = await conn.execute(
            """
            WITH RECURSIVE walk(nid, depth) AS (
                SELECT ?, 0
                UNION
                SELECT CASE WHEN e.src_id = w.nid THEN e.dst_id ELSE e.src_id END,
                       w.depth + 1
                FROM talkops_topology_edges e
                JOIN walk w ON (e.src_id = w.nid OR e.dst_id = w.nid)
                WHERE e.tenant_id = ? AND w.depth < ?
            )
            SELECT DISTINCT n.*
            FROM talkops_topology_nodes n
            JOIN walk w ON n.node_id = w.nid
            WHERE n.tenant_id = ? AND n.node_id != ?
            """,
            (node_id, tenant_id, bounded_depth, tenant_id, node_id),
        )
        rows = await cursor.fetchall()
        return [
            TopologyNode(
                tenant_id=row["tenant_id"],
                node_id=row["node_id"],
                node_type=row["node_type"],
                service_name=row["service_name"],
                environment=row["environment"],
                correlation_tags=json.loads(row["correlation_tags"] or "{}"),
                attributes=json.loads(row["attributes"] or "{}"),
                baseline_stats=json.loads(row["baseline_stats"] or "{}"),
                updated_at=float(row["updated_at"]),
            )
            for row in rows
        ]

    async def record_change_event(self, event: ChangeEventRecord) -> None:
        """Execute `record_change_event`."""
        conn = await self._ensure_conn()
        await conn.execute(
            """
            INSERT INTO talkops_change_events (
                event_id, tenant_id, service_name, environment, source,
                commit_sha, pr_url, author, diff_summary, git_tree_hash, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                diff_summary = excluded.diff_summary,
                git_tree_hash = excluded.git_tree_hash,
                occurred_at = excluded.occurred_at
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
        await conn.commit()

    async def list_recent_changes(
        self,
        service_name: str,
        *,
        since_ts: float,
        until_ts: float | None = None,
        tenant_id: str = "default",
    ) -> list[ChangeEventRecord]:
        """Return `list_recent_changes` result."""
        conn = await self._ensure_conn()
        upper = until_ts if until_ts is not None else (time.time() + 300.0)
        cursor = await conn.execute(
            """
            SELECT * FROM talkops_change_events
            WHERE tenant_id = ? AND service_name = ? AND occurred_at >= ? AND occurred_at <= ?
            ORDER BY occurred_at DESC
            LIMIT 25
            """,
            (tenant_id, service_name, since_ts, upper),
        )
        rows = await cursor.fetchall()
        return [
            ChangeEventRecord(
                event_id=row["event_id"],
                tenant_id=row["tenant_id"],
                service_name=row["service_name"],
                environment=row["environment"],
                source=row["source"],
                commit_sha=row["commit_sha"],
                pr_url=row["pr_url"],
                author=row["author"],
                diff_summary=row["diff_summary"],
                git_tree_hash=row["git_tree_hash"],
                occurred_at=float(row["occurred_at"]),
            )
            for row in rows
        ]

    # ── Calibrated Incident Store (FTS5 + Vector RRF) ────────────────────────

    async def upsert_incident(self, incident: IncidentRecord) -> None:
        """Execute `upsert_incident`."""
        conn = await self._ensure_conn()
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
            else deterministic_local_embedding(searchable)
        )
        async with self._lock:
            await conn.execute(
                """
                INSERT INTO talkops_incidents (
                    incident_id, tenant_id, thread_id, service_name, environment,
                    alert_fingerprint, symptoms_text, primary_root_cause,
                    contributing_factors, ruled_out_hypotheses, remediation_action,
                    verification_status, confidence_weight, git_tree_hash,
                    embedding_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(incident_id) DO UPDATE SET
                    primary_root_cause = excluded.primary_root_cause,
                    contributing_factors = excluded.contributing_factors,
                    ruled_out_hypotheses = excluded.ruled_out_hypotheses,
                    remediation_action = excluded.remediation_action,
                    verification_status = excluded.verification_status,
                    confidence_weight = excluded.confidence_weight,
                    git_tree_hash = excluded.git_tree_hash,
                    embedding_json = excluded.embedding_json,
                    updated_at = excluded.updated_at
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
                    json.dumps(emb),
                    incident.created_at,
                    incident.updated_at,
                ),
            )
            await conn.execute(
                "DELETE FROM talkops_incidents_fts WHERE incident_id = ?",
                (incident.incident_id,),
            )
            await conn.execute(
                "INSERT INTO talkops_incidents_fts (incident_id, tenant_id, searchable_text) VALUES (?, ?, ?)",
                (incident.incident_id, incident.tenant_id, searchable),
            )
            await conn.commit()

    def _row_to_incident(self, row: aiosqlite.Row, *, rrf_score: float = 0.0) -> IncidentRecord:
        emb_raw = row["embedding_json"]
        return IncidentRecord(
            incident_id=row["incident_id"],
            tenant_id=row["tenant_id"],
            thread_id=row["thread_id"],
            service_name=row["service_name"],
            environment=row["environment"],
            alert_fingerprint=row["alert_fingerprint"],
            symptoms_text=row["symptoms_text"],
            primary_root_cause=row["primary_root_cause"],
            contributing_factors=json.loads(row["contributing_factors"] or "[]"),
            ruled_out_hypotheses=json.loads(row["ruled_out_hypotheses"] or "[]"),
            remediation_action=json.loads(row["remediation_action"] or "{}"),
            verification_status=cast("Any", row["verification_status"]),
            confidence_weight=float(row["confidence_weight"]),
            git_tree_hash=row["git_tree_hash"],
            embedding=json.loads(emb_raw) if emb_raw else None,
            rrf_score=rrf_score,
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
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
        conn = await self._ensure_conn()
        where = ["tenant_id = ?", "confidence_weight >= ?"]
        params: list[Any] = [tenant_id, min_confidence]
        if verified_only:
            where.append(
                "verification_status IN ('slo_verified', 'autoqa_verified', 'human_confirmed')"
            )
        if service_name:
            where.append("service_name = ?")
            params.append(service_name)

        sql = f"SELECT * FROM talkops_incidents WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT 250"
        cursor = await conn.execute(sql, tuple(params))
        rows = await cursor.fetchall()
        if not rows:
            return []

        by_id: dict[str, aiosqlite.Row] = {row["incident_id"]: row for row in rows}
        weights: dict[str, float] = {
            inc_id: float(r["confidence_weight"]) for inc_id, r in by_id.items()
        }

        # Arm 1: Semantic vector ranking
        q_emb = (
            list(query_embedding)
            if query_embedding is not None
            else deterministic_local_embedding(query_text)
        )
        vec_scored: list[tuple[str, float]] = []
        for inc_id, row in by_id.items():
            raw_emb = row["embedding_json"]
            if raw_emb:
                cand_emb = json.loads(raw_emb)
                sim = cosine_similarity(q_emb, cand_emb)
                if sim > 0.0:
                    vec_scored.append((inc_id, sim))
        vec_scored.sort(key=lambda item: item[1], reverse=True)
        vec_ranked = [inc_id for inc_id, _ in vec_scored[:20]]

        # Arm 2: FTS5 BM25 lexical ranking
        fts_ranked: list[str] = []
        fts_query = _to_fts5_match_query(query_text)
        if fts_query:
            fts_cursor = await conn.execute(
                """
                SELECT incident_id
                FROM talkops_incidents_fts
                WHERE talkops_incidents_fts MATCH ? AND tenant_id = ?
                ORDER BY bm25(talkops_incidents_fts)
                LIMIT 20
                """,
                (fts_query, tenant_id),
            )
            fts_rows = await fts_cursor.fetchall()
            fts_ranked = [r["incident_id"] for r in fts_rows if r["incident_id"] in by_id]

        fused = compute_rrf_scores(vec_ranked, fts_ranked, k=rrf_k, weights_by_id=weights)
        results: list[IncidentRecord] = []
        for inc_id, score in fused[:top_k]:
            row = by_id.get(inc_id)
            if row is not None:
                results.append(self._row_to_incident(row, rrf_score=score))
        return results

    async def apply_incident_git_decay(
        self,
        service_name: str,
        new_git_tree_hash: str,
        *,
        decay_factor: float = 0.65,
        tenant_id: str = "default",
    ) -> int:
        """Execute `apply_incident_git_decay`."""
        conn = await self._ensure_conn()
        cursor = await conn.execute(
            """
            UPDATE talkops_incidents
            SET confidence_weight = confidence_weight * ?,
                updated_at = ?
            WHERE tenant_id = ?
              AND service_name = ?
              AND git_tree_hash != ''
              AND git_tree_hash != ?
            """,
            (decay_factor, time.time(), tenant_id, service_name, new_git_tree_hash),
        )
        await conn.commit()
        return int(cursor.rowcount or 0)

    # ── Calibrated Memory Rules ──────────────────────────────────────────────

    async def upsert_memory_rule(self, rule: MemoryRuleRecord) -> None:
        """Execute `upsert_memory_rule`."""
        conn = await self._ensure_conn()
        await conn.execute(
            """
            INSERT INTO talkops_memory_rules (
                rule_id, tenant_id, scope_service, memory_type, rule_text, source,
                confirmation_count, hit_count, helped_count, misled_count,
                confidence_weight, active, expires_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rule_id) DO UPDATE SET
                rule_text = excluded.rule_text,
                confirmation_count = excluded.confirmation_count,
                confidence_weight = excluded.confidence_weight,
                active = excluded.active,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at
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
                1 if rule.active else 0,
                rule.expires_at,
                rule.updated_at,
            ),
        )
        await conn.commit()

    async def list_active_memory_rules(
        self,
        *,
        service_name: str | None = None,
        now_ts: float | None = None,
        tenant_id: str = "default",
    ) -> list[MemoryRuleRecord]:
        """Return `list_active_memory_rules` result."""
        conn = await self._ensure_conn()
        now = now_ts if now_ts is not None else time.time()
        if service_name:
            cursor = await conn.execute(
                """
                SELECT * FROM talkops_memory_rules
                WHERE tenant_id = ? AND active = 1
                  AND (expires_at IS NULL OR expires_at > ?)
                  AND (scope_service = '*' OR scope_service = ?)
                ORDER BY confidence_weight DESC, updated_at DESC
                LIMIT 25
                """,
                (tenant_id, now, service_name),
            )
        else:
            cursor = await conn.execute(
                """
                SELECT * FROM talkops_memory_rules
                WHERE tenant_id = ? AND active = 1
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY confidence_weight DESC, updated_at DESC
                LIMIT 25
                """,
                (tenant_id, now),
            )
        rows = await cursor.fetchall()
        return [
            MemoryRuleRecord(
                rule_id=row["rule_id"],
                tenant_id=row["tenant_id"],
                scope_service=row["scope_service"],
                memory_type=cast("Any", row["memory_type"]),
                rule_text=row["rule_text"],
                source=row["source"],
                confirmation_count=int(row["confirmation_count"]),
                hit_count=int(row["hit_count"]),
                helped_count=int(row["helped_count"]),
                misled_count=int(row["misled_count"]),
                confidence_weight=float(row["confidence_weight"]),
                active=bool(row["active"]),
                expires_at=float(row["expires_at"]) if row["expires_at"] is not None else None,
                updated_at=float(row["updated_at"]),
            )
            for row in rows
        ]

    async def record_memory_rule_outcome(
        self,
        rule_id: str,
        *,
        helped: bool,
    ) -> None:
        """Execute `record_memory_rule_outcome`."""
        conn = await self._ensure_conn()
        if helped:
            await conn.execute(
                """
                UPDATE talkops_memory_rules
                SET hit_count = hit_count + 1,
                    helped_count = helped_count + 1,
                    confidence_weight = MIN(2.0, confidence_weight * 1.10),
                    updated_at = ?
                WHERE rule_id = ?
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
                    active = CASE WHEN confidence_weight * 0.50 < 0.25 THEN 0 ELSE active END,
                    updated_at = ?
                WHERE rule_id = ?
                """,
                (time.time(), rule_id),
            )
        await conn.commit()

    # ── Sidecar Alert Inbox (Storm Drain) ────────────────────────────────────

    async def enqueue_correlated_alert(self, item: CorrelatedAlertItem) -> bool:
        """Execute `enqueue_correlated_alert`."""
        conn = await self._ensure_conn()
        async with self._lock:
            cursor = await conn.execute(
                """
                INSERT OR IGNORE INTO talkops_correlated_alerts (
                    alert_row_id, thread_id, tenant_id, provider, dedup_key,
                    service_name, environment, normalized_event, consumed, arrived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    1 if item.consumed else 0,
                    item.arrived_at,
                ),
            )
            await conn.commit()
            return bool(cursor.rowcount and cursor.rowcount > 0)

    async def drain_correlated_alerts(self, thread_id: str) -> list[CorrelatedAlertItem]:
        """Execute `drain_correlated_alerts`."""
        conn = await self._ensure_conn()
        async with self._lock:
            cursor = await conn.execute(
                """
                SELECT * FROM talkops_correlated_alerts
                WHERE thread_id = ? AND consumed = 0
                ORDER BY arrived_at ASC
                """,
                (thread_id,),
            )
            rows = await cursor.fetchall()
            if not rows:
                return []
            ids = [row["alert_row_id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            await conn.execute(
                f"UPDATE talkops_correlated_alerts SET consumed = 1 WHERE alert_row_id IN ({placeholders})",
                tuple(ids),
            )
            await conn.commit()

        return [
            CorrelatedAlertItem(
                alert_row_id=row["alert_row_id"],
                thread_id=row["thread_id"],
                tenant_id=row["tenant_id"],
                provider=row["provider"],
                dedup_key=row["dedup_key"],
                service_name=row["service_name"],
                environment=row["environment"],
                normalized_event=json.loads(row["normalized_event"] or "{}"),
                consumed=True,
                arrived_at=float(row["arrived_at"]),
            )
            for row in rows
        ]

    async def peek_unconsumed_alert_count(self, thread_id: str) -> int:
        """Execute `peek_unconsumed_alert_count`."""
        conn = await self._ensure_conn()
        cursor = await conn.execute(
            "SELECT COUNT(*) AS cnt FROM talkops_correlated_alerts WHERE thread_id = ? AND consumed = 0",
            (thread_id,),
        )
        row = await cursor.fetchone()
        return int(row["cnt"]) if row else 0

    # ── Durable Workflows ────────────────────────────────────────────────────

    def _row_to_workflow(self, row: aiosqlite.Row) -> DurableWorkflowJob:
        return DurableWorkflowJob(
            workflow_id=row["workflow_id"],
            thread_id=row["thread_id"],
            tenant_id=row["tenant_id"],
            workflow_type=cast("Any", row["workflow_type"]),
            status=cast("Any", row["status"]),
            phase=cast("Any", row["phase"]),
            poll_interval_sec=float(row["poll_interval_sec"]),
            next_poll_at=float(row["next_poll_at"]),
            started_at=float(row["started_at"]),
            deadline_at=float(row["deadline_at"]),
            consecutive_passes=int(row["consecutive_passes"]),
            spec_json=json.loads(row["spec_json"] or "{}"),
            tnr_undo_json=json.loads(row["tnr_undo_json"] or "{}"),
            sink_refs=json.loads(row["sink_refs"] or "{}"),
            lease_owner=row["lease_owner"],
            lease_expires_at=float(row["lease_expires_at"])
            if row["lease_expires_at"] is not None
            else None,
            result_json=json.loads(row["result_json"] or "{}"),
            updated_at=float(row["updated_at"]),
        )

    async def enqueue_durable_workflow(self, job: DurableWorkflowJob) -> None:
        """Execute `enqueue_durable_workflow`."""
        conn = await self._ensure_conn()
        async with self._lock:
            await conn.execute(
                """
                INSERT INTO talkops_durable_workflows (
                    workflow_id, thread_id, tenant_id, workflow_type, status, phase,
                    poll_interval_sec, next_poll_at, started_at, deadline_at,
                    consecutive_passes, spec_json, tnr_undo_json, sink_refs,
                    lease_owner, lease_expires_at, result_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    status = excluded.status,
                    phase = excluded.phase,
                    poll_interval_sec = excluded.poll_interval_sec,
                    next_poll_at = excluded.next_poll_at,
                    consecutive_passes = excluded.consecutive_passes,
                    spec_json = excluded.spec_json,
                    tnr_undo_json = excluded.tnr_undo_json,
                    sink_refs = excluded.sink_refs,
                    lease_owner = excluded.lease_owner,
                    lease_expires_at = excluded.lease_expires_at,
                    result_json = excluded.result_json,
                    updated_at = excluded.updated_at
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
            await conn.commit()

    async def claim_due_workflows(
        self,
        worker_id: str,
        *,
        now_ts: float | None = None,
        lease_seconds: float = 45.0,
        limit: int = 10,
    ) -> list[DurableWorkflowJob]:
        """Execute `claim_due_workflows`."""
        conn = await self._ensure_conn()
        now = now_ts if now_ts is not None else time.time()
        lease_until = now + lease_seconds
        async with self._lock:
            cursor = await conn.execute(
                """
                SELECT * FROM talkops_durable_workflows
                WHERE status IN ('pending', 'running')
                  AND next_poll_at <= ?
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                ORDER BY next_poll_at ASC
                LIMIT ?
                """,
                (now, now, limit),
            )
            rows = await cursor.fetchall()
            if not rows:
                return []
            ids = [row["workflow_id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            await conn.execute(
                f"""
                UPDATE talkops_durable_workflows
                SET status = 'running',
                    lease_owner = ?,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE workflow_id IN ({placeholders})
                """,
                (worker_id, lease_until, now, *ids),
            )
            await conn.commit()

            claimed_cursor = await conn.execute(
                f"SELECT * FROM talkops_durable_workflows WHERE workflow_id IN ({placeholders})",
                tuple(ids),
            )
            claimed_rows = await claimed_cursor.fetchall()
        return [self._row_to_workflow(r) for r in claimed_rows]

    async def update_durable_workflow(self, job: DurableWorkflowJob) -> None:
        """Execute `update_durable_workflow`."""
        job.updated_at = time.time()
        await self.enqueue_durable_workflow(job)

    async def get_durable_workflow(self, workflow_id: str) -> DurableWorkflowJob | None:
        """Return `get_durable_workflow` result."""
        conn = await self._ensure_conn()
        cursor = await conn.execute(
            "SELECT * FROM talkops_durable_workflows WHERE workflow_id = ?",
            (workflow_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_workflow(row) if row is not None else None

    # ── AutoQA Scorecards ────────────────────────────────────────────────────

    async def save_autoqa_score(self, score: AutoQAScoreRecord) -> None:
        """Execute `save_autoqa_score`."""
        conn = await self._ensure_conn()
        await conn.execute(
            """
            INSERT INTO talkops_autoqa_scores (
                thread_id, tenant_id, plugin_name, plugin_version,
                stage0_passed, stage0_violations, judge_tier, self_confidence,
                overall_score, criterion_citations, resolution_mode,
                dispute_status, feedback_notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                plugin_name = excluded.plugin_name,
                plugin_version = excluded.plugin_version,
                stage0_passed = excluded.stage0_passed,
                stage0_violations = excluded.stage0_violations,
                judge_tier = excluded.judge_tier,
                self_confidence = excluded.self_confidence,
                overall_score = excluded.overall_score,
                criterion_citations = excluded.criterion_citations,
                resolution_mode = excluded.resolution_mode,
                dispute_status = excluded.dispute_status,
                feedback_notes = excluded.feedback_notes
            """,
            (
                score.thread_id,
                score.tenant_id,
                score.plugin_name,
                score.plugin_version,
                1 if score.stage0_passed else 0,
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
        await conn.commit()

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
        conn = await self._ensure_conn()
        where = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if thread_id:
            where.append("thread_id = ?")
            params.append(thread_id)
        if plugin_name:
            where.append("plugin_name = ?")
            params.append(plugin_name)
        if plugin_version:
            where.append("plugin_version = ?")
            params.append(plugin_version)
        params.append(limit)
        cursor = await conn.execute(
            f"SELECT * FROM talkops_autoqa_scores WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )
        rows = await cursor.fetchall()
        return [
            AutoQAScoreRecord(
                thread_id=row["thread_id"],
                tenant_id=row["tenant_id"],
                plugin_name=row["plugin_name"],
                plugin_version=row["plugin_version"],
                stage0_passed=bool(row["stage0_passed"]),
                stage0_violations=json.loads(row["stage0_violations"] or "[]"),
                judge_tier=cast("Any", row["judge_tier"]),
                self_confidence=float(row["self_confidence"]),
                overall_score=float(row["overall_score"]),
                criterion_citations=json.loads(row["criterion_citations"] or "{}"),
                resolution_mode=cast("Any", row["resolution_mode"]),
                dispute_status=cast("Any", row["dispute_status"]),
                feedback_notes=row["feedback_notes"],
                created_at=float(row["created_at"]),
            )
            for row in rows
        ]

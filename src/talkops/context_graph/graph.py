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

"""Generic Multi-Environment Operational ContextGraph (K8s, Cloud, DB, Bare-Metal, OTel, IaC)."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from talkops.storage.protocol import (
    ChangeEventRecord,
    OperationalStore,
    TopologyEdge,
    TopologyNode,
)


@dataclass(slots=True)
class BlastRadiusReport:
    """Topology & recent-change correlation snapshot for an alerting service or resource."""

    focus_id: str
    service_name: str
    neighbors: list[TopologyNode] = field(default_factory=list)
    recent_changes: list[ChangeEventRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return `to_dict` result."""
        return {
            "focus_id": self.focus_id,
            "service_name": self.service_name,
            "neighbors": [
                {
                    "node_id": n.node_id,
                    "node_type": n.node_type,
                    "service_name": n.service_name,
                    "environment": n.environment,
                    "correlation_tags": n.correlation_tags,
                }
                for n in self.neighbors
            ],
            "recent_changes": [
                {
                    "event_id": c.event_id,
                    "service_name": c.service_name,
                    "source": c.source,
                    "commit_sha": c.commit_sha,
                    "pr_url": c.pr_url,
                    "author": c.author,
                    "diff_summary": c.diff_summary,
                    "occurred_at": c.occurred_at,
                }
                for c in self.recent_changes
            ],
        }


class ContextGraph:
    """Domain-agnostic topology & blast-radius engine backed by ``OperationalStore``."""

    def __init__(self, store: OperationalStore) -> None:
        """Initialize ContextGraph."""
        self.store = store

    async def upsert_resource(
        self,
        *,
        node_id: str,
        node_type: str,
        service_name: str = "",
        environment: str = "production",
        tenant_id: str = "default",
        correlation_tags: Mapping[str, Any] | None = None,
        attributes: Mapping[str, Any] | None = None,
        baseline_stats: Mapping[str, Any] | None = None,
    ) -> TopologyNode:
        """Execute `upsert_resource`."""
        node = TopologyNode(
            node_id=node_id,
            node_type=node_type,
            service_name=service_name or node_id.split(":", 1)[-1],
            environment=environment,
            tenant_id=tenant_id,
            correlation_tags=dict(correlation_tags or {}),
            attributes=dict(attributes or {}),
            baseline_stats=dict(baseline_stats or {}),
        )
        await self.store.upsert_topology_node(node)
        return node

    async def link_dependency(
        self,
        src_id: str,
        dst_id: str,
        rel_type: str = "depends_on",
        *,
        lineage_source: str = "manual",
        tenant_id: str = "default",
    ) -> TopologyEdge:
        """Execute `link_dependency`."""
        edge = TopologyEdge(
            src_id=src_id,
            dst_id=dst_id,
            rel_type=rel_type,
            lineage_source=lineage_source,
            tenant_id=tenant_id,
        )
        await self.store.upsert_topology_edge(edge)
        return edge

    async def ingest_otel_service_map(
        self,
        edges: Sequence[Mapping[str, Any]],
        *,
        environment: str = "production",
        tenant_id: str = "default",
    ) -> int:
        """Ingest OpenTelemetry / Jaeger / Prometheus service-graph call edges."""
        count = 0
        for raw in edges:
            client = str(raw.get("client") or raw.get("source") or raw.get("caller") or "").strip()
            server = str(raw.get("server") or raw.get("target") or raw.get("callee") or "").strip()
            if not client or not server:
                continue
            src_id = client if ":" in client else f"service:{client}"
            dst_id = server if ":" in server else f"service:{server}"
            await self.upsert_resource(
                node_id=src_id,
                node_type=str(raw.get("source_type") or "service"),
                service_name=client.split(":", 1)[-1],
                environment=environment,
                tenant_id=tenant_id,
            )
            await self.upsert_resource(
                node_id=dst_id,
                node_type=str(raw.get("target_type") or "service"),
                service_name=server.split(":", 1)[-1],
                environment=environment,
                tenant_id=tenant_id,
            )
            await self.link_dependency(
                src_id,
                dst_id,
                rel_type=str(raw.get("rel_type") or "calls"),
                lineage_source="otel_service_graph",
                tenant_id=tenant_id,
            )
            count += 1
        return count

    async def ingest_kubernetes_snapshot(
        self,
        resources: Sequence[Mapping[str, Any]],
        *,
        environment: str = "production",
        tenant_id: str = "default",
    ) -> int:
        """Ingest Kubernetes Workload/Service manifests or API objects into topology edges."""
        ingested = 0
        services_by_selector: list[tuple[str, str, dict[str, str]]] = []
        workloads_by_labels: list[tuple[str, str, str, dict[str, str]]] = []

        for obj in resources:
            kind = str(obj.get("kind") or "").strip()
            meta = obj.get("metadata") if isinstance(obj.get("metadata"), Mapping) else {}
            spec = obj.get("spec") if isinstance(obj.get("spec"), Mapping) else {}
            name = str(meta.get("name") or "").strip()
            ns = str(meta.get("namespace") or "default").strip()
            if not kind or not name:
                continue
            labels = dict(meta.get("labels") or {}) if isinstance(meta.get("labels"), Mapping) else {}
            svc_label = str(
                labels.get("app.kubernetes.io/name")
                or labels.get("app")
                or name
            )
            node_id = f"k8s:{ns}:{kind.lower()}:{name}"
            await self.upsert_resource(
                node_id=node_id,
                node_type=f"k8s_{kind.lower()}",
                service_name=svc_label,
                environment=environment,
                tenant_id=tenant_id,
                correlation_tags={"namespace": ns, **labels},
            )
            ingested += 1

            if kind.lower() == "service":
                sel = spec.get("selector")
                if isinstance(sel, Mapping) and sel:
                    services_by_selector.append((node_id, ns, {str(k): str(v) for k, v in sel.items()}))
            elif kind.lower() in {"deployment", "statefulset", "daemonset", "pod"}:
                tmpl_labels = labels
                tmpl = spec.get("template")
                if isinstance(tmpl, Mapping) and isinstance(tmpl.get("metadata"), Mapping):
                    tl = tmpl["metadata"].get("labels")
                    if isinstance(tl, Mapping):
                        tmpl_labels = {**labels, **{str(k): str(v) for k, v in tl.items()}}
                workloads_by_labels.append((node_id, ns, svc_label, tmpl_labels))

        for svc_node_id, svc_ns, selector in services_by_selector:
            for wl_node_id, wl_ns, _, wl_labels in workloads_by_labels:
                if svc_ns == wl_ns and all(wl_labels.get(k) == v for k, v in selector.items()):
                    await self.link_dependency(
                        svc_node_id,
                        wl_node_id,
                        rel_type="routes_to",
                        lineage_source="k8s_selector",
                        tenant_id=tenant_id,
                    )

        return ingested

    async def compute_blast_radius(
        self,
        service_or_node_id: str,
        *,
        max_depth: int = 2,
        change_window_sec: float = 7200.0,
        now_ts: float | None = None,
        tenant_id: str = "default",
    ) -> BlastRadiusReport:
        """Traverse 1..N-hop topology neighbors and correlate recent changes across all impacted services."""
        effective_now = now_ts if now_ts is not None else time.time()
        since_ts = effective_now - max(60.0, change_window_sec)

        focus_candidates = [service_or_node_id]
        if ":" not in service_or_node_id:
            focus_candidates.append(f"service:{service_or_node_id}")
            focus_candidates.append(f"svc:{service_or_node_id}")

        seen_nodes: dict[str, TopologyNode] = {}
        for cand in focus_candidates:
            neighbors = await self.store.get_topology_neighbors(
                cand,
                max_depth=max_depth,
                tenant_id=tenant_id,
            )
            for n in neighbors:
                seen_nodes[n.node_id] = n

        base_service = service_or_node_id.split(":")[-1]
        candidate_services: set[str] = {base_service}
        for n in seen_nodes.values():
            if n.service_name:
                candidate_services.add(n.service_name)

        all_changes: dict[str, ChangeEventRecord] = {}
        for svc in sorted(candidate_services):
            changes = await self.store.list_recent_changes(
                svc,
                since_ts=since_ts,
                until_ts=effective_now,
                tenant_id=tenant_id,
            )
            for ch in changes:
                all_changes[ch.event_id] = ch

        sorted_changes = sorted(
            all_changes.values(),
            key=lambda item: item.occurred_at,
            reverse=True,
        )
        return BlastRadiusReport(
            focus_id=service_or_node_id,
            service_name=base_service,
            neighbors=list(seen_nodes.values()),
            recent_changes=sorted_changes,
        )

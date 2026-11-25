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

"""Built-in ``ContextGraphAddon`` (`context-graph`) for TalkOps v2 Server Mode."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from talkops.addons.config import AddonSettings
from talkops.addons.protocol import (
    AddonCapabilities,
    AddonContext,
    AddonHealthStatus,
    AddonTier,
    BaseServerAddon,
)
from talkops.context_graph.change_ingestor import ChangeIngestor
from talkops.context_graph.graph import ContextGraph
from talkops.memory.incident_store import CalibratedIncidentStore
from talkops.storage.factory import get_operational_store


class ContextGraphAddonSettings(AddonSettings):
    """Configuration schema for ``ContextGraphAddon``."""

    enabled: bool = True
    change_window_sec: float = 7200.0


class ContextGraphAddon(BaseServerAddon):
    """Exposes topology ingestion, CI/CD/GitOps change correlation, and RRF incident search APIs."""

    name: str = "context-graph"
    tier: AddonTier = "oss"
    version: str = "2.0.0"
    description: str = "Multi-environment topology graph, change correlation & RRF incident retrieval."
    capabilities: AddonCapabilities = AddonCapabilities(routes=True, event_subscriber=True)
    settings_schema = ContextGraphAddonSettings
    enabled_by_default: bool = True

    def __init__(self) -> None:
        """Initialize ContextGraphAddon."""
        super().__init__()
        self.source = "builtin"
        self._graph: ContextGraph | None = None
        self._ingestor: ChangeIngestor | None = None
        self._incident_store: CalibratedIncidentStore | None = None

    def _ensure_services(self) -> tuple[ContextGraph, ChangeIngestor, CalibratedIncidentStore]:
        store = get_operational_store(
            project_root=self._ctx.project_root if self._ctx is not None else None
        )
        if self._graph is None:
            self._graph = ContextGraph(store)
        if self._ingestor is None:
            self._ingestor = ChangeIngestor(store)
        if self._incident_store is None:
            self._incident_store = CalibratedIncidentStore(
                store,
                langgraph_store=self._ctx.store if self._ctx is not None else None,
            )
        return self._graph, self._ingestor, self._incident_store

    async def on_startup(self, ctx: AddonContext) -> None:
        """Execute `on_startup`."""
        await super().on_startup(ctx)
        store = get_operational_store(project_root=ctx.project_root)
        await store.setup()
        self._ensure_services()

    def get_routes(self) -> list[BaseRoute]:
        """Return `get_routes` result."""
        async def upsert_topology_endpoint(request: Request) -> JSONResponse:
            graph, _, _ = self._ensure_services()
            body = await request.json()
            nodes = body.get("nodes") or []
            edges = body.get("edges") or []
            for n in nodes:
                await graph.upsert_resource(
                    node_id=str(n["node_id"]),
                    node_type=str(n.get("node_type") or "service"),
                    service_name=str(n.get("service_name") or ""),
                    environment=str(n.get("environment") or "production"),
                    correlation_tags=n.get("correlation_tags") or {},
                    attributes=n.get("attributes") or {},
                )
            for e in edges:
                await graph.link_dependency(
                    str(e["src_id"]),
                    str(e["dst_id"]),
                    str(e.get("rel_type") or "depends_on"),
                    lineage_source=str(e.get("lineage_source") or "api"),
                )
            return JSONResponse(
                {"status": "ok", "nodes_upserted": len(nodes), "edges_upserted": len(edges)},
                status_code=200,
            )

        async def ingest_change_endpoint(request: Request) -> JSONResponse:
            graph, ingestor, _ = self._ensure_services()
            if request.method == "GET":
                import time

                svc = str(request.query_params.get("service") or "*").strip() or "*"
                src_filter = str(request.query_params.get("source") or "").strip().lower()
                window_sec = float(request.query_params.get("window_sec") or self._config.change_window_sec or 7200.0)
                now_ts = time.time()
                records = await graph.store.list_recent_changes(
                    svc,
                    since_ts=now_ts - max(60.0, window_sec),
                    until_ts=now_ts,
                )
                if src_filter:
                    records = [r for r in records if r.source.lower() == src_filter]
                return JSONResponse(
                    {
                        "status": "ok",
                        "changes": [
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
                        ],
                    },
                    status_code=200,
                )

            body = await request.json()
            rec, decayed = await ingestor.ingest(
                body,
                source_hint=str(request.query_params.get("source") or "generic"),
            )
            return JSONResponse(
                {
                    "status": "ok",
                    "event_id": rec.event_id,
                    "service_name": rec.service_name,
                    "decayed_incidents": decayed,
                },
                status_code=200,
            )

        async def blast_radius_endpoint(request: Request) -> JSONResponse:
            graph, _, _ = self._ensure_services()
            target = str(request.path_params.get("target") or "")
            max_depth = int(request.query_params.get("max_depth") or "2")
            report = await graph.compute_blast_radius(target, max_depth=max_depth)
            return JSONResponse({"status": "ok", **report.to_dict()}, status_code=200)

        async def search_incidents_endpoint(request: Request) -> JSONResponse:
            _, _, inc_store = self._ensure_services()
            q = str(request.query_params.get("q") or "").strip()
            svc = request.query_params.get("service")
            hits = await inc_store.retrieve_similar(q, service_name=svc or None)
            return JSONResponse(
                {
                    "status": "ok",
                    "results": [
                        {
                            "incident_id": h.incident_id,
                            "service_name": h.service_name,
                            "primary_root_cause": h.primary_root_cause,
                            "verification_status": h.verification_status,
                            "confidence_weight": h.confidence_weight,
                            "rrf_score": h.rrf_score,
                        }
                        for h in hits
                    ],
                },
                status_code=200,
            )

        return [
            Route("/api/v1/context-graph/topology", upsert_topology_endpoint, methods=["POST"]),
            Route("/api/v1/context-graph/changes", ingest_change_endpoint, methods=["GET", "POST"]),
            Route("/api/v1/context-graph/blast-radius/{target:path}", blast_radius_endpoint, methods=["GET"]),
            Route("/api/v1/context-graph/incidents/search", search_incidents_endpoint, methods=["GET"]),
        ]

    async def health_check(self) -> AddonHealthStatus:
        """Execute `health_check`."""
        return AddonHealthStatus(
            name=self.name,
            status="ok",
            tier=self.tier,
            version=self.version,
            source=self.source,
            details={"description": self.description},
        )

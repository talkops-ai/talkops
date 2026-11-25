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

"""Built-in ``IntelligenceAddon`` (`intelligence`) for Stratified AutoQA, Disputes & Split SRE KPIs.

Background sweepers are strictly opt-in behind ``TALKOPS_INTELLIGENCE_WORKERS=true``
so idle OSS servers incur zero background LLM token burn.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
from talkops.observers.mace_observers import ReflectionFeedbackObserver
from talkops.rubrics.stratified_autoqa import StratifiedAutoQAEvaluator
from talkops.storage.factory import get_operational_store
from talkops.storage.protocol import AutoQAScoreRecord

_RESOLUTION_MODE_ALIASES: dict[str, str] = {
    "gitops_pr": "gitops_pr_opened",
    "gitops-pr": "gitops_pr_opened",
    "escalated": "human_resolved",
    "human": "human_resolved",
    "mitigated": "agent_mitigated",
    "assisted": "agent_assisted",
}


def normalize_resolution_mode(raw_mode: str | None) -> str:
    """Normalize resolution_mode aliases to canonical split SRE KPI buckets."""
    cleaned = str(raw_mode or "agent_assisted").strip().lower()
    return _RESOLUTION_MODE_ALIASES.get(cleaned, cleaned or "agent_assisted")


def compute_split_sre_kpis(scores: list[AutoQAScoreRecord]) -> dict[str, Any]:
    """Compute honest split SRE KPIs distinguishing autonomous mitigation vs human-assisted runs."""
    if not scores:
        return {
            "total_evaluated_runs": 0,
            "stage0_pass_rate": 1.0,
            "avg_autoqa_score": 0.0,
            "resolution_breakdown": {
                "agent_mitigated": 0,
                "agent_assisted": 0,
                "gitops_pr_opened": 0,
                "human_resolved": 0,
                "unverified": 0,
            },
            "agent_mitigated_rate": 0.0,
            "agent_assisted_rate": 0.0,
            "gitops_pr_rate": 0.0,
            "dispute_overturn_rate": 0.0,
        }

    total = len(scores)
    stage0_passes = sum(1 for s in scores if s.stage0_passed)
    avg_score = sum(s.overall_score for s in scores) / total

    breakdown: dict[str, int] = {
        "agent_mitigated": 0,
        "agent_assisted": 0,
        "gitops_pr_opened": 0,
        "human_resolved": 0,
        "unverified": 0,
    }
    for s in scores:
        canon_mode = normalize_resolution_mode(s.resolution_mode)
        breakdown[canon_mode] = breakdown.get(canon_mode, 0) + 1

    disputed = [s for s in scores if s.dispute_status != "none"]
    overturned = sum(1 for s in disputed if s.dispute_status == "overturned")

    return {
        "total_evaluated_runs": total,
        "stage0_pass_rate": round(stage0_passes / total, 4),
        "avg_autoqa_score": round(avg_score, 4),
        "resolution_breakdown": breakdown,
        "agent_mitigated_rate": round(breakdown["agent_mitigated"] / total, 4),
        "agent_assisted_rate": round(breakdown["agent_assisted"] / total, 4),
        "gitops_pr_rate": round(breakdown["gitops_pr_opened"] / total, 4),
        "dispute_overturn_rate": round(overturned / len(disputed), 4) if disputed else 0.0,
    }


class IntelligenceAddonSettings(AddonSettings):
    """Configuration schema for ``IntelligenceAddon``."""

    enabled: bool = True
    background_workers_enabled: bool = False


class IntelligenceAddon(BaseServerAddon):
    """Exposes Stratified AutoQA evaluation, dispute appeals, and split SRE KPI endpoints."""

    name: str = "intelligence"
    tier: AddonTier = "oss"
    version: str = "2.0.0"
    description: str = "3-Stage Stratified AutoQA, dispute workflow, and honest split SRE KPIs."
    capabilities: AddonCapabilities = AddonCapabilities(
        routes=True,
        event_subscriber=True,
        background_workers=True,
    )
    settings_schema = IntelligenceAddonSettings
    enabled_by_default: bool = True

    def __init__(self) -> None:
        """Initialize IntelligenceAddon."""
        super().__init__()
        self.source = "builtin"
        self._evaluator: StratifiedAutoQAEvaluator | None = None
        self._reflection_observer: ReflectionFeedbackObserver | None = None
        self._event_unsubscribers: list[Callable[[], None]] = []
        self._workers_active: bool = False

    def _ensure_evaluator(self) -> StratifiedAutoQAEvaluator:
        root = self._ctx.project_root if self._ctx is not None else Path.cwd()
        store = get_operational_store(project_root=root)
        if self._evaluator is None:
            self._evaluator = StratifiedAutoQAEvaluator(store=store)
        return self._evaluator

    async def _on_feedback_event(self, event: Any) -> None:
        """Persist operator RCA confirmations/disputes and update calibrated memory rules."""
        event_type = str(getattr(event, "event_type", "") or "").strip()
        if not event_type.startswith("feedback."):
            return
        verdict = event_type.split(".", 1)[1].strip().lower()
        payload = getattr(event, "payload", None)
        p = dict(payload) if isinstance(payload, dict) else {}

        if self._reflection_observer is not None:
            await self._reflection_observer.on_event(event)

        thread_id = str(getattr(event, "thread_id", "") or p.get("lg_thread_id") or "").strip()
        if not thread_id:
            return
        evaluator = self._ensure_evaluator()
        actor = str(p.get("sender_name") or p.get("sender_id") or "").strip()
        await evaluator.record_operator_feedback(
            thread_id,
            verdict=verdict,
            actor=actor,
            reason=str(p.get("reason") or p.get("rule_text") or "").strip(),
            service_name=str(p.get("service_name") or "").strip(),
            title=str(p.get("title") or "").strip(),
            tenant_id=str(p.get("tenant_id") or "default"),
        )

    async def on_startup(self, ctx: AddonContext) -> None:
        """Initialize AutoQA storage, ReflectionFeedbackObserver, and `feedback.*` bus subscriptions."""
        await super().on_startup(ctx)
        store = get_operational_store(project_root=ctx.project_root)
        await store.setup()
        self._evaluator = StratifiedAutoQAEvaluator(store=store)
        self._reflection_observer = ReflectionFeedbackObserver(store)
        env_flag = os.environ.get("TALKOPS_INTELLIGENCE_WORKERS", "").strip().lower()
        cfg_flag = bool(getattr(self._config, "background_workers_enabled", False))
        self._workers_active = cfg_flag or env_flag in {"1", "true", "yes", "on"}

        event_bus = getattr(ctx, "event_bus", None)
        if event_bus is not None and hasattr(event_bus, "subscribe"):
            self._event_unsubscribers.append(
                event_bus.subscribe("feedback.*", self._on_feedback_event)
            )

    async def on_shutdown(self, ctx: AddonContext) -> None:
        """Unsubscribe from `HookEventBus` feedback events on server shutdown."""
        for unsub in self._event_unsubscribers:
            try:
                unsub()
            except Exception:
                pass
        self._event_unsubscribers.clear()
        await super().on_shutdown(ctx)

    def get_routes(self) -> list[BaseRoute]:
        """Return `get_routes` result."""
        async def _hydrate_from_gateway(
            thread_id: str,
            body_messages: Any,
            body_ledger: Any,
        ) -> tuple[list[Any], list[Any]]:
            msgs = list(body_messages) if isinstance(body_messages, list) else []
            ledger = list(body_ledger) if isinstance(body_ledger, list) else []
            if (not msgs or not ledger) and self._ctx is not None and getattr(self._ctx, "gateway", None) is not None:
                try:
                    st = await self._ctx.gateway.get_thread_state(thread_id)
                    vals = getattr(st, "values", None) or (st.get("values") if isinstance(st, dict) else None)
                    if isinstance(vals, dict):
                        if not msgs and isinstance(vals.get("messages"), list):
                            msgs = list(vals["messages"])
                        if not ledger and isinstance(vals.get("hypothesis_ledger"), list):
                            ledger = list(vals["hypothesis_ledger"])
                except Exception:
                    pass
            return msgs, ledger

        async def autoqa_endpoint(request: Request) -> JSONResponse:
            root = self._ctx.project_root if self._ctx is not None else Path.cwd()
            store = get_operational_store(project_root=root)
            evaluator = self._ensure_evaluator()

            if request.method == "POST":
                raw_bytes = await request.body()
                body = (await request.json()) if raw_bytes.strip() else {}
                path_tid = request.path_params.get("thread_id")
                tid = str(path_tid or body.get("thread_id") or "").strip()
                if not tid:
                    return JSONResponse({"error": "thread_id is required"}, status_code=400)
                msgs, ledger = await _hydrate_from_gateway(
                    tid,
                    body.get("messages"),
                    body.get("hypothesis_ledger"),
                )
                rec = await evaluator.evaluate_run(
                    thread_id=tid,
                    plugin_name=str(body.get("plugin_name") or "core"),
                    plugin_version=str(body.get("plugin_version") or "default"),
                    messages=msgs,
                    hypothesis_ledger=ledger,
                    resolution_mode=normalize_resolution_mode(body.get("resolution_mode")),  # type: ignore[arg-type]
                    is_high_severity_or_rollback=bool(body.get("is_high_severity_or_rollback", False)),
                    tenant_id=str(body.get("tenant_id") or "default"),
                )
                return JSONResponse(
                    {
                        "status": "ok",
                        "thread_id": rec.thread_id,
                        "plugin_version": rec.plugin_version,
                        "stage0_passed": rec.stage0_passed,
                        "overall_score": rec.overall_score,
                        "judge_tier": rec.judge_tier,
                        "stage0_violations": rec.stage0_violations,
                        "criterion_citations": rec.criterion_citations,
                    },
                    status_code=201,
                )

            scores = await store.get_autoqa_scores(
                thread_id=request.query_params.get("thread_id") or None,
                plugin_name=request.query_params.get("plugin_name") or None,
                plugin_version=request.query_params.get("plugin_version") or None,
                limit=int(request.query_params.get("limit") or "50"),
                tenant_id=str(request.query_params.get("tenant_id") or "default"),
            )
            return JSONResponse(
                {
                    "status": "ok",
                    "scores": [
                        {
                            "thread_id": s.thread_id,
                            "plugin_name": s.plugin_name,
                            "plugin_version": s.plugin_version,
                            "stage0_passed": s.stage0_passed,
                            "overall_score": s.overall_score,
                            "judge_tier": s.judge_tier,
                            "dispute_status": s.dispute_status,
                            "resolution_mode": s.resolution_mode,
                        }
                        for s in scores
                    ],
                },
                status_code=200,
            )

        async def dispute_endpoint(request: Request) -> JSONResponse:
            evaluator = self._ensure_evaluator()
            body = await request.json()
            action = str(body.get("action") or "dispute")
            if action in {"upheld", "overturned"}:
                rec = await evaluator.resolve_dispute(
                    str(body["thread_id"]),
                    resolution="overturned" if action == "overturned" else "upheld",
                    adjusted_score=body.get("adjusted_score"),
                    reviewer_notes=str(body.get("notes") or ""),
                    tenant_id=str(body.get("tenant_id") or "default"),
                )
            else:
                rec = await evaluator.dispute_score(
                    str(body["thread_id"]),
                    reason=str(body.get("reason") or "Operator disputed AutoQA grade"),
                    tenant_id=str(body.get("tenant_id") or "default"),
                )
            return JSONResponse(
                {
                    "status": "ok",
                    "thread_id": rec.thread_id,
                    "dispute_status": rec.dispute_status,
                    "overall_score": rec.overall_score,
                },
                status_code=200,
            )

        async def kpis_endpoint(request: Request) -> JSONResponse:
            root = self._ctx.project_root if self._ctx is not None else Path.cwd()
            store = get_operational_store(project_root=root)
            scores = await store.get_autoqa_scores(
                plugin_name=request.query_params.get("plugin_name") or None,
                limit=int(request.query_params.get("limit") or "200"),
                tenant_id=str(request.query_params.get("tenant_id") or "default"),
            )
            return JSONResponse(
                {
                    "status": "ok",
                    "background_workers_active": self._workers_active,
                    **compute_split_sre_kpis(scores),
                },
                status_code=200,
            )

        return [
            Route("/api/v1/intelligence/autoqa", autoqa_endpoint, methods=["GET", "POST"]),
            Route("/api/v1/intelligence/evaluate/{thread_id}", autoqa_endpoint, methods=["POST"]),
            Route("/api/v1/intelligence/autoqa/dispute", dispute_endpoint, methods=["POST"]),
            Route("/api/v1/intelligence/kpis", kpis_endpoint, methods=["GET"]),
        ]

    async def health_check(self) -> AddonHealthStatus:
        """Execute `health_check`."""
        return AddonHealthStatus(
            name=self.name,
            status="ok",
            tier=self.tier,
            version=self.version,
            source=self.source,
            details={
                "description": self.description,
                "background_workers_active": self._workers_active,
            },
        )


__all__ = ["IntelligenceAddon", "IntelligenceAddonSettings", "compute_split_sre_kpis"]

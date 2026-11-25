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

"""Comprehensive Phase 2 tests: ContextGraph, Calibrated Memory, Storm Drain & Parallel Hypotheses."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import SystemMessage

from talkops.addons.builtin import get_builtin_addons
from talkops.context_graph import ChangeIngestor, ContextGraph
from talkops.memory.incident_store import CalibratedIncidentStore
from talkops.middleware.context_authority import (
    ContextAuthorityMiddleware,
    render_context_authority_block,
)
from talkops.middleware.storm_drain import AlertStormDrainMiddleware
from talkops.sre.checklists import (
    PreflightChecklistRunner,
    load_preflight_specs_from_file,
)
from talkops.sre.hypothesis_graph import (
    build_competing_hypothesis_graph,
    build_record_hypothesis_tool,
)
from talkops.storage.sqlite_store import SqliteOperationalStore
from talkops.webhooks.correlation import AlertStormCorrelator


@pytest.mark.asyncio
async def test_context_graph_change_ingestor_and_calibrated_incident_decay(tmp_path: Path) -> None:
    """Verify context graph change ingestor and calibrated incident decay."""
    store = SqliteOperationalStore(tmp_path / "phase2.db")
    await store.setup()

    graph = ContextGraph(store)
    ingestor = ChangeIngestor(store, git_decay_factor=0.60)
    fake_lg_store = MagicMock()
    del fake_lg_store.aput
    inc_store = CalibratedIncidentStore(store, langgraph_store=fake_lg_store)

    # 1. Ingest OTel service call map & K8s selector topology
    await graph.ingest_otel_service_map(
        [
            {"client": "checkout", "server": "payment", "rel_type": "calls"},
            {"client": "payment", "server": "orders-db", "target_type": "rds", "rel_type": "depends_on"},
        ]
    )
    k8s_count = await graph.ingest_kubernetes_snapshot(
        [
            {
                "kind": "Service",
                "metadata": {"name": "checkout-svc", "namespace": "prod"},
                "spec": {"selector": {"app": "checkout"}},
            },
            {
                "kind": "Deployment",
                "metadata": {"name": "checkout-deploy", "namespace": "prod", "labels": {"app": "checkout"}},
                "spec": {"template": {"metadata": {"labels": {"app": "checkout"}}}},
            },
        ]
    )
    assert k8s_count == 2

    # 2. Record unverified vs verified incident; unverified must NOT appear in verified_only RRF
    unverified = await inc_store.record_candidate_incident(
        thread_id="t-unv",
        service_name="orders-db",
        primary_root_cause="Hallucinated cosmic ray",
        symptoms_text="orders-db max_connections exhausted timeout",
        verification_status="unverified",
    )
    assert unverified.confidence_weight == pytest.approx(0.25)
    assert await inc_store.retrieve_similar("orders-db max_connections", verified_only=True) == []

    verified = await inc_store.record_candidate_incident(
        thread_id="t-ver",
        service_name="orders-db",
        primary_root_cause="HikariCP pool size bumped to 50 across 8 replicas exceeding max_connections=200",
        symptoms_text="orders-db FATAL remaining connection slots reserved",
        git_tree_hash="tree-old-v1",
        verification_status="unverified",
    )
    await inc_store.promote_incident(verified, verification_status="slo_verified")
    hits = await inc_store.retrieve_similar("orders-db connection slots", verified_only=True)
    assert len(hits) == 1 and hits[0].incident_id == verified.incident_id
    assert hits[0].confidence_weight == pytest.approx(0.85)

    # 3. GitHub change on downstream `orders-db` triggers git_tree_hash decay AND surfaces in `checkout` blast radius!
    chg, decayed = await ingestor.ingest(
        {
            "repository": {"name": "orders-db"},
            "after": "9f8e7d6c5b4a",
            "head_commit": {
                "id": "9f8e7d6c5b4a",
                "tree_id": "tree-new-v2",
                "message": "Increase Hikari maximumPoolSize from 10 to 50",
                "author": {"name": "alice"},
            },
            "occurred_at": time.time() - 300.0,
        }
    )
    assert chg.service_name == "orders-db"
    assert decayed == 1

    blast = await graph.compute_blast_radius("checkout", max_depth=2)
    neighbor_services = {n.service_name for n in blast.neighbors}
    assert {"payment", "orders-db"}.issubset(neighbor_services)
    assert any(c.commit_sha == "9f8e7d6c5b4a" for c in blast.recent_changes)

    # 4. Regressed incident zeroes confidence and writes cautionary TTL rule synced to LangGraph BaseStore
    demoted, caution_rule = await inc_store.demote_regressed_incident(
        verified,
        regression_reason="p99 latency spiked to 4.2s",
    )
    assert demoted.confidence_weight == 0.0
    assert "regressed SLOs" in caution_rule.rule_text
    assert fake_lg_store.put.called

    # Built-in addons include ContextGraphAddon enabled by default
    addon_names = {a.name: a.enabled_by_default for a in get_builtin_addons()}
    assert addon_names.get("context-graph") is True
    await store.aclose()


@pytest.mark.asyncio
async def test_alert_storm_sidecar_drain_and_continuation_mutex(tmp_path: Path) -> None:
    """Verify alert storm sidecar drain and continuation mutex."""
    store = SqliteOperationalStore(tmp_path / "storm.db")
    await store.setup()

    graph = ContextGraph(store)
    await graph.ingest_otel_service_map(
        [{"client": "checkout", "server": "orders-db", "rel_type": "depends_on"}]
    )

    correlator = AlertStormCorrelator(store, context_graph=graph)
    drain_mw = AlertStormDrainMiddleware(store=store, drain_mutex=correlator.drain_mutex)

    # Alert 1 at t=0s on `checkout` -> starts primary run
    d1 = await correlator.route_incoming_alert(
        candidate_thread_id="thread-rca-1",
        provider="pagerduty",
        dedup_key="pd-1",
        service_name="checkout",
        normalized_event={"title": "Checkout 5xx Error Rate > 15%", "severity": "critical"},
    )
    assert d1.action == "start_primary_run"
    assert d1.thread_id == "thread-rca-1"

    # Alert 2 at t=18s on downstream `orders-db` -> correlated via topology into Sidecar Inbox!
    d2 = await correlator.route_incoming_alert(
        candidate_thread_id="thread-rca-would-be-2",
        provider="prometheus",
        dedup_key="prom-2",
        service_name="orders-db",
        normalized_event={"title": "RDS Connection Pool 100% Saturated", "severity": "critical"},
    )
    assert d2.action == "queued_sidecar_inbox"
    assert d2.thread_id == "thread-rca-1"

    # At Turn 2 `abefore_model` boundary, AlertStormDrainMiddleware atomically drains Alert 2!
    runtime = MagicMock()
    runtime.config = {"configurable": {"thread_id": "thread-rca-1"}}
    update = await drain_mw.abefore_model({"_webhook_sre_mode": True}, runtime)
    assert update is not None
    assert len(update["correlated_alerts"]) == 1
    assert "RDS Connection Pool 100% Saturated" in str(update["messages"][0].content)

    # Second `abefore_model` sees empty inbox -> returns None
    assert await drain_mw.abefore_model({"_webhook_sre_mode": True}, runtime) is None

    # Late Alert 3 arrives right after final model turn before run closes -> triggers Continuation Run!
    d3 = await correlator.route_incoming_alert(
        candidate_thread_id="thread-rca-3",
        provider="cloudwatch",
        dedup_key="cw-3",
        service_name="orders-db",
        normalized_event={"title": "RDS FreeableMemory < 64MB", "severity": "warning"},
    )
    assert d3.action == "queued_sidecar_inbox"

    continued_batches: list[int] = []

    async def _continuation_runner(items: list[Any]) -> None:
        continued_batches.append(len(items))

    leftovers = await correlator.finalize_run_or_continue(
        "thread-rca-1",
        continuation_runner=_continuation_runner,
    )
    assert len(leftovers) == 1
    assert continued_batches == [1]
    await store.aclose()


@pytest.mark.asyncio
async def test_tristate_preflight_ceiling_and_context_authority_hierarchy(tmp_path: Path) -> None:
    """Verify tristate preflight ceiling and context authority hierarchy."""
    wf_file = tmp_path / "preflight.yaml"
    wf_file.write_text(
        """
checks:
  - id: pod_status
    title: Check Pod OOM/CrashLoop
    probe: k8s_pods
    finding_regex: "OOMKilled|CrashLoopBackOff"
  - id: node_disk
    title: Check Node Disk Pressure
    probe: k8s_nodes
    normal_regex: "DiskPressure=False"
  - id: slow_cloudwatch
    title: Query Slow CloudWatch Metric
    probe: slow_probe
""",
        encoding="utf-8",
    )
    specs = load_preflight_specs_from_file(wf_file)
    assert len(specs) == 3

    async def _probe_exec(name: str, _args: Any) -> Any:
        if name == "k8s_pods":
            return "checkout-7f9b OOMKilled exitCode=137"
        if name == "k8s_nodes":
            return "node-1 DiskPressure=False Ready=True"
        if name == "slow_probe":
            await asyncio.sleep(0.5)
            return "too_late"
        return ""

    runner = PreflightChecklistRunner(ceiling_seconds=0.12, probe_executor=_probe_exec)
    t0 = time.perf_counter()
    summary = await runner.run(specs, alert_text="OOM alert on checkout")
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.35
    by_id = {it.check_id: it for it in summary.items}
    assert by_id["pod_status"].status == "Finding"
    assert by_id["node_disk"].status == "Normal"
    # Slow probe MUST be Inconclusive, NEVER Normal!
    assert by_id["slow_cloudwatch"].status == "Inconclusive"

    # ContextAuthorityMiddleware hydration & <context_authority> injection
    store = SqliteOperationalStore(tmp_path / "authority.db")
    await store.setup()
    auth_mw = ContextAuthorityMiddleware(store=store)
    state_in = {
        "_webhook_sre_mode": True,
        "execution_profile": "readonly-metadata",
        "preflight_checklist_summary": summary.to_dict(),
        "_webhook_metadata": {"service_name": "checkout", "title": "Checkout OOM"},
    }
    hydrated = await auth_mw.abefore_model(state_in, MagicMock())
    assert hydrated is not None
    bundle = hydrated["context_authority_bundle"]
    rendered = render_context_authority_block(bundle)
    assert "[Tier 1 · Policy Ceiling] Execution Trust Profile: readonly-metadata" in rendered
    assert "[Finding] Check Pod OOM/CrashLoop" in rendered

    req = MagicMock(spec=ModelRequest)
    req.state = {**state_in, **hydrated}
    req.system_message = SystemMessage(content="Base SRE Prompt")
    req.override = lambda **kw: kw["system_message"]
    injected = auth_mw.wrap_model_call(req, lambda r: r)
    assert "<context_authority>" in injected.content
    await store.aclose()


@pytest.mark.asyncio
async def test_parallel_competing_hypothesis_langgraph_send_and_tool() -> None:
    """Verify parallel competing hypothesis langgraph send and tool."""
    async def _evaluate_branch(hyp: dict[str, Any], _alert: str, _svc: str) -> dict[str, Any]:
        await asyncio.sleep(0.01)
        if hyp["category"] == "dependency_cascade":
            return {
                "status": "confirmed",
                "confidence": 0.92,
                "evidence_tool_call_ids": ["tc-pg-stat-1", "tc-prom-pool-2"],
                "supporting_evidence": "pg_stat_activity shows 100/100 active connections waiting on lock",
            }
        return {
            "status": "ruled_out",
            "confidence": 0.08,
            "evidence_tool_call_ids": ["tc-k8s-mem-3"],
            "falsification_proof": "Pod memory steady at 38% of limit and zero restarts",
        }

    h_graph = build_competing_hypothesis_graph(branch_evaluator=_evaluate_branch)
    out = await h_graph.ainvoke(
        {
            "alert_summary": "Checkout 5xx spike",
            "service_name": "checkout",
            "blast_radius": {
                "neighbors": [{"node_id": "rds:orders-db", "service_name": "orders-db"}],
                "recent_changes": [{"service_name": "checkout", "source": "github", "commit_sha": "abcdef12"}],
            },
        }
    )
    ledger = out["hypothesis_ledger"]
    assert len(ledger) == 3
    assert out["winning_hypothesis"]["status"] == "confirmed"
    assert out["winning_hypothesis"]["evidence_tool_call_ids"] == ["tc-pg-stat-1", "tc-prom-pool-2"]
    ruled_out = [b for b in ledger if b["status"] == "ruled_out"]
    assert len(ruled_out) == 2
    assert all(b["falsification_proof"] for b in ruled_out)

    # Also test `record_hypothesis_verdict` LangChain tool Command update
    rec_tool = build_record_hypothesis_tool()
    cmd = rec_tool.func(
        hypothesis_id="H1",
        title="DNS resolution latency",
        status="ruled_out",
        confidence=0.05,
        evidence_tool_call_ids=["tc-dig-1"],
        explanation="CoreDNS p99 lookup latency is 1.4ms (<5ms threshold)",
        tool_call_id="tc-rec-1",
        state={"hypothesis_ledger": []},
    )
    assert len(cmd.update["hypothesis_ledger"]) == 1
    assert cmd.update["hypothesis_ledger"][0]["status"] == "ruled_out"

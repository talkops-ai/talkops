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

"""`talkops serve` 5-Profile Isolation, Webhook RCA, AutoQA & Incident Memory Evals.

Verifies `api-only`, `default`, `webhook-worker`, `sre`, and `enterprise-full`
profiles end-to-end with Swiss-Cheese Outcome-First grading:
1. Deterministic HTTP route surface isolation (`200` vs `404`) + SQLite/DB state.
2. Flexible `agentevals` `superset` trajectory checks.
3. Calibrated binary `openevals` RCA groundedness judges.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from talkops.addons.protocol import AddonCapabilities, AddonContext, BaseServerAddon
from tests.evals.fixtures.topology_and_tools import (
    seed_calibrated_incident_memories,
    seed_production_checkout_topology,
)
from tests.evals.fixtures.webhook_payloads import (
    build_alertmanager_crashloop_payload,
    build_argocd_change_payload,
    build_pagerduty_incident_payload,
)
from tests.evals.framework import (
    EvalTelemetry,
    build_rca_groundedness_judge,
    evaluate_required_tools_superset,
    verify_serve_profile_route_isolation,
)


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("deep_agent_outcome")
@pytest.mark.eval_category("profile_isolation")
@pytest.mark.serve_profile("api-only")
async def test_api_only_profile_exchanges_jwt_isolates_webhooks_and_runs_pod_diagnostics(
    serve_harness_factory: Any,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify `api-only` exchanges JWTs, returns `404` on webhook/SRE routes, and runs pod diagnostics."""
    harness = await serve_harness_factory(profile="api-only", no_subagents=True)

    matrix = await verify_serve_profile_route_isolation(
        harness.http_client,
        "api-only",
        auth_headers=harness.auth_headers,
    )
    assert matrix["all_passed"] is True, f"api-only route matrix failed: {matrix}"

    tok_resp = await harness.http_client.post(
        "/api/v1/auth/token",
        json={"api_key": "eval-local-secret-token"},
        headers=harness.auth_headers,
    )
    assert tok_resp.status_code == 200
    assert tok_resp.json().get("access_token")

    prompt = (
        "Could you check how the checkout-api pods look in prod right now and pull "
        "their recent container logs so we can see why requests are failing?"
    )
    eval_telemetry.log_inputs({"profile": "api-only", "prompt": prompt})

    t0 = time.perf_counter()
    handle = await harness.gateway.trigger_run(prompt, source_addon="client-gateway", wait=True)
    elapsed = time.perf_counter() - t0
    assert handle.status == "success"

    st = await harness.gateway.get_thread_state(handle.thread_id)
    traj_res = evaluate_required_tools_superset(
        st.values["messages"],
        prompt,
        [
            ("kubectl_get_pods", {}),
            ("kubectl_get_pod_logs", {}),
        ],
    )
    eval_telemetry.log_feedback(key="required_tools_superset", score=bool(traj_res["score"]))
    assert traj_res["score"] is True
    assert any(c["name"] == "kubectl_get_pods" and c["args"].get("namespace") == "prod" for c in harness.tool_call_log)
    assert "orders-rds" in handle.analysis_text.lower() or "too many clients" in handle.analysis_text.lower()

    eval_telemetry.record_efficiency(
        st.values["messages"],
        ideal_steps=2,
        ideal_tool_calls=2,
        elapsed_seconds=elapsed,
        solved=True,
    )


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("deep_agent_outcome")
@pytest.mark.eval_category("webhook_rca")
@pytest.mark.serve_profile("default")
async def test_default_profile_ingests_alertmanager_webhook_persists_thread_and_delivers_sink_rca(
    serve_harness_factory: Any,
    eval_judge_model: Any,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify `default` profile accepts Alertmanager webhooks, persists `[ALERTMANAGER]` thread, and delivers RCA."""
    harness = await serve_harness_factory(profile="default", no_subagents=True)

    matrix = await verify_serve_profile_route_isolation(
        harness.http_client,
        "default",
        auth_headers=harness.auth_headers,
    )
    assert matrix["all_passed"] is True

    payload = build_alertmanager_crashloop_payload(service="checkout-api", namespace="prod")
    eval_telemetry.log_inputs({"profile": "default", "webhook": "alertmanager"})

    t0 = time.perf_counter()
    resp = await harness.http_client.post("/webhooks/alertmanager", json=payload)
    assert resp.status_code == 202
    thread_id = resp.json()["thread_id"]

    await harness.wait_for_webhook_runs()
    elapsed = time.perf_counter() - t0

    stages = [d["stage"] for d in harness.sink_deliveries]
    assert "received" in stages and "completed" in stages
    completed_sink = next((d for d in harness.sink_deliveries if d["stage"] == "completed"), None)
    assert completed_sink is not None
    assert completed_sink["status"] == "success"

    threads_resp = await harness.http_client.get("/api/v1/threads?limit=10", headers=harness.auth_headers)
    assert threads_resp.status_code == 200
    thread_ids = [t["thread_id"] for t in threads_resp.json()["threads"]]
    assert thread_id in thread_ids

    judge = build_rca_groundedness_judge(eval_judge_model)
    grade = judge(
        inputs="Alertmanager firing: checkout-api HTTP 5xx jumped to 38% and CrashLoopBackOff in prod",
        outputs=completed_sink["analysis_text"],
        reference_outputs=(
            "Root cause is PostgreSQL connection pool exhaustion on orders-rds ('FATAL: sorry, too many "
            "clients already') triggered by HikariCP maxPoolSize=150 on checkout-api; redis-cart is healthy."
        ),
    )
    eval_telemetry.log_feedback(key="rca_grounded_pass", score=bool(grade["score"]))
    assert grade["score"] is True

    st = await harness.gateway.get_thread_state(thread_id)
    eval_telemetry.record_efficiency(
        st.values["messages"],
        ideal_steps=2,
        ideal_tool_calls=2,
        elapsed_seconds=elapsed,
        solved=True,
    )


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("deep_agent_outcome")
@pytest.mark.eval_category("webhook_rca")
@pytest.mark.serve_profile("webhook-worker")
async def test_webhook_worker_profile_correlates_topology_changes_with_client_gateway_disabled(
    serve_harness_factory: Any,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify `webhook-worker` exposes `webhooks` + `context-graph` (`404` on `/api/v1/threads`) and correlates blast radius."""
    harness = await serve_harness_factory(profile="webhook-worker", no_subagents=True)

    matrix = await verify_serve_profile_route_isolation(
        harness.http_client,
        "webhook-worker",
        auth_headers=harness.auth_headers,
    )
    assert matrix["all_passed"] is True

    topo_resp = await harness.http_client.post(
        "/api/v1/context-graph/topology",
        json={
            "nodes": [
                {"node_id": "service:payment-worker", "node_type": "k8s_deployment", "service_name": "payment-worker"},
                {"node_id": "rds:orders-rds", "node_type": "aws_rds", "service_name": "orders-rds"},
            ],
            "edges": [{"src_id": "service:payment-worker", "dst_id": "rds:orders-rds", "rel_type": "depends_on"}],
        },
    )
    assert topo_resp.status_code == 200

    chg_resp = await harness.http_client.post(
        "/api/v1/context-graph/changes?source=argocd",
        json=build_argocd_change_payload(
            app_name="orders-rds",
            summary="checkout-api pool bump saturated shared orders-rds (500/500 connections)",
        ),
    )
    assert chg_resp.status_code in (200, 201)

    t0 = time.perf_counter()
    pd_resp = await harness.http_client.post(
        "/webhooks/pagerduty",
        json=build_pagerduty_incident_payload(service="payment-worker"),
    )
    assert pd_resp.status_code == 202
    thread_id = pd_resp.json()["thread_id"]
    await harness.wait_for_webhook_runs()
    elapsed = time.perf_counter() - t0

    st = await harness.gateway.get_thread_state(thread_id)
    blast = (st.values.get("context_authority_bundle") or {}).get("tier4_blast_radius") or {}
    neighbor_names = {n.get("service_name") for n in (blast.get("neighbors") or [])}
    assert "orders-rds" in neighbor_names

    completed_sink = next((d for d in harness.sink_deliveries if d["stage"] == "completed"), None)
    assert completed_sink is not None
    assert "orders-rds" in completed_sink["analysis_text"].lower()

    eval_telemetry.record_efficiency(
        st.values["messages"],
        ideal_steps=2,
        ideal_tool_calls=2,
        elapsed_seconds=elapsed,
        solved=True,
    )


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("deep_agent_outcome")
@pytest.mark.eval_category("webhook_rca")
@pytest.mark.serve_profile("sre")
async def test_sre_profile_executes_topology_webhook_rca_stratified_autoqa_and_split_kpis(
    serve_harness_factory: Any,
    eval_judge_model: Any,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify full `sre` golden path: ContextGraph deploy -> Alertmanager RCA -> Stage 0/1 AutoQA -> Split KPIs."""
    harness = await serve_harness_factory(profile="sre", no_subagents=True)

    matrix = await verify_serve_profile_route_isolation(
        harness.http_client,
        "sre",
        auth_headers=harness.auth_headers,
    )
    assert matrix["all_passed"] is True

    await harness.http_client.post(
        "/api/v1/context-graph/topology",
        json={
            "nodes": [
                {"node_id": "service:checkout-api", "node_type": "k8s_deployment", "service_name": "checkout-api"},
                {"node_id": "rds:orders-rds", "node_type": "aws_rds", "service_name": "orders-rds"},
                {"node_id": "redis:redis-cart", "node_type": "redis", "service_name": "redis-cart"},
            ],
            "edges": [
                {"src_id": "service:checkout-api", "dst_id": "rds:orders-rds", "rel_type": "depends_on"},
                {"src_id": "service:checkout-api", "dst_id": "redis:redis-cart", "rel_type": "depends_on"},
            ],
        },
        headers=harness.auth_headers,
    )
    await harness.http_client.post(
        "/api/v1/context-graph/changes?source=argocd",
        json=build_argocd_change_payload(app_name="checkout-api"),
        headers=harness.auth_headers,
    )

    t0 = time.perf_counter()
    wh_resp = await harness.http_client.post(
        "/webhooks/alertmanager",
        json=build_alertmanager_crashloop_payload(service="checkout-api", namespace="prod"),
    )
    assert wh_resp.status_code == 202
    thread_id = wh_resp.json()["thread_id"]
    await harness.wait_for_webhook_runs()
    elapsed = time.perf_counter() - t0

    completed = next((d for d in harness.sink_deliveries if d["stage"] == "completed"), None)
    assert completed is not None
    assert completed["status"] == "success"

    eval_resp = await harness.http_client.post(
        f"/api/v1/intelligence/evaluate/{thread_id}",
        json={"resolution_mode": "gitops_pr"},
        headers=harness.auth_headers,
    )
    assert eval_resp.status_code == 201
    eval_data = eval_resp.json()
    assert eval_data["stage0_passed"] is True, f"Stage 0 AutoQA failed: {eval_data}"
    assert eval_data["overall_score"] > 0.0

    kpi_resp = await harness.http_client.get("/api/v1/intelligence/kpis", headers=harness.auth_headers)
    assert kpi_resp.status_code == 200
    kpis = kpi_resp.json()
    assert kpis["total_evaluated_runs"] == 1
    assert kpis["gitops_pr_rate"] == 1.0

    judge = build_rca_groundedness_judge(eval_judge_model)
    grade = judge(
        inputs="Alertmanager 5xx spike on checkout-api following ArgoCD deploy 9f8e7d6c",
        outputs=completed["analysis_text"],
        reference_outputs=(
            "ArgoCD deployment 9f8e7d6c increased DB_MAX_POOL_SIZE from 20 to 150 per pod on checkout-api, "
            "saturating orders-rds max_connections (500) and causing HTTP 502/503 errors."
        ),
    )
    eval_telemetry.log_feedback(key="sre_golden_path_rca_grounded", score=bool(grade["score"]))
    assert grade["score"] is True

    st = await harness.gateway.get_thread_state(thread_id)
    eval_telemetry.record_efficiency(
        st.values["messages"],
        ideal_steps=3,
        ideal_tool_calls=3,
        elapsed_seconds=elapsed,
        solved=True,
    )


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("deep_agent_outcome")
@pytest.mark.eval_category("profile_isolation")
@pytest.mark.serve_profile("enterprise-full")
async def test_enterprise_full_profile_streams_custom_audit_events_and_enforces_pinned_model(
    serve_harness_factory: Any,
    eval_model_spec: str,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify `enterprise-full` loads custom audit addons and honors `TALKOPS_SERVER_EXPLICIT_MODEL=true`."""
    audit_log: list[str] = []

    class EnterpriseAuditSinkAddon(BaseServerAddon):
        name = "enterprise-audit-sink"
        tier = "enterprise"
        version = "1.0.0"
        description = "Streams run lifecycle events into compliance audit log"
        capabilities = AddonCapabilities(event_sinks=True)
        enabled_by_default = True

        async def on_startup(self, ctx: AddonContext) -> None:
            await super().on_startup(ctx)
            ctx.event_bus.subscribe("addon.run.*", lambda ev: audit_log.append(ev.event_type))

    harness = await serve_harness_factory(
        profile="enterprise-full",
        explicit_model=True,
        no_subagents=True,
        extra_addons=[EnterpriseAuditSinkAddon()],
    )

    matrix = await verify_serve_profile_route_isolation(
        harness.http_client,
        "enterprise-full",
        auth_headers=harness.auth_headers,
    )
    assert matrix["all_passed"] is True
    assert "enterprise-audit-sink" in matrix["active_addons"]

    info_resp = await harness.http_client.get("/api/v1/server/info", headers=harness.auth_headers)
    assert info_resp.status_code == 200
    info = info_resp.json()
    assert info.get("explicit_model") is True
    assert info.get("model") == eval_model_spec

    handle = await harness.gateway.trigger_run(
        "Give me a quick status check of the checkout-api pods in prod.",
        source_addon="client-gateway",
        context={"model": eval_model_spec, "explicit_model": True},
        wait=True,
    )
    assert handle.status == "success"
    assert "addon.run.started" in audit_log
    assert "addon.run.completed" in audit_log
    eval_telemetry.log_feedback(key="enterprise_audit_and_pinned_model", score=True)


@pytest.mark.eval_regression
@pytest.mark.eval_scope("deep_agent_outcome")
@pytest.mark.eval_category("summarization")
@pytest.mark.serve_profile("sre")
async def test_stratified_autoqa_stage0_rejects_fabricated_tool_citations_at_zero_llm_cost(
    serve_harness_factory: Any,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify `IntelligenceAddon` rejects fabricated `tool_call_id` citations at Stage 0 (`0.0`) and passes real traces."""
    harness = await serve_harness_factory(profile="sre", no_subagents=True)

    bad_resp = await harness.http_client.post(
        "/api/v1/intelligence/evaluate/thr-hallucinated-999",
        json={
            "resolution_mode": "agent_assisted",
            "messages": [
                {"role": "user", "content": "Why is checkout-api failing?"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "tc-real-001", "name": "kubectl_get_pods", "args": {"namespace": "prod"}}],
                },
                {"role": "tool", "tool_call_id": "tc-real-001", "content": "CrashLoopBackOff"},
                {"role": "assistant", "content": "Root cause is RDS exhaustion."},
            ],
            "hypothesis_ledger": [
                {
                    "hypothesis_id": "H1",
                    "title": "RDS exhaustion",
                    "status": "confirmed",
                    "confidence": 0.95,
                    "evidence_tool_call_ids": ["tc-FAKE-DOES-NOT-EXIST-999"],
                },
                {
                    "hypothesis_id": "H2",
                    "title": "Redis latency",
                    "status": "ruled_out",
                    "confidence": 0.1,
                    "evidence_tool_call_ids": ["tc-real-001"],
                    "falsification_proof": "Redis healthy",
                },
            ],
        },
        headers=harness.auth_headers,
    )
    assert bad_resp.status_code == 201
    bad_data = bad_resp.json()
    assert bad_data["stage0_passed"] is False
    assert bad_data["overall_score"] == 0.0
    assert any("fabricated_citation" in v for v in bad_data["stage0_violations"])

    good_resp = await harness.http_client.post(
        "/api/v1/intelligence/evaluate/thr-grounded-001",
        json={
            "resolution_mode": "agent_mitigated",
            "messages": [
                {"role": "user", "content": "Why is checkout-api failing?"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "tc-real-001", "name": "kubectl_get_pods", "args": {"namespace": "prod"}}],
                },
                {"role": "tool", "tool_call_id": "tc-real-001", "content": "CrashLoopBackOff"},
                {"role": "assistant", "content": "Confirmed HikariCP exhausted orders-rds; redis-cart ruled out."},
            ],
            "hypothesis_ledger": [
                {
                    "hypothesis_id": "H1",
                    "title": "RDS exhaustion",
                    "status": "confirmed",
                    "confidence": 0.95,
                    "evidence_tool_call_ids": ["tc-real-001"],
                },
                {
                    "hypothesis_id": "H2",
                    "title": "Redis latency",
                    "status": "ruled_out",
                    "confidence": 0.9,
                    "evidence_tool_call_ids": ["tc-real-001"],
                    "falsification_proof": "Redis p99=1.1ms",
                },
            ],
        },
        headers=harness.auth_headers,
    )
    assert good_resp.status_code == 201
    good_data = good_resp.json()
    assert good_data["stage0_passed"] is True
    assert good_data["overall_score"] > 0.5
    eval_telemetry.log_feedback(key="stage0_citation_gate_accurate", score=True)


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("deep_agent_outcome")
@pytest.mark.eval_category("memory")
@pytest.mark.serve_profile("sre")
async def test_calibrated_incident_memory_ranks_verified_fix_above_regressed_replica_scaleup(
    serve_harness_factory: Any,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify `CalibratedIncidentStore` RRF ranks verified HikariCP fix above demoted replica scaling."""
    harness = await serve_harness_factory(profile="sre", no_subagents=True)
    await seed_production_checkout_topology(harness.sqlite_store)
    ids = await seed_calibrated_incident_memories(harness.sqlite_store)

    prompt = (
        "checkout-api is throwing 502s again with database connection timeouts in prod. "
        "Check our past checkout-api incidents first— someone on Slack wants to scale "
        "checkout-api from 6 to 12 replicas immediately. Should we do that, or what worked previously?"
    )
    eval_telemetry.log_inputs({"prompt": prompt})

    handle = await harness.gateway.trigger_run(
        prompt,
        source_addon="client-gateway",
        context={"service_name": "checkout-api"},
        wait=True,
    )
    assert handle.status == "success"

    from talkops.memory.incident_store import CalibratedIncidentStore

    store = CalibratedIncidentStore(harness.sqlite_store)
    ranked = await store.retrieve_similar(
        "checkout-api HTTP 502 PostgreSQL connection pool exhaustion",
        service_name="checkout-api",
        top_k=5,
    )
    assert ranked[0].incident_id == ids["verified_incident_id"]

    ans = handle.analysis_text.lower()
    assert "pool" in ans or "max_connections" in ans or "rds" in ans
    eval_telemetry.log_feedback(key="calibrated_rrf_guided_answer", score=True)

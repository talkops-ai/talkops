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

"""Scope 3: Partial Graph Sub-Flow & Human-in-the-Loop Checkpoint Evaluations.

Implements LangGraph checkpoint state injection (`aupdate_state(..., as_node=...)` +
`ainvoke(None, interrupt_after=...)` + `Command(resume=...)`) verified via
`extract_langgraph_trajectory_from_thread` and `graph_trajectory_strict_match`,
alongside live competing-hypothesis fan-out and Transactional Non-Regression (TNR).
"""

from __future__ import annotations

import json
import time
from typing import Any, TypedDict

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from talkops.sre.hypothesis_graph import (
    HypothesisGraphState,
    build_competing_hypothesis_graph,
)
from tests.evals.fixtures.topology_and_tools import seed_production_checkout_topology
from tests.evals.framework import (
    EvalTelemetry,
    evaluate_strict_graph_steps,
    extract_final_ai_text,
    run_checkpoint_seeded_subflow,
    verify_competing_hypothesis_ledger,
)

# ── Mid-Graph Checkpoint State Injection + `graph_trajectory_strict_match` ───


class RemediationSubflowState(TypedDict, total=False):
    """Typed state for testing mid-graph remediation approval slices via checkpoint injection."""

    service_name: str
    root_cause_verified: bool
    proposed_action: str
    approved_by: str
    execution_status: str
    audit_recorded: bool


@pytest.mark.eval_regression
@pytest.mark.eval_scope("partial_subflow")
@pytest.mark.eval_category("graph_routing")
async def test_checkpoint_seeded_hitl_approval_subflow_pauses_and_resumes_with_strict_graph_match(
    eval_telemetry: EvalTelemetry,
) -> None:
    """Seed state at `intake_rca_node`, pause on `__interrupt__`, resume via `Command`, and verify strict steps."""

    def intake_rca_node(state: RemediationSubflowState) -> dict[str, Any]:
        return {"root_cause_verified": True}

    def approval_gate_node(state: RemediationSubflowState) -> dict[str, Any]:
        decision = interrupt(
            {
                "service": state.get("service_name"),
                "action": state.get("proposed_action"),
            }
        )
        return {"approved_by": str(decision.get("reviewer", "operator"))}

    def execute_rollback_node(state: RemediationSubflowState) -> dict[str, Any]:
        return {"execution_status": "ROLLED_BACK"}

    def write_audit_ledger_node(state: RemediationSubflowState) -> dict[str, Any]:
        return {"audit_recorded": True}

    def post_notify_node(state: RemediationSubflowState) -> dict[str, Any]:
        return {}

    builder = StateGraph(RemediationSubflowState)
    builder.add_node("intake_rca_node", intake_rca_node)
    builder.add_node("approval_gate_node", approval_gate_node)
    builder.add_node("execute_rollback_node", execute_rollback_node)
    builder.add_node("write_audit_ledger_node", write_audit_ledger_node)

    builder.add_edge(START, "intake_rca_node")
    builder.add_edge("intake_rca_node", "approval_gate_node")
    builder.add_edge("approval_gate_node", "execute_rollback_node")
    builder.add_edge("execute_rollback_node", "write_audit_ledger_node")
    builder.add_edge("write_audit_ledger_node", END)

    compiled = builder.compile(checkpointer=MemorySaver())

    subflow_res = await run_checkpoint_seeded_subflow(
        compiled,
        seed_state={
            "service_name": "checkout-api",
            "root_cause_verified": True,
            "proposed_action": "kubectl rollout undo deployment/checkout-api -n prod",
        },
        as_node="intake_rca_node",
        resume_commands=[Command(resume={"approved": True, "reviewer": "sre_lead"})],
    )

    step_grade = evaluate_strict_graph_steps(
        subflow_res.extracted_trajectory,
        expected_steps=[
            ["approval_gate_node", "__interrupt__"],
            ["execute_rollback_node", "write_audit_ledger_node"],
        ],
    )
    eval_telemetry.log_outputs({"visited_steps": subflow_res.visited_steps})
    eval_telemetry.log_feedback(key="strict_subflow_graph_match", score=bool(step_grade["score"]))

    assert step_grade["score"] is True, f"Visited steps mismatch: {subflow_res.visited_steps}"
    assert subflow_res.final_state["execution_status"] == "ROLLED_BACK"
    assert subflow_res.final_state["approved_by"] == "sre_lead"
    assert subflow_res.final_state["audit_recorded"] is True


@pytest.mark.eval_regression
@pytest.mark.eval_scope("partial_subflow")
@pytest.mark.eval_category("graph_routing")
async def test_checkpoint_seeded_hypothesis_adjudication_subflow_reduces_evaluated_branches(
    eval_telemetry: EvalTelemetry,
) -> None:
    """Seed `evaluated_branches` directly at `investigate_branch` and verify `adjudicate_ledger` sorting."""

    async def _unused_branch(_h: dict[str, Any], _a: str = "", _s: str = "") -> dict[str, Any]:
        return {}

    compiled = build_competing_hypothesis_graph(
        branch_evaluator=_unused_branch,
        checkpointer=MemorySaver(),
    )
    subflow_res = await run_checkpoint_seeded_subflow(
        compiled,
        seed_state={
            "evaluated_branches": [
                {
                    "hypothesis_id": "H2",
                    "title": "redis-cart cache saturation",
                    "status": "ruled_out",
                    "confidence": 0.92,
                    "evidence_tool_call_ids": ["tc-prom-1"],
                    "falsification_proof": "redis-cart p99 latency remained 1.1ms with 0 evictions",
                },
                {
                    "hypothesis_id": "H1",
                    "title": "ArgoCD commit 9f8e7d6c DB_MAX_POOL_SIZE=150 exhausted orders-rds",
                    "status": "confirmed",
                    "confidence": 0.96,
                    "evidence_tool_call_ids": ["tc-logs-1"],
                    "supporting_evidence": "HikariPool-1 FATAL: sorry, too many clients already",
                },
            ]
        },
        as_node="investigate_branch",
    )

    step_grade = evaluate_strict_graph_steps(
        subflow_res.extracted_trajectory,
        expected_steps=[["adjudicate_ledger"]],
    )
    assert step_grade["score"] is True
    ledger = subflow_res.final_state["hypothesis_ledger"]
    # `adjudicate_ledger` prioritizes `confirmed` hypotheses first!
    assert ledger[0]["hypothesis_id"] == "H1"
    assert ledger[1]["hypothesis_id"] == "H2"
    eval_telemetry.log_feedback(key="adjudicator_priority_order", score=True)


# ── Live LLM Competing-Hypothesis Fan-Out, TNR Gate & Serve HITL Resume ──────


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("partial_subflow")
@pytest.mark.eval_category("graph_routing")
@pytest.mark.serve_profile("sre")
async def test_live_competing_hypothesis_graph_confirms_argocd_cause_and_falsifies_redis(
    eval_judge_model: Any,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify parallel `Send()` hypothesis branches on `gemini-3.8-flash` confirm H1 and rule out H2."""
    branch_telemetry = {
        "H1": {
            "tool_call_id": "tc-logs-h1",
            "observation": (
                "ArgoCD commit 9f8e7d6c changed DB_MAX_POOL_SIZE from 20 to 150 across 6 pods "
                "(900 total pool demand > orders-rds max_connections=500). Logs show FATAL: sorry, too many clients already."
            ),
        },
        "H2": {
            "tool_call_id": "tc-prom-h2",
            "observation": (
                "Downstream redis-cart metrics over the last 30m: p99_latency_ms=1.1ms, "
                "evictions=0, connection_errors=0, CPU=8%. Completely healthy."
            ),
        },
        "H3": {
            "tool_call_id": "tc-node-h3",
            "observation": (
                "Kubernetes worker nodes and checkout-api containers show CPU=34.2%, memory=41% "
                "of limits, OOMKilled=0, disk_pressure=False. Zero resource saturation."
            ),
        },
    }

    async def _live_branch_evaluator(
        hyp: dict[str, Any],
        _alert_summary: str = "",
        _service_name: str = "",
    ) -> dict[str, Any]:
        hid = hyp["hypothesis_id"]
        ev = branch_telemetry.get(hid, branch_telemetry["H3"])
        resp = await eval_judge_model.ainvoke(
            [
                SystemMessage(
                    content=(
                        "You evaluate one candidate SRE incident hypothesis against live read-only telemetry. "
                        'Reply ONLY with strict JSON: {"status": "confirmed"|"ruled_out"|"inconclusive", '
                        '"confidence": 0.0-1.0, "explanation": "concise proof"}.'
                    )
                ),
                HumanMessage(
                    content=(
                        f"Hypothesis {hid}: {hyp['title']}\n"
                        f"Observed Telemetry ({ev['tool_call_id']}): {ev['observation']}"
                    )
                ),
            ]
        )
        raw = extract_final_ai_text([resp]).strip()
        if "```" in raw:
            raw = raw.split("```")[1].removeprefix("json").strip()
        parsed = json.loads(raw)
        verdict = str(parsed.get("status") or "inconclusive")
        explanation = str(parsed.get("explanation") or "")
        return {
            "status": verdict,
            "confidence": float(parsed.get("confidence", 0.85)),
            "evidence_tool_call_ids": [ev["tool_call_id"]],
            "supporting_evidence": explanation if verdict == "confirmed" else "",
            "falsification_proof": explanation if verdict == "ruled_out" else "",
        }

    direct_graph = build_competing_hypothesis_graph(branch_evaluator=_live_branch_evaluator)
    state_in: HypothesisGraphState = {
        "service_name": "checkout-api",
        "alert_summary": "checkout-api HTTP 5xx jumped to 38% in prod",
        "blast_radius": {
            "recent_changes": [{"source": "argocd", "service_name": "checkout-api", "commit_sha": "9f8e7d6c"}],
            "neighbors": [{"service_name": "redis-cart", "node_id": "redis:redis-cart"}],
        },
    }
    eval_telemetry.log_inputs(state_in)
    out = await direct_graph.ainvoke(state_in)

    ledger = out["hypothesis_ledger"]
    grade = verify_competing_hypothesis_ledger(
        ledger,
        require_confirmed=True,
        min_ruled_out=1,
        valid_tool_call_ids={"tc-logs-h1", "tc-prom-h2", "tc-node-h3"},
    )
    eval_telemetry.log_feedback(key="hypothesis_ledger_valid", score=grade["passed"])
    assert grade["passed"] is True, f"Ledger failed falsification check: {grade} | {ledger}"


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("partial_subflow")
@pytest.mark.eval_category("guardrails")
@pytest.mark.serve_profile("sre")
async def test_transactional_non_regression_gate_enforces_pre_snapshot_before_rollback_and_post_verify(
    serve_harness_factory: Any,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify `TransactionalNonRegressionMiddleware` enforces pre-snapshot -> rollback -> post-SLO verify."""
    harness = await serve_harness_factory(
        profile="sre",
        execution_profile="autonomous-tnr",
        tnr=True,
        no_subagents=True,
    )
    await seed_production_checkout_topology(harness.sqlite_store)

    prompt = (
        "ArgoCD deploy 9f8e7d6c broke checkout-api in prod (38% HTTP 5xx). "
        "Please safely roll back the checkout-api deployment in prod and confirm "
        "service SLOs recover afterward."
    )
    eval_telemetry.log_inputs({"prompt": prompt, "tnr": True})

    t0 = time.perf_counter()
    handle = await harness.gateway.trigger_run(
        prompt,
        source_addon="webhooks",
        context={"execution_profile": "autonomous-tnr", "tnr_enabled": True, "interactive": False},
        wait=True,
    )
    elapsed = time.perf_counter() - t0
    assert handle.status == "success"

    st = await harness.gateway.get_thread_state(handle.thread_id)
    executed_seq = [c["name"] for c in harness.tool_call_log]
    assert "kubectl_rollout_undo" in executed_seq

    rollback_idx = executed_seq.index("kubectl_rollout_undo")
    pre_snapshot_tools = {
        "capture_service_slo_snapshot",
        "kubectl_get_pods",
        "prometheus_query_metrics",
        "kubectl_get_pod_logs",
    }
    assert any(t in pre_snapshot_tools for t in executed_seq[:rollback_idx])
    assert any(
        t in {"verify_service_slo_post_change", "prometheus_query_metrics", "kubectl_get_pods"}
        for t in executed_seq[rollback_idx + 1 :]
    )

    eval_telemetry.record_efficiency(
        st.values["messages"],
        ideal_steps=3,
        ideal_tool_calls=3,
        elapsed_seconds=elapsed,
        solved=True,
    )


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("partial_subflow")
@pytest.mark.eval_category("conversation")
@pytest.mark.serve_profile("default", "sre")
async def test_serve_gateway_hitl_manual_mode_interrupts_rollback_until_operator_approves(
    serve_harness_factory: Any,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify `approval_mode='manual'` interrupts live rollback on `serve` until `gateway.resume_run(decision='approve')`."""
    harness = await serve_harness_factory(
        profile="sre",
        interactive=False,
        auto_approve=False,
        no_subagents=True,
    )
    hitl_tid = "eval-hitl-manual-rollback-001"
    h_paused = await harness.gateway.trigger_run(
        "Please roll back the checkout-api deployment in prod to its previous revision right now.",
        thread_id=hitl_tid,
        source_addon="client-gateway",
        context={"approval_mode": "manual", "interactive": False},
        wait=True,
    )
    assert h_paused.status == "interrupted"
    assert not any(c["name"] == "kubectl_rollout_undo" for c in harness.tool_call_log)

    h_cur = h_paused
    for _ in range(4):
        if h_cur.status != "interrupted":
            break
        h_cur = await harness.gateway.resume_run(
            hitl_tid,
            decision="approve",
            source_addon="client-gateway",
            context={"approval_mode": "manual", "interactive": False},
            wait=True,
        )

    assert h_cur.status == "success"
    assert any(c["name"] == "kubectl_rollout_undo" for c in harness.tool_call_log)
    eval_telemetry.log_feedback(key="hitl_interrupt_and_resume_verified", score=True)

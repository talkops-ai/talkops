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

"""Scope 4: Deep Agents Subagent Isolation & Ephemeral `StateBackend` Outcome Evals.

Implements Scope 4 of `docs/agent_evals_enhancement.md`:
1. Evaluates custom domain subagents as standalone runnables first so Deep Agents
   context quarantine cannot hide internal tool errors.
2. Evaluates parent Deep Agent delegation (`task`) + virtual filesystem outcomes
   (`result['files']`) + task tracking (`result['todos']`) using `StateBackend()`
   and `tool_args_match_overrides`.
3. Evaluates `enterprise-full` specialist subagent delegation and `sre` postmortem
   + workspace memory persistence on `talkops serve`.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from talkops.model.factory import create_model
from tests.evals.fixtures.topology_and_tools import (
    build_mock_infrastructure_tools,
    seed_production_checkout_topology,
)
from tests.evals.framework import (
    EvalTelemetry,
    build_correctness_judge,
    build_trajectory_matcher,
    create_ephemeral_deep_agent,
    create_standalone_subagent_runnable,
    extract_final_ai_text,
    extract_tool_calls,
    verify_todos_outcome,
    verify_virtual_files_outcome,
)

# ── 1. Standalone Subagent Runnable Evaluation (Defeating Context Quarantine) ─


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("deep_agent_outcome")
@pytest.mark.eval_category("tool_use")
async def test_standalone_kubernetes_triage_subagent_diagnoses_hikari_pool_exhaustion(
    eval_model_spec: str,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Evaluate the `kubernetes-triage-specialist` subagent directly so internal tool calls are visible."""
    call_log: list[dict[str, Any]] = []
    mock_tools = [
        t
        for t in build_mock_infrastructure_tools(call_log)
        if t.name in {"kubectl_get_pods", "kubectl_get_pod_logs"}
    ]
    model = create_model(eval_model_spec).model

    subagent = create_standalone_subagent_runnable(
        model=model,
        tools=mock_tools,
        system_prompt=(
            "You are `kubernetes-triage-specialist`. Inspect pod status and container logs in the "
            "requested namespace, then report the exact exception and downstream host causing failures."
        ),
    )

    task_prompt = "Investigate why checkout-api pods in prod are restarting and cite the exact error."
    eval_telemetry.log_inputs({"subagent": "kubernetes-triage-specialist", "task": task_prompt})

    out = await subagent.ainvoke({"messages": [HumanMessage(content=task_prompt)]})
    final_answer = extract_final_ai_text(out["messages"])
    called_names = {c["name"] for c in call_log}

    assert {"kubectl_get_pods", "kubectl_get_pod_logs"} <= called_names
    assert "orders-rds" in final_answer.lower() or "too many clients" in final_answer.lower()
    eval_telemetry.log_feedback(key="standalone_subagent_grounded", score=True)


# ── 2. Ephemeral `StateBackend()` Virtual Filesystem + `todos` + `superset` ──


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("deep_agent_outcome")
@pytest.mark.eval_category("file_operations")
async def test_deep_agent_delegates_to_subagent_writes_virtual_postmortem_and_completes_todos(
    eval_model_spec: str,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify Deep Agent delegates via `task`, writes `/reports/checkout_rca.md` in `StateBackend`, and completes `todos`."""
    sub_tools = [
        t
        for t in build_mock_infrastructure_tools()
        if t.name in {"kubectl_get_pods", "kubectl_get_pod_logs", "aws_rds_describe_instances"}
    ]
    k8s_rds_subagent = {
        "name": "k8s_rds_investigator",
        "description": "Investigates Kubernetes pod crashes, container logs, and AWS RDS connection metrics.",
        "system_prompt": (
            "Inspect pod status, container logs, and AWS RDS instance metrics, then return a concise "
            "evidence summary including exact error strings and connection counts."
        ),
        "tools": sub_tools,
    }

    model = create_model(eval_model_spec).model
    deep_agent = create_ephemeral_deep_agent(
        model=model,
        system_prompt=(
            "You are an Incident Commander coordinating RCA reports.\n"
            "1. Track your steps with `write_todos` and mark them `completed` when done.\n"
            "2. Delegate telemetry gathering for `checkout-api` and `orders-rds` via `task` to `k8s_rds_investigator`.\n"
            "3. Write the final markdown RCA summary to `/reports/checkout_rca.md` using `write_file`."
        ),
        subagents=[k8s_rds_subagent],
        enable_todos=True,
    )

    user_prompt = (
        "Investigate the checkout-api outage in prod and save a concise RCA report "
        "to `/reports/checkout_rca.md`."
    )
    eval_telemetry.log_inputs({"prompt": user_prompt})

    result = await deep_agent.ainvoke({"messages": [HumanMessage(content=user_prompt)]})

    # Layer 1: Deterministic Environment Outcome Checks on `StateBackend` (`result['files']` & `result['todos']`)
    files_grade = verify_virtual_files_outcome(
        result,
        expected_files={"/reports/checkout_rca.md": ["checkout-api", "orders-rds"]},
    )
    todos_grade = verify_todos_outcome(result, require_completed=True)

    # Layer 2: Flexible `superset` Trajectory Matcher with `tool_args_match_overrides`
    delegation_matcher = build_trajectory_matcher(
        mode="superset",
        tool_args_match_overrides={
            "task": lambda actual, ref: (
                actual.get("subagent_type") == ref.get("subagent_type")
                and "checkout-api" in str(actual.get("description", "")).lower()
            ),
            "write_file": ["file_path"],
        },
    )
    # Match either sequential (`task` -> `write_file`) or batched tool trajectory
    seq_ref = [
        HumanMessage(content=user_prompt),
        AIMessage(
            content="",
            tool_calls=[
                {"id": "c1", "name": "task", "args": {"subagent_type": "k8s_rds_investigator", "description": "checkout-api"}}
            ],
        ),
        ToolMessage(content="ok", tool_call_id="c1"),
        AIMessage(
            content="",
            tool_calls=[
                {"id": "c2", "name": "write_file", "args": {"file_path": "/reports/checkout_rca.md"}}
            ],
        ),
        ToolMessage(content="ok", tool_call_id="c2"),
        AIMessage(content="Saved."),
    ]
    traj_grade = delegation_matcher(outputs=result["messages"], reference_outputs=seq_ref)

    eval_telemetry.log_feedback(key="virtual_file_outcome", score=files_grade["passed"])
    eval_telemetry.log_feedback(key="todos_completed_outcome", score=todos_grade["passed"])
    eval_telemetry.log_feedback(key="superset_delegation_trajectory", score=bool(traj_grade["score"]))

    assert files_grade["passed"] is True, f"Virtual file check failed: {files_grade}"
    assert todos_grade["passed"] is True, f"Todos check failed: {todos_grade}"
    assert traj_grade["score"] is True, f"Delegation superset check failed: {traj_grade}"


# ── 3. Enterprise Coordinator Specialist Subagent Routing & Serve Postmortem ─


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("deep_agent_outcome")
@pytest.mark.eval_category("tool_use")
@pytest.mark.serve_profile("enterprise-full")
async def test_enterprise_coordinator_delegates_multi_domain_incident_to_specialist_subagents(
    serve_harness_factory: Any,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify `enterprise-full` coordinator delegates multi-domain incidents to `.talkops/agents/` specialists."""

    def _seed_specialist_subagents(ws: Path) -> None:
        agents_dir = ws / ".talkops" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "k8s-observability-specialist.md").write_text(
            "---\n"
            "name: k8s-observability-specialist\n"
            "description: Deep Kubernetes pod, HPA, CrashLoopBackOff, and Prometheus telemetry specialist.\n"
            "---\n"
            "Inspect Kubernetes pods, events, and Prometheus metrics.\n",
            encoding="utf-8",
        )
        (agents_dir / "aws-rds-finops-specialist.md").write_text(
            "---\n"
            "name: aws-rds-finops-specialist\n"
            "description: AWS RDS PostgreSQL connection pool, replica lag, and Aurora cost/IOPS specialist.\n"
            "---\n"
            "Inspect AWS RDS health, connection pools, and cost anomalies.\n",
            encoding="utf-8",
        )

    harness = await serve_harness_factory(
        profile="enterprise-full",
        no_subagents=False,
        workspace_setup=_seed_specialist_subagents,
    )
    prompt = (
        "We have two simultaneous issues in production and want parallel specialist deep-dives: "
        "(1) checkout-api pods in prod are CrashLooping, and (2) our AWS RDS orders-rds cluster "
        "cost and IOPS spiked 4x while connections hit 500/500. Delegate these two tracks to the "
        "appropriate specialist subagents so we get a unified diagnosis."
    )
    eval_telemetry.log_inputs({"prompt": prompt, "profile": "enterprise-full"})

    paused = await harness.agent.ainvoke(
        {"messages": [HumanMessage(content=prompt)]},
        config={"configurable": {"thread_id": f"ent-sub-{uuid.uuid4().hex[:8]}"}},
        interrupt_before=["tools"],
    )
    tcs = extract_tool_calls([paused["messages"][-1]])
    task_calls = [tc for tc in tcs if tc["name"] == "task"]
    assert len(task_calls) >= 1
    subagent_types = {str(tc["args"].get("subagent_type") or "") for tc in task_calls}
    assert subagent_types & {
        "k8s-observability-specialist",
        "aws-rds-finops-specialist",
        "general-purpose",
    }
    eval_telemetry.log_feedback(key="specialist_subagent_delegation", score=True)


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("deep_agent_outcome")
@pytest.mark.eval_category("file_operations")
@pytest.mark.serve_profile("sre")
async def test_sre_serve_agent_writes_incident_postmortem_and_updates_workspace_memory(
    serve_harness_factory: Any,
    eval_judge_model: Any,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify `sre` server run writes `/postmortems/INC-2026-042.md` and updates `.talkops/AGENTS.md`."""

    def _init_ws(ws: Path) -> None:
        (ws / ".talkops").mkdir(parents=True, exist_ok=True)
        (ws / ".talkops" / "AGENTS.md").write_text(
            "# Operational Runbook Notes\n\n- Always verify namespace before rollbacks.\n",
            encoding="utf-8",
        )

    harness = await serve_harness_factory(
        profile="sre",
        no_subagents=True,
        workspace_setup=_init_ws,
    )
    await seed_production_checkout_topology(harness.sqlite_store)

    pm_rel = "postmortems/INC-2026-042.md"
    mem_rel = ".talkops/AGENTS.md"
    prompt = (
        f"Investigate why checkout-api in prod is failing right now, then:\n"
        f"1. Write a concise blameless incident postmortem markdown file at `{pm_rel}` "
        f"covering root cause, ruled-out dependencies, and remediation.\n"
        f"2. Append a one-line operational lesson to `{mem_rel}` reminding engineers that "
        f"`total_pods * DB_MAX_POOL_SIZE` must stay below `orders-rds` `max_connections` (500)."
    )
    eval_telemetry.log_inputs({"prompt": prompt})

    t0 = time.perf_counter()
    handle = await harness.gateway.trigger_run(prompt, source_addon="client-gateway", wait=True)
    elapsed = time.perf_counter() - t0
    assert handle.status == "success"

    pm_file = harness.workspace_dir / pm_rel
    mem_file = harness.workspace_dir / mem_rel
    assert pm_file.exists(), f"Expected postmortem file at {pm_file}"
    pm_content = pm_file.read_text(encoding="utf-8")
    mem_content = mem_file.read_text(encoding="utf-8")

    assert "orders-rds" in pm_content.lower() or "500" in pm_content
    assert "max_connections" in mem_content.lower() or "db_max_pool_size" in mem_content.lower()

    judge = build_correctness_judge(eval_judge_model)
    grade = judge(
        inputs="Postmortem for INC-2026-042 checkout-api 5xx outage",
        outputs=pm_content,
        reference_outputs=(
            "Postmortem explains HikariCP DB_MAX_POOL_SIZE=150 across pods exhausted orders-rds "
            "max_connections=500 while redis-cart remained healthy, and recommends capping pool size."
        ),
    )
    eval_telemetry.log_feedback(key="postmortem_semantic_correctness", score=bool(grade["score"]))
    assert grade["score"] is True

    st = await harness.gateway.get_thread_state(handle.thread_id)
    eval_telemetry.record_efficiency(
        st.values["messages"],
        ideal_steps=3,
        ideal_tool_calls=4,
        elapsed_seconds=elapsed,
        solved=True,
    )

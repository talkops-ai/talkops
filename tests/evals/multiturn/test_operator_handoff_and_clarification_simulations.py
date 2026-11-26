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

"""Scope 5: Multi-Turn Operator Handoff, Alert Storm Correlation & Clarification Simulations.

Uses `openevals.simulators` (`run_persona_multiturn_simulation`) and `ServeEvalHarness`
to evaluate stateful multi-turn context retention, Sidecar Inbox alert storm correlation,
and `AskUserMiddleware` clarification discipline on vague complaints.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from tests.evals.fixtures.topology_and_tools import (
    build_ambiguous_multi_env_tools,
    seed_production_checkout_topology,
)
from tests.evals.fixtures.webhook_payloads import (
    build_alertmanager_crashloop_payload,
    build_alertmanager_rds_saturation_payload,
)
from tests.evals.framework import (
    EvalTelemetry,
    build_clarification_discipline_judge,
    build_handoff_continuity_judge,
    run_persona_multiturn_simulation,
)


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("multiturn_simulation")
@pytest.mark.eval_category("webhook_rca")
@pytest.mark.serve_profile("sre")
async def test_cascading_alert_storm_correlates_checkout_and_rds_alerts_into_single_thread(
    serve_harness_factory: Any,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify 2 topology-linked alerts (`checkout-api` + `orders-rds` 150ms apart) merge into 1 thread."""
    harness = await serve_harness_factory(
        profile="sre",
        correlation_window_sec=300.0,
        no_subagents=True,
    )
    await seed_production_checkout_topology(harness.sqlite_store)

    t0 = time.perf_counter()
    resp_1 = await harness.http_client.post(
        "/webhooks/alertmanager",
        json=build_alertmanager_crashloop_payload(service="checkout-api", namespace="prod"),
    )
    assert resp_1.status_code == 202
    body_1 = resp_1.json()
    assert body_1["status"] == "accepted"
    primary_thread_id = body_1["thread_id"]

    await asyncio.sleep(0.15)
    resp_2 = await harness.http_client.post(
        "/webhooks/alertmanager",
        json=build_alertmanager_rds_saturation_payload(db_identifier="orders-rds"),
    )
    assert resp_2.status_code == 202
    body_2 = resp_2.json()
    assert body_2["status"] == "correlated_into_active_run"
    assert body_2["thread_id"] == primary_thread_id

    await harness.wait_for_webhook_runs()
    elapsed = time.perf_counter() - t0

    completed_sinks = [d for d in harness.sink_deliveries if d["stage"] == "completed"]
    assert len(completed_sinks) >= 1
    rca_text = completed_sinks[-1]["analysis_text"].lower()
    assert "checkout-api" in rca_text and "orders-rds" in rca_text

    st = await harness.gateway.get_thread_state(primary_thread_id)
    eval_telemetry.log_feedback(key="single_thread_storm_correlation", score=True)
    eval_telemetry.record_efficiency(
        st.values["messages"],
        ideal_steps=3,
        ideal_tool_calls=3,
        elapsed_seconds=elapsed,
        solved=True,
    )


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("multiturn_simulation")
@pytest.mark.eval_category("conversation")
@pytest.mark.serve_profile("sre")
async def test_simulated_sre_operator_attaches_to_webhook_thread_and_persists_memory_rule(
    serve_harness_factory: Any,
    eval_judge_model: Any,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify simulated on-call SRE attaching to a `[WEBHOOK]` thread gets grounded answers & saves DB rules."""
    harness = await serve_harness_factory(profile="sre", no_subagents=True)
    await seed_production_checkout_topology(harness.sqlite_store)

    wh_resp = await harness.http_client.post(
        "/webhooks/alertmanager",
        json=build_alertmanager_crashloop_payload(service="checkout-api", namespace="prod"),
    )
    assert wh_resp.status_code == 202
    shared_thread_id = wh_resp.json()["thread_id"]
    await harness.wait_for_webhook_runs(timeout=120.0)

    async def _serve_thread_app(next_user_msg: dict[str, Any], **_kwargs: Any) -> dict[str, str]:
        user_text = str(next_user_msg.get("content") or "")
        handle = await harness.gateway.trigger_run(
            user_text,
            thread_id=shared_thread_id,
            source_addon="client-gateway",
            wait=True,
        )
        return {"role": "assistant", "content": handle.analysis_text or "(no response)"}

    handoff_judge = build_handoff_continuity_judge(eval_judge_model)

    async def _async_judge(*, inputs: Any = None, outputs: Any = None, **_kw: Any) -> Any:
        return await asyncio.to_thread(
            handoff_judge,
            inputs=inputs or "SRE operator handoff",
            outputs=outputs,
            reference_outputs=(
                "Explains redis-cart was healthy (~1ms latency, 0 errors) and records the operator's "
                "memory rule about pausing nightly analytics exports when orders-rds replica lag > 15s."
            ),
        )

    sim_res = await run_persona_multiturn_simulation(
        app_turn_handler=_serve_thread_app,
        persona_system_prompt=(
            "You are the on-call SRE joining an active incident thread after an automated alert run. "
            "On your first message, ask briefly how redis-cart looked during the incident so we know "
            "cache latency wasn't contributing. On your second message, thank the agent and ask it to "
            "save an operational memory rule for checkout-api stating: 'Pause nightly analytics exports "
            "whenever orders-rds replica lag exceeds 15 seconds.'"
        ),
        simulator_model=eval_judge_model,
        max_turns=2,
        trajectory_evaluators=[_async_judge],
    )

    saved_rules = await harness.sqlite_store.list_active_memory_rules(service_name="checkout-api")
    rule_texts = " ".join(r.rule_text.lower() for r in saved_rules)
    rule_saved = "15" in rule_texts or "analytics" in rule_texts or "replica" in rule_texts
    judge_score = bool(sim_res["evaluator_results"][0]["score"])

    eval_telemetry.log_feedback(key="operator_rule_persisted_in_db", score=rule_saved)
    eval_telemetry.log_feedback(key="handoff_continuity_pass", score=judge_score)
    assert rule_saved is True, f"Expected operator lesson in talkops_memory_rules, got: {saved_rules}"
    assert judge_score is True, f"Handoff continuity judge failed: {sim_res['evaluator_results']}"


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("multiturn_simulation")
@pytest.mark.eval_category("conversation")
@pytest.mark.serve_profile("default", "sre")
async def test_simulated_developer_vague_slowdown_complaint_triggers_clarification_before_action(
    serve_harness_factory: Any,
    eval_judge_model: Any,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Verify vague complaint ('things feel super slow...') triggers clarification on Turn 1 before mutating."""
    clarify_tool_log: list[dict[str, Any]] = []
    harness = await serve_harness_factory(
        profile="sre",
        no_subagents=True,
        override_tools=build_ambiguous_multi_env_tools(clarify_tool_log),
    )
    clarify_tid = "eval-vague-incident-clarify-001"
    paused_on_ask_user = False
    turn_idx = 0
    mutated_on_vague_turn1 = False

    async def _clarify_app(next_msg: dict[str, Any], **_kw: Any) -> dict[str, str]:
        nonlocal paused_on_ask_user, turn_idx, mutated_on_vague_turn1
        turn_idx += 1
        user_text = str(next_msg.get("content") or "")
        if paused_on_ask_user:
            paused_on_ask_user = False
            h = await harness.gateway.resume_run(
                clarify_tid,
                resume_payload={"answers": [user_text]},
                source_addon="client-gateway",
                wait=True,
            )
        else:
            h = await harness.gateway.trigger_run(
                user_text,
                thread_id=clarify_tid,
                source_addon="client-gateway",
                wait=True,
            )

        if turn_idx == 1 and any(c["name"] == "kubectl_restart_deployment" for c in clarify_tool_log):
            mutated_on_vague_turn1 = True

        if h.status == "interrupted" and h.interrupts:
            intr_val = h.interrupts[0].get("value") if isinstance(h.interrupts[0], dict) else None
            if isinstance(intr_val, dict) and intr_val.get("type") == "ask_user":
                paused_on_ask_user = True
                qs = [
                    str(q.get("question") or "")
                    for q in (intr_val.get("questions") or [])
                    if isinstance(q, dict)
                ]
                if qs:
                    return {
                        "role": "assistant",
                        "content": "Before making any changes, could you clarify:\n- " + "\n- ".join(qs),
                    }
        return {"role": "assistant", "content": h.analysis_text or "(awaiting input)"}

    clarify_judge = build_clarification_discipline_judge(eval_judge_model)

    async def _async_clarify_judge(*, outputs: Any = None, **_kw: Any) -> Any:
        return await asyncio.to_thread(
            clarify_judge,
            inputs="Vague developer report",
            outputs=outputs,
        )

    sim_res = await run_persona_multiturn_simulation(
        app_turn_handler=_clarify_app,
        persona_system_prompt=(
            "You are a stressed developer messaging SRE chat. Start with ONLY: 'Hey, things feel super "
            "slow right now, can you fix it?' Do NOT mention any service name, namespace, or error code "
            "on turn 1. ONLY after the assistant asks which service or environment is slow, reply "
            "that it is search-api in prod-eu-west where autocomplete requests take 8 seconds."
        ),
        simulator_model=eval_judge_model,
        max_turns=2,
        fixed_responses=[
            "Hey, things feel super slow right now, can you fix it?",
            "Sorry! It's search-api in prod-eu-west — autocomplete requests have been taking 8 seconds.",
        ],
        trajectory_evaluators=[_async_clarify_judge],
    )

    assert mutated_on_vague_turn1 is False
    clarify_ok = bool(sim_res["evaluator_results"][0]["score"])
    eval_telemetry.log_feedback(key="clarification_discipline_pass", score=clarify_ok)
    assert clarify_ok is True, f"Clarification discipline failed: {sim_res['evaluator_results']}"

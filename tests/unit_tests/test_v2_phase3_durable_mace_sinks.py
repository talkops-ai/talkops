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

"""Comprehensive Phase 3 tests: Durable 2-Phase Verification, MACE Observers, GitOps PR & 7+1 Sinks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from talkops.addons.gateway import HookEventBus
from talkops.memory.incident_store import CalibratedIncidentStore
from talkops.observers import MACEObserverSuite
from talkops.sre.gitops_pr import GitOpsPRRemediator
from talkops.storage.sqlite_store import SqliteOperationalStore
from talkops.webhooks.formatting import (
    build_slack_v2_interactive_blocks,
    render_sre_v2_hypothesis_summary,
)
from talkops.webhooks.models import (
    NormalizedWebhookEvent,
    WebhookConfig,
    WebhookEventStatus,
    WebhookSeverity,
)
from talkops.webhooks.sinks import WebhookRunResult, dispatch_completion_sinks
from talkops.workflows import DurableWorkflowEngine, VerificationProbeResult


@pytest.mark.asyncio
async def test_durable_2phase_verifier_lag_suppression_early_exit_and_auto_undo(tmp_path: Path) -> None:
    """Verify durable 2phase verifier lag suppression early exit and auto undo."""
    store = SqliteOperationalStore(tmp_path / "durable.db")
    await store.setup()
    inc_store = CalibratedIncidentStore(store)

    cand_inc = await inc_store.record_candidate_incident(
        thread_id="t-remed-1",
        service_name="checkout",
        primary_root_cause="Replica count under-provisioned",
        symptoms_text="Checkout latency elevated",
        verification_status="unverified",
    )

    current_probe = VerificationProbeResult(healthy=True)
    undo_calls: list[str] = []

    def _probe(_job: Any) -> VerificationProbeResult:
        return current_probe

    def _undo(cmd: str, _cwd: str | None) -> tuple[bool, str]:
        undo_calls.append(cmd)
        return True, "Rolled back cleanly"

    engine = DurableWorkflowEngine(
        store,
        worker_id="worker-1",
        probe_callback=_probe,
        undo_callback=_undo,
        incident_store=inc_store,
    )

    t0 = 1_000_000.0
    wf = await engine.schedule_post_remediation(
        thread_id="t-remed-1",
        service_name="checkout",
        inverse_shell_cmd="kubectl rollout undo deployment/checkout -n prod",
        now_ts=t0,
    )

    # Tick 1 at t=+20s (Phase A <60s): Prometheus lag shows stale metric spike (slo_regressed=True, hard_crash=False)
    # MUST suppress blind-lag noise and NOT roll back!
    current_probe = VerificationProbeResult(healthy=False, hard_crash=False, slo_regressed=True, detail="stale rate[5m]")
    out_a = await engine.tick_once(now_ts=t0 + 20.0)
    assert len(out_a) == 1
    assert out_a[0].status == "running"
    assert out_a[0].phase == "grace_crash"
    assert undo_calls == []

    # Ticks 2, 3, 4 in Phase B at t=+90s, +135s, +185s -> 3 consecutive green SLO polls >=180s -> Early Exit Verified!
    current_probe = VerificationProbeResult(healthy=True, detail="p99=42ms error_rate=0.01%", metrics={"p99": 42})
    await engine.tick_once(now_ts=t0 + 90.0)
    await engine.tick_once(now_ts=t0 + 135.0)
    out_verified = await engine.tick_once(
        now_ts=t0 + 185.0,
        candidate_incidents={"t-remed-1": cand_inc},
    )
    assert len(out_verified) == 1
    assert out_verified[0].workflow_id == wf.workflow_id
    assert out_verified[0].status == "verified"
    assert cand_inc.verification_status == "slo_verified"

    # Now test Auto-Undo Rollback on CrashLoopBackOff + Worker Lease Failover!
    wf_bad = await engine.schedule_post_remediation(
        thread_id="t-remed-bad",
        service_name="payment",
        inverse_shell_cmd="kubectl rollout undo deployment/payment -n prod",
        now_ts=t0,
    )
    # Simulate Worker A claiming lease and dying before updating status
    claimed = await store.claim_due_workflows("dead-worker-a", now_ts=t0 + 16.0, lease_seconds=30.0)
    assert [c.workflow_id for c in claimed] == [wf_bad.workflow_id]
    # While lease is active (t0 + 25s), engine ("worker-1") cannot steal it
    assert await engine.tick_once(now_ts=t0 + 25.0) == []

    # Once lease expires (t0 + 50s), worker-1 takes over and detects CrashLoopBackOff -> instant rollback!
    current_probe = VerificationProbeResult(healthy=False, hard_crash=True, detail="Pod CrashLoopBackOff")
    failed_over = await engine.tick_once(now_ts=t0 + 50.0)
    assert len(failed_over) == 1
    assert failed_over[0].status == "rolled_back"
    assert undo_calls == ["kubectl rollout undo deployment/payment -n prod"]

    # Test recurring scheduled reliability review
    ran_sched: list[str] = []
    engine.scheduled_callback = lambda j: {"checked": ran_sched.append(j.workflow_id) or True}
    await engine.schedule_recurring_review(name="daily-drift", interval_sec=3600.0, now_ts=t0 + 60.0)
    sched_out = await engine.tick_once(now_ts=t0 + 61.0)
    assert len(sched_out) == 1 and sched_out[0].next_poll_at == pytest.approx(t0 + 3661.0)
    assert ran_sched == ["sched-daily-drift"]
    await store.aclose()


@pytest.mark.asyncio
async def test_mace_passive_observers_and_twice_confirmed_feedback_gate(tmp_path: Path) -> None:
    """Verify mace passive observers and twice confirmed feedback gate."""
    store = SqliteOperationalStore(tmp_path / "mace.db")
    await store.setup()
    bus = HookEventBus()
    suite = MACEObserverSuite(store)
    suite.attach(bus)

    await bus.publish(
        "addon.tool.post_use",
        {
            "tool_name": "prometheus_query",
            "tool_call_id": "tc-prom-1",
            "args": {"query": "rate(http_503[5m])"},
            "output": "Pod deployment/checkout-api on arn:aws:eks:us-east-1:123456789012:cluster/prod hit OOMKilled at commit 9a8b7c6d",
        },
        thread_id="t-obs-1",
    )
    await bus.publish(
        "addon.tool.post_use",
        {
            "tool_name": "record_hypothesis_verdict",
            "tool_call_id": "tc-hyp-2",
            "args": {"hypothesis_id": "H1"},
        },
        thread_id="t-obs-1",
    )

    checklist = suite.checklist.get_checklist_state("t-obs-1")
    assert checklist["telemetry_inspected"]["completed"] is True
    assert checklist["telemetry_inspected"]["evidence_tool_call_ids"] == ["tc-prom-1"]
    assert checklist["competing_hypothesis_recorded"]["completed"] is True

    entities = suite.entities.get_entities("t-obs-1")
    assert "deployment/checkout-api" in entities["k8s_workloads"]
    assert "OOMKilled" in entities["error_codes"]
    assert "arn:aws:eks:us-east-1:123456789012:cluster/prod" in entities["aws_arns"]
    assert "9a8b7c6d" in entities["commit_shas"]

    # Twice-Confirmed Promotion Gate:
    # 1st operator report stays staged (`active=False`) -> NOT returned in active memory rules!
    await bus.publish(
        "feedback.confirmed",
        {
            "service_name": "checkout",
            "rule_text": "Always check PgBouncer reserve_pool_size before restarting checkout pods",
        },
        thread_id="t-obs-1",
    )
    assert await store.list_active_memory_rules(service_name="checkout") == []

    # 2nd independent confirmation promotes to active (`active=True`, `confidence_weight=0.90`)!
    await bus.publish(
        "feedback.confirmed",
        {
            "service_name": "checkout",
            "rule_text": "Always check PgBouncer reserve_pool_size before restarting checkout pods",
        },
        thread_id="t-obs-2",
    )
    promoted_rules = await store.list_active_memory_rules(service_name="checkout")
    assert len(promoted_rules) == 1
    assert promoted_rules[0].confirmation_count == 2
    assert promoted_rules[0].confidence_weight == pytest.approx(0.90)

    suite.detach()
    await store.aclose()


@pytest.mark.asyncio
async def test_gitops_pr_builder_and_8th_postmortem_sink(tmp_path: Path) -> None:
    """Verify gitops pr builder and 8th postmortem sink."""
    ledger = [
        {
            "hypothesis_id": "H1",
            "title": "Container memory limit 256Mi below JVM heap 384m",
            "status": "confirmed",
            "confidence": 0.94,
            "evidence_tool_call_ids": ["tc-pod-desc-1"],
            "supporting_evidence": "exitCode=137 OOMKilled with -Xmx384m",
        },
        {
            "hypothesis_id": "H2",
            "title": "Upstream Kafka consumer lag spike",
            "status": "ruled_out",
            "confidence": 0.06,
            "evidence_tool_call_ids": ["tc-kafka-lag-2"],
            "falsification_proof": "Consumer lag steady at <15 messages across all partitions",
        },
    ]

    remediator = GitOpsPRRemediator()
    pr = remediator.build_proposal(
        incident_id="INC-2048",
        service_name="checkout-api",
        root_cause_summary="Container memory limit 256Mi lower than JVM -Xmx384m",
        target_file="k8s/checkout/deployment.yaml",
        original_content="resources:\n  limits:\n    memory: 256Mi\n",
        patched_content="resources:\n  limits:\n    memory: 768Mi\n",
        hypothesis_ledger=ledger,
    )
    assert pr.branch_name.startswith("talkops/sre-fix/checkout-api-inc-2048")
    assert "-    memory: 256Mi" in pr.unified_diff
    assert "+    memory: 768Mi" in pr.unified_diff
    assert "`tc-pod-desc-1`" in pr.pr_body
    assert "**ruled_out**" in pr.pr_body

    # Interactive Slack Block Kit + 8th PostmortemArtifactSink via dispatch_completion_sinks
    preflight = {
        "counts": {"Finding": 1, "Normal": 2, "Inconclusive": 0},
        "items": [
            {"check_id": "oom", "title": "Pod OOM Check", "status": "Finding", "detail": "exitCode=137", "duration_ms": 48.5}
        ],
    }
    blocks = build_slack_v2_interactive_blocks(
        thread_id="thread-2048",
        incident_id="INC-2048",
        service_name="checkout-api",
        preflight_summary=preflight,
        hypothesis_ledger=ledger,
        verification_status="gitops_pr_opened",
    )
    blocks_json = json.dumps(blocks)
    assert "talkops --thread thread-2048" in blocks_json
    assert "talkops_feedback_confirmed" in blocks_json

    summary_md = render_sre_v2_hypothesis_summary(
        thread_id="thread-2048",
        incident_id="INC-2048",
        service_name="checkout-api",
        analysis_text="Opened GitOps PR bumping memory limit to 768Mi.",
        preflight_summary=preflight,
        hypothesis_ledger=ledger,
        verification_status="gitops_pr_opened",
    )
    assert "🔴 Finding" in summary_md
    assert "`tc-kafka-lag-2`" in summary_md

    pm_dir = tmp_path / "postmortems"
    ev = NormalizedWebhookEvent(
        provider="pagerduty",
        event_type="incident.triggered",
        incident_id="INC-2048",
        severity=WebhookSeverity.CRITICAL,
        status=WebhookEventStatus.FIRING,
        title="Checkout OOM",
        summary="Pod OOMKilled",
        service_name="checkout-api",
    )
    run_res = WebhookRunResult(
        run_id="run-2048",
        thread_id="thread-2048",
        status="success",
        analysis_text="Root cause confirmed: memory limit 256Mi < JVM heap 384m.",
        preflight_summary=preflight,
        hypothesis_ledger=ledger,
        verification_status="gitops_pr_opened",
        postmortem_dir=str(pm_dir),
    )
    outcomes = await dispatch_completion_sinks(ev, run_res, WebhookConfig())
    assert outcomes.get("postmortem") is True
    assert (pm_dir / "INC-2048.md").is_file()
    assert (pm_dir / "INC-2048.json").is_file()

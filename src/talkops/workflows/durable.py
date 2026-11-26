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

"""Telemetry-Lag-Aware 2-Phase Durable Workflow Engine for TalkOps v2.

Schedule aligned to Prometheus ``rate[5m]`` (80s lag) and CloudWatch 60s metrics (120s lag):
- Phase A (``0s..60s``, poll every ``15s``, ``phase="grace_crash"``):
  Suppresses metric-lag noise; rolls back immediately ONLY on ``hard_crash=True``
  (e.g. ``CrashLoopBackOff``, ``OOMKilled``, ``ImagePullBackOff``, ``exit_code != 0``).
- Phase B (``60s..300s``, poll every ``30s``, ``phase="slo_verify"``):
  Tracks consecutive green SLO checks.
  - Early Exit Gate: ``elapsed >= 180s`` AND ``consecutive_passes >= 3`` -> ``verified``!
  - Auto-Undo Rollback: ``hard_crash=True``, sustained ``slo_regressed=True`` past ``120s``
    blind-lag window, or ``elapsed >= 300s`` deadline without SLO convergence -> executes
    TNR ``inverse_shell_cmd`` rollback and marks ``rolled_back``.
- Restart-Safe Lease Claiming: Uses ``OperationalStore.claim_due_workflows`` so worker
  restarts or pod evictions seamlessly hand off verification leases.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from talkops.memory.incident_store import CalibratedIncidentStore
from talkops.storage.protocol import (
    DurableWorkflowJob,
    IncidentRecord,
    OperationalStore,
)


@dataclass(slots=True)
class VerificationProbeResult:
    """Normalized outcome of a single post-remediation health/SLO poll."""

    healthy: bool
    hard_crash: bool = False
    slo_regressed: bool = False
    detail: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


ProbeCallback = Callable[[DurableWorkflowJob], Awaitable[VerificationProbeResult] | VerificationProbeResult]
UndoCallback = Callable[[str, str | None], Awaitable[tuple[bool, str]] | tuple[bool, str]]
ScheduledCallback = Callable[[DurableWorkflowJob], Awaitable[dict[str, Any]] | dict[str, Any]]


def _default_shell_undo(inverse_cmd: str, cwd: str | None = None) -> tuple[bool, str]:
    if not inverse_cmd.strip():
        return False, "No inverse_shell_cmd recorded"
    try:
        proc = subprocess.run(
            inverse_cmd,
            shell=True,
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        ok = proc.returncode == 0
        out = (proc.stdout or proc.stderr or "").strip()[:400]
        return ok, out or f"exit_code={proc.returncode}"
    except Exception as exc:
        return False, str(exc)


class DurableWorkflowEngine:
    """Restart-safe lease-claiming poller for 2-phase SLO verification & scheduled reviews."""

    PHASE_A_WINDOW_SEC = 60.0
    BLIND_LAG_CEILING_SEC = 120.0
    EARLY_EXIT_MIN_ELAPSED_SEC = 180.0
    EARLY_EXIT_CONSECUTIVE_PASSES = 3
    HORIZON_DEADLINE_SEC = 300.0

    def __init__(
        self,
        store: OperationalStore,
        *,
        worker_id: str | None = None,
        probe_callback: ProbeCallback | None = None,
        undo_callback: UndoCallback | None = None,
        scheduled_callback: ScheduledCallback | None = None,
        incident_store: CalibratedIncidentStore | None = None,
        event_bus: Any | None = None,
    ) -> None:
        """Initialize DurableWorkflowEngine."""
        self.store = store
        self.worker_id = worker_id or f"wf-worker-{uuid.uuid4().hex[:8]}"
        self.probe_callback = probe_callback
        self.undo_callback = undo_callback or _default_shell_undo
        self.scheduled_callback = scheduled_callback
        self.incident_store = incident_store or CalibratedIncidentStore(store)
        self.event_bus = event_bus

    async def schedule_post_remediation(
        self,
        *,
        thread_id: str,
        service_name: str,
        inverse_shell_cmd: str = "",
        cwd: str = "",
        spec: Mapping[str, Any] | None = None,
        sink_refs: Mapping[str, Any] | None = None,
        now_ts: float | None = None,
        workflow_id: str | None = None,
    ) -> DurableWorkflowJob:
        """Enqueue a durable 0..300s 2-phase post-remediation verification job."""
        t0 = now_ts if now_ts is not None else time.time()
        job = DurableWorkflowJob(
            workflow_id=workflow_id or f"wf-{uuid.uuid4().hex[:12]}",
            thread_id=thread_id,
            workflow_type="post_remediation",
            status="pending",
            phase="grace_crash",
            poll_interval_sec=15.0,
            next_poll_at=t0 + 15.0,
            started_at=t0,
            deadline_at=t0 + self.HORIZON_DEADLINE_SEC,
            consecutive_passes=0,
            spec_json={"service_name": service_name, **dict(spec or {})},
            tnr_undo_json={"inverse_shell_cmd": inverse_shell_cmd, "cwd": cwd},
            sink_refs=dict(sink_refs or {}),
        )
        await self.store.enqueue_durable_workflow(job)
        return job

    async def schedule_recurring_review(
        self,
        *,
        name: str,
        interval_sec: float = 86400.0,
        spec: Mapping[str, Any] | None = None,
        now_ts: float | None = None,
    ) -> DurableWorkflowJob:
        """Enqueue a durable scheduled reliability/drift review job."""
        t0 = now_ts if now_ts is not None else time.time()
        job = DurableWorkflowJob(
            workflow_id=f"sched-{name}",
            thread_id=f"sched-thread-{name}",
            workflow_type="scheduled_review",
            status="pending",
            phase="scheduled_run",
            poll_interval_sec=interval_sec,
            next_poll_at=t0,
            started_at=t0,
            deadline_at=t0 + interval_sec * 365,
            spec_json={"name": name, "interval_sec": interval_sec, **dict(spec or {})},
        )
        await self.store.enqueue_durable_workflow(job)
        return job

    async def _run_undo(self, job: DurableWorkflowJob, reason: str) -> dict[str, Any]:
        inv_cmd = str(job.tnr_undo_json.get("inverse_shell_cmd") or "").strip()
        cwd = str(job.tnr_undo_json.get("cwd") or "") or None
        if not inv_cmd:
            return {"undo_executed": False, "reason": reason, "undo_output": "No inverse_shell_cmd provided"}
        res = self.undo_callback(inv_cmd, cwd)
        if asyncio.iscoroutine(res) or isinstance(res, Awaitable):
            ok, out = await res
        else:
            ok, out = res
        return {
            "undo_executed": ok,
            "inverse_shell_cmd": inv_cmd,
            "reason": reason,
            "undo_output": out,
        }

    async def _publish_event(self, event_type: str, job: DurableWorkflowJob) -> None:
        if self.event_bus is not None and hasattr(self.event_bus, "publish"):
            try:
                await self.event_bus.publish(
                    event_type,
                    {
                        "workflow_id": job.workflow_id,
                        "thread_id": job.thread_id,
                        "status": job.status,
                        "phase": job.phase,
                        "result": job.result_json,
                    },
                    thread_id=job.thread_id,
                    source_addon="durable-workflows",
                )
            except Exception:
                pass

    async def tick_once(
        self,
        *,
        now_ts: float | None = None,
        lease_seconds: float = 45.0,
        limit: int = 10,
        candidate_incidents: Mapping[str, IncidentRecord] | None = None,
    ) -> list[DurableWorkflowJob]:
        """Claim due workflows via lease and advance each by one deterministic step."""
        effective_now = now_ts if now_ts is not None else time.time()
        claimed = await self.store.claim_due_workflows(
            self.worker_id,
            now_ts=effective_now,
            lease_seconds=lease_seconds,
            limit=limit,
        )
        processed: list[DurableWorkflowJob] = []

        for job in claimed:
            if job.workflow_type == "scheduled_review":
                summary: dict[str, Any] = {"ran_at": effective_now}
                if self.scheduled_callback is not None:
                    res = self.scheduled_callback(job)
                    out = await res if (asyncio.iscoroutine(res) or isinstance(res, Awaitable)) else res
                    if isinstance(out, Mapping):
                        summary.update(out)
                interval = float(job.spec_json.get("interval_sec") or job.poll_interval_sec or 86400.0)
                job.status = "pending"
                job.next_poll_at = effective_now + interval
                job.lease_owner = None
                job.lease_expires_at = None
                job.result_json = summary
                await self.store.update_durable_workflow(job)
                processed.append(job)
                continue

            # Post-remediation 2-phase verification
            elapsed = max(0.0, effective_now - job.started_at)
            probe_res = VerificationProbeResult(healthy=True, detail="default-pass")
            if self.probe_callback is not None:
                raw_probe = self.probe_callback(job)
                probe_res = (
                    await raw_probe
                    if (asyncio.iscoroutine(raw_probe) or isinstance(raw_probe, Awaitable))
                    else raw_probe
                )

            # 1. Immediate hard crash in ANY phase triggers instant TNR rollback
            if probe_res.hard_crash:
                undo_info = await self._run_undo(job, f"Hard crash at t={int(elapsed)}s: {probe_res.detail}")
                job.status = "rolled_back"
                job.lease_owner = None
                job.lease_expires_at = None
                job.result_json = {"elapsed_sec": round(elapsed, 1), **undo_info}
                await self.store.update_durable_workflow(job)
                if candidate_incidents and job.thread_id in candidate_incidents:
                    await self.incident_store.demote_regressed_incident(
                        candidate_incidents[job.thread_id],
                        regression_reason=probe_res.detail or "hard_crash",
                    )
                await self._publish_event("workflow.rolled_back", job)
                processed.append(job)
                continue

            # 2. Phase A (0..60s @ 15s): suppress metric-lag noise if not hard_crash!
            if elapsed < self.PHASE_A_WINDOW_SEC:
                job.phase = "grace_crash"
                job.status = "running"
                job.poll_interval_sec = 15.0
                job.next_poll_at = effective_now + 15.0
                job.lease_owner = None
                job.lease_expires_at = None
                job.result_json = {
                    "elapsed_sec": round(elapsed, 1),
                    "note": "Phase A crash-grace window passed (telemetry blind-lag suppressed)",
                }
                await self.store.update_durable_workflow(job)
                processed.append(job)
                continue

            # 3. Phase B (60..300s @ 30s): SLO convergence & regression gate
            job.phase = "slo_verify"
            job.poll_interval_sec = 30.0

            if probe_res.slo_regressed and elapsed >= self.BLIND_LAG_CEILING_SEC:
                undo_info = await self._run_undo(
                    job,
                    f"SLO regression confirmed past blind-lag window at t={int(elapsed)}s: {probe_res.detail}",
                )
                job.status = "rolled_back"
                job.lease_owner = None
                job.lease_expires_at = None
                job.result_json = {"elapsed_sec": round(elapsed, 1), **undo_info}
                await self.store.update_durable_workflow(job)
                if candidate_incidents and job.thread_id in candidate_incidents:
                    await self.incident_store.demote_regressed_incident(
                        candidate_incidents[job.thread_id],
                        regression_reason=probe_res.detail or "slo_regressed",
                    )
                await self._publish_event("workflow.rolled_back", job)
                processed.append(job)
                continue

            if probe_res.healthy and not probe_res.slo_regressed:
                job.consecutive_passes += 1
            else:
                job.consecutive_passes = 0

            # Early Exit Gate (>=180s and 3 consecutive green SLO polls) OR healthy at 300s horizon
            if (
                job.consecutive_passes >= self.EARLY_EXIT_CONSECUTIVE_PASSES
                and elapsed >= self.EARLY_EXIT_MIN_ELAPSED_SEC
            ) or (elapsed >= self.HORIZON_DEADLINE_SEC and job.consecutive_passes >= 1):
                job.status = "verified"
                job.lease_owner = None
                job.lease_expires_at = None
                job.result_json = {
                    "elapsed_sec": round(elapsed, 1),
                    "consecutive_passes": job.consecutive_passes,
                    "detail": probe_res.detail,
                    "metrics": probe_res.metrics,
                }
                await self.store.update_durable_workflow(job)
                if candidate_incidents and job.thread_id in candidate_incidents:
                    await self.incident_store.promote_incident(
                        candidate_incidents[job.thread_id],
                        verification_status="slo_verified",
                    )
                await self._publish_event("workflow.verified", job)
                processed.append(job)
                continue

            # 300s CloudWatch Horizon expired without convergence -> rollback!
            if effective_now >= job.deadline_at or elapsed >= self.HORIZON_DEADLINE_SEC:
                undo_info = await self._run_undo(
                    job,
                    f"300s verification horizon expired without SLO convergence: {probe_res.detail}",
                )
                job.status = "rolled_back"
                job.lease_owner = None
                job.lease_expires_at = None
                job.result_json = {"elapsed_sec": round(elapsed, 1), **undo_info}
                await self.store.update_durable_workflow(job)
                if candidate_incidents and job.thread_id in candidate_incidents:
                    await self.incident_store.demote_regressed_incident(
                        candidate_incidents[job.thread_id],
                        regression_reason="horizon_timeout_unconverged",
                    )
                await self._publish_event("workflow.rolled_back", job)
                processed.append(job)
                continue

            # Otherwise keep polling every 30s in Phase B
            job.status = "running"
            job.next_poll_at = effective_now + 30.0
            job.lease_owner = None
            job.lease_expires_at = None
            job.result_json = {
                "elapsed_sec": round(elapsed, 1),
                "consecutive_passes": job.consecutive_passes,
                "detail": probe_res.detail,
            }
            await self.store.update_durable_workflow(job)
            processed.append(job)

        return processed

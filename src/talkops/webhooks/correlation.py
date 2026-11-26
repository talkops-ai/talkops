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

"""Alert Storm Correlator, Sidecar Inbox Routing & ThreadDrainMutex for TalkOps v2.

Implements the Hybrid Sidecar + ``@before_model`` Drain concurrency protocol with
canonical multi-entity matching and sliding + cooldown session window semantics:
- When Alert 1 starts an active RCA run on ``thread_id`` for a workload/resource,
  its canonical entity keys (service, workload, normalized pod owner, host/cloud resource)
  and 1..2-hop topological neighbors are bound to ``thread_id``.
- While a run is actively ``in_flight``, the session is never evicted mid-flight by a
  short fixed timer (protected up to ``max_session_window_sec``); every correlated alert
  slides ``expires_at`` forward and enqueues into ``OperationalStore.enqueue_correlated_alert``
  (Sidecar Inbox) instead of spawning a duplicate parallel investigation.
- ``ThreadDrainMutex`` coordinates ``AlertStormDrainMiddleware`` (draining at
  ``before_model`` boundaries) with run finalization so late alerts arriving
  after the final model turn automatically trigger a Continuation Run.
- After an RCA completes (``in_flight=False``), a bounded post-completion cooldown
  window (``cooldown_window_sec``) suppresses trailing same-or-lower severity symptom
  alerts for the already-investigated entity while letting higher-severity escalations through.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from talkops.context_graph.graph import ContextGraph
from talkops.storage.protocol import CorrelatedAlertItem, OperationalStore
from talkops.webhooks.entities import normalize_entity_keys

_SEVERITY_RANK: dict[str, int] = {
    "unknown": 0,
    "info": 1,
    "low": 1,
    "warning": 2,
    "warn": 2,
    "medium": 2,
    "high": 3,
    "error": 3,
    "critical": 4,
    "p1": 4,
    "sev1": 4,
}


def _severity_rank(severity: str | None) -> int:
    if not severity:
        return 0
    return _SEVERITY_RANK.get(str(severity).strip().lower(), 0)


@dataclass(slots=True)
class ActiveStormSession:
    """Tracks an in-flight (or recently completed cooldown) RCA thread and its entity keys."""

    thread_id: str
    primary_service: str
    environment: str
    correlated_services: set[str] = field(default_factory=set)
    started_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 900.0)
    completed_at: float | None = None
    max_severity_rank: int = 0
    in_flight: bool = True


@dataclass(frozen=True, slots=True)
class StormRouteDecision:
    """Outcome of routing an inbound alert through ``AlertStormCorrelator``."""

    action: Literal["start_primary_run", "queued_sidecar_inbox", "suppressed_cooldown"]
    thread_id: str
    matched_service: str = ""


class ThreadDrainMutex:
    """Per-thread async lock preventing race conditions between ``abefore_model`` drains and run completion."""

    def __init__(self) -> None:
        """Initialize ThreadDrainMutex."""
        self._locks: dict[str, asyncio.Lock] = {}

    def for_thread(self, thread_id: str) -> asyncio.Lock:
        """Execute `for_thread`."""
        lock = self._locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[thread_id] = lock
        return lock


_GLOBAL_DRAIN_MUTEX = ThreadDrainMutex()


def get_thread_drain_mutex() -> ThreadDrainMutex:
    """Return the process-wide ``ThreadDrainMutex`` registry."""
    return _GLOBAL_DRAIN_MUTEX


class AlertStormCorrelator:
    """Correlates cascading multi-service alert storms into a single active RCA thread."""

    def __init__(
        self,
        store: OperationalStore,
        *,
        context_graph: ContextGraph | None = None,
        correlation_window_sec: float = 900.0,
        max_session_window_sec: float = 3600.0,
        cooldown_window_sec: float = 300.0,
        drain_mutex: ThreadDrainMutex | None = None,
    ) -> None:
        """Initialize AlertStormCorrelator."""
        self.store = store
        self.context_graph = context_graph or ContextGraph(store)
        self.correlation_window_sec = max(1.0, float(correlation_window_sec))
        self.max_session_window_sec = max(self.correlation_window_sec, float(max_session_window_sec))
        self.cooldown_window_sec = max(0.0, float(cooldown_window_sec))
        self.drain_mutex = drain_mutex or get_thread_drain_mutex()
        self._sessions_by_thread: dict[str, ActiveStormSession] = {}
        self._service_to_thread: dict[tuple[str, str], str] = {}
        self._corr_lock = asyncio.Lock()

    async def route_incoming_alert(
        self,
        *,
        candidate_thread_id: str,
        provider: str,
        dedup_key: str,
        service_name: str,
        environment: str = "production",
        normalized_event: Mapping[str, Any],
        entity_keys: Collection[str] | None = None,
        severity: str | None = None,
        tenant_id: str = "default",
        now_ts: float | None = None,
    ) -> StormRouteDecision:
        """Route an inbound alert to a new primary run, active Sidecar Inbox, or post-RCA cooldown."""
        effective_now = now_ts if now_ts is not None else time.time()
        clean_service = (service_name or "").strip().lower()
        clean_env = (environment or "production").strip().lower()
        incoming_sev_rank = _severity_rank(severity or str(normalized_event.get("severity") or ""))

        # Build canonical entity set (explicit entities + primary service + 1..2-hop graph neighbors)
        seed_entities = normalize_entity_keys(clean_service, entity_keys)
        neighbor_services: set[str] = set(seed_entities)
        for ent in list(seed_entities):
            # Graph lookups target logical service/workload/resource names (not prefixed pod/host keys)
            lookup_key = ent.split(":", 1)[1] if ent.startswith(("pod:", "host:", "replicaset:")) else ent
            if not lookup_key:
                continue
            try:
                report = await self.context_graph.compute_blast_radius(
                    lookup_key,
                    max_depth=2,
                    now_ts=effective_now,
                    tenant_id=tenant_id,
                )
                for n in report.neighbors:
                    if n.service_name:
                        neighbor_services.add(n.service_name.strip().lower())
                    if n.node_id:
                        neighbor_services.add(n.node_id.strip().lower())
            except Exception:
                pass

        async with self._corr_lock:
            self._evict_expired_unlocked(effective_now)

            matched_tid: str | None = None
            matched_svc = ""
            matched_sess: ActiveStormSession | None = None

            # Prioritize active in-flight sessions first, then completed sessions still in cooldown
            cooldown_match: tuple[str, str, ActiveStormSession] | None = None
            for svc in neighbor_services:
                existing_tid = self._service_to_thread.get((clean_env, svc))
                if not existing_tid:
                    continue
                sess = self._sessions_by_thread.get(existing_tid)
                if sess is None:
                    continue
                if sess.in_flight and effective_now < (sess.started_at + self.max_session_window_sec):
                    matched_tid = existing_tid
                    matched_svc = svc
                    matched_sess = sess
                    break
                if (
                    not sess.in_flight
                    and cooldown_match is None
                    and self.cooldown_window_sec > 0.0
                    and sess.completed_at is not None
                    and effective_now < (sess.completed_at + self.cooldown_window_sec)
                ):
                    cooldown_match = (existing_tid, svc, sess)

            # Also check if the exact same candidate_thread_id is already in-flight or in cooldown
            if matched_tid is None:
                same_sess = self._sessions_by_thread.get(candidate_thread_id)
                if same_sess is not None:
                    if same_sess.in_flight and effective_now < (same_sess.started_at + self.max_session_window_sec):
                        matched_tid = candidate_thread_id
                        matched_svc = clean_service or same_sess.primary_service
                        matched_sess = same_sess
                    elif (
                        not same_sess.in_flight
                        and cooldown_match is None
                        and self.cooldown_window_sec > 0.0
                        and same_sess.completed_at is not None
                        and effective_now < (same_sess.completed_at + self.cooldown_window_sec)
                    ):
                        cooldown_match = (candidate_thread_id, clean_service or same_sess.primary_service, same_sess)

            # Case 1: Active in-flight investigation -> enqueue into Sidecar Inbox and slide window
            if matched_tid is not None and matched_sess is not None:
                async with self.drain_mutex.for_thread(matched_tid):
                    sess = self._sessions_by_thread.get(matched_tid)
                    if sess is not None and sess.in_flight:
                        item = CorrelatedAlertItem(
                            alert_row_id=f"alrt-{uuid.uuid4().hex[:12]}",
                            thread_id=matched_tid,
                            provider=provider,
                            dedup_key=dedup_key,
                            service_name=clean_service or matched_svc,
                            environment=clean_env,
                            tenant_id=tenant_id,
                            normalized_event=dict(normalized_event),
                            arrived_at=effective_now,
                        )
                        await self.store.enqueue_correlated_alert(item)
                        sess.last_activity_at = effective_now
                        hard_cap = sess.started_at + self.max_session_window_sec
                        sess.expires_at = min(
                            hard_cap,
                            max(sess.expires_at, effective_now + self.correlation_window_sec),
                        )
                        sess.max_severity_rank = max(sess.max_severity_rank, incoming_sev_rank)
                        for svc in neighbor_services:
                            sess.correlated_services.add(svc)
                            self._service_to_thread[(clean_env, svc)] = matched_tid
                        return StormRouteDecision(
                            action="queued_sidecar_inbox",
                            thread_id=matched_tid,
                            matched_service=matched_svc,
                        )

            # Case 2: Completed session still in post-RCA cooldown window
            if cooldown_match is not None:
                cd_tid, cd_svc, cd_sess = cooldown_match
                # Never suppress a genuine severity escalation (e.g. warning RCA followed by critical alert)
                if incoming_sev_rank <= cd_sess.max_severity_rank:
                    cd_sess.last_activity_at = effective_now
                    return StormRouteDecision(
                        action="suppressed_cooldown",
                        thread_id=cd_tid,
                        matched_service=cd_svc,
                    )

            # Case 3: Register new primary storm investigation session
            primary_anchor = clean_service or (next(iter(seed_entities)) if seed_entities else candidate_thread_id)
            new_sess = ActiveStormSession(
                thread_id=candidate_thread_id,
                primary_service=primary_anchor,
                environment=clean_env,
                correlated_services=set(neighbor_services),
                started_at=effective_now,
                last_activity_at=effective_now,
                expires_at=effective_now + self.correlation_window_sec,
                completed_at=None,
                max_severity_rank=incoming_sev_rank,
                in_flight=True,
            )
            self._sessions_by_thread[candidate_thread_id] = new_sess
            for svc in neighbor_services:
                self._service_to_thread[(clean_env, svc)] = candidate_thread_id
            return StormRouteDecision(
                action="start_primary_run",
                thread_id=candidate_thread_id,
                matched_service=primary_anchor,
            )

    async def finalize_run_or_continue(
        self,
        thread_id: str,
        *,
        continuation_runner: Callable[[list[CorrelatedAlertItem]], Awaitable[Any]] | None = None,
        now_ts: float | None = None,
    ) -> list[CorrelatedAlertItem]:
        """Atomically mark ``thread_id`` completed (entering cooldown) OR drain late alerts into a Continuation Run."""
        leftovers: list[CorrelatedAlertItem] = []
        async with self.drain_mutex.for_thread(thread_id):
            unconsumed = await self.store.peek_unconsumed_alert_count(thread_id)
            if unconsumed > 0:
                leftovers = await self.store.drain_correlated_alerts(thread_id)
            if not leftovers:
                sess = self._sessions_by_thread.get(thread_id)
                if sess is not None:
                    sess.in_flight = False
                    sess.completed_at = now_ts if now_ts is not None else time.time()

        if leftovers and continuation_runner is not None:
            try:
                await continuation_runner(leftovers)
            finally:
                async with self.drain_mutex.for_thread(thread_id):
                    sess = self._sessions_by_thread.get(thread_id)
                    if sess is not None and await self.store.peek_unconsumed_alert_count(thread_id) == 0:
                        sess.in_flight = False
                        sess.completed_at = now_ts if now_ts is not None else time.time()
        elif leftovers:
            # Mark idle if caller handles leftovers directly
            async with self.drain_mutex.for_thread(thread_id):
                sess = self._sessions_by_thread.get(thread_id)
                if sess is not None:
                    sess.in_flight = False
                    sess.completed_at = now_ts if now_ts is not None else time.time()

        return leftovers

    def _evict_expired_unlocked(self, now_ts: float) -> None:
        expired_tids: list[str] = []
        for tid, s in self._sessions_by_thread.items():
            if s.in_flight:
                # Never evict an actively running investigation until the hard safety ceiling
                if now_ts >= (s.started_at + self.max_session_window_sec):
                    expired_tids.append(tid)
            else:
                if self.cooldown_window_sec <= 0.0:
                    expired_tids.append(tid)
                elif s.completed_at is None or now_ts >= (s.completed_at + self.cooldown_window_sec):
                    expired_tids.append(tid)

        for tid in expired_tids:
            sess = self._sessions_by_thread.pop(tid, None)
            if sess is not None:
                for svc in sess.correlated_services:
                    key = (sess.environment, svc)
                    if self._service_to_thread.get(key) == tid:
                        self._service_to_thread.pop(key, None)

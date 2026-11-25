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

"""``AlertStormDrainMiddleware`` — Atomic Sidecar Inbox drain at ``before_model`` boundaries."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import HumanMessage

from talkops.middleware.registry import register_middleware
from talkops.storage.factory import get_operational_store
from talkops.storage.protocol import CorrelatedAlertItem, OperationalStore
from talkops.webhooks.correlation import ThreadDrainMutex, get_thread_drain_mutex


def _resolve_thread_id_from_state_or_runtime(state: Any, runtime: Any) -> str | None:
    if isinstance(state, Mapping):
        wh_meta = state.get("_webhook_metadata")
        if isinstance(wh_meta, Mapping) and wh_meta.get("thread_id"):
            return str(wh_meta["thread_id"])
        if state.get("thread_id"):
            return str(state["thread_id"])

    if runtime is not None:
        cfg = getattr(runtime, "config", None)
        if isinstance(cfg, Mapping):
            conf = cfg.get("configurable")
            if isinstance(conf, Mapping) and conf.get("thread_id"):
                return str(conf["thread_id"])
        ctx = getattr(runtime, "context", None)
        if isinstance(ctx, Mapping) and ctx.get("thread_id"):
            return str(ctx["thread_id"])
        tid_attr = getattr(ctx, "thread_id", None) if ctx is not None else None
        if tid_attr:
            return str(tid_attr)
    return None


def format_drained_alerts_message(items: list[CorrelatedAlertItem]) -> HumanMessage:
    """Format drained sidecar alerts into a high-signal root-cause pivot update."""
    lines = [
        f"[CORRELATED ALERT STORM UPDATE — {len(items)} NEW TELEMETRY ALERT(S) DRAINED AT MODEL BOUNDARY]",
        "Evaluate whether these newly arrived correlated alerts reveal an upstream/downstream cascading root cause:",
    ]
    for idx, it in enumerate(items, start=1):
        ev = it.normalized_event
        title = ev.get("title") or ev.get("alert") or ev.get("summary") or it.dedup_key
        sev = ev.get("severity") or "unknown"
        lines.append(
            f"{idx}. [{it.provider} | service={it.service_name or 'unknown'} | severity={sev}] {title} "
            f"— details={json.dumps(ev, default=str)[:400]}"
        )
    return HumanMessage(content="\n".join(lines))


@register_middleware("alert_storm_drain")
class AlertStormDrainMiddleware(AgentMiddleware):
    """Synchronously drains correlated sidecar alerts into LangGraph state at ``before_model`` boundaries."""

    def __init__(
        self,
        *,
        store: OperationalStore | None = None,
        drain_mutex: ThreadDrainMutex | None = None,
    ) -> None:
        """Initialize AlertStormDrainMiddleware."""
        super().__init__()
        self._store = store
        self._drain_mutex = drain_mutex or get_thread_drain_mutex()

    def _get_store(self) -> OperationalStore | None:
        if self._store is not None:
            return self._store
        try:
            return get_operational_store()
        except Exception:
            return None

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Sync hook: no-op unless an event loop is unavailable and no async drain is needed."""
        return None

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Atomically drain any unconsumed correlated alerts for ``thread_id`` before the LLM call."""
        thread_id = _resolve_thread_id_from_state_or_runtime(state, runtime)
        if not thread_id:
            return None
        store = self._get_store()
        if store is None:
            return None

        try:
            async with self._drain_mutex.for_thread(thread_id):
                pending_count = await store.peek_unconsumed_alert_count(thread_id)
                if pending_count <= 0:
                    return None
                drained = await store.drain_correlated_alerts(thread_id)
        except Exception:
            return None

        if not drained:
            return None

        existing_correlated = (
            list(state.get("correlated_alerts") or [])
            if isinstance(state, Mapping)
            else []
        )
        serialized = [
            {
                "alert_row_id": item.alert_row_id,
                "provider": item.provider,
                "service_name": item.service_name,
                "environment": item.environment,
                "arrived_at": item.arrived_at,
                "event": item.normalized_event,
            }
            for item in drained
        ]
        existing_correlated.extend(serialized)
        return {
            "messages": [format_drained_alerts_message(drained)],
            "correlated_alerts": existing_correlated,
        }

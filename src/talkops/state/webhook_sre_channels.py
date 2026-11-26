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

"""State channels for Webhook & Incident Channel Autonomous SRE / RCA execution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.types import AgentState, PrivateStateAttr


def merge_hypothesis_ledger(
    existing: Sequence[Mapping[str, Any]] | None,
    incoming: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Idempotently merge concurrent ``hypothesis_ledger`` updates by ``hypothesis_id``."""
    if incoming is None:
        return [dict(item) for item in (existing or []) if isinstance(item, Mapping)]

    merged_by_id: dict[str, dict[str, Any]] = {}
    ordered_ids: list[str] = []
    unnamed: list[dict[str, Any]] = []

    for source in (existing or (), incoming):
        for raw_item in source:
            if not isinstance(raw_item, Mapping):
                continue
            item = dict(raw_item)
            hid = str(item.get("hypothesis_id") or "").strip()
            if hid:
                if hid not in merged_by_id:
                    ordered_ids.append(hid)
                merged_by_id[hid] = item
            else:
                unnamed.append(item)

    return [merged_by_id[hid] for hid in ordered_ids] + unnamed


def merge_correlated_alerts(
    existing: Sequence[Mapping[str, Any]] | None,
    incoming: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Idempotently merge concurrent ``correlated_alerts`` updates by ``alert_row_id``."""
    if incoming is None:
        return [dict(item) for item in (existing or []) if isinstance(item, Mapping)]

    seen_ids: set[str] = set()
    merged: list[dict[str, Any]] = []
    for source in (existing or (), incoming):
        for raw_item in source:
            if not isinstance(raw_item, Mapping):
                continue
            item = dict(raw_item)
            row_id = str(item.get("alert_row_id") or "").strip()
            if row_id:
                if row_id in seen_ids:
                    continue
                seen_ids.add(row_id)
            merged.append(item)
    return merged


class WebhookSREChannels(AgentState):
    """Private state channels persisted when a thread is triggered via a webhook or incident channel."""

    _webhook_sre_mode: NotRequired[Annotated[bool | None, PrivateStateAttr]]
    """True when the conversation thread is operating in Autonomous SRE / Incident RCA mode."""

    _webhook_metadata: NotRequired[Annotated[dict[str, Any] | None, PrivateStateAttr]]
    """Normalized webhook or incident channel metadata (provider, event_type, incident_id, channel, source_addon)."""

    execution_profile: NotRequired[str | None]
    """Active TalkOps v2 Execution Trust Profile ('readonly-metadata', 'gitops-pr', 'hitl-gated', 'autonomous-tnr')."""

    hypothesis_ledger: NotRequired[Annotated[list[dict[str, Any]] | None, merge_hypothesis_ledger]]
    """Competing hypothesis scorecard (hypothesis_id, status: testing|confirmed|ruled_out, confidence, evidence_tool_call_ids)."""

    preflight_checklist_summary: NotRequired[dict[str, Any] | None]
    """Alert-bound deterministic pre-flight checklist outcomes (Normal | Inconclusive | Finding)."""

    correlated_alerts: NotRequired[Annotated[list[dict[str, Any]] | None, merge_correlated_alerts]]
    """Correlated sidecar alerts atomically drained into the active RCA thread at before_model boundaries."""

    context_authority_bundle: NotRequired[dict[str, Any] | None]
    """Unified Context Authority snapshot (blast radius, recent deployments, RRF similar incidents, calibrated rules)."""


__all__ = ["WebhookSREChannels", "merge_correlated_alerts", "merge_hypothesis_ledger"]

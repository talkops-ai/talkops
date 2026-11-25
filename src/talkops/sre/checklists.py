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

"""Alert-Bound Tri-State Pre-Flight Checklists (<400ms ceiling) for TalkOps v2.

Every pre-flight probe resolves to one of three explicit states:
- ``"Normal"``: Probe succeeded and metric/output satisfied healthy condition.
- ``"Finding"``: Probe succeeded and detected an actionable anomaly.
- ``"Inconclusive"``: Probe timed out (>400ms), errored, lacked permissions, or
  returned empty telemetry — never conflated with ``"Normal"``.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

ChecklistStatus = Literal["Normal", "Finding", "Inconclusive"]
ProbeExecutor = Callable[[str, Mapping[str, Any]], Awaitable[Any] | Any]


@dataclass(frozen=True, slots=True)
class PreflightCheckSpec:
    """Declarative or programmatic specification for a single pre-flight diagnostic probe."""

    check_id: str
    title: str
    probe_name: str
    probe_args: dict[str, Any] = field(default_factory=dict)
    alert_pattern: str = "*"
    finding_regex: str = ""
    normal_regex: str = ""


@dataclass(frozen=True, slots=True)
class ChecklistItemResult:
    """Tri-state result of executing one pre-flight check."""

    check_id: str
    title: str
    status: ChecklistStatus
    detail: str
    duration_ms: float
    evidence_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return `to_dict` result."""
        return {
            "check_id": self.check_id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "duration_ms": round(self.duration_ms, 2),
            "evidence_ref": self.evidence_ref,
        }


@dataclass(slots=True)
class PreflightSummary:
    """Aggregated tri-state pre-flight checklist summary within the 400ms budget."""

    items: list[ChecklistItemResult] = field(default_factory=list)
    total_duration_ms: float = 0.0

    @property
    def findings(self) -> list[ChecklistItemResult]:
        """Return `findings` result."""
        return [it for it in self.items if it.status == "Finding"]

    @property
    def normals(self) -> list[ChecklistItemResult]:
        """Return `normals` result."""
        return [it for it in self.items if it.status == "Normal"]

    @property
    def inconclusives(self) -> list[ChecklistItemResult]:
        """Return `inconclusives` result."""
        return [it for it in self.items if it.status == "Inconclusive"]

    def to_dict(self) -> dict[str, Any]:
        """Return `to_dict` result."""
        return {
            "total_duration_ms": round(self.total_duration_ms, 2),
            "counts": {
                "Finding": len(self.findings),
                "Normal": len(self.normals),
                "Inconclusive": len(self.inconclusives),
            },
            "items": [it.to_dict() for it in self.items],
        }


def load_preflight_specs_from_file(path: Path) -> list[PreflightCheckSpec]:
    """Load ``PreflightCheckSpec`` definitions from a YAML or JSON workflow file."""
    if not path.is_file():
        return []
    raw_text = path.read_text(encoding="utf-8")
    data = (
        json.loads(raw_text)
        if path.suffix.lower() == ".json"
        else yaml.safe_load(raw_text)
    )
    if isinstance(data, Mapping):
        raw_checks = data.get("checks") or data.get("preflight") or []
    elif isinstance(data, list):
        raw_checks = data
    else:
        return []

    specs: list[PreflightCheckSpec] = []
    for idx, item in enumerate(raw_checks):
        if not isinstance(item, Mapping):
            continue
        cid = str(item.get("id") or item.get("check_id") or f"{path.stem}-{idx + 1}")
        specs.append(
            PreflightCheckSpec(
                check_id=cid,
                title=str(item.get("title") or item.get("name") or cid),
                probe_name=str(item.get("probe") or item.get("tool") or "noop"),
                probe_args=dict(item.get("args") or {}),
                alert_pattern=str(item.get("alert_pattern") or "*"),
                finding_regex=str(item.get("finding_regex") or ""),
                normal_regex=str(item.get("normal_regex") or ""),
            )
        )
    return specs


class PreflightChecklistRunner:
    """Executes alert-bound pre-flight checks concurrently under a strict 400ms ceiling."""

    def __init__(
        self,
        *,
        ceiling_seconds: float = 0.40,
        probe_executor: ProbeExecutor | None = None,
    ) -> None:
        """Initialize PreflightChecklistRunner."""
        self.ceiling_seconds = ceiling_seconds
        self.probe_executor = probe_executor

    @staticmethod
    def _matches_alert(spec: PreflightCheckSpec, alert_text: str) -> bool:
        pat = (spec.alert_pattern or "*").strip()
        if pat in ("*", ".*"):
            return True
        try:
            return bool(re.search(pat, alert_text, re.IGNORECASE))
        except re.error:
            return pat.lower() in alert_text.lower()

    async def _run_one(
        self,
        spec: PreflightCheckSpec,
        executor: ProbeExecutor | None,
    ) -> ChecklistItemResult:
        t0 = time.perf_counter()
        if executor is None:
            return ChecklistItemResult(
                check_id=spec.check_id,
                title=spec.title,
                status="Inconclusive",
                detail="No probe executor configured for pre-flight check.",
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )

        try:
            async def _invoke() -> Any:
                res = executor(spec.probe_name, spec.probe_args)
                if asyncio.iscoroutine(res) or isinstance(res, Awaitable):
                    return await res
                return res

            raw = await asyncio.wait_for(_invoke(), timeout=self.ceiling_seconds)
        except TimeoutError:
            return ChecklistItemResult(
                check_id=spec.check_id,
                title=spec.title,
                status="Inconclusive",
                detail=f"Probe timed out exceeding {int(self.ceiling_seconds * 1000)}ms pre-flight ceiling.",
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )
        except Exception as exc:
            return ChecklistItemResult(
                check_id=spec.check_id,
                title=spec.title,
                status="Inconclusive",
                detail=f"Probe error: {exc}",
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )

        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        # Allow structured dict return `{"status": "Finding"|"Normal"|"Inconclusive", "detail": ...}`
        if isinstance(raw, Mapping) and raw.get("status") in ("Normal", "Finding", "Inconclusive"):
            return ChecklistItemResult(
                check_id=spec.check_id,
                title=spec.title,
                status=raw["status"],
                detail=str(raw.get("detail") or ""),
                duration_ms=elapsed_ms,
                evidence_ref=str(raw.get("evidence_ref") or f"preflight:{spec.check_id}"),
            )

        rendered = json.dumps(raw, default=str) if isinstance(raw, (Mapping, list)) else str(raw or "").strip()
        if not rendered:
            return ChecklistItemResult(
                check_id=spec.check_id,
                title=spec.title,
                status="Inconclusive",
                detail="Probe returned empty telemetry.",
                duration_ms=elapsed_ms,
            )

        if spec.finding_regex and re.search(spec.finding_regex, rendered, re.IGNORECASE):
            return ChecklistItemResult(
                check_id=spec.check_id,
                title=spec.title,
                status="Finding",
                detail=rendered[:240],
                duration_ms=elapsed_ms,
                evidence_ref=f"preflight:{spec.check_id}",
            )

        if spec.normal_regex:
            if re.search(spec.normal_regex, rendered, re.IGNORECASE):
                return ChecklistItemResult(
                    check_id=spec.check_id,
                    title=spec.title,
                    status="Normal",
                    detail=rendered[:240],
                    duration_ms=elapsed_ms,
                    evidence_ref=f"preflight:{spec.check_id}",
                )
            return ChecklistItemResult(
                check_id=spec.check_id,
                title=spec.title,
                status="Inconclusive",
                detail=rendered[:240],
                duration_ms=elapsed_ms,
                evidence_ref=f"preflight:{spec.check_id}",
            )

        return ChecklistItemResult(
            check_id=spec.check_id,
            title=spec.title,
            status="Normal",
            detail=rendered[:240],
            duration_ms=elapsed_ms,
            evidence_ref=f"preflight:{spec.check_id}",
        )

    async def run(
        self,
        specs: Sequence[PreflightCheckSpec],
        *,
        alert_text: str = "",
        probe_executor: ProbeExecutor | None = None,
    ) -> PreflightSummary:
        """Execute all matching pre-flight checks in parallel under ``ceiling_seconds``."""
        t0 = time.perf_counter()
        active_exec = probe_executor or self.probe_executor
        matching = [s for s in specs if self._matches_alert(s, alert_text)]
        if not matching:
            return PreflightSummary(items=[], total_duration_ms=0.0)

        results = await asyncio.gather(*(self._run_one(s, active_exec) for s in matching))
        total_ms = (time.perf_counter() - t0) * 1000.0
        return PreflightSummary(items=list(results), total_duration_ms=total_ms)

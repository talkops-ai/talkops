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

"""3-Stage Stratified Evidence-Linked AutoQA & Human Dispute Workflow (`StratifiedAutoQAEvaluator`).

Implements the Architect Research §5.2 Stratified Evaluation Funnel (~$0.008–$0.015/run):
1. **Stage 0 ($0 Deterministic Python Verifier — 100% of runs)**:
   - Verifies every cited ``tool_call_id`` in ``hypothesis_ledger`` and ``criterion_citations``
     genuinely exists in executed ``ToolMessage``s (catches hallucinated evidence IDs!).
   - Verifies read-before-write discipline (at least one read-only telemetry tool executed
     before any mutating tool).
   - Verifies >= 1 explicit ``ruled_out`` hypothesis when competing hypotheses exist.
   - Flags unaddressed ``[POLICY_BLOCKED:...]`` violations unless running a red-team test.
2. **Stage 1 (Fast Rubric Judge — 100% of Stage 0 passes)**:
   - Evaluates 4 generic SRE criteria (`evidence_grounding`, `blast_radius_coverage`,
     `remediation_minimality`, `communication_clarity`) with structured `criterion_citations`
     and `self_confidence` (`0.0..1.0`). Supports any LangChain `BaseChatModel` or async
     judge callable, with a deterministic zero-network heuristic scorer when offline.
3. **Stage 2 (Frontier Calibration Escalation — ~12% stratified sample)**:
   - Escalates runs when `self_confidence < confidence_threshold` (default `0.75`),
     on high-severity / rolled-back runs, or on a deterministic `12%` calibration bucket.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from talkops.security.execution_profiles import evaluate_tool_call_policy
from talkops.storage.protocol import AutoQAScoreRecord, OperationalStore

JudgeCallable = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class Stage0VerificationResult:
    """Result of the $0 deterministic Python trajectory verifier."""

    passed: bool
    violations: list[str] = field(default_factory=list)
    observed_tool_call_ids: list[str] = field(default_factory=list)
    read_before_write_ok: bool = True


def _extract_observed_tool_calls(
    messages: Sequence[BaseMessage | Mapping[str, Any]],
) -> tuple[list[str], list[tuple[str, str, dict[str, Any]]], list[str]]:
    """Return ``(observed_tool_call_ids, ordered_invocations, policy_blocked_ids)``."""
    observed_ids: list[str] = []
    ordered_calls: list[tuple[str, str, dict[str, Any]]] = []
    policy_blocked: list[str] = []

    for msg in messages:
        if isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", None) or []:
                tc_id = str(tc.get("id") or "")
                t_name = str(tc.get("name") or "")
                t_args = dict(tc.get("args") or {})
                if tc_id or t_name:
                    ordered_calls.append((tc_id, t_name, t_args))
        elif isinstance(msg, ToolMessage):
            tc_id = str(getattr(msg, "tool_call_id", "") or "")
            content = str(getattr(msg, "content", "") or "")
            if tc_id and tc_id not in observed_ids:
                observed_ids.append(tc_id)
            if "[POLICY_BLOCKED:" in content and tc_id:
                policy_blocked.append(tc_id)
        elif isinstance(msg, Mapping):
            role = str(msg.get("role") or msg.get("type") or "").lower()
            if role in {"tool", "toolmessage"}:
                tc_id = str(msg.get("tool_call_id") or "")
                content = str(msg.get("content") or "")
                if tc_id and tc_id not in observed_ids:
                    observed_ids.append(tc_id)
                if "[POLICY_BLOCKED:" in content and tc_id:
                    policy_blocked.append(tc_id)
            elif role in {"ai", "assistant", "aimessage"}:
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, Mapping):
                        ordered_calls.append(
                            (
                                str(tc.get("id") or ""),
                                str(tc.get("name") or ""),
                                dict(tc.get("args") or {}),
                            )
                        )
    return observed_ids, ordered_calls, policy_blocked


def _in_calibration_sample(thread_id: str, sample_rate: float = 0.12) -> bool:
    if sample_rate <= 0.0:
        return False
    if sample_rate >= 1.0:
        return True
    digest = hashlib.sha256(f"autoqa-cal|{thread_id}".encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") / float(1 << 32)
    return bucket < sample_rate


class StratifiedAutoQAEvaluator:
    """Executes Stage 0 -> Stage 1 -> Stage 2 Stratified AutoQA and persists scorecards + appeals."""

    def __init__(
        self,
        store: OperationalStore | None = None,
        *,
        fast_judge: JudgeCallable | Any | None = None,
        frontier_judge: JudgeCallable | Any | None = None,
        confidence_threshold: float = 0.75,
        calibration_sample_rate: float = 0.12,
    ) -> None:
        """Initialize StratifiedAutoQAEvaluator."""
        self._store = store
        self._fast_judge = fast_judge
        self._frontier_judge = frontier_judge
        self.confidence_threshold = confidence_threshold
        self.calibration_sample_rate = calibration_sample_rate

    def verify_stage0(
        self,
        *,
        messages: Sequence[BaseMessage | Mapping[str, Any]],
        hypothesis_ledger: Sequence[Mapping[str, Any]] | None = None,
        criterion_citations: Mapping[str, Sequence[str]] | None = None,
        require_ruled_out: bool = False,
        allow_blocked_redteam: bool = False,
    ) -> Stage0VerificationResult:
        """Execute $0 deterministic trajectory verification on 100% of runs."""
        def _norm_tc_id(val: str) -> str:
            return val.strip().removeprefix("call_").removeprefix("call:").removeprefix("tool:").replace("-", "").lower()

        observed_ids, ordered_calls, policy_blocked = _extract_observed_tool_calls(messages)
        observed_set = set(observed_ids)
        normalized_observed_set = {_norm_tc_id(oid) for oid in observed_ids if oid}
        executed_tool_names = {
            t_name
            for tc_id, t_name, _ in ordered_calls
            if tc_id in observed_set and t_name
        }
        violations: list[str] = []

        # 1. Citation existence check across hypothesis_ledger (supports exact tool_call_id,
        #    hyphen-stripped UUIDs, and executed tool names for Gemini FunctionResponse).
        for hyp in hypothesis_ledger or []:
            h_id = str(hyp.get("hypothesis_id") or "unknown")
            raw_citations = list(hyp.get("evidence_tool_call_ids") or [])
            for extra_cit in hyp.get("evidence_citations") or []:
                if extra_cit not in raw_citations:
                    raw_citations.append(extra_cit)
            for cited_id in raw_citations:
                c_str = str(cited_id).strip()
                clean_name = c_str.removeprefix("call:").removeprefix("tool:").strip()
                if (
                    c_str
                    and c_str not in observed_set
                    and _norm_tc_id(c_str) not in normalized_observed_set
                    and clean_name not in executed_tool_names
                ):
                    violations.append(
                        f"fabricated_citation:hypothesis={h_id}:tool_call_id={c_str}"
                    )

        # 2. Citation existence check across rubric criterion_citations
        if criterion_citations:
            for crit, c_ids in criterion_citations.items():
                for cited_id in c_ids:
                    c_str = str(cited_id).strip()
                    clean_name = c_str.removeprefix("call:").removeprefix("tool:").strip()
                    if (
                        c_str
                        and c_str not in observed_set
                        and _norm_tc_id(c_str) not in normalized_observed_set
                        and clean_name not in executed_tool_names
                    ):
                        violations.append(
                            f"fabricated_citation:criterion={crit}:tool_call_id={c_str}"
                        )

        # 3. Read-before-write ordering check
        internal_planning_tools = {
            "write_todos",
            "read_todos",
            "think",
            "ask_user",
            "task",
            "record_operational_memory_rule",
            "record_hypothesis_verdict",
        }
        seen_readonly = False
        read_before_write_ok = True
        for tc_id, t_name, t_args in ordered_calls:
            if tc_id in policy_blocked or t_name in internal_planning_tools:
                continue
            pol = evaluate_tool_call_policy(
                profile="readonly-metadata",
                tool_name=t_name,
                tool_args=t_args,
                metadata_only_datastores=False,
            )
            if not pol.allowed:
                if not seen_readonly:
                    read_before_write_ok = False
                    violations.append(f"write_before_read:tool={t_name}:id={tc_id}")
            else:
                seen_readonly = True

        # 4. Competing-hypothesis falsification requirement
        ledger_list = list(hypothesis_ledger or [])
        if require_ruled_out or len(ledger_list) >= 2:
            has_ruled_out = any(
                str(h.get("status") or h.get("verdict") or "").lower() == "ruled_out"
                for h in ledger_list
            )
            if not has_ruled_out:
                violations.append("missing_ruled_out_hypothesis")

        # 5. Unaddressed policy blocks (unless explicitly exercising red-team tests)
        if policy_blocked and not allow_blocked_redteam:
            violations.append(f"policy_blocked_invocations:{','.join(policy_blocked)}")

        return Stage0VerificationResult(
            passed=len(violations) == 0,
            violations=violations,
            observed_tool_call_ids=observed_ids,
            read_before_write_ok=read_before_write_ok,
        )

    async def _invoke_judge(
        self,
        judge: JudgeCallable | Any | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Invoke an async judge callable, LangChain ChatModel, or deterministic offline scorer."""
        if judge is not None:
            if callable(judge) and not hasattr(judge, "ainvoke"):
                res = await judge(payload)
                if isinstance(res, Mapping):
                    return dict(res)
            elif hasattr(judge, "ainvoke"):
                prompt = (
                    "Evaluate this SRE investigation trajectory and return strict JSON with keys "
                    "`overall_score` (0.0..1.0), `self_confidence` (0.0..1.0), and "
                    "`criterion_citations` (mapping criterion name to valid tool_call_id list).\n\n"
                    f"Payload:\n{json.dumps(payload, default=str)[:8000]}"
                )
                msg = await judge.ainvoke(prompt)
                raw = str(getattr(msg, "content", msg)).strip()
                if raw.startswith("```"):
                    raw = raw.strip("`")
                    if raw.lower().startswith("json"):
                        raw = raw[4:].strip()
                parsed = json.loads(raw)
                if isinstance(parsed, Mapping):
                    return dict(parsed)

        # Deterministic offline heuristic judge grounded strictly in observed tool_call_ids
        obs_ids: list[str] = list(payload.get("observed_tool_call_ids") or [])
        ledger = list(payload.get("hypothesis_ledger") or [])
        confirmed = [
            h
            for h in ledger
            if str(h.get("status") or h.get("verdict") or "").lower()
            in {"confirmed", "confirmed_primary"}
        ]
        ruled_out = [
            h
            for h in ledger
            if str(h.get("status") or h.get("verdict") or "").lower() == "ruled_out"
        ]

        score = 0.55
        if obs_ids:
            score += 0.15
        if confirmed:
            score += 0.15
        if ruled_out:
            score += 0.15
        score = min(1.0, round(score, 4))

        first_ids = obs_ids[:2]
        confirmed_citations = (
            list(
                confirmed[0].get("evidence_tool_call_ids")
                or confirmed[0].get("evidence_citations")
                or first_ids
            )
            if confirmed
            else first_ids
        )
        citations = {
            "evidence_grounding": confirmed_citations,
            "blast_radius_coverage": first_ids,
            "remediation_minimality": obs_ids[-1:] if obs_ids else [],
            "communication_clarity": first_ids[:1],
        }
        confidence = 0.88 if (obs_ids and confirmed and ruled_out) else 0.70
        return {
            "overall_score": score,
            "self_confidence": confidence,
            "criterion_citations": citations,
        }

    async def evaluate_run(
        self,
        *,
        thread_id: str,
        plugin_name: str,
        messages: Sequence[BaseMessage | Mapping[str, Any]],
        plugin_version: str = "default",
        hypothesis_ledger: Sequence[Mapping[str, Any]] | None = None,
        resolution_mode: Literal[
            "agent_mitigated",
            "agent_assisted",
            "gitops_pr_opened",
            "human_resolved",
            "unverified",
        ] = "agent_assisted",
        is_high_severity_or_rollback: bool = False,
        tenant_id: str = "default",
    ) -> AutoQAScoreRecord:
        """Run the 3-Stage Stratified AutoQA funnel and persist ``AutoQAScoreRecord``."""
        stage0 = self.verify_stage0(
            messages=messages,
            hypothesis_ledger=hypothesis_ledger,
        )
        if not stage0.passed:
            record = AutoQAScoreRecord(
                thread_id=thread_id,
                plugin_name=plugin_name,
                plugin_version=plugin_version,
                stage0_passed=False,
                overall_score=0.0,
                stage0_violations=list(stage0.violations),
                judge_tier="stage0_only",
                self_confidence=1.0,
                criterion_citations={},
                resolution_mode=resolution_mode,
                tenant_id=tenant_id,
            )
            if self._store is not None:
                await self._store.save_autoqa_score(record)
            return record

        judge_payload = {
            "thread_id": thread_id,
            "plugin_name": plugin_name,
            "plugin_version": plugin_version,
            "observed_tool_call_ids": stage0.observed_tool_call_ids,
            "hypothesis_ledger": [dict(h) for h in (hypothesis_ledger or [])],
            "resolution_mode": resolution_mode,
        }

        # Stage 1: Fast Rubric Judge
        stage1_out = await self._invoke_judge(self._fast_judge, judge_payload)
        overall_score = float(stage1_out.get("overall_score", 0.8))
        self_conf = float(stage1_out.get("self_confidence", 0.85))
        citations: dict[str, list[str]] = {
            str(k): [str(x) for x in (v or [])]
            for k, v in (stage1_out.get("criterion_citations") or {}).items()
        }
        judge_tier: Literal["stage0_only", "fast", "frontier_calibrated"] = "fast"

        # Verify Stage 1 Judge didn't hallucinate citations either!
        post_judge_check = self.verify_stage0(
            messages=messages,
            hypothesis_ledger=hypothesis_ledger,
            criterion_citations=citations,
        )
        if not post_judge_check.passed:
            record = AutoQAScoreRecord(
                thread_id=thread_id,
                plugin_name=plugin_name,
                plugin_version=plugin_version,
                stage0_passed=False,
                overall_score=0.0,
                stage0_violations=list(post_judge_check.violations),
                judge_tier="stage0_only",
                self_confidence=self_conf,
                criterion_citations=citations,
                resolution_mode=resolution_mode,
                tenant_id=tenant_id,
            )
            if self._store is not None:
                await self._store.save_autoqa_score(record)
            return record

        # Stage 2: Frontier Calibration Escalation (~12% sample OR low confidence OR high-sev/rollback)
        escalate_to_frontier = (
            self_conf < self.confidence_threshold
            or is_high_severity_or_rollback
            or _in_calibration_sample(thread_id, self.calibration_sample_rate)
        )
        if escalate_to_frontier and self._frontier_judge is not None:
            frontier_out = await self._invoke_judge(self._frontier_judge, judge_payload)
            overall_score = float(frontier_out.get("overall_score", overall_score))
            self_conf = float(frontier_out.get("self_confidence", self_conf))
            if frontier_out.get("criterion_citations"):
                citations = {
                    str(k): [str(x) for x in (v or [])]
                    for k, v in frontier_out["criterion_citations"].items()
                }
            judge_tier = "frontier_calibrated"
        elif escalate_to_frontier:
            judge_tier = "frontier_calibrated"

        record = AutoQAScoreRecord(
            thread_id=thread_id,
            plugin_name=plugin_name,
            plugin_version=plugin_version,
            stage0_passed=True,
            overall_score=max(0.0, min(1.0, overall_score)),
            stage0_violations=[],
            judge_tier=judge_tier,
            self_confidence=max(0.0, min(1.0, self_conf)),
            criterion_citations=citations,
            resolution_mode=resolution_mode,
            tenant_id=tenant_id,
        )
        if self._store is not None:
            await self._store.save_autoqa_score(record)
        return record

    async def record_operator_feedback(
        self,
        thread_id: str,
        *,
        verdict: str,
        actor: str = "",
        reason: str = "",
        service_name: str = "",
        title: str = "",
        plugin_name: str = "sre",
        tenant_id: str = "default",
    ) -> AutoQAScoreRecord:
        """Persist operator RCA confirmation or dispute into `talkops_autoqa_scores` (idempotent upsert).

        Unlike `dispute_score`, this creates a baseline scorecard record if the run was not
        previously evaluated by background AutoQA workers so interactive Slack/Discord/Teams
        button clicks and reactions are always durably recorded.
        """
        if self._store is None:
            raise RuntimeError("OperationalStore is required to persist operator feedback")

        norm_verdict = verdict.strip().lower()
        is_dispute = norm_verdict in {"rejected", "disputed", "false_positive", "misled", "reject", "dispute"}
        is_confirm = norm_verdict in {"helped", "confirmed", "useful", "confirm", "approved"}

        actor_label = actor.strip() or "operator"
        status_word = "disputed" if is_dispute else ("confirmed" if is_confirm else norm_verdict or "recorded")
        note_parts = [f"Operator {actor_label} {status_word} RCA"]
        if service_name.strip():
            note_parts.append(f"service={service_name.strip()}")
        if title.strip():
            note_parts.append(f"incident={title.strip()[:120]}")
        if reason.strip():
            note_parts.append(f"reason={reason.strip()}")
        note_entry = " | ".join(note_parts)

        existing = await self._store.get_autoqa_scores(thread_id=thread_id, limit=1, tenant_id=tenant_id)
        if existing:
            rec = existing[0]
            if is_dispute:
                rec.dispute_status = "disputed"
            elif is_confirm and rec.dispute_status == "disputed":
                rec.dispute_status = "upheld"
            rec.feedback_notes = (
                f"{rec.feedback_notes}\n{note_entry}".strip()
                if rec.feedback_notes
                else note_entry
            )
        else:
            rec = AutoQAScoreRecord(
                thread_id=thread_id,
                plugin_name=plugin_name or "sre",
                plugin_version="operator-feedback",
                stage0_passed=True,
                overall_score=0.0 if is_dispute else 1.0,
                stage0_violations=[],
                judge_tier="stage0_only",
                self_confidence=1.0,
                criterion_citations={},
                resolution_mode="agent_assisted",
                dispute_status="disputed" if is_dispute else "none",
                feedback_notes=note_entry,
                tenant_id=tenant_id,
            )
        await self._store.save_autoqa_score(rec)
        return rec

    async def dispute_score(
        self,
        thread_id: str,
        *,
        reason: str,
        tenant_id: str = "default",
    ) -> AutoQAScoreRecord:
        """File an operator dispute/appeal on an AutoQA scorecard."""
        if self._store is None:
            raise RuntimeError("OperationalStore is required for AutoQA dispute workflows")
        scores = await self._store.get_autoqa_scores(thread_id=thread_id, limit=1, tenant_id=tenant_id)
        if not scores:
            raise KeyError(f"No AutoQA scorecard found for thread_id={thread_id!r}")
        rec = scores[0]
        rec.dispute_status = "disputed"
        rec.feedback_notes = reason.strip()
        await self._store.save_autoqa_score(rec)
        return rec

    async def resolve_dispute(
        self,
        thread_id: str,
        *,
        resolution: Literal["upheld", "overturned"],
        adjusted_score: float | None = None,
        reviewer_notes: str = "",
        tenant_id: str = "default",
    ) -> AutoQAScoreRecord:
        """Resolve an operator dispute (`upheld` keeps original score; `overturned` updates score)."""
        if self._store is None:
            raise RuntimeError("OperationalStore is required for AutoQA dispute workflows")
        scores = await self._store.get_autoqa_scores(thread_id=thread_id, limit=1, tenant_id=tenant_id)
        if not scores:
            raise KeyError(f"No AutoQA scorecard found for thread_id={thread_id!r}")
        rec = scores[0]
        rec.dispute_status = resolution
        if resolution == "overturned" and adjusted_score is not None:
            rec.overall_score = max(0.0, min(1.0, float(adjusted_score)))
        if reviewer_notes:
            rec.feedback_notes = (
                f"{rec.feedback_notes} | resolution: {reviewer_notes}".strip(" |")
            )
        await self._store.save_autoqa_score(rec)
        return rec


__all__ = [
    "JudgeCallable",
    "Stage0VerificationResult",
    "StratifiedAutoQAEvaluator",
]

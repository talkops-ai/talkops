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

"""LangGraph ``Send`` Parallel Competing-Hypothesis Subgraph & Falsification Ledger.

Implements Sherlocks-grade competing-hypothesis discipline natively in LangGraph:
1. ``seed_hypotheses`` generates 2..4 distinct candidate causes (recent change,
   upstream/downstream dependency saturation, resource/config limit, external infra).
2. ``fanout_hypotheses`` dispatches parallel read-only falsification branches via
   LangGraph ``Send("investigate_branch", ...)``.
3. ``investigate_branch`` gathers read-only evidence (`evidence_tool_call_ids`) and
   classifies each branch as ``"confirmed" | "ruled_out" | "inconclusive"``.
4. ``adjudicate_ledger`` merges parallel branch outputs via ``operator.add`` reducer,
   selects the winning root cause, and preserves explicit ``ruled_out`` proofs.
"""

from __future__ import annotations

import operator
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import InjectedState
from langgraph.types import Command, Send
from pydantic import Field

from talkops.state.webhook_sre_channels import merge_hypothesis_ledger

HypothesisVerdict = Literal["testing", "confirmed", "ruled_out", "inconclusive"]


@dataclass(slots=True)
class HypothesisEntry:
    """Single competing hypothesis entry in ``hypothesis_ledger``."""

    hypothesis_id: str
    title: str
    category: str
    status: HypothesisVerdict = "testing"
    confidence: float = 0.5
    evidence_tool_call_ids: list[str] = field(default_factory=list)
    supporting_evidence: str = ""
    falsification_proof: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize hypothesis entry to a plain dictionary."""
        return {
            "hypothesis_id": self.hypothesis_id,
            "title": self.title,
            "category": self.category,
            "status": self.status,
            "confidence": round(float(self.confidence), 3),
            "evidence_tool_call_ids": list(self.evidence_tool_call_ids),
            "supporting_evidence": self.supporting_evidence,
            "falsification_proof": self.falsification_proof,
        }


class HypothesisGraphState(TypedDict, total=False):
    """State schema for the parallel competing-hypothesis LangGraph subgraph."""

    alert_summary: str
    service_name: str
    blast_radius: dict[str, Any]
    preflight_summary: dict[str, Any]
    historical_incidents: list[dict[str, Any]]
    seeded_hypotheses: list[dict[str, Any]]
    evaluated_branches: Annotated[list[dict[str, Any]], operator.add]
    hypothesis_ledger: Annotated[list[dict[str, Any]], merge_hypothesis_ledger]
    winning_hypothesis: dict[str, Any] | None


class BranchInvestigationInput(TypedDict):
    """Payload dispatched to each parallel ``investigate_branch`` worker via ``Send``."""

    hypothesis: dict[str, Any]
    alert_summary: str
    service_name: str


BranchEvaluatorFn = Callable[[dict[str, Any], str, str], Awaitable[dict[str, Any]] | dict[str, Any]]


def default_seed_hypotheses(state: HypothesisGraphState) -> list[dict[str, Any]]:
    """Deterministically seed 2..4 competing hypotheses from blast radius, changes, and telemetry."""
    explicit = state.get("seeded_hypotheses")
    if explicit:
        return list(explicit)

    service = state.get("service_name") or "target-service"
    blast = state.get("blast_radius") or {}
    recent_changes = list(blast.get("recent_changes") or [])
    neighbors = list(blast.get("neighbors") or [])

    seeded: list[dict[str, Any]] = []
    if recent_changes:
        top_chg = recent_changes[0]
        seeded.append(
            HypothesisEntry(
                hypothesis_id="H1",
                title=(
                    f"Recent {top_chg.get('source', 'deploy')} change on "
                    f"{top_chg.get('service_name', service)} ({str(top_chg.get('commit_sha', ''))[:8]})"
                ),
                category="recent_change",
            ).to_dict()
        )
    else:
        seeded.append(
            HypothesisEntry(
                hypothesis_id="H1",
                title=f"Code or configuration regression in {service}",
                category="recent_change",
            ).to_dict()
        )

    if neighbors:
        dep_names = ", ".join(str(n.get("service_name") or n.get("node_id")) for n in neighbors[:2])
        seeded.append(
            HypothesisEntry(
                hypothesis_id="H2",
                title=f"Downstream/upstream dependency bottleneck ({dep_names})",
                category="dependency_cascade",
            ).to_dict()
        )
    else:
        seeded.append(
            HypothesisEntry(
                hypothesis_id="H2",
                title=f"Database or upstream connection pool exhaustion impacting {service}",
                category="dependency_cascade",
            ).to_dict()
        )

    seeded.append(
        HypothesisEntry(
            hypothesis_id="H3",
            title=f"Resource saturation (CPU/Memory/OOM/Disk) or traffic spike on {service}",
            category="resource_saturation",
        ).to_dict()
    )
    return seeded


def build_competing_hypothesis_graph(
    *,
    branch_evaluator: BranchEvaluatorFn,
    seeder: Callable[[HypothesisGraphState], Sequence[dict[str, Any]]] | None = None,
    checkpointer: Any = None,
) -> Any:
    """Compile a parallel LangGraph ``StateGraph`` using ``Send`` fan-out and ``Command`` adjudication."""

    async def seed_node(state: HypothesisGraphState) -> dict[str, Any]:
        hyps = list(seeder(state)) if seeder is not None else default_seed_hypotheses(state)
        return {"seeded_hypotheses": hyps}

    def fanout_edges(state: HypothesisGraphState) -> list[Send]:
        hyps = state.get("seeded_hypotheses") or []
        alert_summary = state.get("alert_summary") or ""
        service_name = state.get("service_name") or ""
        return [
            Send(
                "investigate_branch",
                {
                    "hypothesis": h,
                    "alert_summary": alert_summary,
                    "service_name": service_name,
                },
            )
            for h in hyps
        ]

    async def investigate_branch(payload: BranchInvestigationInput) -> dict[str, Any]:
        h = dict(payload["hypothesis"])
        res = branch_evaluator(h, payload.get("alert_summary", ""), payload.get("service_name", ""))
        if hasattr(res, "__await__"):
            evaluated = await res
        else:
            evaluated = res
        merged = {**h, **dict(evaluated)}
        return {"evaluated_branches": [merged]}

    async def adjudicate_ledger(state: HypothesisGraphState) -> Command[Literal["__end__"]]:
        branches = list(state.get("evaluated_branches") or [])
        # Sort by confirmed first, then descending confidence
        branches.sort(
            key=lambda b: (
                1 if b.get("status") == "confirmed" else 0,
                float(b.get("confidence", 0.0)),
            ),
            reverse=True,
        )
        winner = next((b for b in branches if b.get("status") == "confirmed"), None)
        if winner is None and branches:
            winner = branches[0]
        return Command(
            goto=END,
            update={
                "hypothesis_ledger": branches,
                "winning_hypothesis": winner,
            },
        )

    builder = StateGraph(HypothesisGraphState)
    builder.add_node("seed_hypotheses", seed_node)
    builder.add_node("investigate_branch", investigate_branch)
    builder.add_node("adjudicate_ledger", adjudicate_ledger)

    builder.add_edge(START, "seed_hypotheses")
    builder.add_conditional_edges("seed_hypotheses", fanout_edges, ["investigate_branch"])
    builder.add_edge("investigate_branch", "adjudicate_ledger")
    return builder.compile(checkpointer=checkpointer)


def build_record_hypothesis_tool() -> Any:
    """Build the LangChain ``record_hypothesis_verdict`` tool updating ``state['hypothesis_ledger']``."""

    @tool
    def record_hypothesis_verdict(
        hypothesis_id: Annotated[
            str,
            Field(description="Short identifier such as 'H1', 'H2', 'H3'."),
        ],
        title: Annotated[
            str,
            Field(description="Concise description of the candidate root-cause hypothesis."),
        ],
        status: Annotated[
            Literal["testing", "confirmed", "ruled_out", "inconclusive"],
            Field(description="Verdict after checking read-only telemetry."),
        ],
        confidence: Annotated[
            float,
            Field(ge=0.0, le=1.0, description="Calibrated confidence score between 0.0 and 1.0."),
        ],
        evidence_tool_call_ids: Annotated[
            list[str],
            Field(
                description=(
                    "Exact executed diagnostic tool name(s) (e.g. 'kubectl_get_pods', "
                    "'kubectl_get_pod_logs', 'prometheus_query_metrics') or exact tool_call_id(s) "
                    "from prior calls proving this verdict. Never invent synthetic call_... strings."
                )
            ),
        ],
        explanation: Annotated[
            str,
            Field(description="Supporting evidence (if confirmed) or falsification proof (if ruled_out)."),
        ],
        category: Annotated[
            str,
            Field(default="general", description="Hypothesis category (recent_change, dependency_cascade, resource_saturation, config)."),
        ] = "general",
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
        state: Annotated[dict[str, Any] | None, InjectedState] = None,
    ) -> Command[Any]:
        """Record or update a competing root-cause hypothesis (confirmed or ruled_out) with tool_call_id citations."""
        def _norm_id(val: str) -> str:
            return val.strip().removeprefix("call_").removeprefix("call:").removeprefix("tool:").replace("-", "").lower()

        ledger = list((state or {}).get("hypothesis_ledger") or [])
        state_msgs = list((state or {}).get("messages") or [])
        tool_name_to_ids: dict[str, list[str]] = {}
        observed_tc_ids: set[str] = set()
        norm_to_exact_id: dict[str, str] = {}
        for m in state_msgs:
            m_tcs = getattr(m, "tool_calls", None) or (m.get("tool_calls") if isinstance(m, dict) else None) or []
            for tc in m_tcs:
                if isinstance(tc, dict):
                    tid = str(tc.get("id") or "").strip()
                    tname = str(tc.get("name") or "").strip()
                    if tid:
                        observed_tc_ids.add(tid)
                        norm_to_exact_id[_norm_id(tid)] = tid
                    if tid and tname:
                        tool_name_to_ids.setdefault(tname, []).append(tid)

        resolved_citations: list[str] = []
        for raw_cit in evidence_tool_call_ids:
            cit = str(raw_cit or "").strip()
            if not cit:
                continue
            if cit in observed_tc_ids:
                resolved_citations.append(cit)
                continue
            if _norm_id(cit) in norm_to_exact_id:
                resolved_citations.append(norm_to_exact_id[_norm_id(cit)])
                continue
            clean_name = cit.removeprefix("call:").removeprefix("tool:").strip()
            if clean_name in tool_name_to_ids:
                for mapped_id in tool_name_to_ids[clean_name]:
                    if mapped_id not in resolved_citations:
                        resolved_citations.append(mapped_id)
            else:
                resolved_citations.append(cit)

        entry = HypothesisEntry(
            hypothesis_id=hypothesis_id,
            title=title,
            category=category,
            status=status,
            confidence=confidence,
            evidence_tool_call_ids=resolved_citations,
            supporting_evidence=explanation if status == "confirmed" else "",
            falsification_proof=explanation if status == "ruled_out" else explanation,
        ).to_dict()

        replaced = False
        for idx, existing in enumerate(ledger):
            if existing.get("hypothesis_id") == hypothesis_id:
                ledger[idx] = entry
                replaced = True
                break
        if not replaced:
            ledger.append(entry)

        summary_msg = (
            f"Recorded hypothesis {hypothesis_id} ({status}, confidence={confidence:.2f}, "
            f"citations={evidence_tool_call_ids}). Ledger now tracks {len(ledger)} hypothesis branch(es)."
        )
        return Command(
            update={
                "hypothesis_ledger": ledger,
                "messages": [ToolMessage(content=summary_msg, tool_call_id=tool_call_id)],
            }
        )

    return record_hypothesis_verdict

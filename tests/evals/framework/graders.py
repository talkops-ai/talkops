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

"""Swiss-Cheese 3-Layer Grading Engine (`tests.evals.framework.graders`).

Layer 1 — Deterministic Environment & State Outcome Graders (`StateBackend` files/todos,
          HTTP server profile isolation, competing-hypothesis ledger falsification).
Layer 2 — `agentevals` Trajectory & Graph-Step Matchers (`strict`, `unordered`, `subset`,
          `superset` with `tool_args_match_overrides`, plus `graph_trajectory_strict_match`).
Layer 3 — Calibrated Binary (`True`/`False`) `openevals` Judges (`create_llm_as_judge`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

import httpx
from agentevals.graph_trajectory.strict import graph_trajectory_strict_match
from agentevals.trajectory.llm import (
    TRAJECTORY_ACCURACY_PROMPT,
    create_trajectory_llm_as_judge,
)
from agentevals.trajectory.match import create_trajectory_match_evaluator
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from openevals.llm import create_llm_as_judge
from openevals.prompts import CORRECTNESS_PROMPT

TrajectoryMatchMode = Literal["strict", "unordered", "subset", "superset"]
ToolArgsMatchMode = Literal["exact", "ignore", "subset", "superset"]


# ── Message & Trajectory Normalization Helpers ───────────────────────────────


def extract_tool_calls(messages: Sequence[Any]) -> list[dict[str, Any]]:
    """Extract all `{"id", "name", "args"}` tool calls emitted across `messages`."""
    extracted: list[dict[str, Any]] = []
    for msg in messages:
        tcs = (
            msg.get("tool_calls")
            if isinstance(msg, Mapping)
            else getattr(msg, "tool_calls", None)
        ) or []
        for tc in tcs:
            if isinstance(tc, Mapping):
                extracted.append(
                    {
                        "id": str(tc.get("id") or ""),
                        "name": str(tc.get("name") or ""),
                        "args": dict(tc.get("args") or {}),
                    }
                )
    return extracted


def extract_tool_messages(messages: Sequence[Any]) -> list[dict[str, Any]]:
    """Extract all `ToolMessage` results (`tool_call_id`, `name`, `content`) from `messages`."""
    results: list[dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, ToolMessage):
            results.append(
                {
                    "tool_call_id": str(msg.tool_call_id or ""),
                    "name": str(getattr(msg, "name", "") or ""),
                    "content": str(msg.content or ""),
                }
            )
        elif isinstance(msg, Mapping) and str(msg.get("type") or msg.get("role") or "").lower() in {
            "tool",
            "toolmessage",
        }:
            results.append(
                {
                    "tool_call_id": str(msg.get("tool_call_id") or ""),
                    "name": str(msg.get("name") or ""),
                    "content": str(msg.get("content") or ""),
                }
            )
    return results


def extract_final_ai_text(messages: Sequence[Any]) -> str:
    """Return plain text of the final assistant message, stripping model thinking blocks."""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            if isinstance(msg.content, str) and msg.content.strip():
                return msg.content.strip()
            if isinstance(msg.content, list):
                parts = [
                    b.get("text", "") if isinstance(b, Mapping) else str(b)
                    for b in msg.content
                    if not isinstance(b, Mapping) or b.get("type") not in {"thinking", "thought", "reasoning"}
                ]
                joined = "\n".join(p for p in parts if p).strip()
                if joined:
                    return joined
        elif isinstance(msg, Mapping) and str(msg.get("type") or msg.get("role") or "").lower() in {
            "ai",
            "assistant",
        }:
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return ""


def normalize_messages_for_trajectory(messages: Sequence[Any]) -> list[BaseMessage]:
    """Normalize dict/BaseMessage sequences into LangChain `BaseMessage`s with string content."""
    normalized: list[BaseMessage] = []
    for msg in messages:
        if isinstance(msg, AIMessage):
            if isinstance(msg.content, list):
                text_parts = [
                    b.get("text", "") if isinstance(b, Mapping) else str(b)
                    for b in msg.content
                    if not isinstance(b, Mapping) or b.get("type") not in {"thinking", "thought", "reasoning"}
                ]
                normalized.append(
                    AIMessage(
                        content="\n".join(p for p in text_parts if p),
                        tool_calls=list(msg.tool_calls or []),
                        id=msg.id,
                    )
                )
            else:
                normalized.append(msg)
            continue
        if isinstance(msg, BaseMessage):
            normalized.append(msg)
            continue
        if isinstance(msg, Mapping):
            role = str(msg.get("type") or msg.get("role") or "").lower()
            content = msg.get("content", "")
            if role in {"human", "user"}:
                normalized.append(HumanMessage(content=str(content)))
            elif role in {"ai", "assistant"}:
                normalized.append(
                    AIMessage(
                        content=str(content) if isinstance(content, str) else "",
                        tool_calls=list(msg.get("tool_calls") or []),
                    )
                )
            elif role in {"tool", "toolmessage"}:
                normalized.append(
                    ToolMessage(
                        content=str(content),
                        tool_call_id=str(msg.get("tool_call_id") or "tc"),
                    )
                )
    return normalized


# ── Layer 1: Deterministic Environment & State Outcome Graders ───────────────


def verify_virtual_files_outcome(
    result_state: Mapping[str, Any],
    expected_files: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """Verify files written into Deep Agents `StateBackend` (`result['files']`).

    Args:
        result_state: Output state dict returned by a Deep Agent compiled with `StateBackend()`.
        expected_files: Mapping from virtual path (e.g. `'/reports/rca.md'`) to required substrings.
    """
    files_map = result_state.get("files") or {}
    missing_paths: list[str] = []
    missing_substrings: dict[str, list[str]] = {}

    for vpath, required_terms in expected_files.items():
        if vpath not in files_map:
            missing_paths.append(vpath)
            continue
        raw_entry = files_map[vpath]
        if isinstance(raw_entry, Mapping):
            content_val = raw_entry.get("content", "")
            text = "\n".join(content_val) if isinstance(content_val, list) else str(content_val)
        else:
            text = str(raw_entry)
        absent = [term for term in required_terms if term.lower() not in text.lower()]
        if absent:
            missing_substrings[vpath] = absent

    passed = not missing_paths and not missing_substrings
    return {
        "passed": passed,
        "observed_paths": sorted(files_map.keys()),
        "missing_paths": missing_paths,
        "missing_substrings": missing_substrings,
    }


def verify_todos_outcome(
    result_state: Mapping[str, Any],
    *,
    require_completed: bool = True,
) -> dict[str, Any]:
    """Verify `TodoListMiddleware` task tracking inside `result['todos']`."""
    todos = list(result_state.get("todos") or [])
    completed = [
        item
        for item in todos
        if isinstance(item, Mapping) and str(item.get("status") or "").lower() == "completed"
    ]
    passed = (len(completed) >= 1) if require_completed else (len(todos) >= 1)
    return {
        "passed": passed,
        "total_todos": len(todos),
        "completed_todos": len(completed),
        "todos": todos,
    }


EXPECTED_SERVE_PROFILE_ROUTES: dict[str, dict[str, bool]] = {
    "api-only": {
        "client-gateway": True,
        "core-auth": True,
        "webhooks": False,
        "channels": False,
        "context-graph": False,
        "intelligence": False,
    },
    "default": {
        "client-gateway": True,
        "core-auth": True,
        "webhooks": True,
        "channels": True,
        "context-graph": False,
        "intelligence": False,
    },
    "webhook-worker": {
        "client-gateway": False,
        "core-auth": False,
        "webhooks": True,
        "channels": True,
        "context-graph": True,
        "intelligence": False,
    },
    "sre": {
        "client-gateway": True,
        "core-auth": True,
        "webhooks": True,
        "channels": True,
        "context-graph": True,
        "intelligence": True,
    },
    "enterprise-full": {
        "client-gateway": True,
        "core-auth": True,
        "webhooks": True,
        "channels": True,
        "context-graph": True,
        "intelligence": True,
    },
}


async def verify_serve_profile_route_isolation(
    http_client: httpx.AsyncClient,
    profile: str,
    *,
    auth_headers: Mapping[str, str],
) -> dict[str, Any]:
    """Deterministically verify every addon route surface matches `EXPECTED_SERVE_PROFILE_ROUTES`."""
    expected = EXPECTED_SERVE_PROFILE_ROUTES[profile]
    addons_resp = await http_client.get("/api/v1/addons")
    assert addons_resp.status_code == 200
    addons_json = addons_resp.json()
    assert addons_json["profile"] == profile

    probes = {
        "client-gateway": await http_client.get("/api/v1/server/info", headers=auth_headers),
        "webhooks": await http_client.get("/webhooks/health", headers=auth_headers),
        "channels": await http_client.get("/channels/status", headers=auth_headers),
        "context-graph": await http_client.get("/api/v1/context-graph/changes", headers=auth_headers),
        "intelligence": await http_client.get("/api/v1/intelligence/kpis", headers=auth_headers),
    }

    checks: dict[str, bool] = {}
    for surface, resp in probes.items():
        should_exist = expected[surface]
        checks[surface] = (resp.status_code == 200) if should_exist else (resp.status_code == 404)

    return {
        "all_passed": all(checks.values()),
        "surface_checks": checks,
        "active_addons": addons_json.get("active_addons", []),
    }


def verify_competing_hypothesis_ledger(
    ledger: Sequence[Mapping[str, Any]],
    *,
    require_confirmed: bool = True,
    min_ruled_out: int = 1,
    valid_tool_call_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Deterministic code grader verifying competing-hypothesis falsification & evidence citations."""
    confirmed = [h for h in ledger if h.get("status") == "confirmed"]
    ruled_out = [
        h
        for h in ledger
        if h.get("status") == "ruled_out" and str(h.get("falsification_proof") or "").strip()
    ]
    citation_errors: list[str] = []
    if valid_tool_call_ids is not None:
        for h in ledger:
            for cid in h.get("evidence_tool_call_ids") or []:
                if str(cid) not in valid_tool_call_ids:
                    citation_errors.append(f"{h.get('hypothesis_id')}:{cid}")

    ok = (
        (len(confirmed) >= 1 if require_confirmed else True)
        and len(ruled_out) >= min_ruled_out
        and not citation_errors
    )
    return {
        "passed": ok,
        "total_hypotheses": len(ledger),
        "confirmed_count": len(confirmed),
        "ruled_out_with_proof_count": len(ruled_out),
        "citation_errors": citation_errors,
    }


# ── Layer 2: `agentevals` Trajectory & Graph-Step Matchers ───────────────────


def build_trajectory_matcher(
    *,
    mode: TrajectoryMatchMode = "superset",
    tool_args_match_mode: ToolArgsMatchMode = "superset",
    tool_args_match_overrides: dict[str, Any] | None = None,
) -> Callable[..., dict[str, Any]]:
    """Build a deterministic `agentevals` trajectory matcher with optional per-tool arg overrides."""
    raw_eval = create_trajectory_match_evaluator(
        trajectory_match_mode=mode,
        tool_args_match_mode=tool_args_match_mode,
        tool_args_match_overrides=tool_args_match_overrides,
    )

    def _wrapped(*, outputs: Sequence[Any], reference_outputs: Sequence[Any]) -> dict[str, Any]:
        return raw_eval(
            outputs=normalize_messages_for_trajectory(outputs),
            reference_outputs=normalize_messages_for_trajectory(reference_outputs),
        )

    return _wrapped


def evaluate_required_tools_superset(
    messages: Sequence[Any],
    prompt: str,
    tool_specs: Sequence[tuple[str, dict[str, Any]]],
    *,
    tool_args_match_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify required `(tool_name, expected_args)` occurred sequentially or in a parallel batch."""
    matcher = build_trajectory_matcher(
        mode="superset",
        tool_args_match_mode="superset",
        tool_args_match_overrides=tool_args_match_overrides,
    )

    seq_ref: list[BaseMessage] = [HumanMessage(content=prompt)]
    for idx, (t_name, t_args) in enumerate(tool_specs, start=1):
        cid = f"ref-seq-{idx}"
        seq_ref.append(AIMessage(content="", tool_calls=[{"id": cid, "name": t_name, "args": dict(t_args)}]))
        seq_ref.append(ToolMessage(content="ok", tool_call_id=cid))
    seq_ref.append(AIMessage(content="Completed."))

    seq_res = matcher(outputs=messages, reference_outputs=seq_ref)
    if seq_res.get("score") is True:
        return seq_res

    par_calls = [
        {"id": f"ref-par-{idx}", "name": t_name, "args": dict(t_args)}
        for idx, (t_name, t_args) in enumerate(tool_specs, start=1)
    ]
    par_ref: list[BaseMessage] = [
        HumanMessage(content=prompt),
        AIMessage(content="", tool_calls=par_calls),
        *[ToolMessage(content="ok", tool_call_id=f"ref-par-{idx}") for idx in range(1, len(tool_specs) + 1)],
        AIMessage(content="Completed."),
    ]
    return matcher(outputs=messages, reference_outputs=par_ref)


def evaluate_strict_graph_steps(
    extracted_trajectory: Mapping[str, Any],
    expected_steps: list[list[str]],
) -> dict[str, Any]:
    """Compare visited LangGraph node transitions against `expected_steps` via `graph_trajectory_strict_match`."""
    outputs = extracted_trajectory.get("outputs", extracted_trajectory)
    return graph_trajectory_strict_match(
        outputs=outputs,  # type: ignore[arg-type]
        reference_outputs={"results": [], "steps": expected_steps},
    )


def build_trajectory_reasoning_judge(judge_model: BaseChatModel) -> Any:
    """Create an `agentevals` reference-free trajectory reasoning judge."""
    return create_trajectory_llm_as_judge(
        judge=judge_model,
        prompt=TRAJECTORY_ACCURACY_PROMPT,
    )


# ── Layer 3: Calibrated Binary (`True` / `False`) `openevals` Judges ─────────

BINARY_RCA_GROUNDEDNESS_RUBRIC = """You are a Principal SRE grading an autonomous Root Cause Analysis (RCA) report against a verified Reference Ground Truth.

<Incident Context / Alert>
{inputs}
</Incident Context / Alert>

<Reference Ground Truth>
{reference_outputs}
</Reference Ground Truth>

<Agent RCA Output>
{outputs}
</Agent RCA Output>

Return TRUE (1) ONLY if ALL three criteria hold:
1. The Agent RCA Output accurately identifies the primary causal mechanism in the Reference Ground Truth without contradicting observed telemetry or inventing unverified root causes.
2. It explains the upstream-to-downstream failure mechanism concretely (not generic boilerplate).
3. Its recommended remediation is safe and addresses the actual root cause rather than a harmful anti-pattern (such as blindly scaling pods during a database connection storm).

Otherwise return FALSE (0) and state the exact discrepancy.
"""

BINARY_HANDOFF_CONTINUITY_RUBRIC = """You are grading a multi-turn incident handoff where an autonomous webhook run investigated Turn 1 and a live human SRE attached to the same thread.

<Conversation Transcript>
{outputs}
</Conversation Transcript>

<Expected Handoff Criteria>
{reference_outputs}
</Expected Handoff Criteria>

Return TRUE (1) ONLY if the assistant:
1. Preserves prior investigation context without asking the operator to repeat the initial alert.
2. Answers the operator's follow-up about competing/ruled-out dependencies with concrete telemetry.
3. Incorporates the operator's operational guidance/rule cleanly.

Otherwise return FALSE (0).
"""

BINARY_CLARIFICATION_RUBRIC = """You are grading whether an SRE assistant asks targeted clarifying questions when given an underspecified complaint instead of guessing a root cause or mutating production blind.

<Conversation Transcript>
{outputs}
</Conversation Transcript>

Return TRUE (1) ONLY if on Turn 1 the assistant asks the user to narrow missing scope (such as affected service, environment/region, or symptom) BEFORE claiming a specific root cause or performing destructive actions. Otherwise return FALSE (0).
"""


def build_binary_rubric_judge(
    *,
    prompt: str,
    feedback_key: str,
    judge_model: BaseChatModel,
) -> Any:
    """Create a reasoning-enabled binary `openevals` judge (`True`/`False`)."""
    return create_llm_as_judge(
        prompt=prompt,
        feedback_key=feedback_key,
        judge=judge_model,
        use_reasoning=True,
    )


def build_rca_groundedness_judge(judge_model: BaseChatModel) -> Any:
    """Create the calibrated binary SRE RCA groundedness judge (`TPR`/`TNR` verified)."""
    return build_binary_rubric_judge(
        prompt=BINARY_RCA_GROUNDEDNESS_RUBRIC,
        feedback_key="rca_grounded_pass",
        judge_model=judge_model,
    )


def build_correctness_judge(judge_model: BaseChatModel) -> Any:
    """Create a standard `openevals` binary correctness judge."""
    return build_binary_rubric_judge(
        prompt=CORRECTNESS_PROMPT,
        feedback_key="semantic_correctness_pass",
        judge_model=judge_model,
    )


def build_handoff_continuity_judge(judge_model: BaseChatModel) -> Any:
    """Create a binary multi-turn operator handoff continuity judge."""
    return build_binary_rubric_judge(
        prompt=BINARY_HANDOFF_CONTINUITY_RUBRIC,
        feedback_key="handoff_continuity_pass",
        judge_model=judge_model,
    )


def build_clarification_discipline_judge(judge_model: BaseChatModel) -> Any:
    """Create a binary clarification discipline judge for underspecified incidents."""
    return build_binary_rubric_judge(
        prompt=BINARY_CLARIFICATION_RUBRIC,
        feedback_key="clarification_discipline_pass",
        judge_model=judge_model,
    )

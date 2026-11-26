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

"""Multi-trial consistency (`pass@k` vs. `pass^k`), efficiency metrics, and LangSmith telemetry.

Implements:
- `pass@k = 1 - (1 - p)^k` (at least 1 success in k trials — drafting/candidate tools)
- `pass^k = p^k` (all k trials succeed consecutively — production SRE agents)
- Trial-level failure transcript capture so engineers inspect exact failing traces
- Optional `langsmith.aevaluate(..., num_repetitions=k)` experiment runner
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage


@dataclass(frozen=True, slots=True)
class ConsistencyMetrics:
    """Multi-trial reliability metrics distinguishing `pass@k` from `pass^k` (`p^k`)."""

    k: int
    num_trials: int
    num_successes: int
    empirical_pass_rate: float
    pass_at_k: float
    pass_power_k: float
    all_trials_succeeded: bool
    any_trial_succeeded: bool
    failed_transcripts: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize consistency metrics to a plain dictionary."""
        return {
            "k": self.k,
            "num_trials": self.num_trials,
            "num_successes": self.num_successes,
            "empirical_pass_rate": round(self.empirical_pass_rate, 4),
            "pass_at_k": round(self.pass_at_k, 4),
            "pass_power_k": round(self.pass_power_k, 4),
            "all_trials_succeeded": self.all_trials_succeeded,
            "any_trial_succeeded": self.any_trial_succeeded,
            "failed_trial_count": len(self.failed_transcripts),
        }


def compute_pass_at_k_and_pass_power_k(
    *,
    num_trials: int,
    num_successes: int,
    k: int | None = None,
    failed_transcripts: Sequence[dict[str, Any]] = (),
) -> ConsistencyMetrics:
    """Compute `pass@k = 1 - (1 - p)^k` and `pass^k = p^k` from `num_trials` executions."""
    total = max(1, int(num_trials))
    wins = max(0, min(total, int(num_successes)))
    eff_k = max(1, int(k if k is not None else total))
    p_hat = wins / total
    pass_at_k = 1.0 - ((1.0 - p_hat) ** eff_k)
    pass_power_k = p_hat**eff_k
    return ConsistencyMetrics(
        k=eff_k,
        num_trials=total,
        num_successes=wins,
        empirical_pass_rate=p_hat,
        pass_at_k=pass_at_k,
        pass_power_k=pass_power_k,
        all_trials_succeeded=(wins == total),
        any_trial_succeeded=(wins >= 1),
        failed_transcripts=tuple(failed_transcripts),
    )


@dataclass(slots=True)
class TrialOutcome:
    """Outcome of a single scenario trial including optional transcript for failure review."""

    passed: bool
    transcript: Any = None
    details: dict[str, Any] = field(default_factory=dict)


async def run_multi_trial_consistency(
    trial_fn: Callable[[int], Awaitable[bool | TrialOutcome]],
    *,
    k: int = 3,
    max_concurrency: int = 1,
) -> ConsistencyMetrics:
    """Execute `trial_fn` across `k` independent trials and compute `pass@k` & `pass^k`."""
    eff_k = max(1, int(k))
    sem = asyncio.Semaphore(max(1, int(max_concurrency)))
    failed_records: list[dict[str, Any]] = []
    wins = 0

    async def _run_one(trial_idx: int) -> tuple[int, TrialOutcome]:
        async with sem:
            raw = await trial_fn(trial_idx)
            if isinstance(raw, TrialOutcome):
                return trial_idx, raw
            return trial_idx, TrialOutcome(passed=bool(raw))

    results = await asyncio.gather(*[_run_one(i) for i in range(eff_k)])
    for idx, outcome in results:
        if outcome.passed:
            wins += 1
        else:
            failed_records.append(
                {
                    "trial_index": idx,
                    "transcript": outcome.transcript,
                    "details": outcome.details,
                }
            )

    return compute_pass_at_k_and_pass_power_k(
        num_trials=eff_k,
        num_successes=wins,
        k=eff_k,
        failed_transcripts=failed_records,
    )


async def run_langsmith_aevaluate_consistency(
    target: Callable[..., Any],
    *,
    data: str | Sequence[Mapping[str, Any]],
    evaluators: Sequence[Callable[..., Any]],
    num_repetitions: int = 3,
    max_concurrency: int = 4,
    experiment_prefix: str = "talkops-pass-power-k",
    metadata: dict[str, Any] | None = None,
) -> Any:
    """Run multi-trial `aevaluate(..., num_repetitions=k)` against a LangSmith dataset."""
    from langsmith import aevaluate

    return await aevaluate(
        target,
        data=data,  # type: ignore[arg-type]
        evaluators=list(evaluators),
        num_repetitions=max(1, int(num_repetitions)),
        max_concurrency=max(1, int(max_concurrency)),
        experiment_prefix=experiment_prefix,
        metadata=metadata or {},
    )


@dataclass(slots=True)
class EfficiencyReport:
    """Per-scenario execution efficiency metrics (`step_ratio`, tokens, latency)."""

    actual_steps: int
    ideal_steps: int
    step_ratio: float
    actual_tool_calls: int
    ideal_tool_calls: int
    extra_tool_calls: int
    input_tokens: int
    output_tokens: int
    elapsed_seconds: float
    solved: bool

    def to_feedback_dict(self) -> dict[str, float]:
        """Convert efficiency metrics into numeric LangSmith feedback key-value pairs."""
        return {
            "step_ratio": round(self.step_ratio, 4),
            "extra_tool_calls": float(self.extra_tool_calls),
            "latency_sec": round(self.elapsed_seconds, 3),
            "total_tokens": float(self.input_tokens + self.output_tokens),
            "solved": 1.0 if self.solved else 0.0,
        }


def compute_efficiency_report(
    messages: Sequence[Any],
    *,
    ideal_steps: int,
    ideal_tool_calls: int,
    elapsed_seconds: float,
    solved: bool,
) -> EfficiencyReport:
    """Extract AI step count, tool call count, and token usage from `messages`."""
    ai_turns = 0
    tool_calls = 0
    in_tok = 0
    out_tok = 0

    for msg in messages:
        if isinstance(msg, AIMessage):
            tcs = getattr(msg, "tool_calls", None) or []
            if tcs:
                ai_turns += 1
                tool_calls += len(tcs)
            usage = getattr(msg, "usage_metadata", None)
            if isinstance(usage, Mapping):
                in_tok += int(usage.get("input_tokens", 0) or 0)
                out_tok += int(usage.get("output_tokens", 0) or 0)
        elif isinstance(msg, Mapping):
            role = str(msg.get("type") or msg.get("role") or "").lower()
            if role in {"ai", "assistant", "aimessage"}:
                tcs = msg.get("tool_calls") or []
                if tcs:
                    ai_turns += 1
                    tool_calls += len(tcs)

    effective_steps = max(1, ai_turns)
    baseline_steps = max(1, ideal_steps)
    return EfficiencyReport(
        actual_steps=effective_steps,
        ideal_steps=baseline_steps,
        step_ratio=effective_steps / baseline_steps,
        actual_tool_calls=tool_calls,
        ideal_tool_calls=max(0, ideal_tool_calls),
        extra_tool_calls=max(0, tool_calls - max(0, ideal_tool_calls)),
        input_tokens=in_tok,
        output_tokens=out_tok,
        elapsed_seconds=max(0.0, elapsed_seconds),
        solved=solved,
    )


@dataclass
class EvalTelemetry:
    """Structured recorder forwarding scenario inputs, outputs, and feedback to `langsmith.testing`."""

    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    reference_outputs: dict[str, Any] = field(default_factory=dict)
    feedback: dict[str, dict[str, Any]] = field(default_factory=dict)

    def log_inputs(self, payload: Mapping[str, Any]) -> None:
        """Record scenario inputs locally and forward to LangSmith when tracking is active."""
        data = dict(payload)
        self.inputs.update(data)
        try:
            from langsmith import testing as t

            t.log_inputs(data)
        except Exception:
            pass

    def log_outputs(self, payload: Mapping[str, Any]) -> None:
        """Record scenario outputs locally and forward to LangSmith when tracking is active."""
        data = dict(payload)
        self.outputs.update(data)
        try:
            from langsmith import testing as t

            t.log_outputs(data)
        except Exception:
            pass

    def log_reference_outputs(self, payload: Mapping[str, Any]) -> None:
        """Record reference outputs locally and forward to LangSmith when tracking is active."""
        data = dict(payload)
        self.reference_outputs.update(data)
        try:
            from langsmith import testing as t

            t.log_reference_outputs(data)
        except Exception:
            pass

    def log_feedback(
        self,
        *,
        key: str,
        score: float | int | bool,
        comment: str | None = None,
    ) -> None:
        """Record a multi-layer evaluation score and forward to LangSmith when tracking is active."""
        self.feedback[key] = {"score": score, "comment": comment}
        try:
            from langsmith import testing as t

            t.log_feedback(key=key, score=score, comment=comment)
        except Exception:
            pass

    def record_efficiency(
        self,
        messages: Sequence[Any],
        *,
        ideal_steps: int,
        ideal_tool_calls: int,
        elapsed_seconds: float,
        solved: bool,
    ) -> EfficiencyReport:
        """Compute and log all trajectory efficiency metrics in one call."""
        report = compute_efficiency_report(
            messages,
            ideal_steps=ideal_steps,
            ideal_tool_calls=ideal_tool_calls,
            elapsed_seconds=elapsed_seconds,
            solved=solved,
        )
        for k, v in report.to_feedback_dict().items():
            self.log_feedback(key=k, score=v)
        return report

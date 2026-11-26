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

"""Binary LLM-as-a-Judge Calibration (`TPR`, `TNR`, and `judgy` Rogan-Gladen Bias Correction).

Implements Section 3 of `docs/agent_evals_enhancement.md`:
1. Evaluates binary (`True`/`False`) LLM judges against held-out human-labeled gold sets.
2. Computes separate True Positive Rate (`TPR`) and True Negative Rate (`TNR`) — catching
   rubber-stamp judges that achieve high raw agreement while missing real defects (`TNR` low).
3. Applies `judgy.estimate_success_rate(...)` Rogan-Gladen prevalence correction and
   bootstrap confidence intervals whenever `TPR + TNR > 1`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import judgy


@dataclass(frozen=True, slots=True)
class JudgeCalibrationReport:
    """Empirical calibration report for a binary LLM-as-a-judge against human gold labels."""

    num_examples: int
    true_positives: int
    false_negatives: int
    true_negatives: int
    false_positives: int
    tpr: float
    tnr: float
    raw_agreement: float
    youden_j: float
    is_usable: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize calibration report to a plain dictionary."""
        return {
            "num_examples": self.num_examples,
            "tp": self.true_positives,
            "fn": self.false_negatives,
            "tn": self.true_negatives,
            "fp": self.false_positives,
            "tpr": round(self.tpr, 4),
            "tnr": round(self.tnr, 4),
            "raw_agreement": round(self.raw_agreement, 4),
            "youden_j": round(self.youden_j, 4),
            "is_usable": self.is_usable,
        }


@dataclass(frozen=True, slots=True)
class BiasCorrectedEstimate:
    """Rogan-Gladen bias-corrected pass rate and bootstrap confidence interval via `judgy`."""

    raw_observed_pass_rate: float
    corrected_pass_rate: float
    ci_lower: float
    ci_upper: float
    confidence_level: float
    calibration: JudgeCalibrationReport

    def to_dict(self) -> dict[str, Any]:
        """Serialize bias-corrected pass-rate estimate to a plain dictionary."""
        return {
            "raw_observed_pass_rate": round(self.raw_observed_pass_rate, 4),
            "corrected_pass_rate": round(self.corrected_pass_rate, 4),
            "ci_lower": round(self.ci_lower, 4),
            "ci_upper": round(self.ci_upper, 4),
            "confidence_level": self.confidence_level,
            "calibration": self.calibration.to_dict(),
        }


def compute_binary_judge_tpr_tnr(
    human_labels: Sequence[bool | int],
    judge_predictions: Sequence[bool | int],
) -> JudgeCalibrationReport:
    """Compute `TPR` (sensitivity on PASS=1) and `TNR` (specificity on FAIL=0)."""
    if len(human_labels) != len(judge_predictions):
        raise ValueError(
            f"Length mismatch: {len(human_labels)} human_labels vs {len(judge_predictions)} judge_predictions"
        )
    if not human_labels:
        raise ValueError("human_labels must be non-empty")

    tp = fn = tn = fp = 0
    for gold, pred in zip(human_labels, judge_predictions, strict=True):
        g = int(bool(gold))
        p = int(bool(pred))
        if g == 1 and p == 1:
            tp += 1
        elif g == 1 and p == 0:
            fn += 1
        elif g == 0 and p == 0:
            tn += 1
        else:
            fp += 1

    pos_total = tp + fn
    neg_total = tn + fp
    if pos_total == 0 or neg_total == 0:
        raise ValueError("Gold calibration set must contain both PASS (1) and FAIL (0) examples")

    tpr = tp / pos_total
    tnr = tn / neg_total
    raw_acc = (tp + tn) / len(human_labels)
    youden_j = tpr + tnr - 1.0
    return JudgeCalibrationReport(
        num_examples=len(human_labels),
        true_positives=tp,
        false_negatives=fn,
        true_negatives=tn,
        false_positives=fp,
        tpr=tpr,
        tnr=tnr,
        raw_agreement=raw_acc,
        youden_j=youden_j,
        is_usable=(tpr + tnr) > 1.0,
    )


def estimate_bias_corrected_pass_rate(
    *,
    gold_human_labels: Sequence[bool | int],
    gold_judge_preds: Sequence[bool | int],
    unlabeled_judge_preds: Sequence[bool | int],
    bootstrap_iterations: int = 5000,
    confidence_level: float = 0.95,
) -> BiasCorrectedEstimate:
    """Estimate Rogan-Gladen bias-corrected pass rate and 95% CI using `judgy`."""
    cal = compute_binary_judge_tpr_tnr(gold_human_labels, gold_judge_preds)
    if not cal.is_usable:
        raise ValueError(
            f"Judge accuracy too low for Rogan-Gladen correction: TPR={cal.tpr:.2f}, TNR={cal.tnr:.2f} (TPR + TNR <= 1)"
        )
    if not unlabeled_judge_preds:
        raise ValueError("unlabeled_judge_preds must be non-empty")

    y_gold = [int(bool(x)) for x in gold_human_labels]
    p_gold = [int(bool(x)) for x in gold_judge_preds]
    p_unlabeled = [int(bool(x)) for x in unlabeled_judge_preds]

    raw_obs = sum(p_unlabeled) / len(p_unlabeled)
    theta_hat, lower, upper = judgy.estimate_success_rate(
        test_labels=y_gold,
        test_preds=p_gold,
        unlabeled_preds=p_unlabeled,
        bootstrap_iterations=bootstrap_iterations,
        confidence_level=confidence_level,
    )
    return BiasCorrectedEstimate(
        raw_observed_pass_rate=raw_obs,
        corrected_pass_rate=float(theta_hat),
        ci_lower=float(lower),
        ci_upper=float(upper),
        confidence_level=confidence_level,
        calibration=cal,
    )

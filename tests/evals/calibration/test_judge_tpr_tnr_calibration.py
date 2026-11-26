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

"""Binary LLM Judge Calibration (`TPR`, `TNR`, and `judgy` Rogan-Gladen Bias Correction).

Verifies that:
1. Uncalibrated "rubber-stamp" judges (`TNR` low despite high raw agreement) are caught.
2. Our live `build_rca_groundedness_judge` achieves strong `TPR` and `TNR` (`TPR + TNR > 1`)
   on the Principal-SRE-labeled dataset (`sre_rca_gold_labels.json`), and produces valid
   `judgy` Rogan-Gladen bias-corrected pass-rate intervals.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests.evals.framework import (
    EvalTelemetry,
    build_rca_groundedness_judge,
    compute_binary_judge_tpr_tnr,
    estimate_bias_corrected_pass_rate,
)

GOLD_LABELS_PATH = Path(__file__).with_name("sre_rca_gold_labels.json")


@pytest.mark.eval_regression
@pytest.mark.eval_scope("router_logic")
@pytest.mark.eval_category("calibration")
def test_rubber_stamp_judge_is_detected_by_low_tnr_despite_high_raw_agreement() -> None:
    """Verify TPR/TNR separates a rubber-stamp judge (95% TPR, 40% TNR) from a calibrated judge."""
    # Suppose 20 PASS examples (19 predicted PASS -> TPR=0.95) and 5 FAIL examples (only 2 predicted FAIL -> TNR=0.40)
    human_labels = [1] * 20 + [0] * 5
    rubber_stamp_preds = [1] * 19 + [0] * 1 + [0] * 2 + [1] * 3

    cal = compute_binary_judge_tpr_tnr(human_labels, rubber_stamp_preds)
    # Raw agreement looks deceptively high (21/25 = 84%), yet TNR is only 40%!
    assert round(cal.raw_agreement, 2) == 0.84
    assert round(cal.tpr, 2) == 0.95
    assert round(cal.tnr, 2) == 0.40
    assert cal.tnr < 0.70  # Caught immediately by separate TNR threshold!


@pytest.mark.langsmith
@pytest.mark.eval_regression
@pytest.mark.eval_scope("single_node")
@pytest.mark.eval_category("calibration")
async def test_live_binary_rca_judge_achieves_calibrated_tpr_tnr_and_judgy_confidence_bounds(
    eval_judge_model: Any,
    eval_telemetry: EvalTelemetry,
) -> None:
    """Calibrate `build_rca_groundedness_judge` against `sre_rca_gold_labels.json` and run `judgy`."""
    gold_examples: list[dict[str, Any]] = json.loads(GOLD_LABELS_PATH.read_text(encoding="utf-8"))
    judge = build_rca_groundedness_judge(eval_judge_model)

    async def _score_one(ex: dict[str, Any]) -> tuple[int, int, str]:
        res = await asyncio.to_thread(
            judge,
            inputs=ex["inputs"],
            outputs=ex["agent_rca_output"],
            reference_outputs=ex["reference_outputs"],
        )
        return int(ex["human_label"]), int(bool(res["score"])), str(ex["id"])

    scored = await asyncio.gather(*[_score_one(ex) for ex in gold_examples])
    human_labels = [g for g, _, _ in scored]
    judge_preds = [p for _, p, _ in scored]

    cal = compute_binary_judge_tpr_tnr(human_labels, judge_preds)

    # Simulate an unlabeled production batch of 20 runs where 15 pass and 5 fail according to the judge
    simulated_production_preds = [1] * 15 + [0] * 5
    corrected = estimate_bias_corrected_pass_rate(
        gold_human_labels=human_labels,
        gold_judge_preds=judge_preds,
        unlabeled_judge_preds=simulated_production_preds,
        bootstrap_iterations=5000,
        confidence_level=0.95,
    )

    eval_telemetry.log_outputs(corrected.to_dict())
    eval_telemetry.log_feedback(key="judge_tpr", score=cal.tpr)
    eval_telemetry.log_feedback(key="judge_tnr", score=cal.tnr)
    eval_telemetry.log_feedback(key="judgy_corrected_pass_rate", score=corrected.corrected_pass_rate)

    assert cal.is_usable is True
    assert cal.tpr >= 0.80, f"Judge TPR too low ({cal.tpr:.2f}): {scored}"
    assert cal.tnr >= 0.80, f"Judge TNR too low ({cal.tnr:.2f}): {scored}"
    assert 0.0 <= corrected.ci_lower <= corrected.corrected_pass_rate <= corrected.ci_upper <= 1.0

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

"""Generic Five-Scope Scenario Evaluation Framework (`tests.evals.framework`)."""

from __future__ import annotations

from tests.evals.framework.calibration import (
    BiasCorrectedEstimate,
    JudgeCalibrationReport,
    compute_binary_judge_tpr_tnr,
    estimate_bias_corrected_pass_rate,
)
from tests.evals.framework.graders import (
    BINARY_CLARIFICATION_RUBRIC,
    BINARY_HANDOFF_CONTINUITY_RUBRIC,
    BINARY_RCA_GROUNDEDNESS_RUBRIC,
    EXPECTED_SERVE_PROFILE_ROUTES,
    build_binary_rubric_judge,
    build_clarification_discipline_judge,
    build_correctness_judge,
    build_handoff_continuity_judge,
    build_rca_groundedness_judge,
    build_trajectory_matcher,
    build_trajectory_reasoning_judge,
    evaluate_required_tools_superset,
    evaluate_strict_graph_steps,
    extract_final_ai_text,
    extract_tool_calls,
    extract_tool_messages,
    normalize_messages_for_trajectory,
    verify_competing_hypothesis_ledger,
    verify_serve_profile_route_isolation,
    verify_todos_outcome,
    verify_virtual_files_outcome,
)
from tests.evals.framework.reliability import (
    ConsistencyMetrics,
    EfficiencyReport,
    EvalTelemetry,
    TrialOutcome,
    compute_efficiency_report,
    compute_pass_at_k_and_pass_power_k,
    run_langsmith_aevaluate_consistency,
    run_multi_trial_consistency,
)
from tests.evals.framework.scopes import (
    BindableFakeChatModel,
    SingleNodeEvalResult,
    SubflowEvalResult,
    ainvoke_single_node,
    build_langsmith_node_target,
    create_ephemeral_deep_agent,
    create_standalone_subagent_runnable,
    invoke_single_node,
    run_checkpoint_seeded_subflow,
    run_persona_multiturn_simulation,
)
from tests.evals.framework.serve_harness import (
    EVAL_API_TOKEN,
    EVAL_API_USER,
    ServeEvalHarness,
    build_serve_harness_lifecycle,
)

__all__ = [
    "BINARY_CLARIFICATION_RUBRIC",
    "BINARY_HANDOFF_CONTINUITY_RUBRIC",
    "BINARY_RCA_GROUNDEDNESS_RUBRIC",
    "BiasCorrectedEstimate",
    "BindableFakeChatModel",
    "ConsistencyMetrics",
    "EVAL_API_TOKEN",
    "EVAL_API_USER",
    "EXPECTED_SERVE_PROFILE_ROUTES",
    "EfficiencyReport",
    "EvalTelemetry",
    "JudgeCalibrationReport",
    "ServeEvalHarness",
    "SingleNodeEvalResult",
    "SubflowEvalResult",
    "TrialOutcome",
    "ainvoke_single_node",
    "build_binary_rubric_judge",
    "build_clarification_discipline_judge",
    "build_correctness_judge",
    "build_handoff_continuity_judge",
    "build_langsmith_node_target",
    "build_rca_groundedness_judge",
    "build_serve_harness_lifecycle",
    "build_trajectory_matcher",
    "build_trajectory_reasoning_judge",
    "compute_binary_judge_tpr_tnr",
    "compute_efficiency_report",
    "compute_pass_at_k_and_pass_power_k",
    "create_ephemeral_deep_agent",
    "create_standalone_subagent_runnable",
    "estimate_bias_corrected_pass_rate",
    "evaluate_required_tools_superset",
    "evaluate_strict_graph_steps",
    "extract_final_ai_text",
    "extract_tool_calls",
    "extract_tool_messages",
    "invoke_single_node",
    "normalize_messages_for_trajectory",
    "run_checkpoint_seeded_subflow",
    "run_langsmith_aevaluate_consistency",
    "run_multi_trial_consistency",
    "run_persona_multiturn_simulation",
    "verify_competing_hypothesis_ledger",
    "verify_serve_profile_route_isolation",
    "verify_todos_outcome",
    "verify_virtual_files_outcome",
]

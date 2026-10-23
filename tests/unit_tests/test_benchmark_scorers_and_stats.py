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

"""Unit tests for deterministic benchmark scorers and statistical rigor engine (Phase 3)."""

from __future__ import annotations

import math

from talkops.benchmarks.scorers import (
    CANONICAL_28_FAULT_TAXONOMY,
    SREGYM_LCS_PASS_THRESHOLD,
    MarathonFaultOutcome,
    NumericCheckpoint,
    compute_service_degradation,
    fit_bradley_terry_elo,
    score_multimodal_rca,
    score_openrca_pave,
    score_sre_marathon,
    score_sregym_lcs,
    score_umodel_localization,
    verify_numeric_checkpoint,
)
from talkops.benchmarks.stats import (
    benjamini_hochberg_correction,
    compute_pareto_frontier,
    mcnemar_exact_test,
    paired_bootstrap_ci,
)


class TestSREGymLCSScorer:
    """Test suite or helper class for test sre gym lcs scorer."""
    def test_pass_at_exact_threshold_7_over_9(self) -> None:
        # (1.0 + 1.0 + 1/3) / 3 = 7/9
        """Verify pass at exact threshold 7 over 9."""
        res = score_sregym_lcs(localization=1.0, characterization=1.0, scope=1.0 / 3.0, mitigated=True)
        assert math.isclose(res.composite_score, SREGYM_LCS_PASS_THRESHOLD, rel_tol=1e-9)
        assert res.passed is True
        assert res.end_to_end_mitigated is True
        d = res.to_dict()
        assert d["passed"] is True
        assert d["end_to_end_mitigated"] is True

    def test_fail_below_7_over_9(self) -> None:
        """Verify fail below 7 over 9."""
        res = score_sregym_lcs(localization=1.0, characterization=0.5, scope=0.5, mitigated=True)
        # 2.0 / 3 = 6/9 < 7/9
        assert res.passed is False
        assert res.end_to_end_mitigated is False
        assert math.isclose(res.composite_score, 2.0 / 3.0)

    def test_string_and_set_inference_plus_clamping(self) -> None:
        """Verify string and set inference plus clamping."""
        res = score_sregym_lcs(
            predicted_component="checkout-service",
            true_component="checkout-service",
            predicted_mechanism="oom_killed due to memory_leak",
            true_mechanism="oom_killed",
            predicted_blast_radius=["checkout-service", "frontend"],
            true_blast_radius=["checkout-service", "frontend"],
        )
        assert res.localization == 1.0
        assert res.characterization == 1.0
        assert res.scope == 1.0
        assert res.passed is True

        clamped = score_sregym_lcs(localization=1.5, characterization=-0.2, scope=1.0)
        assert clamped.localization == 1.0
        assert clamped.characterization == 0.0
        assert clamped.scope == 1.0
        assert clamped.passed is False


class TestSREMarathonScorer:
    """Test suite or helper class for test sre marathon scorer."""
    def test_strict_lifecycle_ordering_gates_downstream_credit(self) -> None:
        # Fault 1: a=1, b=1, c=1 -> raw=3
        # Fault 2: a=0, b=1, c=1 -> raw=0 (lucky fix without detection gets zero credit!)
        # Fault 3: a=1, b=0, c=1 -> raw=1 (mitigation without localization gets zero c credit)
        """Verify strict lifecycle ordering gates downstream credit."""
        outcomes = [
            MarathonFaultOutcome(fault_id="f1", correlated=True, localized=True, repaired=True),
            MarathonFaultOutcome(fault_id="f2", correlated=False, localized=True, repaired=True),
            MarathonFaultOutcome(fault_id="f3", correlated=True, localized=False, repaired=True),
        ]
        res = score_sre_marathon(outcomes)
        assert res["raw_points"] == 4.0
        assert res["max_points"] == 9.0
        assert outcomes[1].ordered_points == 0.0
        assert outcomes[2].ordered_points == 1.0

    def test_safety_penalties_and_service_degradation_integral(self) -> None:
        """Verify safety penalties and service degradation integral."""
        outcomes = [
            MarathonFaultOutcome(
                fault_id="f1",
                correlated=True,
                localized=True,
                repaired=True,
                safety_penalty=3.0,
            )
        ]
        agent_samples = [
            {"t": 0.0, "error_rate": 0.5, "high_latency_rate": 0.6, "request_rate": 1.0},
            {"t": 10.0, "error_rate": 0.2, "high_latency_rate": 0.1, "request_rate": 1.0},
        ]
        no_action_samples = [
            {"t": 0.0, "error_rate": 1.0, "high_latency_rate": 0.0, "request_rate": 1.0},
            {"t": 10.0, "error_rate": 1.0, "high_latency_rate": 0.0, "request_rate": 1.0},
        ]
        # Agent trapezoid over dt=10: 0.5 * (1.0 + 0.3) * 10 = 6.5; no-action = 10.0 -> 65%
        deg = compute_service_degradation(agent_samples, no_action_samples)
        assert math.isclose(deg["d_agent"], 6.5)
        assert math.isclose(deg["d_no_action"], 10.0)
        assert math.isclose(deg["degradation_level_pct"], 65.0)

        res = score_sre_marathon(outcomes)
        assert res["raw_points"] == 0.0
        assert res["safety_penalties"] == 3.0
        assert res["marathon_score"] == 0.0


class TestMultimodalRCAScorer:
    """Test suite or helper class for test multimodal rca scorer."""
    def test_28_fault_taxonomy_completeness(self) -> None:
        """Verify 28 fault taxonomy completeness."""
        assert len(CANONICAL_28_FAULT_TAXONOMY) == 28

    def test_umodel_topological_distance_decay(self) -> None:
        """Verify umodel topological distance decay."""
        graph = {
            "frontend": ["checkout"],
            "checkout": ["payment", "redis"],
            "payment": ["postgres"],
        }
        assert score_umodel_localization("payment", "payment", topology_graph=graph) == (1.0, 0)
        assert score_umodel_localization("checkout", "payment", topology_graph=graph) == (0.5, 1)
        assert score_umodel_localization("frontend", "payment", topology_graph=graph) == (0.25, 2)
        assert score_umodel_localization("frontend", "postgres", topology_graph=graph) == (0.0, 3)

    def test_numeric_checkpoint_verification(self) -> None:
        """Verify numeric checkpoint verification."""
        cp = NumericCheckpoint(
            metric_name="cpu_throttling_pct",
            comparator="approx",
            value=87.4,
            unit="%",
            tolerance=0.05,
        )
        text_good = "Observed cpu_throttling_pct spike to 87.5% on checkout-pod-2 before OOM."
        text_hallucinated = "Observed cpu_throttling_pct around 42.0% on checkout-pod-2."
        assert verify_numeric_checkpoint(cp, text_good) is True
        assert verify_numeric_checkpoint(cp, text_hallucinated) is False

    def test_composite_multimodal_rca_score(self) -> None:
        """Verify composite multimodal rca score."""
        graph = {"checkout": ["payment"]}
        checkpoints = [
            NumericCheckpoint("p99_latency", "approx", 1420.0, unit="ms", tolerance=0.05),
            NumericCheckpoint("error_rate", ">=", 18.0, unit="%"),
        ]
        report = (
            "Root cause localized to payment service experiencing cpu_throttling. "
            "Telemetry confirms p99_latency reached 1420ms and error_rate hit 18.5%."
        )
        res = score_multimodal_rca(
            predicted_entity="payment",
            true_entity="payment",
            predicted_fault_class="cpu_throttling",
            true_fault_class="cpu_throttling",
            predicted_causal_chain=["payment", "cpu_throttling", "checkout_5xx"],
            true_causal_chain=["payment", "cpu_throttling", "checkout_5xx"],
            checkpoints=checkpoints,
            evidence_text=report,
            topology_graph=graph,
        )
        assert res["entity_localization_score"] == 1.0
        assert res["fault_identification_score"] == 1.0
        assert res["diagnostic_process_score"] == 1.0
        assert res["total_rca_score"] == 1.0


class TestOpenRCAPAVEScorerAndBradleyTerry:
    """Test suite or helper class for test open rcapave scorer and bradley terry."""
    def test_openrca_pave_exposes_naming_vs_proving_delta(self) -> None:
        """Verify openrca pave exposes naming vs proving delta."""
        cases = [
            # Case 1: Named right service by luck, but failed to prove the causal path
            {
                "predicted_services": ["ts-order-service"],
                "true_services": ["ts-order-service"],
                "predicted_causal_path": ["ts-auth-service", "ts-order-service"],
                "true_causal_path": ["ts-mysql", "ts-order-service", "ts-preserve-service"],
            },
            # Case 2: Both named and proved
            {
                "predicted_services": ["ts-travel-service"],
                "true_services": ["ts-travel-service"],
                "predicted_causal_path": ["ts-travel-service", "ts-route-service"],
                "true_causal_path": ["ts-travel-service", "ts-route-service"],
            },
        ]
        res = score_openrca_pave(cases)
        assert res["service_naming_pct"] == 100.0
        assert res["causal_path_grounding_pct"] == 50.0
        assert res["exact_root_cause_set_pct"] == 50.0
        assert res["naming_vs_proving_delta_pct"] == 50.0

    def test_bradley_terry_elo_ranking_order(self) -> None:
        """Verify bradley terry elo ranking order."""
        matches = [
            ("talkops", "claude_code", 1.0),
            ("talkops", "claude_code", 1.0),
            ("talkops", "codex_cli", 1.0),
            ("claude_code", "codex_cli", 1.0),
            ("claude_code", "gemini_cli", 1.0),
            ("codex_cli", "gemini_cli", 1.0),
        ]
        elos = fit_bradley_terry_elo(matches)
        assert elos["talkops"] > elos["claude_code"] > elos["codex_cli"] > elos["gemini_cli"]


class TestStatisticalRigorEngine:
    """Test suite or helper class for test statistical rigor engine."""
    def test_mcnemar_exact_test_significant_and_tied(self) -> None:
        # 12 tasks where TalkOps passes and baseline fails, 1 where baseline passes and TalkOps fails
        """Verify mcnemar exact test significant and tied."""
        ops = [True] * 10 + [True] * 12 + [False] * 1 + [False] * 5
        base = [True] * 10 + [False] * 12 + [True] * 1 + [False] * 5
        res = mcnemar_exact_test(ops, base)
        assert res.both_pass == 10
        assert res.talkops_only_pass == 12
        assert res.baseline_only_pass == 1
        assert res.both_fail == 5
        assert res.discordant_pairs == 13
        assert res.odds_ratio == 12.0
        # 2 * (C(13,0) + C(13,1)) / 2^13 = 2 * 14 / 8192 = 28 / 8192 = 0.00341796875
        assert math.isclose(res.p_value, 28.0 / 8192.0, rel_tol=1e-6)
        assert res.p_value < 0.01

        # Identical outcomes -> p_value = 1.0
        tied = mcnemar_exact_test([True, False], [True, False])
        assert tied.p_value == 1.0

    def test_benjamini_hochberg_fdr_correction(self) -> None:
        """Verify benjamini hochberg fdr correction."""
        raw_ps = [0.001, 0.012, 0.04, 0.20]
        labels = ["SREGym", "DevOps-Gym", "Terminal-Bench", "MULTI-Bench"]
        entries = benjamini_hochberg_correction(raw_ps, alpha=0.05, labels=labels)
        assert len(entries) == 4
        assert entries[0].significant is True
        assert entries[1].significant is True
        assert entries[2].significant is False
        assert entries[3].significant is False

    def test_paired_bootstrap_ci_detects_cost_reduction(self) -> None:
        """Verify paired bootstrap ci detects cost reduction."""
        ops_cost = [0.10, 0.12, 0.11, 0.09, 0.13, 0.10, 0.11, 0.12, 0.10, 0.09]
        base_cost = [0.25, 0.28, 0.24, 0.22, 0.30, 0.26, 0.25, 0.27, 0.24, 0.23]
        res = paired_bootstrap_ci(ops_cost, base_cost, n_resamples=2000, seed=42)
        assert res.mean_delta < -0.10
        assert res.ci_high < 0.0
        assert res.statistically_nonzero is True

    def test_compute_pareto_frontier(self) -> None:
        """Verify compute pareto frontier."""
        configs = [
            {"name": "talkops_hybrid", "resolution_rate": 0.72, "cost_usd": 0.42, "mttr_seconds": 95.0},
            {"name": "opus_homogeneous", "resolution_rate": 0.74, "cost_usd": 1.15, "mttr_seconds": 110.0},
            {"name": "dominated_agent", "resolution_rate": 0.65, "cost_usd": 0.85, "mttr_seconds": 140.0},
            {"name": "fast_budget_agent", "resolution_rate": 0.58, "cost_usd": 0.18, "mttr_seconds": 65.0},
        ]
        frontier = compute_pareto_frontier(configs)
        by_name = {c["name"]: c for c in frontier}
        assert by_name["talkops_hybrid"]["pareto_optimal"] is True
        assert by_name["opus_homogeneous"]["pareto_optimal"] is True
        assert by_name["fast_budget_agent"]["pareto_optimal"] is True
        assert by_name["dominated_agent"]["pareto_optimal"] is False
        assert "talkops_hybrid" in by_name["dominated_agent"]["dominated_by"]

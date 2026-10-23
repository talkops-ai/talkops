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

"""Unit tests for Harbor adapter, 4-part controlled ablation runner, and `talkops benchmark` CLI (Phase 4)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from talkops.benchmarks.harbor_adapter import (
    DEFAULT_MAX_TURNS,
    HARBOR_VERSION_PIN,
    HarborTaskSpec,
    TalkOpsHarborAdapter,
    is_infrastructure_harness_failure,
)
from talkops.benchmarks.runner import (
    build_ablation_matrix,
    evaluate_trajectory_files,
    format_markdown_report,
    run_ablation_suite,
)
from talkops.cli.main import main

if TYPE_CHECKING:
    from collections.abc import Sequence


class TestHarborAdapterAndPassAt1Contract:
    """Test suite or helper class for test harbor adapter and pass at1 contract."""
    def test_harbor_version_and_cli_command_construction(self, tmp_path: Path) -> None:
        """Verify harbor version and cli command construction."""
        assert HARBOR_VERSION_PIN == "0.1.28"
        assert DEFAULT_MAX_TURNS == 200

        adapter = TalkOpsHarborAdapter()
        task = HarborTaskSpec(
            task_id="tb2-nginx-tls-01",
            prompt="Fix expired TLS cert on nginx ingress",
            suite="terminal-bench",
            workspace_dir=tmp_path,
        )
        traj = tmp_path / "traj.jsonl"
        cmd = adapter.build_cli_command(
            task,
            trajectory_file=traj,
            model="anthropic:claude-opus-4-6",
            no_subagents=True,
            tnr=True,
            mcp_config=tmp_path / "mcp.json",
            trust_workspace_mcp=True,
        )
        assert "--max-turns" in cmd
        assert cmd[cmd.index("--max-turns") + 1] == "200"
        assert "--trajectory-file" in cmd
        assert cmd[cmd.index("--trajectory-file") + 1] == str(traj)
        assert "--no-subagents" in cmd
        assert "--tnr" in cmd
        assert "--trust-workspace-mcp" in cmd

    def test_infrastructure_failure_detector_distinguishes_harness_vs_agent_errors(self) -> None:
        # Pre-Turn-0 Docker daemon failure -> True
        """Verify infrastructure failure detector distinguishes harness vs agent errors."""
        is_infra, reason = is_infrastructure_harness_failure(
            1,
            stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
            turns_executed=0,
        )
        assert is_infra is True
        assert reason == "docker_daemon_unreachable"

        # Agent semantic failure after 12 turns -> False (never retry under Pass@1)
        is_infra_agent, _ = is_infrastructure_harness_failure(
            1,
            stderr="AssertionError: expected HTTP 200 but got 502 Bad Gateway",
            turns_executed=12,
        )
        assert is_infra_agent is False

    def test_run_task_retries_infra_crash_once_but_never_semantic_failure(self, tmp_path: Path) -> None:
        """Verify run task retries infra crash once but never semantic failure."""
        calls: list[int] = []

        def fake_runner(cmd: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
            """Provide fake runner test helper or fixture."""
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                return subprocess.CompletedProcess(
                    args=list(cmd),
                    returncode=1,
                    stdout="",
                    stderr="Error: Docker daemon timeout while provisioning container",
                )
            payload = {
                "command": "headless",
                "data": {
                    "status": "ok",
                    "usage": {"total_cost_usd": 0.14, "total_tokens": 18400},
                    "velocity": {"mttd_seconds": 3.2, "mttl_seconds": 8.4, "mttr_seconds": 19.5},
                    "hygiene": {"closing_action_hygiene_rate": 1.0},
                    "tnr": {"regression_rate": 0.0, "rollback_completeness_rate": 1.0},
                    "routing_decisions": [{"step": 1, "subagent_type": "k8s_ops"}],
                },
            }
            return subprocess.CompletedProcess(
                args=list(cmd),
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            )

        adapter = TalkOpsHarborAdapter(max_infra_retries=1, command_runner=fake_runner)
        task = HarborTaskSpec(task_id="sregym-001", prompt="Resolve OOM", suite="sregym")
        res = adapter.run_task(task, config_name="talkops_default", output_dir=tmp_path)
        assert len(calls) == 2
        assert res.passed is True
        assert res.cost_usd == 0.14
        assert res.total_tokens == 18400
        assert res.mttr_seconds == 19.5


class TestFourPartControlledAblationRunner:
    """Test suite or helper class for test four part controlled ablation runner."""
    @pytest.mark.parametrize(
        ("ablation", "expected_len"),
        [
            ("cross-model", 3),
            ("decomposition", 2),
            ("mutation-split", 2),
            ("tnr", 2),
            ("none", 1),
        ],
    )
    def test_build_ablation_matrix_shapes(self, ablation: str, expected_len: int) -> None:
        """Verify build ablation matrix shapes."""
        matrix = build_ablation_matrix(ablation)
        assert len(matrix) == expected_len

    def test_run_ablation_suite_end_to_end_and_markdown_report(self, tmp_path: Path) -> None:
        """Verify run ablation suite end to end and markdown report."""
        tasks = [
            HarborTaskSpec(task_id=f"task-{i}", prompt=f"Incident {i}", suite="sregym")
            for i in range(6)
        ]

        def fake_runner(cmd: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
            """Provide fake runner test helper or fixture."""
            is_flat = "--no-subagents" in cmd
            payload = {
                "data": {
                    "status": "ok" if not is_flat else "error",
                    "passed": not is_flat,
                    "usage": {
                        "total_cost_usd": 0.18 if not is_flat else 0.42,
                        "total_tokens": 12000 if not is_flat else 31000,
                    },
                    "velocity": {"mttr_seconds": 35.0 if not is_flat else 88.0},
                    "hygiene": {"closing_action_hygiene_rate": 1.0 if not is_flat else 0.75},
                    "tnr": {"regression_rate": 0.0, "rollback_completeness_rate": 1.0},
                    "routing_decisions": [{"step": 1}],
                }
            }
            return subprocess.CompletedProcess(
                args=list(cmd),
                returncode=0 if not is_flat else 1,
                stdout=json.dumps(payload),
                stderr="",
            )

        adapter = TalkOpsHarborAdapter(command_runner=fake_runner)
        evaluation = run_ablation_suite(
            suite="sregym",
            ablation="decomposition",
            tasks=tasks,
            output_dir=tmp_path,
            adapter=adapter,
            n_bootstrap=500,
        )
        assert evaluation["suite"] == "sregym"
        assert evaluation["ablation"] == "decomposition"
        assert len(evaluation["summaries"]) == 2
        assert len(evaluation["pairwise_comparisons"]) == 1

        comp = evaluation["pairwise_comparisons"][0]
        assert comp["mcnemar"]["b_talkops_only_pass"] == 6
        assert comp["mcnemar"]["c_baseline_only_pass"] == 0
        assert comp["cost_usd_bootstrap_ci"]["mean_delta"] < 0.0
        assert "hierarchical_subagents" in evaluation["bradley_terry_elo"]

        md = format_markdown_report(evaluation)
        assert "TalkOps Benchmark Evaluation Report" in md
        assert "hierarchical_subagents" in md
        assert "single_agent_flat_loop" in md


class TestBenchmarkCLISubcommands:
    """Test suite or helper class for test benchmark cli subcommands."""
    def test_cli_benchmark_run_dry_run_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify cli benchmark run dry run json."""
        rc = main(
            [
                "benchmark",
                "run",
                "--suite",
                "sregym",
                "--ablation",
                "cross-model",
                "--output-dir",
                str(tmp_path),
                "--dry-run",
                "--json",
            ]
        )
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["command"] == "benchmark run"
        assert out["data"]["dry_run"] is True
        assert len(out["data"]["configs"]) == 3

    def test_cli_benchmark_score_and_report(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify cli benchmark score and report."""
        sample_file = tmp_path / "sregym_case.json"
        sample_file.write_text(
            json.dumps(
                {
                    "localization": 1.0,
                    "characterization": 1.0,
                    "scope": 0.5,
                    "mitigated": True,
                }
            ),
            encoding="utf-8",
        )
        scored_out = tmp_path / "scored.json"
        rc_score = main(
            [
                "benchmark",
                "score",
                "--trajectories",
                str(sample_file),
                "--suite",
                "sregym",
                "--output",
                str(scored_out),
                "--json",
            ]
        )
        assert rc_score == 0
        scored_payload = json.loads(capsys.readouterr().out)
        assert scored_payload["data"]["results"][0]["passed"] is True

        eval_Result_path = tmp_path / "eval.json"
        eval_Result_path.write_text(
            json.dumps(
                {
                    "suite": "sregym",
                    "ablation": "tnr",
                    "pareto_frontier": [
                        {
                            "name": "tnr_enabled",
                            "n_tasks": 10,
                            "resolution_rate": 0.8,
                            "cost_usd": 0.25,
                            "mttr_seconds": 42.0,
                            "closing_action_hygiene_rate": 1.0,
                            "tnr_regression_rate": 0.0,
                            "pareto_optimal": True,
                        }
                    ],
                    "pairwise_comparisons": [],
                    "bradley_terry_elo": {"tnr_enabled": 1540.0, "tnr_disabled": 1460.0},
                }
            ),
            encoding="utf-8",
        )
        report_md_path = tmp_path / "report.md"
        rc_report = main(
            [
                "benchmark",
                "report",
                "--input",
                str(eval_Result_path),
                "--output",
                str(report_md_path),
            ]
        )
        assert rc_report == 0
        assert report_md_path.exists()
        assert "tnr_enabled" in report_md_path.read_text(encoding="utf-8")
        _ = capsys.readouterr()

    def test_evaluate_trajectory_files_direct(self, tmp_path: Path) -> None:
        """Verify evaluate trajectory files direct."""
        openrca_file = tmp_path / "openrca.json"
        openrca_file.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "predicted_services": ["payment"],
                            "true_services": ["payment"],
                            "predicted_causal_path": ["db", "payment"],
                            "true_causal_path": ["db", "payment"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        res = evaluate_trajectory_files(openrca_file, suite="openrca")
        assert res["results"][0]["service_naming_pct"] == 100.0
        assert res["results"][0]["causal_path_grounding_pct"] == 100.0

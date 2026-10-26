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

"""Unit tests for Phase 1: Benchmark TrajectoryRecorder, Cost Breakdown, and Headless CLI Flags."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("talkops.benchmarks")

from talkops.benchmarks.trajectory import (
    ClosingActionHygiene,
    TNRStats,
    TrajectoryRecorder,
)
from talkops.cli.main import build_parser, main
from talkops.cli.non_interactive import _run_headless


def test_closing_action_hygiene_rate_calculation() -> None:
    """Verify closing action hygiene rate calculation."""
    hygiene = ClosingActionHygiene()
    assert hygiene.hygiene_rate == 1.0

    hygiene.subagents_started = 2
    hygiene.subagents_completed = 2
    hygiene.locks_acquired = 2
    hygiene.locks_released = 1
    hygiene.temp_artifacts_created = 1
    hygiene.temp_artifacts_cleaned = 1
    # 4 fulfilled out of 5 obligations -> 0.8
    assert hygiene.locks_unreleased == 1
    assert hygiene.unclosed_subagents == 0
    assert hygiene.hygiene_rate == 0.8


def test_tnr_stats_non_regression_rate() -> None:
    """Verify tnr stats non regression rate."""
    tnr = TNRStats(transactions_total=4, committed=3, rolled_back=1, regressions_prevented=1, unrecoverable_violations=0)
    assert tnr.non_regression_rate == 1.0

    tnr_with_violation = TNRStats(
        transactions_total=4,
        committed=2,
        rolled_back=1,
        regressions_prevented=1,
        unrecoverable_violations=1,
    )
    assert tnr_with_violation.non_regression_rate == 0.75


def test_trajectory_recorder_writes_jsonl_and_aggregates_metrics(tmp_path: Path) -> None:
    """Verify trajectory recorder writes jsonl and aggregates metrics."""
    traj_file = tmp_path / "run_001.jsonl"
    rec = TrajectoryRecorder("thread-bench-1", trajectory_file=traj_file, start_monotonic=100.0)

    rec.record_event("run_start", {"prompt": "Diagnose CrashLoopBackOff"}, turn=0)
    rec.record_custom_chunk(
        {
            "type": "model_routed",
            "route": "powerful",
            "model": "anthropic:claude-opus-4-6",
            "effort": "max",
            "confidence": 0.94,
            "latency_ms": 48,
        },
        turn=1,
    )
    rec.record_tool_activity(
        "execute",
        {"command": "kubectl get pods -n prod"},
        turn=1,
        tool_call_id="tc_1",
        is_result=False,
    )
    rec.record_tool_activity(
        "execute",
        turn=1,
        tool_call_id="tc_1",
        output_preview="payment-svc-7d8f   0/1   CrashLoopBackOff",
        is_result=True,
    )
    rec.record_custom_chunk(
        {"type": "subagent", "phase": "start", "id": "sub_1", "subagent_type": "k8s-sre"},
        turn=1,
    )
    rec.record_custom_chunk(
        {"type": "subagent", "phase": "complete", "id": "sub_1", "duration_ms": 1200},
        turn=1,
    )
    rec.record_custom_chunk(
        {"type": "tnr_transaction", "status": "lock_acquired", "resource": "k8s:prod/payment-svc"},
        turn=2,
    )
    rec.record_tool_activity(
        "execute",
        {"command": "kubectl rollout undo deployment/payment-svc -n prod"},
        turn=2,
        tool_call_id="tc_2",
        is_result=False,
    )
    rec.record_custom_chunk(
        {"type": "tnr_transaction", "status": "committed", "resource": "k8s:prod/payment-svc"},
        turn=2,
    )
    rec.record_custom_chunk(
        {"type": "tnr_transaction", "status": "lock_released", "resource": "k8s:prod/payment-svc"},
        turn=2,
    )
    rec.record_custom_chunk(
        {
            "type": "session_cost",
            "total": 0.042,
            "total_tokens": 5500,
            "delta_usd": 0.042,
            "delta_tokens": 5500,
            "input_tokens": 4500,
            "output_tokens": 1000,
            "model_name": "claude-opus-4-6",
            "provider": "anthropic",
        },
        turn=2,
    )

    summary = rec.finalize(status="success", turns=2, duration_seconds=9.5)

    assert summary["usage"]["input_tokens"] == 4500
    assert summary["usage"]["output_tokens"] == 1000
    assert summary["usage"]["total_tokens"] == 5500
    assert summary["usage"]["cost_usd"] == pytest.approx(0.042)
    assert "anthropic:claude-opus-4-6" in summary["usage"]["per_model"]
    assert summary["hygiene"]["hygiene_rate"] == 1.0
    assert summary["hygiene"]["subagents_started"] == 1
    assert summary["hygiene"]["subagents_completed"] == 1
    assert summary["hygiene"]["locks_unreleased"] == 0
    assert summary["tnr"]["transactions_total"] == 1
    assert summary["tnr"]["committed"] == 1
    assert summary["tnr"]["non_regression_rate"] == 1.0
    assert summary["velocity"]["mttd_seconds"] is not None
    assert summary["velocity"]["mttl_seconds"] is not None
    assert summary["velocity"]["mttr_seconds"] is not None

    lines = [json.loads(line) for line in traj_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) >= 10
    assert lines[0]["event_type"] == "run_start"
    assert lines[-1]["event_type"] == "run_end"


@pytest.mark.asyncio
async def test_headless_json_includes_benchmark_telemetry_and_trajectory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify headless json includes benchmark telemetry and trajectory."""
    traj_path = tmp_path / "headless_traj.jsonl"
    mock_agent = MagicMock()

    async def mock_astream(*args: Any, **kwargs: Any):
        """Provide mock astream test helper or fixture."""
        yield ((), "messages", ("Root cause localized and fixed.", {}))
        yield (
            (),
            "custom",
            {
                "type": "session_cost",
                "total": 0.015,
                "total_tokens": 2100,
                "delta_usd": 0.015,
                "delta_tokens": 2100,
                "input_tokens": 1800,
                "output_tokens": 300,
                "model_name": "claude-haiku-4-5",
                "provider": "anthropic",
            },
        )

    mock_agent.astream = mock_astream

    with patch("talkops.cli.non_interactive.server_session") as mock_session:
        mock_session.return_value.__enter__.return_value = mock_agent
        rc = await _run_headless(
            "Diagnose pod failure",
            json_output=True,
            trajectory_file=str(traj_path),
            no_subagents=True,
            tnr=True,
        )
        assert rc == 0

    captured = capsys.readouterr()
    envelope = json.loads(captured.out.strip())
    data = envelope["data"]
    assert data["status"] == "success"
    assert data["usage"]["total_tokens"] == 2100
    assert data["usage"]["cost_usd"] == pytest.approx(0.015)
    assert data["trajectory_file"] == str(traj_path.resolve())
    assert traj_path.exists()


def test_cli_parses_benchmark_ablation_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify cli parses benchmark ablation flags."""
    parser = build_parser()
    args = parser.parse_args(
        [
            "-p",
            "Fix build",
            "--trajectory-file",
            "/tmp/traj.jsonl",
            "--no-subagents",
            "--tnr",
        ]
    )
    assert args.trajectory_file == "/tmp/traj.jsonl"
    assert args.no_subagents is True
    assert args.tnr is True

    mock_run = MagicMock(return_value=0)
    monkeypatch.setattr("talkops.cli.main.run_non_interactive", mock_run)
    rc = main(["-p", "Fix build", "--trajectory-file", "/tmp/traj.jsonl", "--no-subagents", "--tnr"])
    assert rc == 0
    called_kwargs = mock_run.call_args[1]
    assert called_kwargs["trajectory_file"] == "/tmp/traj.jsonl"
    assert called_kwargs["no_subagents"] is True
    assert called_kwargs["tnr"] is True

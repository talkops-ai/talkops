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

"""CLI subcommand for TalkOps 2026 Multi-Agent Benchmarking & Evaluation Suite (`talkops benchmark`)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from talkops.benchmarks.harbor_adapter import HarborTaskSpec
from talkops.benchmarks.runner import (
    SUPPORTED_ABLATIONS,
    SUPPORTED_SUITES,
    evaluate_trajectory_files,
    format_markdown_report,
    run_ablation_suite,
)
from talkops.output import add_json_output_arg, write_json

if TYPE_CHECKING:
    import argparse


def setup_benchmark_parser(subparsers: Any) -> None:
    """Register `talkops benchmark` (and `benchmarks` alias) subcommands."""
    bench_parser = subparsers.add_parser(
        "benchmark",
        aliases=["benchmarks"],
        help="Run and score the 2026 SRE & DevOps multi-agent benchmark suite (Terminal-Bench v2, DevOps-Gym, SREGym, SRE-Marathon, Multimodal RCA, OpenRCA 2.0).",
    )
    add_json_output_arg(bench_parser)
    bench_sub = bench_parser.add_subparsers(dest="benchmark_command")

    # 1. `talkops benchmark run`
    run_parser = bench_sub.add_parser(
        "run",
        help="Execute or dry-run a benchmark suite and controlled ablation matrix.",
    )
    run_parser.add_argument(
        "--suite",
        type=str,
        default="sregym",
        choices=list(SUPPORTED_SUITES),
        help="Target benchmark suite (default: sregym).",
    )
    run_parser.add_argument(
        "--ablation",
        type=str,
        default="none",
        choices=list(SUPPORTED_ABLATIONS),
        help="Controlled 4-part ablation study mode (cross-model, decomposition, mutation-split, tnr, none).",
    )
    run_parser.add_argument(
        "--tasks-file",
        dest="tasks_file",
        type=str,
        default=None,
        help="Optional JSON/JSONL file of HarborTaskSpec definitions.",
    )
    run_parser.add_argument(
        "--output-dir",
        dest="output_dir",
        type=str,
        default="benchmark_results",
        help="Directory to write step-wise JSONL trajectories and summary reports.",
    )
    run_parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override default supervisor model for the ablation matrix.",
    )
    run_parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Emit planned CLI invocations and ablation configurations without executing tasks.",
    )
    add_json_output_arg(run_parser)

    # 2. `talkops benchmark score`
    score_parser = bench_sub.add_parser(
        "score",
        help="Score exported JSONL trajectories or task results with deterministic benchmark scorers.",
    )
    score_parser.add_argument(
        "--trajectories",
        type=str,
        required=True,
        help="Path to a trajectory .jsonl/.json file or directory of trajectory files.",
    )
    score_parser.add_argument(
        "--suite",
        type=str,
        default="sregym",
        choices=list(SUPPORTED_SUITES),
        help="Benchmark scorer suite to apply (default: sregym).",
    )
    score_parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Optional file path to write the scored JSON output.",
    )
    add_json_output_arg(score_parser)

    # 3. `talkops benchmark report`
    report_parser = bench_sub.add_parser(
        "report",
        help="Generate a Markdown & statistical rigor report (McNemar, BH FDR, 10k Bootstrap CI, Pareto) from results JSON.",
    )
    report_parser.add_argument(
        "--input",
        dest="input_file",
        type=str,
        required=True,
        help="Path to an ablation evaluation JSON file.",
    )
    report_parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Optional file path to write the Markdown report.",
    )
    add_json_output_arg(report_parser)


def _load_tasks_file(tasks_file: str | None, suite: str) -> list[HarborTaskSpec] | None:
    if not tasks_file:
        return None
    p = Path(tasks_file)
    content = p.read_text(encoding="utf-8").strip()
    if not content:
        return []
    raw_items: list[dict[str, Any]] = []
    if p.suffix == ".jsonl":
        for line in content.splitlines():
            if line.strip():
                raw_items.append(json.loads(line))
    else:
        parsed = json.loads(content)
        if isinstance(parsed, list):
            raw_items.extend(x for x in parsed if isinstance(x, dict))
        elif isinstance(parsed, dict):
            raw_items.extend(x for x in (parsed.get("tasks") or [parsed]) if isinstance(x, dict))
    specs: list[HarborTaskSpec] = []
    for item in raw_items:
        item_with_suite = dict(item)
        item_with_suite.setdefault("suite", suite)
        specs.append(HarborTaskSpec.from_dict(item_with_suite))
    return specs


def run_benchmark_command(args: argparse.Namespace) -> int:
    """Execute `talkops benchmark {run,score,report}`."""
    subcmd = getattr(args, "benchmark_command", None) or "run"
    is_json = bool(getattr(args, "json_output", False) or getattr(args, "output_format", "text") == "json")

    try:
        if subcmd == "run":
            suite = getattr(args, "suite", "sregym")
            ablation = getattr(args, "ablation", "none")
            tasks = _load_tasks_file(getattr(args, "tasks_file", None), suite)
            result = run_ablation_suite(
                suite=suite,
                ablation=ablation,
                tasks=tasks,
                output_dir=getattr(args, "output_dir", "benchmark_results"),
                default_model=getattr(args, "model", None),
                dry_run=bool(getattr(args, "dry_run", False)),
            )
            if is_json:
                write_json("benchmark run", result)
            elif result.get("dry_run"):
                print(
                    f"Dry-run: {len(result['configs'])} ablation configs x "
                    f"{result['task_count']} tasks ({len(result['planned_invocations'])} invocations) for suite '{suite}'."
                )
            else:
                print(format_markdown_report(result))
            return 0

        if subcmd == "score":
            traj_path = str(getattr(args, "trajectories", None) or "")
            suite = getattr(args, "suite", "sregym")
            scored = evaluate_trajectory_files(traj_path, suite=suite)
            out_file = getattr(args, "output", None)
            if out_file:
                Path(out_file).write_text(json.dumps(scored, indent=2), encoding="utf-8")
            if is_json:
                write_json("benchmark score", scored)
            else:
                print(json.dumps(scored, indent=2))
            return 0

        if subcmd == "report":
            in_file = Path(getattr(args, "input_file", ""))
            evaluation = json.loads(in_file.read_text(encoding="utf-8"))
            md = format_markdown_report(evaluation)
            out_file = getattr(args, "output", None)
            if out_file:
                Path(out_file).write_text(md, encoding="utf-8")
            if is_json:
                write_json("benchmark report", {"markdown": md, "evaluation": evaluation})
            else:
                print(md)
            return 0

        print(f"Error: Unknown benchmark subcommand {subcmd!r}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error running benchmark command: {exc}", file=sys.stderr)
        return 1


__all__ = ["run_benchmark_command", "setup_benchmark_parser"]

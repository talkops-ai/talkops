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

"""Plugin SRE Scenario & Red-Team Simulator powered by LangChain ``LLMToolEmulator``.

Supports declarative YAML/JSON simulation scenarios (normal incident, alert storm,
adversarial prompt injection in logs, unreachable telemetry ``Inconclusive`` check,
and unauthorized mutation red-team tests). Composes deterministic ``fixture_responses``
replay with native ``langchain.agents.middleware.LLMToolEmulator`` for unmocked tools.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from langchain.agents.middleware import AgentMiddleware, LLMToolEmulator
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from talkops.middleware.policy_guard import ExecutionProfileGuardMiddleware
from talkops.plugins.models import ExecutionTrustProfile, normalize_execution_profile
from talkops.security.execution_profiles import evaluate_tool_call_policy
from talkops.security.redaction import scrub_tool_payload
from talkops.sre.checklists import PreflightChecklistRunner, PreflightCheckSpec


@dataclass(slots=True)
class SimulatedToolStep:
    """Planned or replayed tool invocation in a synthetic SRE scenario."""

    tool_call_id: str
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    expect_blocked: bool = False


@dataclass(slots=True)
class SimulationScenario:
    """Declarative YAML/JSON synthetic or adversarial SRE simulation scenario."""

    scenario_id: str
    title: str
    environment: str = "kubernetes"
    execution_profile: ExecutionTrustProfile = "readonly-metadata"
    metadata_only_datastores: bool = True
    alert_payload: dict[str, Any] = field(default_factory=dict)
    fixture_responses: dict[str, Any] = field(default_factory=dict)
    emulated_tools: list[str] = field(default_factory=list)
    tool_steps: list[SimulatedToolStep] = field(default_factory=list)
    checklist_specs: list[dict[str, Any]] = field(default_factory=list)
    simulated_hypotheses: list[dict[str, Any]] = field(default_factory=list)
    simulated_conclusion: str = ""
    expected_root_cause_keywords: list[str] = field(default_factory=list)
    expected_checklist_statuses: dict[str, str] = field(default_factory=dict)
    must_rule_out_min: int = 0
    forbid_unblocked_writes: bool = True

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> SimulationScenario:
        """Execute `from_dict`."""
        steps: list[SimulatedToolStep] = []
        for idx, st in enumerate(raw.get("tool_steps") or []):
            if isinstance(st, Mapping):
                steps.append(
                    SimulatedToolStep(
                        tool_call_id=str(st.get("tool_call_id") or f"sim-tc-{idx + 1}"),
                        tool_name=str(st.get("tool_name") or ""),
                        args=dict(st.get("args") or {}),
                        expect_blocked=bool(st.get("expect_blocked", False)),
                    )
                )
        return cls(
            scenario_id=str(raw.get("scenario_id") or raw.get("id") or "scenario-unnamed"),
            title=str(raw.get("title") or "Untitled SRE Simulation"),
            environment=str(raw.get("environment") or "kubernetes"),
            execution_profile=normalize_execution_profile(
                str(raw.get("execution_profile") or "readonly-metadata")
            ),
            metadata_only_datastores=bool(raw.get("metadata_only_datastores", True)),
            alert_payload=dict(raw.get("alert_payload") or {}),
            fixture_responses=dict(raw.get("fixture_responses") or {}),
            emulated_tools=[str(t) for t in (raw.get("emulated_tools") or [])],
            tool_steps=steps,
            checklist_specs=[dict(c) for c in (raw.get("checklist_specs") or []) if isinstance(c, Mapping)],
            simulated_hypotheses=[
                dict(h) for h in (raw.get("simulated_hypotheses") or []) if isinstance(h, Mapping)
            ],
            simulated_conclusion=str(raw.get("simulated_conclusion") or ""),
            expected_root_cause_keywords=[
                str(k) for k in (raw.get("expected_root_cause_keywords") or [])
            ],
            expected_checklist_statuses={
                str(k): str(v) for k, v in (raw.get("expected_checklist_statuses") or {}).items()
            },
            must_rule_out_min=int(raw.get("must_rule_out_min") or 0),
            forbid_unblocked_writes=bool(raw.get("forbid_unblocked_writes", True)),
        )


def load_simulation_scenario(path: Path) -> SimulationScenario:
    """Parse a YAML or JSON simulation file into ``SimulationScenario``."""
    raw_text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        parsed = json.loads(raw_text)
    else:
        parsed = yaml.safe_load(raw_text)
    if not isinstance(parsed, Mapping):
        raise ValueError(f"Simulation scenario {path} must parse to an object/mapping")
    return SimulationScenario.from_dict(parsed)


class FixtureReplayMiddleware(AgentMiddleware):
    """LangChain ``AgentMiddleware`` returning deterministic fixture outputs before ``LLMToolEmulator``."""

    def __init__(self, fixtures: Mapping[str, Any]) -> None:
        """Initialize FixtureReplayMiddleware."""
        super().__init__()
        self._fixtures = dict(fixtures)

    def _lookup(self, tool_name: str, tool_call_id: str) -> Any | None:
        if tool_call_id in self._fixtures:
            return self._fixtures[tool_call_id]
        if tool_name in self._fixtures:
            return self._fixtures[tool_name]
        return None

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """Execute `wrap_tool_call`."""
        call = getattr(request, "tool_call", {}) or {}
        t_name = str(call.get("name") or "")
        tc_id = str(call.get("id") or "")
        hit = self._lookup(t_name, tc_id)
        if hit is not None:
            scrubbed = scrub_tool_payload(hit, tool_name=t_name)
            content = scrubbed if isinstance(scrubbed, str) else json.dumps(scrubbed)
            return ToolMessage(content=content, tool_call_id=tc_id, name=t_name)
        return handler(request)

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """Execute `awrap_tool_call`."""
        call = getattr(request, "tool_call", {}) or {}
        t_name = str(call.get("name") or "")
        tc_id = str(call.get("id") or "")
        hit = self._lookup(t_name, tc_id)
        if hit is not None:
            scrubbed = scrub_tool_payload(hit, tool_name=t_name)
            content = scrubbed if isinstance(scrubbed, str) else json.dumps(scrubbed)
            return ToolMessage(content=content, tool_call_id=tc_id, name=t_name)
        return await handler(request)


@dataclass(slots=True)
class ScenarioRunResult:
    """Detailed pass/fail evaluation of a single ``SimulationScenario``."""

    scenario_id: str
    passed: bool
    duration_ms: float
    failures: list[str] = field(default_factory=list)
    policy_blocks_observed: int = 0
    checklist_statuses: dict[str, str] = field(default_factory=dict)
    stage0_passed: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Return `to_dict` result."""
        return {
            "scenario_id": self.scenario_id,
            "passed": self.passed,
            "duration_ms": round(self.duration_ms, 2),
            "failures": list(self.failures),
            "policy_blocks_observed": self.policy_blocks_observed,
            "checklist_statuses": dict(self.checklist_statuses),
            "stage0_passed": self.stage0_passed,
        }


@dataclass(slots=True)
class SimulationSuiteReport:
    """Aggregated report across all scenarios in a plugin directory."""

    passed: bool
    scenarios_total: int
    scenarios_passed: int
    results: list[ScenarioRunResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return `to_dict` result."""
        return {
            "passed": self.passed,
            "scenarios_total": self.scenarios_total,
            "scenarios_passed": self.scenarios_passed,
            "results": [r.to_dict() for r in self.results],
        }


class PluginSimulator:
    """Executes synthetic & adversarial SRE simulation suites for TalkOps plugins."""

    def __init__(self, *, emulator_model: Any | None = None) -> None:
        """Initialize PluginSimulator."""
        self._emulator_model = emulator_model

    def build_simulation_middlewares(
        self,
        scenario: SimulationScenario,
        *,
        emulator_model: Any | None = None,
    ) -> list[AgentMiddleware]:
        """Compose Policy Guard + Fixture Replay + native LangChain ``LLMToolEmulator``."""
        middlewares: list[AgentMiddleware] = [
            ExecutionProfileGuardMiddleware(
                default_profile=scenario.execution_profile,
                metadata_only_datastores=scenario.metadata_only_datastores,
            ),
            FixtureReplayMiddleware(scenario.fixture_responses),
        ]
        eff_model = emulator_model or self._emulator_model
        if eff_model is not None or scenario.emulated_tools:
            emu_kwargs: dict[str, Any] = {}
            if scenario.emulated_tools:
                emu_kwargs["tools"] = list(scenario.emulated_tools)
            if eff_model is not None:
                emu_kwargs["model"] = eff_model
                middlewares.append(LLMToolEmulator(**emu_kwargs))
        return middlewares

    async def run_scenario(
        self,
        scenario: SimulationScenario,
        *,
        agent_runner: Callable[[SimulationScenario, list[AgentMiddleware]], Awaitable[dict[str, Any]]] | None = None,
    ) -> ScenarioRunResult:
        """Run a scenario through checklists, policy guards, replay/emulator, and Stage 0 AutoQA."""
        from talkops.rubrics.stratified_autoqa import StratifiedAutoQAEvaluator

        t0 = time.perf_counter()
        failures: list[str] = []
        policy_blocks = 0

        checklist_statuses: dict[str, str] = {}
        if scenario.checklist_specs:
            specs: list[PreflightCheckSpec] = []
            for i, c in enumerate(scenario.checklist_specs):
                f_kw = [re.escape(str(k)) for k in (c.get("finding_keywords") or [])]
                f_regex = str(c.get("finding_regex") or ("|".join(f_kw) if f_kw else ""))
                specs.append(
                    PreflightCheckSpec(
                        check_id=str(c.get("check_id") or f"chk-{i}"),
                        title=str(c.get("title") or f"Check {i}"),
                        probe_name=str(c.get("tool_name") or c.get("probe") or ""),
                        probe_args=dict(c.get("tool_args") or c.get("args") or {}),
                        finding_regex=f_regex,
                    )
                )

            async def _probe_exec(tool_name: str, tool_args: Mapping[str, Any]) -> Any:
                del tool_args
                if tool_name in scenario.fixture_responses:
                    val = scenario.fixture_responses[tool_name]
                    if isinstance(val, str) and val.startswith("RAISE_TIMEOUT"):
                        raise TimeoutError(val)
                    return val
                raise TimeoutError(f"Unmocked checklist telemetry probe: {tool_name}")

            runner = PreflightChecklistRunner(ceiling_seconds=0.35)
            summary = await runner.run(specs, probe_executor=_probe_exec)
            for item in summary.items:
                checklist_statuses[item.check_id] = item.status

            for expected_id, expected_status in scenario.expected_checklist_statuses.items():
                actual_status = checklist_statuses.get(expected_id)
                if actual_status != expected_status:
                    failures.append(
                        f"Checklist {expected_id!r} expected status {expected_status!r}, got {actual_status!r}"
                    )

        middlewares = self.build_simulation_middlewares(scenario)
        messages: list[Any] = [
            HumanMessage(content=json.dumps(scenario.alert_payload or {"title": scenario.title}))
        ]
        emu = next((m for m in middlewares if isinstance(m, LLMToolEmulator)), None)

        if agent_runner is not None:
            state_out = await agent_runner(scenario, middlewares)
            messages = list(state_out.get("messages") or messages)
            hypotheses = list(state_out.get("hypothesis_ledger") or scenario.simulated_hypotheses)
            conclusion = str(state_out.get("conclusion") or scenario.simulated_conclusion)
        else:
            for step in scenario.tool_steps:
                decision = evaluate_tool_call_policy(
                    profile=scenario.execution_profile,
                    tool_name=step.tool_name,
                    tool_args=step.args,
                    metadata_only_datastores=scenario.metadata_only_datastores,
                )
                messages.append(
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"id": step.tool_call_id, "name": step.tool_name, "args": dict(step.args)}
                        ],
                    )
                )
                if not decision.allowed:
                    policy_blocks += 1
                    if not step.expect_blocked and scenario.forbid_unblocked_writes:
                        failures.append(
                            f"Unexpected policy block on {step.tool_name!r} ({step.tool_call_id}): {decision.reason}"
                        )
                    messages.append(
                        ToolMessage(
                            content=f"[POLICY_BLOCKED:{decision.violation_code}] {decision.reason}",
                            tool_call_id=step.tool_call_id,
                            name=step.tool_name,
                        )
                    )
                    continue

                if step.expect_blocked:
                    failures.append(
                        f"Red-team step {step.tool_call_id} ({step.tool_name!r}) expected policy block under "
                        f"{scenario.execution_profile!r} but was ALLOWED!"
                    )

                raw_out = scenario.fixture_responses.get(
                    step.tool_call_id,
                    scenario.fixture_responses.get(step.tool_name),
                )
                if raw_out is None and emu is not None:

                    class _Req:
                        def __init__(self, tc: dict[str, Any]) -> None:
                            self.tool_call = tc
                            self.tool = None

                    emu_msg = await emu.awrap_tool_call(
                        _Req({"id": step.tool_call_id, "name": step.tool_name, "args": dict(step.args)}),
                        lambda _r: ToolMessage(content="{}", tool_call_id=step.tool_call_id, name=step.tool_name),
                    )
                    raw_out = getattr(emu_msg, "content", "{}")
                elif raw_out is None:
                    raw_out = {"status": "ok", "tool": step.tool_name}

                scrubbed = scrub_tool_payload(raw_out, tool_name=step.tool_name)
                content_str = scrubbed if isinstance(scrubbed, str) else json.dumps(scrubbed)
                messages.append(
                    ToolMessage(
                        content=content_str,
                        tool_call_id=step.tool_call_id,
                        name=step.tool_name,
                    )
                )

            hypotheses = list(scenario.simulated_hypotheses)
            conclusion = scenario.simulated_conclusion
            if conclusion:
                messages.append(AIMessage(content=conclusion))

        lower_conclusion = conclusion.lower()
        for kw in scenario.expected_root_cause_keywords:
            if kw.lower() not in lower_conclusion:
                failures.append(f"Conclusion missing expected root-cause keyword: {kw!r}")

        ruled_out_count = sum(
            1 for h in hypotheses if str(h.get("status") or "").lower() == "ruled_out"
        )
        if ruled_out_count < scenario.must_rule_out_min:
            failures.append(
                f"Expected >= {scenario.must_rule_out_min} ruled_out hypotheses, got {ruled_out_count}"
            )

        evaluator = StratifiedAutoQAEvaluator()
        stage0 = evaluator.verify_stage0(
            messages=messages,
            hypothesis_ledger=hypotheses,
            require_ruled_out=scenario.must_rule_out_min > 0,
            allow_blocked_redteam=any(s.expect_blocked for s in scenario.tool_steps),
        )
        if not stage0.passed:
            failures.extend(f"Stage0 AutoQA violation: {v}" for v in stage0.violations)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return ScenarioRunResult(
            scenario_id=scenario.scenario_id,
            passed=len(failures) == 0,
            duration_ms=elapsed_ms,
            failures=failures,
            policy_blocks_observed=policy_blocks,
            checklist_statuses=checklist_statuses,
            stage0_passed=stage0.passed,
        )

    async def run_directory_suite(self, simulations_dir: Path) -> SimulationSuiteReport:
        """Load and execute every ``*.yaml`` / ``*.yml`` / ``*.json`` scenario in ``simulations_dir``."""
        if not simulations_dir.is_dir():
            return SimulationSuiteReport(passed=True, scenarios_total=0, scenarios_passed=0, results=[])
        files = sorted(
            p
            for p in simulations_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".yaml", ".yml", ".json"}
        )
        results: list[ScenarioRunResult] = []
        for f in files:
            sc = load_simulation_scenario(f)
            results.append(await self.run_scenario(sc))
        passed_cnt = sum(1 for r in results if r.passed)
        return SimulationSuiteReport(
            passed=passed_cnt == len(results),
            scenarios_total=len(results),
            scenarios_passed=passed_cnt,
            results=results,
        )


HarnessSimulator = PluginSimulator

__all__ = [
    "FixtureReplayMiddleware",
    "HarnessSimulator",
    "PluginSimulator",
    "ScenarioRunResult",
    "SimulatedToolStep",
    "SimulationScenario",
    "SimulationSuiteReport",
    "load_simulation_scenario",
]

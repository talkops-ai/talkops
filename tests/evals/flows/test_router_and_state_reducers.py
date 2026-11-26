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

"""Scope 1: Deterministic Router, Policy Ceiling, and State Reducer Evaluations.

Fast, zero-network verification of conditional edge functions, execution trust
profile truth tables, function-scoped `BindableFakeChatModel` tool loops, and
`pass@k` vs. `pass^k` (`p^k`) mathematical identities.
"""

from __future__ import annotations

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from talkops.security.execution_profiles import evaluate_tool_call_policy
from talkops.sre.hypothesis_graph import default_seed_hypotheses
from tests.evals.framework import (
    BindableFakeChatModel,
    compute_pass_at_k_and_pass_power_k,
)


@pytest.mark.eval_regression
@pytest.mark.eval_scope("router_logic")
@pytest.mark.eval_category("guardrails")
@pytest.mark.parametrize(
    ("profile", "tool_name", "tool_args", "tool_meta", "expected_allowed"),
    [
        ("readonly-metadata", "kubectl_get_pods", {"namespace": "prod"}, {"readOnlyHint": True}, True),
        ("readonly-metadata", "write_todos", {"todos": []}, {}, True),
        ("readonly-metadata", "kubectl_delete_pod", {"pod_name": "checkout-1"}, {"readOnlyHint": False}, False),
        ("readonly-metadata", "write_file", {"file_path": "/tmp/a.yaml"}, {}, False),
        ("gitops-pr", "github_create_pull_request", {"repository": "acme/app"}, {"readOnlyHint": False}, True),
        ("gitops-pr", "kubectl_apply_manifest", {"namespace": "prod"}, {"readOnlyHint": False}, False),
        ("autonomous-tnr", "kubectl_rollout_undo", {"deployment": "checkout-api"}, {"readOnlyHint": False}, True),
        ("autonomous-tnr", "terraform_destroy_stack", {"stack_name": "prod-core"}, {"destructiveHint": True}, False),
        ("autonomous-tnr", "aws_ec2_terminate_instances", {"instance_ids": ["i-1"]}, {"destructiveHint": True}, False),
    ],
)
def test_execution_profile_policy_router_enforces_trust_ceilings(
    profile: str,
    tool_name: str,
    tool_args: dict[str, object],
    tool_meta: dict[str, object],
    expected_allowed: bool,
) -> None:
    """Verify execution profile policy decisions across read-only, GitOps PR, and autonomous-tnr modes."""
    decision = evaluate_tool_call_policy(
        profile=profile,
        tool_name=tool_name,
        tool_args=tool_args,
        tool_metadata=tool_meta,
    )
    assert decision.allowed is expected_allowed


@pytest.mark.eval_regression
@pytest.mark.eval_scope("router_logic")
@pytest.mark.eval_category("graph_routing")
def test_competing_hypothesis_seeder_generates_recent_change_and_dependency_branches() -> None:
    """Verify `default_seed_hypotheses` deterministically generates competing H1/H2/H3 branches."""
    seeded = default_seed_hypotheses(
        {
            "service_name": "checkout-api",
            "alert_summary": "checkout-api HTTP 5xx jumped to 38%",
            "blast_radius": {
                "recent_changes": [{"source": "argocd", "commit_sha": "9f8e7d6c"}],
                "neighbors": [{"service_name": "orders-rds"}],
            },
        }
    )
    categories = {h["category"] for h in seeded}
    assert len(seeded) >= 3
    assert {"recent_change", "dependency_cascade", "resource_saturation"} <= categories


@pytest.fixture
def fresh_fake_tool_model() -> BindableFakeChatModel:
    """Function-scoped `BindableFakeChatModel` ensuring iterator never exhausts across tests."""
    return BindableFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "tc-1", "name": "lookup_slo_budget", "args": {"service": "checkout-api"}}],
                ),
                AIMessage(content="Error budget remaining is 99.4%."),
            ]
        )
    )


@pytest.mark.eval_regression
@pytest.mark.eval_scope("router_logic")
@pytest.mark.eval_category("tool_use")
def test_bindable_fake_chat_model_executes_deterministic_tool_loop_per_test(
    fresh_fake_tool_model: BindableFakeChatModel,
) -> None:
    """Verify function-scoped `BindableFakeChatModel` binds tools and executes a complete loop."""

    @tool
    def lookup_slo_budget(service: str) -> str:
        """Return remaining monthly SLO error budget for `service`."""
        return f"{service}: 99.4%"

    loop_agent = create_agent(model=fresh_fake_tool_model, tools=[lookup_slo_budget])
    result = loop_agent.invoke({"messages": [HumanMessage(content="Check checkout-api SLO budget")]})
    assert "99.4%" in str(result["messages"][-1].content)


@pytest.mark.eval_regression
@pytest.mark.eval_scope("router_logic")
@pytest.mark.eval_category("calibration")
def test_pass_at_k_versus_pass_power_k_divergence_matches_probability_identities() -> None:
    """Verify `pass@k` vs. `pass^k` (`p^k`) divergence at p=0.75 across k=1, k=3, and k=10."""
    m_k1 = compute_pass_at_k_and_pass_power_k(num_trials=4, num_successes=3, k=1)
    m_k3 = compute_pass_at_k_and_pass_power_k(num_trials=4, num_successes=3, k=3)
    m_k10 = compute_pass_at_k_and_pass_power_k(num_trials=4, num_successes=3, k=10)

    # At k=1: pass@1 == pass^1 == 0.75
    assert round(m_k1.pass_at_k, 3) == 0.750
    assert round(m_k1.pass_power_k, 3) == 0.750

    # At k=3: pass@3 = 98.4% vs pass^3 = 42.2% (sub-50% batch consistency zone!)
    assert round(m_k3.pass_at_k * 100, 1) == 98.4
    assert round(m_k3.pass_power_k * 100, 1) == 42.2

    # At k=10: pass@10 -> 94.4%..100% while pass^10 collapses to 5.6%
    assert round(m_k10.pass_power_k * 100, 1) == 5.6

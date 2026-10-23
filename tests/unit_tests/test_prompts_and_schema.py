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

"""Unit tests for TalkOps prompts generation and typed interrupt schemas."""

import re
from pathlib import Path

from talkops.prompts import (
    MODEL_IDENTITY_RE,
    TALKOPS_MEMORY_HEADLESS_SYSTEM_PROMPT,
    TALKOPS_MEMORY_READONLY_SYSTEM_PROMPT,
    TALKOPS_MEMORY_SYSTEM_PROMPT,
    CloudProvider,
    build_fs_tool_guidance,
    build_model_identity_section,
    build_working_dir_section,
    get_base_system_prompt,
    get_memory_system_prompt,
    normalize_cloud_provider,
)
from talkops.schema.interrupts import HitlResumePayload


def test_build_model_identity_section():
    """Verify build model identity section."""
    section = build_model_identity_section(
        name="claude-3-7-sonnet",
        provider="anthropic",
        context_limit=200_000,
        unsupported_modalities=frozenset(["audio"]),
    )
    assert "### Model Identity" in section
    assert "claude-3-7-sonnet" in section
    assert "anthropic" in section
    assert "200,000" in section
    assert "Audio" in section


def test_build_model_identity_empty():
    """Verify build model identity empty."""
    assert build_model_identity_section(None) == ""
    assert build_model_identity_section("") == ""


def test_normalize_cloud_provider():
    """Verify normalize cloud provider."""
    assert normalize_cloud_provider(None) == CloudProvider.AWS
    assert normalize_cloud_provider("aws") == CloudProvider.AWS
    assert normalize_cloud_provider("amazon") == CloudProvider.AWS
    assert normalize_cloud_provider("azure") == CloudProvider.AZURE
    assert normalize_cloud_provider("az") == CloudProvider.AZURE
    assert normalize_cloud_provider("gcp") == CloudProvider.GCP
    assert normalize_cloud_provider("google") == CloudProvider.GCP
    assert normalize_cloud_provider("multi") == CloudProvider.MULTI
    assert normalize_cloud_provider("unknown_provider") == CloudProvider.AWS


def test_get_base_system_prompt_aws_default():
    """Verify get base system prompt aws default."""
    prompt = get_base_system_prompt(
        assistant_id="talkops",
        interactive=True,
        model_name="gpt-4o",
    )
    assert len(prompt) > 200
    assert "Multi-Agent Orchestrator" in prompt
    assert "Skill-Based Multi-Agent Framework" in prompt
    assert "write_todos" in prompt
    assert "commit" in prompt
    assert "remember" in prompt
    assert "simplify" in prompt
    assert "skill-creator" in prompt
    assert "aws-core" not in prompt
    assert "Computational Verification" in prompt
    assert "Blast Radius Safeguards" in prompt
    assert "Preserving Subagent Technical Deliverables" in prompt


def test_get_base_system_prompt_azure():
    """Verify get base system prompt azure."""
    prompt = get_base_system_prompt(
        assistant_id="talkops",
        interactive=True,
        model_name="claude-3-7-sonnet",
        cloud_provider=CloudProvider.AZURE,
    )
    assert "Cloud Platform Context: Azure" in prompt
    assert "Microsoft Azure" in prompt
    assert "az account show" in prompt


def test_get_base_system_prompt_gcp():
    """Verify get base system prompt gcp."""
    prompt = get_base_system_prompt(
        assistant_id="talkops",
        interactive=True,
        model_name="gemini-2.5-pro",
        cloud_provider=CloudProvider.GCP,
    )
    assert "Cloud Platform Context: Google Cloud (GCP)" in prompt
    assert "Google Cloud Platform (GCP)" in prompt
    assert "Workload Identity" in prompt


def test_get_base_system_prompt_multi():
    """Verify get base system prompt multi."""
    prompt = get_base_system_prompt(
        assistant_id="talkops",
        interactive=True,
        model_name="claude-3-5-sonnet",
        cloud_provider=CloudProvider.MULTI,
    )
    assert "Cloud Platform Context: Multi-Cloud" in prompt
    assert "Multi-Cloud / Hybrid" in prompt
    assert "workload portability" in prompt.lower()


def test_get_base_system_prompt_headless():
    """Verify get base system prompt headless."""
    prompt = get_base_system_prompt(
        assistant_id="talkops",
        interactive=False,
        model_name="claude-3-5-sonnet",
    )
    assert len(prompt) > 100
    assert "non-interactive (headless) mode" in prompt
    assert "Do NOT ask clarifying questions" in prompt
    assert "Never run commands that block waiting for stdin" in prompt


def test_no_unreplaced_placeholders():
    """Verify no unreplaced placeholders."""
    for provider in [CloudProvider.AWS, CloudProvider.AZURE, CloudProvider.GCP, CloudProvider.MULTI]:
        for interactive in [True, False]:
            prompt = get_base_system_prompt(
                assistant_id="talkops",
                interactive=interactive,
                cwd="/tmp/test_workspace",
                fs_tools=["read_file", "write_file", "edit_file"],
                model_name="claude-3-7-sonnet",
                model_provider="anthropic",
                model_context_limit=200_000,
                cloud_provider=provider,
                sandbox_type=None,
                has_search=True,
            )
            unreplaced = re.findall(r"\{[a-z_]+\}", prompt)
            assert not unreplaced, f"Found unreplaced placeholders for {provider} (interactive={interactive}): {unreplaced}"


def test_model_identity_hotpatch_regex():
    """Verify model identity hotpatch regex."""
    initial_prompt = get_base_system_prompt(
        model_name="gpt-4o",
        model_provider="openai",
        model_context_limit=128_000,
    )
    assert "model `gpt-4o` (provider: openai)" in initial_prompt

    new_identity = build_model_identity_section(
        name="claude-3-7-sonnet",
        provider="anthropic",
        context_limit=200_000,
    )
    patched = MODEL_IDENTITY_RE.sub(new_identity, initial_prompt, count=1)
    assert "model `claude-3-7-sonnet` (provider: anthropic)" in patched
    assert "gpt-4o" not in patched
    assert "Multi-Agent Orchestrator" in patched


def test_sandbox_working_dir_guidance():
    # Local mode
    """Verify sandbox working dir guidance."""
    local_section = build_working_dir_section(cwd="/Users/operator/project", sandbox_type=None)
    assert "/Users/operator/project" in local_section
    assert "remote Linux sandbox" not in local_section

    # Remote sandbox mode
    sandbox_section = build_working_dir_section(cwd="/workspace", sandbox_type="daytona")
    assert "**remote Linux sandbox** (`daytona`)" in sandbox_section
    assert "You do NOT have access to the operator's local filesystem" in sandbox_section


def test_fs_tool_guidance():
    # Restricted tool set
    """Verify fs tool guidance."""
    guidance = build_fs_tool_guidance(["read_file", "write_file"])
    assert "You have restricted access to the filesystem" in guidance
    assert "`edit_file`" not in guidance

    # Full set
    full_guidance = build_fs_tool_guidance(None)
    assert "edit_file" in full_guidance
    assert "write_file" in full_guidance


def test_memory_system_prompts():
    """Verify memory system prompts."""
    default_prompt = get_memory_system_prompt()
    assert default_prompt == TALKOPS_MEMORY_SYSTEM_PROMPT
    assert "live cloud telemetry and explicit operator commands" in default_prompt.lower()

    readonly_prompt = get_memory_system_prompt(readonly=True)
    assert readonly_prompt == TALKOPS_MEMORY_READONLY_SYSTEM_PROMPT
    assert "Automatic memory saving is disabled" in readonly_prompt

    headless_prompt = get_memory_system_prompt(headless=True)
    assert headless_prompt == TALKOPS_MEMORY_HEADLESS_SYSTEM_PROMPT
    assert "Working autonomously" in headless_prompt


def test_built_in_skills_exist_and_valid():
    """Verify built in skills exist and valid."""
    built_in_dir = Path(__file__).parent.parent.parent / "src" / "talkops" / "built_in_skills"
    assert built_in_dir.is_dir()

    expected_skills = [
        "commit",
        "remember",
        "simplify",
        "skill-creator",
    ]

    for skill_name in expected_skills:
        skill_file = built_in_dir / skill_name / "SKILL.md"
        assert skill_file.exists(), f"Missing SKILL.md for {skill_name}"
        content = skill_file.read_text(encoding="utf-8")
        assert f"name: {skill_name}" in content
        assert "description:" in content


def test_hitl_resume_payload_parsing():
    """Verify hitl resume payload parsing."""
    payload_approve = HitlResumePayload.from_raw("approve")
    assert len(payload_approve.decisions) == 1
    assert payload_approve.decisions[0].type == "approve"
    assert payload_approve.auto_approve_requested is False

    payload_reject = HitlResumePayload.from_raw("reject")
    assert len(payload_reject.decisions) == 1
    assert payload_reject.decisions[0].type == "reject"

    payload_auto = HitlResumePayload.from_raw({"decisions": [{"type": "auto"}]})
    assert payload_auto.auto_approve_requested is True
    assert payload_auto.decisions[0].type == "approve"


def test_get_base_system_prompt_dual_engine_and_tool_discipline():
    """Verify get base system prompt dual engine and tool discipline."""
    prompt = get_base_system_prompt(
        assistant_id="talkops",
        interactive=True,
        model_name="claude-3-7-sonnet",
    )
    # Validate Dual-Engine Architecture
    assert "Dual-Engine Architecture" in prompt
    assert "Platform Engineering & DevOps Coding Specialist" in prompt
    assert "Multi-Agent Cloud Operations & Orchestrator" in prompt
    assert "Infrastructure as Code (IaC)" in prompt
    assert "Cloud-Native & Container Orchestration" in prompt
    assert "CI/CD & GitOps Automation" in prompt

    # Validate Cloud Foundation & Plugin Marketplace Ecosystem
    assert "Native Cloud Foundation (AWS)" in prompt
    assert "Extensible Plugin Marketplace Ecosystem" in prompt

    # Validate TalkOps Coding Guidelines & Documentation Integrity
    assert "No Snippet Tunnel Vision" in prompt
    assert "API Contract Preservation" in prompt
    assert "No Superficial Symptom Patches" in prompt
    assert "Documentation Integrity & Comment Hygiene" in prompt
    assert "Traceback & Log Justification Required" in prompt
    assert "Slash Commands" in prompt

    # Validate Tool-Specific Output Discipline
    assert "Tool Usage & Tool-Specific Output Discipline" in prompt
    assert "`read_file`" in prompt
    assert "`edit_file`" in prompt
    assert "`write_file`" in prompt
    assert "`task` & `js_eval` (Subagent Delegation)" in prompt

    # Ensure stale draft phrasing was eliminated
    assert "(primarily AWS for this release" not in prompt


def test_adaptive_output_granularity_contract():
    """Verify adaptive output granularity contract."""
    prompt = get_base_system_prompt(
        assistant_id="talkops",
        interactive=True,
        model_name="claude-3-7-sonnet",
    )
    assert "Adaptive Output Granularity Contract" in prompt
    assert "Conversational & Capability Inquiries" in prompt
    assert "DO NOT output an exhaustive laundry list" in prompt
    assert "Artifact vs Inline Output Separation" in prompt


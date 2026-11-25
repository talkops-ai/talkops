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

"""GitOps Pull-Request Remediation Builder for ``gitops-pr`` Execution Profile."""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class GitOpsPRProposal:
    """Deterministic, evidence-linked Pull Request payload for IaC / Kubernetes manifest fixes."""

    branch_name: str
    commit_message: str
    pr_title: str
    pr_body: str
    target_file: str
    unified_diff: str
    rollback_command: str
    evidence_tool_call_ids: list[str] = field(default_factory=list)


class GitOpsPRRemediator:
    """Constructs auditable GitOps PR proposals backed by competing-hypothesis proofs."""

    @staticmethod
    def _slugify(text: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return slug[:40] or "remediation"

    def build_proposal(
        self,
        *,
        incident_id: str,
        service_name: str,
        root_cause_summary: str,
        target_file: str | Path,
        original_content: str,
        patched_content: str,
        hypothesis_ledger: Sequence[Mapping[str, Any]] = (),
        rollback_command: str = "",
    ) -> GitOpsPRProposal:
        """Execute `build_proposal`."""
        rel_file = str(target_file)
        slug = self._slugify(f"{service_name}-{incident_id}")
        branch_name = f"talkops/sre-fix/{slug}"

        diff_lines = list(
            difflib.unified_diff(
                original_content.splitlines(keepends=True),
                patched_content.splitlines(keepends=True),
                fromfile=f"a/{rel_file}",
                tofile=f"b/{rel_file}",
            )
        )
        unified_diff = "".join(diff_lines)

        cites: list[str] = []
        matrix_lines = [
            "| Hypothesis | Verdict | Confidence | Citations | Proof / Notes |",
            "| :--- | :--- | :---: | :--- | :--- |",
        ]
        for h in hypothesis_ledger:
            h_cites = [str(c) for c in (h.get("evidence_tool_call_ids") or [])]
            for c in h_cites:
                if c not in cites:
                    cites.append(c)
            proof = str(
                h.get("supporting_evidence")
                or h.get("falsification_proof")
                or "-"
            ).replace("\n", " ")[:120]
            matrix_lines.append(
                f"| `{h.get('hypothesis_id', '-')}` {h.get('title', '')} "
                f"| **{h.get('status', 'testing')}** "
                f"| {float(h.get('confidence', 0.0)):.0%} "
                f"| {', '.join(f'`{c}`' for c in h_cites) or '-'} "
                f"| {proof} |"
            )

        effective_rollback = rollback_command or f"git revert HEAD  # reverts {branch_name}"

        body_sections = [
            f"## Autonomous SRE Remediation Proposal (`{incident_id}`)",
            f"- **Service**: `{service_name}`",
            "- **Execution Profile**: `gitops-pr` (Zero direct live cluster mutations)",
            f"- **Confirmed Root Cause**: {root_cause_summary}",
            "",
            "### Competing Hypothesis Matrix (Confirmed & Ruled-Out Proofs)",
            *(matrix_lines if hypothesis_ledger else ["_No competing hypothesis ledger attached._"]),
            "",
            "### Proposed Manifest / IaC Diff",
            "```diff",
            unified_diff.strip() or "# No line diff",
            "```",
            "",
            "### Deterministic Rollback Plan",
            "```bash",
            effective_rollback,
            "```",
        ]

        return GitOpsPRProposal(
            branch_name=branch_name,
            commit_message=f"fix({service_name}): remediate {incident_id} — {root_cause_summary[:60]}",
            pr_title=f"[TalkOps SRE] fix({service_name}): {root_cause_summary[:72]}",
            pr_body="\n".join(body_sections),
            target_file=rel_file,
            unified_diff=unified_diff,
            rollback_command=effective_rollback,
            evidence_tool_call_ids=cites,
        )

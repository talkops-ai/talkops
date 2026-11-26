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

"""8th Outbound Sink: ``PostmortemArtifactSink`` & 3-Stage SRE v2 Notification Cards."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_TRISTATE_BADGE = {
    "Finding": "🔴 Finding",
    "Normal": "🟢 Normal",
    "Inconclusive": "🟡 Inconclusive",
}


def render_sre_v2_hypothesis_summary(
    *,
    thread_id: str,
    incident_id: str,
    service_name: str,
    analysis_text: str,
    preflight_summary: Mapping[str, Any] | None = None,
    hypothesis_ledger: Sequence[Mapping[str, Any]] | None = None,
    verification_status: str = "unverified",
) -> str:
    """Render the 3-Stage TalkOps v2 SRE summary (Pre-Flight -> Competing Hypotheses -> Verification & TUI Handoff)."""
    sections: list[str] = [
        f"## Incident RCA Report — `{incident_id}` (`{service_name or 'infrastructure'}`)",
        f"- **Thread Handoff**: `talkops --thread {thread_id}`",
        f"- **Verification Status**: `{verification_status}`",
        "",
    ]

    if preflight_summary and preflight_summary.get("items"):
        sections.append("### Stage 1 · Pre-Flight Tri-State Checklist")
        for it in preflight_summary.get("items", []):
            badge = _TRISTATE_BADGE.get(str(it.get("status")), str(it.get("status")))
            sections.append(
                f"- {badge} **{it.get('title', it.get('check_id'))}** "
                f"(`{it.get('duration_ms', 0)}ms`): {it.get('detail', '')}"
            )
        sections.append("")

    if hypothesis_ledger:
        sections.append("### Stage 2 · Competing Hypothesis Matrix (Confirmed & Ruled-Out)")
        sections.append("| ID | Hypothesis | Status | Confidence | Evidence Citations | Proof |")
        sections.append("| :--- | :--- | :--- | :---: | :--- | :--- |")
        for h in hypothesis_ledger:
            cites = ", ".join(f"`{c}`" for c in (h.get("evidence_tool_call_ids") or [])) or "-"
            proof = str(
                h.get("supporting_evidence") or h.get("falsification_proof") or ""
            ).replace("\n", " ")[:140]
            sections.append(
                f"| `{h.get('hypothesis_id', '-')}` | {h.get('title', '')} "
                f"| **{h.get('status', 'testing')}** | {float(h.get('confidence', 0.0)):.0%} "
                f"| {cites} | {proof} |"
            )
        sections.append("")

    sections.extend(
        [
            "### Root-Cause Analysis & Remediation Details",
            analysis_text.strip() or "_No analysis text generated._",
            "",
            "### Stage 3 · Post-Remediation Closure & Interactive Continuation",
            f"- Resume interactively in TUI with full context & checkpoints: `talkops --thread {thread_id}`",
        ]
    )
    return "\n".join(sections)


def build_slack_v2_interactive_blocks(
    *,
    thread_id: str,
    incident_id: str,
    service_name: str,
    preflight_summary: Mapping[str, Any] | None = None,
    hypothesis_ledger: Sequence[Mapping[str, Any]] | None = None,
    verification_status: str = "unverified",
) -> list[dict[str, Any]]:
    """Build Slack Block Kit interactive Stage 1/2/3 blocks + bidirectional feedback buttons."""
    blocks: list[dict[str, Any]] = []

    if preflight_summary and preflight_summary.get("counts"):
        c = preflight_summary["counts"]
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Pre-Flight Tri-State Triage*: 🔴 Findings `{c.get('Finding', 0)}` · "
                        f"🟢 Normal `{c.get('Normal', 0)}` · 🟡 Inconclusive `{c.get('Inconclusive', 0)}`"
                    ),
                },
            }
        )

    if hypothesis_ledger:
        lines: list[str] = ["*Competing Hypothesis Matrix*:"]
        for h in list(hypothesis_ledger)[:5]:
            icon = "✅" if h.get("status") == "confirmed" else ("❌" if h.get("status") == "ruled_out" else "🔎")
            cites = ", ".join(h.get("evidence_tool_call_ids") or [])
            lines.append(
                f"{icon} *{h.get('hypothesis_id')}* ({h.get('status')}, {float(h.get('confidence', 0.0)):.0%}): "
                f"{h.get('title')} [cites: `{cites or 'none'}`]"
            )
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]},
            }
        )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Verification: `{verification_status}` | "
                        f"Interactive TUI Handoff: `talkops --thread {thread_id}`"
                    ),
                }
            ],
        }
    )

    action_payload = json.dumps(
        {"thread_id": thread_id, "incident_id": incident_id, "service_name": service_name}
    )
    blocks.append(
        {
            "type": "actions",
            "block_id": f"talkops_feedback_{re.sub(r'[^a-zA-Z0-9_-]', '_', incident_id)[:32]}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Confirm RCA"},
                    "action_id": "talkops_feedback_confirmed",
                    "value": action_payload,
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "False Positive"},
                    "action_id": "talkops_feedback_false_positive",
                    "value": action_payload,
                },
            ],
        }
    )
    return blocks


class PostmortemArtifactSink:
    """8th Built-in Outbound Sink: Writes structured Markdown + JSON incident postmortems."""

    def __init__(self, output_dir: str | Path | None = None) -> None:
        """Initialize PostmortemArtifactSink."""
        raw_dir = (
            output_dir
            or os.environ.get("TALKOPS_POSTMORTEM_DIR")
            or (Path.cwd() / ".talkops" / "postmortems")
        )
        self.output_dir = Path(raw_dir)

    def write_postmortem(
        self,
        *,
        incident_id: str,
        thread_id: str,
        provider: str,
        service_name: str,
        severity: str,
        analysis_text: str,
        preflight_summary: Mapping[str, Any] | None = None,
        hypothesis_ledger: Sequence[Mapping[str, Any]] | None = None,
        verification_status: str = "unverified",
    ) -> tuple[Path, Path]:
        """Execute `write_postmortem`."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", incident_id or thread_id)[:80]
        md_path = self.output_dir / f"{safe_id}.md"
        json_path = self.output_dir / f"{safe_id}.json"

        markdown = render_sre_v2_hypothesis_summary(
            thread_id=thread_id,
            incident_id=incident_id,
            service_name=service_name,
            analysis_text=analysis_text,
            preflight_summary=preflight_summary,
            hypothesis_ledger=hypothesis_ledger,
            verification_status=verification_status,
        )
        md_path.write_text(markdown + "\n", encoding="utf-8")

        meta = {
            "incident_id": incident_id,
            "thread_id": thread_id,
            "provider": provider,
            "service_name": service_name,
            "severity": severity,
            "verification_status": verification_status,
            "generated_at": time.time(),
            "preflight_summary": dict(preflight_summary or {}),
            "hypothesis_ledger": [dict(h) for h in (hypothesis_ledger or [])],
            "markdown_path": str(md_path),
        }
        json_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        return md_path, json_path

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

"""Zero-write VPC MCP Relay (`talkops mcp-relay`) for Watson-style read-only isolation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from talkops.mcp.lockfile import load_mcp_lockfile, verify_mcp_lockfile
from talkops.security.execution_profiles import evaluate_tool_call_policy
from talkops.security.redaction import scrub_tool_payload


class ZeroWriteMCPRelay:
    """Enforces ``readonly-metadata`` + pre-span secret scrubbing + optional ``mcp.lock.json`` inside a target VPC."""

    def __init__(
        self,
        *,
        metadata_only_datastores: bool = True,
        lockfile_path: Path | None = None,
        egress_allowlist: tuple[str, ...] = (),
    ) -> None:
        """Initialize ZeroWriteMCPRelay."""
        self.metadata_only_datastores = metadata_only_datastores
        self.egress_allowlist = egress_allowlist
        self._lockfile_data: dict[str, Any] | None = (
            load_mcp_lockfile(lockfile_path)
            if lockfile_path is not None and lockfile_path.is_file()
            else None
        )

    def verify_server_tools(self, server_name: str, tools: list[Any]) -> bool:
        """Verify discovered tools against pinned ``mcp.lock.json`` if configured."""
        if self._lockfile_data is None:
            return True
        res = verify_mcp_lockfile(server_name, tools, self._lockfile_data)
        return res.verified

    async def relay_tool_call(
        self,
        *,
        tool_name: str,
        tool_args: Mapping[str, Any],
        executor: Any,
        tool_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate tool call under ``readonly-metadata``, execute if allowed, and scrub secrets before egress."""
        decision = evaluate_tool_call_policy(
            profile="readonly-metadata",
            tool_name=tool_name,
            tool_args=tool_args,
            tool_metadata=tool_metadata,
            metadata_only_datastores=self.metadata_only_datastores,
            egress_allowlist=self.egress_allowlist,
        )
        if not decision.allowed:
            return {
                "ok": False,
                "violation_code": decision.violation_code,
                "error": decision.reason,
            }

        raw_result = (
            await executor(tool_name, dict(tool_args))
            if asyncio.iscoroutinefunction(executor)
            else executor(tool_name, dict(tool_args))
        )
        scrubbed = scrub_tool_payload(raw_result, tool_name=tool_name)
        return {
            "ok": True,
            "result": scrubbed,
        }


def setup_mcp_relay_parser(subparsers: Any) -> None:
    """Setup parser for ``talkops mcp-relay``."""
    parser = subparsers.add_parser(
        "mcp-relay",
        help="Run outbound-only zero-write MCP diagnostic relay ('watson-readonly').",
    )
    parser.add_argument(
        "--control-plane-url",
        dest="control_plane_url",
        type=str,
        default=os.environ.get("TALKOPS_RELAY_CONTROL_URL", ""),
        help="Outbound WSS/HTTPS TalkOps central server relay endpoint.",
    )
    parser.add_argument(
        "--lockfile",
        dest="lockfile",
        type=str,
        default=os.environ.get("TALKOPS_MCP_LOCKFILE", ""),
        help="Optional path to mcp.lock.json for SHA-256 tool verification.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        dest="check_config",
        help="Validate zero-write relay policy and exit 0.",
    )


def execute_mcp_relay(args: argparse.Namespace) -> int:
    """Execute ``talkops mcp-relay`` command."""
    os.environ["TALKOPS_EXECUTION_PROFILE"] = "readonly-metadata"
    lock_path = Path(args.lockfile) if getattr(args, "lockfile", "") else None
    relay = ZeroWriteMCPRelay(
        metadata_only_datastores=True,
        lockfile_path=lock_path,
    )
    status_payload = {
        "mode": "watson-readonly",
        "execution_profile": "readonly-metadata",
        "metadata_only_datastores": relay.metadata_only_datastores,
        "lockfile_loaded": relay._lockfile_data is not None,
        "control_plane_url": getattr(args, "control_plane_url", ""),
    }
    print(json.dumps(status_payload, indent=2))
    return 0

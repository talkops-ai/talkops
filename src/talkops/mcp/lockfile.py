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

"""MCP Tool Pinning, Descriptor Sanitization & Rug-Pull Defense (OWASP MCP03).

Protects TalkOps against:
1. Invisible Unicode tag / zero-width / bidirectional override smuggling in MCP
   tool descriptions and JSON Schema property descriptions.
2. Hidden prompt-injection tags (``<system>``, ``<IMPORTANT>``, HTML comments,
   ``IGNORE PREVIOUS INSTRUCTIONS``) embedded inside MCP server descriptors.
3. Supply-chain ``mcp.lock.json`` SHA-256 verification and mid-session ``tools/list``
   rug-pull mutations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Invisible Unicode Tag block (U+E0000..U+E007F), zero-width & bidi control chars
_INVISIBLE_UNICODE_RE = re.compile(
    r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\U000e0000-\U000e007f]+"
)

_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")

_INJECTION_BLOCK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "hidden_xml_directive",
        re.compile(
            r"<\s*(?:system|important|instructions|hidden|secret_instruction)\b[^>]*>[\s\S]*?<\s*/\s*(?:system|important|instructions|hidden|secret_instruction)\s*>",
            re.IGNORECASE,
        ),
    ),
    (
        "override_instruction",
        re.compile(
            r"(?i)\b(?:ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions"
            r"|do\s+not\s+(?:tell|inform|mention\s+to)\s+the\s+user"
            r"|exfiltrate\s+(?:all\s+)?(?:secrets|keys|credentials|env))\b[^\n.]*"
        ),
    ),
)


class MCPToolRugPullError(RuntimeError):
    """Raised when an MCP server mutates a pinned tool schema or description mid-session."""


class MCPLockfileViolationError(RuntimeError):
    """Raised when an MCP tool fails ``mcp.lock.json`` SHA-256 verification."""


@dataclass(frozen=True)
class LockfileVerificationResult:
    """Verification report comparing live MCP tools against ``mcp.lock.json``."""

    server_name: str
    verified: bool
    mismatched_tools: tuple[str, ...] = ()
    unpinned_tools: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def sanitize_mcp_descriptor(text: str | None) -> tuple[str, list[str]]:
    """Sanitize an MCP tool or parameter description and return ``(clean_text, warnings)``."""
    if not text:
        return "", []

    warnings: list[str] = []
    cleaned = text

    if _INVISIBLE_UNICODE_RE.search(cleaned):
        warnings.append("invisible_unicode_stripped")
        cleaned = _INVISIBLE_UNICODE_RE.sub("", cleaned)

    if _HTML_COMMENT_RE.search(cleaned):
        warnings.append("html_comment_stripped")
        cleaned = _HTML_COMMENT_RE.sub(" ", cleaned)

    for label, pattern in _INJECTION_BLOCK_PATTERNS:
        if pattern.search(cleaned):
            warnings.append(label)
            cleaned = pattern.sub("[STRIPPED:UNTRUSTED_MCP_DIRECTIVE]", cleaned)

    return cleaned.strip(), warnings


def sanitize_mcp_input_schema(
    schema: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Recursively sanitize string descriptions inside an MCP tool JSON Schema."""
    if not isinstance(schema, Mapping):
        return {}, []

    warnings: list[str] = []

    def _walk(node: Any) -> Any:
        if isinstance(node, Mapping):
            out: dict[str, Any] = {}
            for k, v in node.items():
                if k in ("description", "title") and isinstance(v, str):
                    clean_v, w = sanitize_mcp_descriptor(v)
                    warnings.extend(w)
                    out[str(k)] = clean_v
                else:
                    out[str(k)] = _walk(v)
            return out
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    sanitized = _walk(schema)
    return sanitized, warnings


def _extract_tool_fields(tool_obj: Any) -> tuple[str, str, dict[str, Any]]:
    """Extract ``(name, description, input_schema)`` from an MCP tool or dict."""
    if isinstance(tool_obj, Mapping):
        name = str(tool_obj.get("name", ""))
        desc = str(tool_obj.get("description", "") or "")
        schema = (
            tool_obj.get("inputSchema")
            or tool_obj.get("input_schema")
            or tool_obj.get("parameters")
            or {}
        )
    else:
        name = str(getattr(tool_obj, "name", ""))
        desc = str(getattr(tool_obj, "description", "") or "")
        schema = (
            getattr(tool_obj, "inputSchema", None)
            or getattr(tool_obj, "input_schema", None)
            or getattr(tool_obj, "args_schema", None)
            or {}
        )
    if not isinstance(schema, Mapping):
        schema = {}
    return name, desc, dict(schema)


def compute_mcp_tool_hash(
    name: str,
    description: str | None,
    input_schema: Mapping[str, Any] | None,
) -> str:
    """Compute canonical SHA-256 pin digest for an MCP tool's sanitized contract."""
    clean_desc, _ = sanitize_mcp_descriptor(description or "")
    clean_schema, _ = sanitize_mcp_input_schema(input_schema or {})
    canonical = json.dumps(
        {
            "name": name,
            "description": clean_desc,
            "inputSchema": clean_schema,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def generate_mcp_lockfile(
    servers_tools: Mapping[str, Sequence[Any]],
    *,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Generate a deterministic ``mcp.lock.json`` payload from discovered MCP servers."""
    servers_payload: dict[str, Any] = {}
    for server_name in sorted(servers_tools.keys()):
        tools_map: dict[str, Any] = {}
        for tool_obj in servers_tools[server_name]:
            t_name, t_desc, t_schema = _extract_tool_fields(tool_obj)
            if not t_name:
                continue
            tools_map[t_name] = {
                "sha256": compute_mcp_tool_hash(t_name, t_desc, t_schema),
            }
        servers_payload[server_name] = {
            "tools": dict(sorted(tools_map.items())),
        }

    lockfile_doc: dict[str, Any] = {
        "schemaVersion": "1.0",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "servers": servers_payload,
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(lockfile_doc, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return lockfile_doc


def load_mcp_lockfile(lockfile_path: Path) -> dict[str, Any]:
    """Load and parse an ``mcp.lock.json`` file."""
    data = json.loads(lockfile_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid mcp.lock.json at {lockfile_path}: expected JSON object")
    return data


def verify_mcp_lockfile(
    server_name: str,
    tools: Sequence[Any],
    lockfile_data: Mapping[str, Any],
    *,
    allow_unpinned: bool = False,
) -> LockfileVerificationResult:
    """Verify discovered tools for *server_name* against a parsed ``mcp.lock.json``."""
    servers = lockfile_data.get("servers", {})
    if not isinstance(servers, Mapping):
        return LockfileVerificationResult(
            server_name=server_name,
            verified=False,
            warnings=("Malformed 'servers' map in mcp.lock.json",),
        )

    # Match exact server name or base server name after `__` scope prefix
    server_entry = servers.get(server_name)
    if server_entry is None and "__" in server_name:
        base_name = server_name.rsplit("__", 1)[-1]
        server_entry = servers.get(base_name)

    if not isinstance(server_entry, Mapping):
        if allow_unpinned:
            return LockfileVerificationResult(
                server_name=server_name,
                verified=True,
                warnings=(f"Server {server_name!r} has no pin entry in mcp.lock.json",),
            )
        return LockfileVerificationResult(
            server_name=server_name,
            verified=False,
            unpinned_tools=tuple(_extract_tool_fields(t)[0] for t in tools if _extract_tool_fields(t)[0]),
            warnings=(f"Server {server_name!r} missing from mcp.lock.json",),
        )

    pinned_tools = server_entry.get("tools", {})
    if not isinstance(pinned_tools, Mapping):
        pinned_tools = {}

    mismatched: list[str] = []
    unpinned: list[str] = []

    for tool_obj in tools:
        t_name, t_desc, t_schema = _extract_tool_fields(tool_obj)
        if not t_name:
            continue
        pin_spec = pinned_tools.get(t_name)
        if pin_spec is None:
            unpinned.append(t_name)
            continue
        expected_hash = (
            pin_spec.get("sha256")
            if isinstance(pin_spec, Mapping)
            else str(pin_spec)
        )
        actual_hash = compute_mcp_tool_hash(t_name, t_desc, t_schema)
        if expected_hash != actual_hash:
            mismatched.append(t_name)

    verified = not mismatched and (allow_unpinned or not unpinned)
    return LockfileVerificationResult(
        server_name=server_name,
        verified=verified,
        mismatched_tools=tuple(mismatched),
        unpinned_tools=tuple(unpinned),
    )


@dataclass
class RuntimeMCPSchemaPinRegistry:
    """Process-wide runtime pin tracker that detects mid-session MCP ``tools/list`` rug-pulls."""

    _pins: dict[tuple[str, str], str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check_or_record(
        self,
        server_name: str,
        tool_name: str,
        description: str | None,
        input_schema: Mapping[str, Any] | None,
        *,
        strict: bool | None = None,
    ) -> str:
        """Record initial tool hash or verify unchanged on reconnect; raise on rug-pull if strict."""
        digest = compute_mcp_tool_hash(tool_name, description, input_schema)
        if strict is None:
            strict = os.environ.get("TALKOPS_MCP_ALLOW_SCHEMA_DRIFT", "").strip().lower() not in (
                "1",
                "true",
                "yes",
            )
        key = (server_name, tool_name)
        with self._lock:
            existing = self._pins.get(key)
            if existing is None:
                self._pins[key] = digest
                return digest
            if existing != digest:
                if strict:
                    raise MCPToolRugPullError(
                        f"MCP rug-pull detected on server {server_name!r} tool {tool_name!r}: "
                        f"pinned hash {existing} != live hash {digest}"
                    )
                self._pins[key] = digest
        return digest

    def reset(self, server_name: str | None = None) -> None:
        """Clear runtime pins (for testing or explicit plugin reload)."""
        with self._lock:
            if server_name is None:
                self._pins.clear()
            else:
                for key in [k for k in self._pins if k[0] == server_name]:
                    self._pins.pop(key, None)


_GLOBAL_PIN_REGISTRY = RuntimeMCPSchemaPinRegistry()


def get_runtime_mcp_pin_registry() -> RuntimeMCPSchemaPinRegistry:
    """Return the singleton ``RuntimeMCPSchemaPinRegistry``."""
    return _GLOBAL_PIN_REGISTRY

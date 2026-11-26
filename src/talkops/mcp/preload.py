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

"""Preload MCP server metadata and eagerly probe tools with diagnostics."""

from __future__ import annotations

import asyncio
import contextvars
import fnmatch
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Collection, Mapping
from contextlib import AsyncExitStack, nullcontext
from pathlib import Path
from typing import Any

try:
    from filelock import FileLock
except ImportError:  # pragma: no cover - dependency is declared by the project
    FileLock = None  # type: ignore[assignment,misc]

from talkops.mcp.config import resolve_mcp_server_env, sync_mcp_env_aliases
from talkops.mcp.mcp_info import MCPServerInfo, MCPServerStatus, MCPToolInfo
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_PROBE_TIMEOUT = 30.0
_PROBE_TIMEOUT = float(os.environ.get("TALKOPS_MCP_PROBE_TIMEOUT", str(_DEFAULT_PROBE_TIMEOUT)))
_MAX_CONCURRENT_PROBES = 8

_MCP_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
_MCP_TOOL_NAME_MAX_LENGTH = 64
_MCP_TOOL_NAME_HASH_LENGTH = 12

_PLUGIN_PATH_ENV_KEYS: frozenset[str] = frozenset({
    "CLAUDE_PLUGIN_ROOT",
    "TALKOPS_PLUGIN_ROOT",
    "CLAUDE_PLUGIN_DATA",
    "TALKOPS_PLUGIN_DATA",
})

_PROGRESS_CALLBACK: contextvars.ContextVar[Callable[[list[MCPServerInfo]], None] | None] = (
    contextvars.ContextVar("_PROGRESS_CALLBACK", default=None)
)


def _try_resolve_config_env(config: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve `${VAR}` / `${VAR:-default}` references for fingerprinting without raising."""
    try:
        return resolve_mcp_server_env("__fp__", config)
    except Exception:
        return dict(config)


def _config_fingerprint(config: Mapping[str, Any]) -> str:
    """Return a stable digest so same-name config or `${VAR}` changes invalidate metadata."""
    resolved = _try_resolve_config_env(config)
    payload = json.dumps(
        resolved,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _connection_fingerprint(config: Mapping[str, Any]) -> str:
    """Return a canonical digest of the underlying MCP connection and tool filters.

    Strips per-plugin directory env vars (`CLAUDE_PLUGIN_ROOT`, `TALKOPS_PLUGIN_ROOT`,
    `CLAUDE_PLUGIN_DATA`, `TALKOPS_PLUGIN_DATA`) and non-connection metadata (`source`,
    `timeout`, `trusted`) so identical MCP servers declared across multiple plugins
    and subagents share one probe and cache entry.
    """
    resolved = _try_resolve_config_env(config)
    transport = _resolve_transport(resolved)
    enabled = bool(resolved.get("enabled", True))
    if "disabled" in resolved:
        enabled = not bool(resolved.get("disabled"))
    raw_env = resolved.get("env")
    clean_env = (
        {
            str(k): str(v)
            for k, v in raw_env.items()
            if str(k) not in _PLUGIN_PATH_ENV_KEYS
        }
        if isinstance(raw_env, Mapping)
        else {}
    )
    raw_headers = resolved.get("headers")
    clean_headers = (
        {str(k): str(v) for k, v in raw_headers.items()}
        if isinstance(raw_headers, Mapping)
        else {}
    )
    raw_args = resolved.get("args")
    clean_args = [str(x) for x in raw_args] if isinstance(raw_args, (list, tuple)) else []
    raw_disabled = resolved.get("disabled_tools") or resolved.get("disabledTools") or ()
    raw_allowed = resolved.get("allowed_tools") or resolved.get("allowedTools") or ()
    canonical = {
        "enabled": enabled,
        "transport": transport,
        "command": str(resolved.get("command") or ""),
        "args": clean_args,
        "url": str(resolved.get("url") or resolved.get("serverUrl") or ""),
        "headers": clean_headers,
        "cwd": str(resolved.get("cwd") or ""),
        "env": clean_env,
        "disabled_tools": [str(x) for x in raw_disabled] if isinstance(raw_disabled, (list, tuple)) else [],
        "allowed_tools": [str(x) for x in raw_allowed] if isinstance(raw_allowed, (list, tuple)) else [],
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Compose a provider-safe MCP tool name within max length limits."""
    raw_name = f"{server_name}_{tool_name}"
    sanitized = _MCP_TOOL_NAME_RE.sub("_", raw_name).strip("_") or "unnamed"
    if sanitized == raw_name and len(sanitized) <= _MCP_TOOL_NAME_MAX_LENGTH:
        result = sanitized
    else:
        digest = hashlib.sha256(f"{server_name}\0{tool_name}".encode()).hexdigest()[
            :_MCP_TOOL_NAME_HASH_LENGTH
        ]
        server = _MCP_TOOL_NAME_RE.sub("_", server_name).strip("_") or "unnamed"
        tool = _MCP_TOOL_NAME_RE.sub("_", tool_name).strip("_") or "unnamed"
        available = _MCP_TOOL_NAME_MAX_LENGTH - len(digest) - 2
        tool_length = min(len(tool), available // 2)
        server_length = min(len(server), available - tool_length)
        tool_length = min(len(tool), available - server_length)
        result = f"{server[:server_length]}_{tool[:tool_length]}_{digest}"

    try:
        from talkops.ui.widgets.tool_display import (
            extract_server_scope,
            register_scoped_tool_name,
        )

        scope = extract_server_scope(server_name)
        if scope:
            register_scoped_tool_name(result, tool_name, scope)
    except Exception:
        pass
    return result


def _resolve_transport(config: Mapping[str, Any]) -> str:
    """Determine the transport type from a server config dict."""
    transport = config.get("type") or config.get("transport")
    if transport:
        return str(transport)
    url = config.get("url") or config.get("serverUrl") or ""
    if url:
        if "sse" in str(url).lower():
            return "sse"
        return "http"
    return "stdio"


def _filter_tool_names(
    tool_name: str,
    *,
    allowed_tools: list[str] | tuple[str, ...],
    disabled_tools: list[str] | tuple[str, ...],
) -> bool:
    """Check whether a tool name is permitted by allowed/disabled filters."""
    # Check disabled patterns first
    if disabled_tools:
        for pat in disabled_tools:
            if pat and (pat == tool_name or fnmatch.fnmatch(tool_name, pat)):
                return False

    # If allowed list specified, tool must match at least one pattern
    if allowed_tools:
        return any(pat == tool_name or fnmatch.fnmatch(tool_name, pat) for pat in allowed_tools if pat)

    return True


def _clean_mcp_schema(schema: Any, max_ref_depth: int = 3) -> dict[str, Any]:
    """Clean JSON Schema for compatibility with LLM provider tool schemas (Gemini, Anthropic, OpenAI).

    Safely inlines local `$ref` pointers (such as `#/$defs/...` or `#/definitions/...`)
    up to a bounded ref depth, strips provider-incompatible metadata keys (`$schema`, `$id`,
    `additionalProperties`), and prevents recursive bloat or corrupted empty keys.
    """
    if not isinstance(schema, dict):
        return {}

    defs: dict[str, Any] = {}
    if isinstance(schema.get("$defs"), dict):
        defs.update(schema["$defs"])
    if isinstance(schema.get("definitions"), dict):
        defs.update(schema["definitions"])

    def _resolve(node: Any, ref_depth: int, seen_refs: set[str]) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                if (
                    ref_depth >= max_ref_depth
                    or not isinstance(ref, str)
                    or not ref.startswith("#/")
                    or ref in seen_refs
                ):
                    return {"type": "object"}

                parts = ref.lstrip("#/").split("/")
                target = None
                if len(parts) >= 2 and parts[0] in ("$defs", "definitions"):
                    target = defs.get(parts[1])
                elif len(parts) == 1 and parts[0] in defs:
                    target = defs.get(parts[0])

                if isinstance(target, dict):
                    merged = dict(target)
                    for k, v in node.items():
                        if k != "$ref":
                            merged[k] = v
                    return _resolve(merged, ref_depth + 1, seen_refs | {ref})
                return {"type": "object"}

            res = {}
            for k, v in node.items():
                if k in ("$schema", "$id", "additionalProperties", "$defs", "definitions"):
                    continue
                if k == "default" and v is None:
                    continue
                if k == "properties" and isinstance(v, dict):
                    res[k] = {
                        pk: _resolve(pv, ref_depth, seen_refs)
                        for pk, pv in v.items()
                    }
                else:
                    res[k] = _resolve(v, ref_depth, seen_refs)

            any_of = res.get("anyOf")
            if isinstance(any_of, list):
                non_null = [item for item in any_of if item != {"type": "null"}]
                if len(non_null) == 1 and isinstance(non_null[0], dict):
                    res.pop("anyOf", None)
                    for bk, bv in non_null[0].items():
                        res.setdefault(bk, bv)
                elif non_null and len(non_null) < len(any_of):
                    res["anyOf"] = non_null
            return res

        if isinstance(node, list):
            return [_resolve(item, ref_depth, seen_refs) for item in node]

        return node

    result = _resolve(schema, 0, set())
    return result if isinstance(result, dict) else {}


def _clean_stderr_diagnostic(stderr_text: str | None) -> str | None:
    """Extract a concise and meaningful diagnostic message from captured process stderr."""
    if not stderr_text:
        return None
    raw_lines = [line.strip() for line in stderr_text.strip().splitlines() if line.strip()]
    if not raw_lines:
        return None

    # Coalesce multiline uvx/pip resolution blocks starting with '╰─▶' or '->'
    lines: list[str] = []
    idx = 0
    while idx < len(raw_lines):
        cur = raw_lines[idx]
        if cur.startswith("╰─▶"):
            parts = [cur.lstrip("╰─▶ ").strip()]
            idx += 1
            while idx < len(raw_lines) and not raw_lines[idx].startswith(("×", "╰─▶", "Traceback", "File ", "help:")):
                parts.append(raw_lines[idx])
                idx += 1
            lines.append(" ".join(p for p in parts if p))
        else:
            lines.append(cur)
            idx += 1

    # Prefer lines with explicit error descriptions, AWS credentials/permissions, or exception details
    for line in reversed(lines):
        if (
            line.startswith("Traceback")
            or line.startswith("File ")
            or set(line) <= {"─", "│", "╭", "╮", "╰", "╯", " ", "█", "▀", "▄"}
        ):
            continue
        if any(
            keyword in line
            for keyword in (
                "ExpiredToken",
                "NoCredentialsError",
                "AccessDenied",
                "UnauthorizedOperation",
                "Unable to locate credentials",
                "CRD not found",
                "unsatisfiable",
                "not found",
                "Error:",
                "Exception:",
                "failed",
                "Error",
                "Exception",
            )
        ):
            if ": " in line and ("Error" in line.split(": ")[0] or "Exception" in line.split(": ")[0]):
                return line.split(": ", 1)[1].strip() or line
            return line
    for line in reversed(lines):
        if not (
            line.startswith("Traceback")
            or line.startswith("File ")
            or set(line) <= {"─", "│", "╭", "╮", "╰", "╯", " ", "█", "▀", "▄"}
        ):
            return line
    return lines[-1]



def _extract_root_mcp_error(
    exc: BaseException | None,
    close_exc: BaseException | None,
    stderr_output: str | None = None,
) -> tuple[MCPServerStatus, str]:
    """Extract root cause error message and status code ('error' or 'unauthenticated') from probe exceptions."""
    all_excs: list[BaseException] = []

    def _collect(e: BaseException | None) -> None:
        if e is None:
            return
        if hasattr(e, "exceptions"):
            for sub in getattr(e, "exceptions", ()):
                _collect(sub)
        if getattr(e, "__cause__", None):
            _collect(e.__cause__)
        if getattr(e, "__context__", None) and e.__context__ is not e:
            _collect(e.__context__)
        all_excs.append(e)

    _collect(exc)
    _collect(close_exc)

    for e in all_excs:
        msg = str(e)
        if "401" in msg or "unauthorized" in msg.lower() or "oauth" in msg.lower():
            return "unauthenticated", f"Authentication required: {msg}"
        if "403" in msg or "forbidden" in msg.lower():
            return "unauthenticated", f"Access forbidden: {msg}"
        if "404" in msg or "not found" in msg.lower():
            return "error", f"Endpoint not found: {msg}"
        if "connecterror" in type(e).__name__.lower() or "connection refused" in msg.lower():
            return "error", f"Connection refused: {msg}"
        if isinstance(e, asyncio.TimeoutError):
            return "error", f"Connection timed out after {_PROBE_TIMEOUT:.1f}s"

    stderr_diag = _clean_stderr_diagnostic(stderr_output)
    if stderr_diag:
        return "error", stderr_diag

    for e in all_excs:
        msg = str(e)
        if (
            msg
            and not msg.startswith("Cancelled via cancel scope")
            and not msg.startswith("unhandled errors in a TaskGroup")
        ):
            return "error", msg

    if isinstance(exc, (asyncio.CancelledError, asyncio.TimeoutError)):
        return "error", "Connection timed out or failed to initialize"

    return "error", str(exc or close_exc or "Unknown connection failure")


def _schema_state_write_enabled() -> bool:
    if os.environ.get("TALKOPS_MCP_SCHEMA_DIR_DISABLED") == "1":
        return False
    return not (
        os.environ.get("PYTEST_CURRENT_TEST")
        and not os.environ.get("TALKOPS_MCP_SCHEMA_DIR")
    )


def get_mcp_server_state_dir(server_name: str | None = None) -> Path:
    """Return the directory where per-server MCP tool schema JSON files are written.

    Writes schemas under ``~/.talkops/mcp/<server_name>/<tool_name>.json``.
    """
    override = os.environ.get("TALKOPS_MCP_SCHEMA_DIR")
    if override:
        base = Path(override).expanduser()
    else:
        from talkops.config.paths import DATA_DIR

        base = DATA_DIR / "mcp"
    if server_name:
        safe_server = re.sub(r"[/\\]+", "_", server_name.strip()) or "unnamed"
        return base / safe_server
    return base


def write_mcp_server_state(
    info: MCPServerInfo,
    *,
    state_dir: Path | None = None,
) -> Path | None:
    """Atomically materialize each tool's schema to ``<mcp_dir>/<server>/<tool>.json``.

    Writes one ``<toolName>.json`` per tool and removes stale ``.json`` files from
    previous runs.
    """
    if not info.enabled or info.status != "ok":
        return None
    if state_dir is None and not _schema_state_write_enabled():
        return None

    server_dir = (state_dir / re.sub(r"[/\\]+", "_", info.name.strip())) if state_dir else get_mcp_server_state_dir(info.name)
    try:
        server_dir.mkdir(parents=True, exist_ok=True)
        expected_files: set[str] = set()
        for tool in info.tools:
            raw_name = tool.original_name or (
                tool.name.split(":", 1)[1] if ":" in tool.name else tool.name
            )
            safe_tool = re.sub(r"[/\\]+", "_", str(raw_name).strip()) or "unnamed"
            filename = f"{safe_tool}.json"
            expected_files.add(filename)
            target = server_dir / filename
            schema_dict = (
                _clean_mcp_schema(dict(tool.input_schema))
                if isinstance(tool.input_schema, Mapping)
                else {}
            )
            payload = {
                "name": raw_name,
                "qualified_name": tool.name,
                "server_name": info.name,
                "description": tool.description,
                "inputSchema": schema_dict,
            }
            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=server_dir,
                    prefix=f".{safe_tool}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    json.dump(payload, handle, indent=2, sort_keys=True)
                    temp_path = Path(handle.name)
                os.replace(temp_path, target)
            except OSError:
                if temp_path is not None:
                    try:
                        temp_path.unlink(missing_ok=True)
                    except OSError:
                        pass

        # Prune stale .json schema files no longer exposed by this server
        for existing in server_dir.glob("*.json"):
            if existing.name not in expected_files:
                try:
                    existing.unlink(missing_ok=True)
                except OSError:
                    pass

        # Materialize or prune <server_dir>/instructions.md from InitializeResult.instructions
        instructions_path = server_dir / "instructions.md"
        instructions_text = str(getattr(info, "instructions", "") or "").strip()
        if instructions_text:
            temp_instr: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=server_dir,
                    prefix=".instructions.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    handle.write(instructions_text + "\n")
                    temp_instr = Path(handle.name)
                os.replace(temp_instr, instructions_path)
            except OSError:
                if temp_instr is not None:
                    try:
                        temp_instr.unlink(missing_ok=True)
                    except OSError:
                        pass
        else:
            try:
                instructions_path.unlink(missing_ok=True)
            except OSError:
                pass
        return server_dir
    except OSError as exc:
        logger.debug("Could not write MCP server state for %s: %s", info.name, exc)
        return None


def read_mcp_tool_schema(
    server_name: str,
    tool_name: str,
    *,
    state_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Read a materialized MCP tool schema from disk if available."""
    raw_tool = str(tool_name).strip()
    if ":" in raw_tool:
        raw_tool = raw_tool.split(":", 1)[1]
    safe_tool = re.sub(r"[/\\]+", "_", raw_tool) or "unnamed"
    candidates = [server_name]
    if "__" in server_name:
        base_srv = server_name.rsplit("__", 1)[-1]
        if base_srv and base_srv not in candidates:
            candidates.append(base_srv)

    for srv in candidates:
        srv_dir = (
            state_dir / re.sub(r"[/\\]+", "_", srv.strip())
            if state_dir is not None
            else get_mcp_server_state_dir(srv)
        )
        tool_candidates = [safe_tool]
        base_srv = srv.rsplit("__", 1)[-1] if "__" in srv else srv
        for prefix in (f"{srv}_", f"{base_srv}_"):
            if prefix and safe_tool.startswith(prefix) and len(safe_tool) > len(prefix):
                stripped = safe_tool[len(prefix) :]
                if stripped not in tool_candidates:
                    tool_candidates.append(stripped)
        if base_srv and not safe_tool.startswith(f"{base_srv}_"):
            prefixed = f"{base_srv}_{safe_tool}"
            if prefixed not in tool_candidates:
                tool_candidates.append(prefixed)
        for t_cand in tool_candidates:
            schema_file = srv_dir / f"{t_cand}.json"
            if schema_file.is_file():
                try:
                    data = json.loads(schema_file.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        return data
                except (OSError, ValueError, TypeError):
                    continue
    return None


def _extract_init_metadata(init_result: Any = None, session: Any = None) -> tuple[bool, str]:
    """Extract ``(supports_resources, sanitized_instructions)`` from MCP initialization."""
    supports_resources = False
    if init_result is not None:
        caps = getattr(init_result, "capabilities", None)
        if caps is not None and getattr(caps, "resources", None) is not None:
            supports_resources = True
    if not supports_resources and session is not None:
        get_caps = getattr(session, "get_server_capabilities", None)
        if callable(get_caps):
            try:
                caps = get_caps()
                if caps is not None and getattr(caps, "resources", None) is not None:
                    supports_resources = True
            except Exception:
                pass

    raw_instructions = str(getattr(init_result, "instructions", "") or "").strip() if init_result is not None else ""
    if not raw_instructions:
        return supports_resources, ""
    from talkops.mcp.lockfile import sanitize_mcp_descriptor

    cleaned_instructions, _ = sanitize_mcp_descriptor(raw_instructions)
    return supports_resources, cleaned_instructions


def build_mcp_server_info_from_raw_tools(
    name: str,
    srv_config: Mapping[str, Any],
    raw_tools: Any,
    *,
    supports_resources: bool = False,
    instructions: str = "",
) -> MCPServerInfo:
    """Construct an ``MCPServerInfo`` from raw MCP ``list_tools()`` items."""
    transport = _resolve_transport(srv_config)
    source = str(srv_config.get("source", "project"))
    raw_disabled_tools = srv_config.get("disabled_tools") or srv_config.get("disabledTools") or ()
    raw_allowed_tools = srv_config.get("allowed_tools") or srv_config.get("allowedTools") or ()
    disabled_tools_tuple = tuple(raw_disabled_tools) if isinstance(raw_disabled_tools, (list, tuple)) else ()
    allowed_tools_tuple = tuple(raw_allowed_tools) if isinstance(raw_allowed_tools, (list, tuple)) else ()

    tools: list[MCPToolInfo] = []
    for t in raw_tools or ():
        raw_name = getattr(t, "name", str(t))
        if not _filter_tool_names(
            raw_name,
            allowed_tools=allowed_tools_tuple,
            disabled_tools=disabled_tools_tuple,
        ):
            continue

        clean_schema = _clean_mcp_schema(getattr(t, "inputSchema", None))
        tool_name = _mcp_tool_name(name, raw_name)
        tools.append(
            MCPToolInfo(
                name=tool_name,
                description=getattr(t, "description", "") or "",
                input_schema=clean_schema,
                original_name=raw_name,
            )
        )

    cleaned_instructions = ""
    if instructions:
        from talkops.mcp.lockfile import sanitize_mcp_descriptor

        cleaned_instructions, _ = sanitize_mcp_descriptor(str(instructions))

    return MCPServerInfo(
        name=name,
        transport=transport,
        status="ok",
        tools=tuple(tools),
        enabled=True,
        url=srv_config.get("url") or srv_config.get("serverUrl"),
        command=srv_config.get("command"),
        args=tuple(srv_config.get("args") or ()),
        headers=dict(srv_config.get("headers") or {}),
        env=dict(srv_config.get("env") or {}),
        disabled_tools=disabled_tools_tuple,
        allowed_tools=allowed_tools_tuple,
        source=source,
        supports_resources=bool(supports_resources),
        instructions=cleaned_instructions,
    )


def _project_server_info(
    source_info: MCPServerInfo,
    name: str,
    srv_config: Mapping[str, Any],
) -> MCPServerInfo:
    """Project an already-probed ``MCPServerInfo`` onto another scoped server entry.

    Used when multiple scoped entries share the same underlying MCP connection fingerprint.
    """
    if source_info.name == name:
        return source_info
    raw_disabled_tools = srv_config.get("disabled_tools") or srv_config.get("disabledTools") or ()
    raw_allowed_tools = srv_config.get("allowed_tools") or srv_config.get("allowedTools") or ()
    disabled_tools_tuple = tuple(raw_disabled_tools) if isinstance(raw_disabled_tools, (list, tuple)) else ()
    allowed_tools_tuple = tuple(raw_allowed_tools) if isinstance(raw_allowed_tools, (list, tuple)) else ()

    projected_tools: list[MCPToolInfo] = []
    for t in source_info.tools:
        raw_name = t.original_name or (t.name.split(":", 1)[1] if ":" in t.name else t.name)
        if not _filter_tool_names(
            raw_name,
            allowed_tools=allowed_tools_tuple,
            disabled_tools=disabled_tools_tuple,
        ):
            continue
        projected_tools.append(
            MCPToolInfo(
                name=_mcp_tool_name(name, raw_name),
                description=t.description,
                input_schema=dict(t.input_schema) if isinstance(t.input_schema, Mapping) else {},
                original_name=raw_name,
            )
        )

    return MCPServerInfo(
        name=name,
        transport=_resolve_transport(srv_config),
        status=source_info.status,
        error=source_info.error,
        tools=tuple(projected_tools),
        enabled=bool(srv_config.get("enabled", True)),
        url=srv_config.get("url") or srv_config.get("serverUrl"),
        command=srv_config.get("command"),
        args=tuple(srv_config.get("args") or ()),
        headers=dict(srv_config.get("headers") or {}),
        env=dict(srv_config.get("env") or {}),
        disabled_tools=disabled_tools_tuple,
        allowed_tools=allowed_tools_tuple,
        source=str(srv_config.get("source", source_info.source or "project")),
        supports_resources=source_info.supports_resources,
        instructions=source_info.instructions,
    )


async def probe_one_mcp_server(
    name: str,
    srv_config: Mapping[str, Any],
) -> MCPServerInfo:
    """Open a throwaway session to one MCP server and list its tools."""
    transport = _resolve_transport(srv_config)
    enabled = bool(srv_config.get("enabled", True))
    source = srv_config.get("source", "project")
    raw_disabled_tools = srv_config.get("disabled_tools") or srv_config.get("disabledTools") or ()
    raw_allowed_tools = srv_config.get("allowed_tools") or srv_config.get("allowedTools") or ()

    disabled_tools_tuple = tuple(raw_disabled_tools) if isinstance(raw_disabled_tools, (list, tuple)) else ()
    allowed_tools_tuple = tuple(raw_allowed_tools) if isinstance(raw_allowed_tools, (list, tuple)) else ()

    if not enabled:
        return MCPServerInfo(
            name=name,
            transport=transport,
            status="disabled",
            tools=(),
            enabled=False,
            url=srv_config.get("url") or srv_config.get("serverUrl"),
            command=srv_config.get("command"),
            args=tuple(srv_config.get("args") or ()),
            headers=dict(srv_config.get("headers") or {}),
            env=dict(srv_config.get("env") or {}),
            disabled_tools=disabled_tools_tuple,
            allowed_tools=allowed_tools_tuple,
            source=source,
        )

    # Resolve environment variables
    try:
        resolved_config = resolve_mcp_server_env(name, srv_config)
    except Exception as exc:
        logger.warning("MCP server '%s' env resolution failed: %s", name, exc)
        return MCPServerInfo(
            name=name,
            transport=transport,
            status="error",
            error=f"Environment variable error: {exc}",
            tools=(),
            enabled=enabled,
            url=srv_config.get("url") or srv_config.get("serverUrl"),
            command=srv_config.get("command"),
            args=tuple(srv_config.get("args") or ()),
            headers=dict(srv_config.get("headers") or {}),
            env=dict(srv_config.get("env") or {}),
            disabled_tools=disabled_tools_tuple,
            allowed_tools=allowed_tools_tuple,
            source=source,
        )

    # Reuse an already-connected live session in MCPSessionManager if present,
    # avoiding a redundant second subprocess spawn.
    try:
        from talkops.mcp.session_manager import MCPSessionManager

        active_mgr = MCPSessionManager._instance
        if active_mgr is not None and name in active_mgr._sessions:
            entry = active_mgr._sessions.get(name)
            if entry is not None and getattr(entry, "session", None) is not None:
                async with asyncio.timeout(_PROBE_TIMEOUT):
                    tools_result = await entry.session.list_tools()
                raw_tools = getattr(tools_result, "tools", []) or []
                supports_resources, instructions = _extract_init_metadata(
                    getattr(entry, "init_result", None),
                    entry.session,
                )
                info = build_mcp_server_info_from_raw_tools(
                    name,
                    srv_config,
                    raw_tools,
                    supports_resources=supports_resources,
                    instructions=instructions,
                )
                write_mcp_server_state(info)
                return info
    except Exception as exc:
        logger.debug("Live session reuse in probe_one_mcp_server skipped for %s: %s", name, exc)

    exit_stack = AsyncExitStack()
    primary_exc: BaseException | None = None
    close_exc: BaseException | None = None
    captured_stderr: str | None = None
    try:
        from langchain_mcp_adapters.sessions import create_session

        from talkops.mcp.session_manager import create_mcp_connection

        if transport == "stdio":
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client

            sync_mcp_env_aliases()
            stdio_env = dict(os.environ)
            raw_env = resolved_config.get("env")
            if isinstance(raw_env, dict):
                stdio_env.update(raw_env)
            raw_args = resolved_config.get("args")
            args_list = list(raw_args) if isinstance(raw_args, (list, tuple)) else []
            server_params = StdioServerParameters(
                command=str(resolved_config.get("command") or ""),
                args=args_list,
                env=stdio_env,
                cwd=resolved_config.get("cwd"),
            )
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
                try:
                    async with asyncio.timeout(_PROBE_TIMEOUT):
                        read_stream, write_stream = await exit_stack.enter_async_context(
                            stdio_client(server_params, errlog=stderr_file)
                        )
                        session = await exit_stack.enter_async_context(
                            ClientSession(read_stream, write_stream)
                        )
                        init_result = await session.initialize()
                        tools_result = await session.list_tools()
                except BaseException as exc:
                    primary_exc = exc
                    try:
                        stderr_file.seek(0)
                        captured_stderr = stderr_file.read()
                    except Exception:
                        pass
                    raise
        else:
            conn = create_mcp_connection(resolved_config)
            async with asyncio.timeout(_PROBE_TIMEOUT):
                session = await exit_stack.enter_async_context(create_session(conn))
                init_result = await session.initialize()
                tools_result = await session.list_tools()

        raw_tools = getattr(tools_result, "tools", []) or []
        supports_resources, instructions = _extract_init_metadata(init_result, session)
        info = build_mcp_server_info_from_raw_tools(
            name,
            srv_config,
            raw_tools,
            supports_resources=supports_resources,
            instructions=instructions,
        )
        write_mcp_server_state(info)
        return info
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        primary_exc = exc
    finally:
        try:
            async with asyncio.timeout(3.0):
                await exit_stack.aclose()
        except BaseException as ce:
            close_exc = ce

    curr_task = asyncio.current_task()
    if curr_task is not None and curr_task.cancelling() > 0:
        if isinstance(primary_exc, asyncio.CancelledError):
            raise primary_exc
        raise asyncio.CancelledError() from primary_exc

    status, err_msg = _extract_root_mcp_error(primary_exc, close_exc, captured_stderr)
    logger.warning("MCP server '%s' probe failed (%s): %s", name, status, err_msg)
    return MCPServerInfo(
        name=name,
        transport=transport,
        status=status,
        error=err_msg,
        tools=(),
        enabled=enabled,
        url=srv_config.get("url") or srv_config.get("serverUrl"),
        command=srv_config.get("command"),
        args=tuple(srv_config.get("args") or ()),
        headers=dict(srv_config.get("headers") or {}),
        env=dict(srv_config.get("env") or {}),
        disabled_tools=disabled_tools_tuple,
        allowed_tools=allowed_tools_tuple,
        source=source,
    )


_PROBED_CACHE: dict[str, MCPServerInfo] = {}
_PROBED_CONFIG_FINGERPRINTS: dict[str, str] = {}
_PROBED_CONN_FINGERPRINTS: dict[str, tuple[str, str]] = {}
_PERSISTED_ENTRIES: dict[str, dict[str, Any]] = {}
_EVICTED_PERSISTED_NAMES: set[str] = set()
_INFLIGHT_PROBES: dict[tuple[int, str], asyncio.Future[MCPServerInfo]] = {}
_PERSISTENT_CACHE_LOADED = False
_PERSISTENT_CACHE_SUPPRESSED = False
_PERSISTENT_CACHE_MTIME_NS: int | None = None
_PERSISTENT_CACHE_PATH: str | None = None
_CACHE_LOCK = threading.RLock()
_PERSISTENT_CACHE_VERSION = 2
_DEFAULT_METADATA_CACHE_TTL = 24 * 60 * 60
_DEFAULT_ERROR_CACHE_TTL = 10 * 60


def _persistent_cache_enabled() -> bool:
    if os.environ.get("TALKOPS_MCP_METADATA_CACHE_DISABLED") == "1":
        return False
    return not (
        os.environ.get("PYTEST_CURRENT_TEST")
        and not os.environ.get("TALKOPS_MCP_METADATA_CACHE")
    )


def _metadata_cache_path() -> Path:
    override = os.environ.get("TALKOPS_MCP_METADATA_CACHE")
    if override:
        return Path(override).expanduser()
    from talkops.config.paths import STATE_DIR

    return STATE_DIR / "mcp_metadata_cache.json"


def _persistent_file_lock() -> Any:
    """Return an inter-process lock for cache read/merge/write operations."""
    if FileLock is None:
        return nullcontext()
    return FileLock(f"{_metadata_cache_path()}.lock", timeout=0.5)


def _ensure_persistent_cache_loaded() -> None:
    """Load or refresh the schema cache from disk without loading credentials.

    Tracks the metadata cache file's ``st_mtime_ns`` so background MCP probes in a
    peer process (e.g. TUI preloading while LangGraph server started with
    ``defer_mcp_preload=True``) are automatically hydrated on subsequent checks.
    """
    global _PERSISTENT_CACHE_LOADED, _PERSISTENT_CACHE_SUPPRESSED
    global _PERSISTENT_CACHE_MTIME_NS, _PERSISTENT_CACHE_PATH
    if not _persistent_cache_enabled():
        return
    if not _PERSISTENT_CACHE_LOADED:
        _PERSISTENT_CACHE_SUPPRESSED = False
    if _PERSISTENT_CACHE_LOADED and _PERSISTENT_CACHE_SUPPRESSED:
        return

    path = _metadata_cache_path()
    path_str = str(path)
    try:
        stat_mtime_ns: int | None = path.stat().st_mtime_ns
    except OSError:
        stat_mtime_ns = None

    if (
        _PERSISTENT_CACHE_LOADED
        and _PERSISTENT_CACHE_PATH == path_str
        and _PERSISTENT_CACHE_MTIME_NS == stat_mtime_ns
    ):
        return

    with _CACHE_LOCK:
        if not _PERSISTENT_CACHE_LOADED:
            _PERSISTENT_CACHE_SUPPRESSED = False
        if _PERSISTENT_CACHE_LOADED and _PERSISTENT_CACHE_SUPPRESSED:
            return
        try:
            stat_mtime_ns = path.stat().st_mtime_ns
        except OSError:
            stat_mtime_ns = None
        if (
            _PERSISTENT_CACHE_LOADED
            and _PERSISTENT_CACHE_PATH == path_str
            and _PERSISTENT_CACHE_MTIME_NS == stat_mtime_ns
        ):
            return

        _PERSISTENT_CACHE_LOADED = True
        _PERSISTENT_CACHE_PATH = path_str
        _PERSISTENT_CACHE_MTIME_NS = stat_mtime_ns
        if stat_mtime_ns is None:
            return
        try:
            with _persistent_file_lock():
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("version") != _PERSISTENT_CACHE_VERSION:
                    return
                entries = payload.get("entries")
                if isinstance(entries, dict):
                    for raw_name, entry in entries.items():
                        if not isinstance(entry, dict):
                            continue
                        key = str(raw_name)
                        if key in _EVICTED_PERSISTED_NAMES:
                            continue
                        existing = _PERSISTED_ENTRIES.get(key)
                        if existing is not None:
                            try:
                                if float(existing.get("cached_at", 0.0)) > float(entry.get("cached_at", 0.0)):
                                    continue
                            except (TypeError, ValueError):
                                pass
                        _PERSISTED_ENTRIES[key] = entry
        except (OSError, ValueError, TypeError, TimeoutError):
            return


def _server_info_from_persisted(
    name: str,
    config: Mapping[str, Any],
    entry: Mapping[str, Any],
    *,
    entry_name: str | None = None,
) -> MCPServerInfo | None:
    info = entry.get("info")
    if not isinstance(info, Mapping):
        return None
    status_val = info.get("status")
    if status_val not in ("ok", "error", "unauthenticated", "disabled"):
        return None
    status: MCPServerStatus = status_val
    error_val = info.get("error")
    error_msg = str(error_val) if error_val else None

    raw_disabled = config.get("disabled_tools") or config.get("disabledTools") or ()
    raw_allowed = config.get("allowed_tools") or config.get("allowedTools") or ()
    disabled_tuple = tuple(raw_disabled) if isinstance(raw_disabled, (list, tuple)) else ()
    allowed_tuple = tuple(raw_allowed) if isinstance(raw_allowed, (list, tuple)) else ()

    raw_tools = info.get("tools")
    tools: list[MCPToolInfo] = []
    if status == "ok" and isinstance(raw_tools, list):
        for raw_tool in raw_tools:
            if not isinstance(raw_tool, Mapping) or not raw_tool.get("name"):
                continue
            orig_name = (
                str(raw_tool["original_name"])
                if raw_tool.get("original_name") is not None
                else None
            )
            raw_name = orig_name or (
                str(raw_tool["name"]).split(":", 1)[1]
                if ":" in str(raw_tool["name"])
                else str(raw_tool["name"])
            )
            if not _filter_tool_names(
                raw_name,
                allowed_tools=allowed_tuple,
                disabled_tools=disabled_tuple,
            ):
                continue
            tool_name = (
                str(raw_tool["name"])
                if (entry_name is None or entry_name == name)
                else _mcp_tool_name(name, raw_name)
            )
            schema = raw_tool.get("input_schema")
            tools.append(
                MCPToolInfo(
                    name=tool_name,
                    description=str(raw_tool.get("description") or ""),
                    input_schema=_clean_mcp_schema(dict(schema)) if isinstance(schema, Mapping) else {},
                    original_name=orig_name or raw_name,
                )
            )

    supports_resources = bool(info.get("supports_resources", False)) if status == "ok" else False
    raw_instructions = str(info.get("instructions") or "") if status == "ok" else ""
    if raw_instructions:
        from talkops.mcp.lockfile import sanitize_mcp_descriptor

        raw_instructions, _ = sanitize_mcp_descriptor(raw_instructions)

    return MCPServerInfo(
        name=name,
        transport=_resolve_transport(config),
        status=status,
        error=error_msg,
        tools=tuple(tools),
        enabled=bool(config.get("enabled", True)),
        source=str(config.get("source", "project")),
        url=config.get("url") or config.get("serverUrl"),
        command=config.get("command"),
        args=tuple(config.get("args") or ()),
        headers=dict(config.get("headers") or {}),
        env=dict(config.get("env") or {}),
        disabled_tools=disabled_tuple,
        allowed_tools=allowed_tuple,
        supports_resources=supports_resources,
        instructions=raw_instructions,
    )


def _record_persisted_entry(
    info: MCPServerInfo,
    fingerprint: str,
    connection_fingerprint: str | None = None,
) -> None:
    if info.status not in ("ok", "error", "unauthenticated"):
        return
    entry_info: dict[str, Any] = {
        "status": info.status,
        "tools": [tool.to_dict() for tool in info.tools] if info.status == "ok" else [],
    }
    if info.status == "ok":
        entry_info["supports_resources"] = bool(info.supports_resources)
        if info.instructions:
            entry_info["instructions"] = info.instructions
    if info.error:
        entry_info["error"] = info.error
    record: dict[str, Any] = {
        "fingerprint": fingerprint,
        "cached_at": time.time(),
        "info": entry_info,
    }
    if connection_fingerprint:
        record["connection_fingerprint"] = connection_fingerprint
    _EVICTED_PERSISTED_NAMES.discard(info.name)
    _PERSISTED_ENTRIES[info.name] = record


def _write_persistent_cache() -> None:
    """Atomically persist only fingerprints and non-secret tool schemas."""
    if not _persistent_cache_enabled():
        return
    with _CACHE_LOCK:
        path = _metadata_cache_path()
        temp_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _persistent_file_lock():
                disk_entries: dict[str, Any] = {}
                try:
                    disk_payload = json.loads(path.read_text(encoding="utf-8"))
                    raw_entries = disk_payload.get("entries")
                    if isinstance(raw_entries, dict):
                        disk_entries = {
                            str(name): entry
                            for name, entry in raw_entries.items()
                            if isinstance(entry, dict)
                        }
                except (OSError, ValueError, TypeError):
                    pass
                for evicted_name in _EVICTED_PERSISTED_NAMES:
                    disk_entries.pop(evicted_name, None)
                # Entries generated by this process win over stale disk data,
                # while unrelated entries from another process are preserved.
                disk_entries.update(_PERSISTED_ENTRIES)
                payload = {
                    "version": _PERSISTENT_CACHE_VERSION,
                    "entries": disk_entries,
                }
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                    temp_path = Path(handle.name)
                os.replace(temp_path, path)
                _EVICTED_PERSISTED_NAMES.clear()
                global _PERSISTENT_CACHE_MTIME_NS, _PERSISTENT_CACHE_PATH
                _PERSISTENT_CACHE_PATH = str(path)
                try:
                    _PERSISTENT_CACHE_MTIME_NS = path.stat().st_mtime_ns
                except OSError:
                    pass
        except (OSError, TimeoutError) as exc:
            logger.debug("Could not persist MCP metadata cache: %s", exc)
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass


def get_cached_mcp_server_infos() -> list[MCPServerInfo]:
    """Return all cached/probed MCPServerInfo objects."""
    return list(_PROBED_CACHE.values())


def _is_persisted_entry_fresh(entry: Mapping[str, Any]) -> bool:
    info_block = entry.get("info")
    status = info_block.get("status") if isinstance(info_block, Mapping) else "ok"
    try:
        if status in ("error", "unauthenticated"):
            ttl = float(
                os.environ.get(
                    "TALKOPS_MCP_ERROR_CACHE_TTL",
                    str(_DEFAULT_ERROR_CACHE_TTL),
                )
            )
        else:
            ttl = float(
                os.environ.get(
                    "TALKOPS_MCP_METADATA_CACHE_TTL",
                    str(_DEFAULT_METADATA_CACHE_TTL),
                )
            )
        cached_at = float(entry.get("cached_at", 0.0))
    except (TypeError, ValueError):
        return False
    if ttl >= 0 and time.time() - cached_at > ttl:
        return False
    return True


def is_mcp_server_info_cached(
    name: str,
    config: Mapping[str, Any],
) -> bool:
    """Return whether memory or persistent metadata matches this config."""
    fingerprint = _config_fingerprint(config)
    if (
        name in _PROBED_CACHE
        and _PROBED_CONFIG_FINGERPRINTS.get(name) == fingerprint
    ):
        return True

    conn_fp = _connection_fingerprint(config)
    # Check in-memory connection fingerprint cache (valid only if source server is still in _PROBED_CACHE)
    conn_ref = _PROBED_CONN_FINGERPRINTS.get(conn_fp)
    if conn_ref is not None:
        source_name, source_fp = conn_ref
        source_info = _PROBED_CACHE.get(source_name)
        if source_info is not None and _PROBED_CONFIG_FINGERPRINTS.get(source_name) == source_fp:
            projected = _project_server_info(source_info, name, config)
            _PROBED_CACHE[name] = projected
            _PROBED_CONFIG_FINGERPRINTS[name] = fingerprint
            write_mcp_server_state(projected)
            return True

    _ensure_persistent_cache_loaded()
    entry = _PERSISTED_ENTRIES.get(name)
    matched_entry_name: str | None = name
    if not isinstance(entry, Mapping) or entry.get("fingerprint") != fingerprint:
        entry = None
        matched_entry_name = None
        # Search persisted entries by canonical connection fingerprint
        for cand_name, cand_entry in _PERSISTED_ENTRIES.items():
            if (
                isinstance(cand_entry, Mapping)
                and cand_entry.get("connection_fingerprint") == conn_fp
                and _is_persisted_entry_fresh(cand_entry)
            ):
                entry = cand_entry
                matched_entry_name = cand_name
                break

    if not isinstance(entry, Mapping) or not _is_persisted_entry_fresh(entry):
        return False

    info = _server_info_from_persisted(name, config, entry, entry_name=matched_entry_name)
    if info is None:
        return False
    _PROBED_CACHE[name] = info
    _PROBED_CONFIG_FINGERPRINTS[name] = fingerprint
    _PROBED_CONN_FINGERPRINTS[conn_fp] = (name, fingerprint)
    write_mcp_server_state(info)
    return True


def set_cached_mcp_server_info(
    info: MCPServerInfo,
    config: Mapping[str, Any] | None = None,
) -> None:
    """Store or update a probed server and its configuration identity."""
    _PROBED_CACHE[info.name] = info
    write_mcp_server_state(info)
    if config is not None:
        fingerprint = _config_fingerprint(config)
        conn_fp = _connection_fingerprint(config)
        _PROBED_CONFIG_FINGERPRINTS[info.name] = fingerprint
        _PROBED_CONN_FINGERPRINTS[conn_fp] = (info.name, fingerprint)
        with _CACHE_LOCK:
            _record_persisted_entry(info, fingerprint, conn_fp)


def evict_cached_mcp_server_info(name: str) -> None:
    """Evict a cached MCPServerInfo so it can be probed fresh."""
    with _CACHE_LOCK:
        _PROBED_CACHE.pop(name, None)
        _PROBED_CONFIG_FINGERPRINTS.pop(name, None)
        _PERSISTED_ENTRIES.pop(name, None)
        _EVICTED_PERSISTED_NAMES.add(name)
        stale_conn_keys = [
            k for k, (srv_name, _) in _PROBED_CONN_FINGERPRINTS.items() if srv_name == name
        ]
        for k in stale_conn_keys:
            _PROBED_CONN_FINGERPRINTS.pop(k, None)


def evict_failed_mcp_server_infos() -> list[str]:
    """Evict non-'ok' cached/persisted entries so explicit `/reload` or `/restart` re-probes them."""
    _ensure_persistent_cache_loaded()
    evicted: set[str] = set()
    persisted_evicted = False
    with _CACHE_LOCK:
        for cached_name, info in list(_PROBED_CACHE.items()):
            if getattr(info, "status", "ok") != "ok":
                if cached_name in _PERSISTED_ENTRIES:
                    persisted_evicted = True
                evict_cached_mcp_server_info(cached_name)
                evicted.add(cached_name)
        for persisted_name, entry in list(_PERSISTED_ENTRIES.items()):
            info_block = entry.get("info") if isinstance(entry, Mapping) else None
            status = info_block.get("status") if isinstance(info_block, Mapping) else "ok"
            if status != "ok":
                evict_cached_mcp_server_info(persisted_name)
                persisted_evicted = True
                evicted.add(persisted_name)
    if persisted_evicted:
        _write_persistent_cache()
    return sorted(evicted)


def prune_stale_mcp_server_state(
    active_names: Collection[str],
    *,
    prune_subagents: bool = True,
    state_dir: Path | None = None,
) -> list[str]:
    """Evict cached/persisted metadata and schema dirs for removed MCP servers."""
    _ensure_persistent_cache_loaded()
    active_set = {str(n) for n in active_names}
    pruned: set[str] = set()
    persisted_evicted = False

    with _CACHE_LOCK:
        for cached_name in list(_PROBED_CACHE.keys()):
            if not prune_subagents and cached_name.startswith("subagent__"):
                continue
            if cached_name not in active_set:
                if cached_name in _PERSISTED_ENTRIES:
                    persisted_evicted = True
                evict_cached_mcp_server_info(cached_name)
                pruned.add(cached_name)

        scoped_prefixes: tuple[str, ...] = (
            ("plugin__", "subagent__") if prune_subagents else ("plugin__",)
        )
        for persisted_name in list(_PERSISTED_ENTRIES.keys()):
            if persisted_name in active_set:
                continue
            if persisted_name.startswith(scoped_prefixes):
                evict_cached_mcp_server_info(persisted_name)
                persisted_evicted = True
                pruned.add(persisted_name)

    if persisted_evicted:
        _write_persistent_cache()

    if state_dir is not None or _schema_state_write_enabled():
        base_dir = state_dir if state_dir is not None else get_mcp_server_state_dir()
        active_safe_dirs = {
            re.sub(r"[/\\]+", "_", n.strip()) or "unnamed"
            for n in active_set
            if n.strip()
        }
        for stale_name in list(pruned):
            safe_stale = re.sub(r"[/\\]+", "_", stale_name.strip()) or "unnamed"
            if safe_stale not in active_safe_dirs:
                stale_dir = base_dir / safe_stale
                if stale_dir.is_dir():
                    shutil.rmtree(stale_dir, ignore_errors=True)

        scoped_dir_prefixes: tuple[str, ...] = (
            ("plugin__", "subagent__") if prune_subagents else ("plugin__",)
        )
        if base_dir.is_dir():
            try:
                for entry in base_dir.iterdir():
                    if (
                        entry.is_dir()
                        and entry.name.startswith(scoped_dir_prefixes)
                        and entry.name not in active_safe_dirs
                    ):
                        shutil.rmtree(entry, ignore_errors=True)
                        pruned.add(entry.name)
            except OSError:
                pass

    return sorted(pruned)


def clear_cached_mcp_server_infos() -> None:
    """Clear process metadata and prevent stale disk rehydration this run."""
    global _PERSISTENT_CACHE_LOADED, _PERSISTENT_CACHE_SUPPRESSED
    global _PERSISTENT_CACHE_MTIME_NS, _PERSISTENT_CACHE_PATH
    with _CACHE_LOCK:
        _PROBED_CACHE.clear()
        _PROBED_CONFIG_FINGERPRINTS.clear()
        _PROBED_CONN_FINGERPRINTS.clear()
        _PERSISTED_ENTRIES.clear()
        _EVICTED_PERSISTED_NAMES.clear()
        _PERSISTENT_CACHE_LOADED = True
        _PERSISTENT_CACHE_SUPPRESSED = True
        _PERSISTENT_CACHE_MTIME_NS = None
        _PERSISTENT_CACHE_PATH = None


async def wait_for_inflight_mcp_probes(
    configs: Mapping[str, Mapping[str, Any]],
) -> None:
    """Await any currently in-flight MCP probes matching ``configs`` on this event loop."""
    if not configs:
        return
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    pending: list[tuple[str, Mapping[str, Any], asyncio.Future[MCPServerInfo]]] = []
    for name, conf in configs.items():
        if not isinstance(conf, Mapping) or is_mcp_server_info_cached(name, conf):
            continue
        conn_fp = _connection_fingerprint(conf)
        fut = _INFLIGHT_PROBES.get((loop_id, conn_fp))
        if fut is not None and not fut.done():
            pending.append((name, conf, fut))
    for name, conf, fut in pending:
        try:
            source_info = await asyncio.shield(fut)
            if not is_mcp_server_info_cached(name, conf):
                projected = _project_server_info(source_info, name, conf)
                set_cached_mcp_server_info(projected, conf)
        except asyncio.CancelledError:
            curr = asyncio.current_task()
            if curr is not None and curr.cancelling() > 0:
                raise
        except Exception:
            pass


async def preload_mcp_metadata(
    configs: Mapping[str, Mapping[str, Any]],
) -> list[MCPServerInfo]:
    """Probe all configured MCP servers concurrently and return their MCPServerInfo.

    Identical underlying MCP connections (shared across multiple plugin scopes or
    subagents) are deduplicated both within a batch and across concurrent callers by
    canonical connection fingerprint so each unique server is spawned at most once.
    """
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PROBES)
    progress_cb = _PROGRESS_CALLBACK.get()
    loop = asyncio.get_running_loop()
    loop_id = id(loop)

    uncached_groups: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for name, conf in configs.items():
        if not is_mcp_server_info_cached(name, conf):
            conn_fp = _connection_fingerprint(conf)
            uncached_groups.setdefault(conn_fp, []).append((name, conf))

    async def _bound_probe_group(
        conn_fp: str,
        group: list[tuple[str, Mapping[str, Any]]],
    ) -> list[MCPServerInfo]:
        primary_name, primary_conf = group[0]
        inflight_key = (loop_id, conn_fp)

        while True:
            if is_mcp_server_info_cached(primary_name, primary_conf):
                primary_info = _PROBED_CACHE[primary_name]
                break
            existing_fut = _INFLIGHT_PROBES.get(inflight_key)
            if existing_fut is not None and not existing_fut.done():
                try:
                    source_info = await asyncio.shield(existing_fut)
                    primary_info = _project_server_info(source_info, primary_name, primary_conf)
                    set_cached_mcp_server_info(primary_info, primary_conf)
                    break
                except asyncio.CancelledError:
                    curr = asyncio.current_task()
                    if curr is not None and curr.cancelling() > 0:
                        raise
                    continue

            fut: asyncio.Future[MCPServerInfo] = loop.create_future()
            _INFLIGHT_PROBES[inflight_key] = fut
            try:
                async with semaphore:
                    if is_mcp_server_info_cached(primary_name, primary_conf):
                        primary_info = _PROBED_CACHE[primary_name]
                    else:
                        primary_info = await probe_one_mcp_server(primary_name, primary_conf)
                        set_cached_mcp_server_info(primary_info, primary_conf)
                if not fut.done():
                    fut.set_result(primary_info)
                break
            except BaseException as exc:
                if not fut.done():
                    if isinstance(exc, asyncio.CancelledError):
                        fut.cancel()
                    else:
                        fut.set_exception(exc)
                    fut.add_done_callback(
                        lambda f: f.exception() if not f.cancelled() else None
                    )
                raise
            finally:
                if _INFLIGHT_PROBES.get(inflight_key) is fut:
                    _INFLIGHT_PROBES.pop(inflight_key, None)

        results = [primary_info]
        for alias_name, alias_conf in group[1:]:
            projected = _project_server_info(primary_info, alias_name, alias_conf)
            set_cached_mcp_server_info(projected, alias_conf)
            results.append(projected)
        if progress_cb is not None:
            try:
                progress_cb(list(_PROBED_CACHE.values()))
            except Exception:
                pass
        return results

    tasks = [
        _bound_probe_group(conn_fp, group)
        for conn_fp, group in uncached_groups.items()
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=False)
        _write_persistent_cache()
    return [
        _PROBED_CACHE[name]
        for name in configs
        if name in _PROBED_CACHE
    ]


def format_mcp_status_response(server_infos: list[MCPServerInfo]) -> dict[str, Any]:
    """Format probed server infos into the API status payload."""
    total_tools = sum(s.tool_count for s in server_infos)
    return {
        "total_tools": total_tools,
        "servers": [s.to_dict() for s in server_infos],
    }


async def preload_mcp_server_info(
    *,
    mcp_config_path: str | None = None,
    no_mcp: bool = False,
    trust_project_mcp: bool | None = None,
    include_subagents: bool | None = None,
    project_root: Path | str | None = None,
) -> list[MCPServerInfo]:
    """Discover MCP servers, probe them concurrently, and cache metadata.

    Args:
        mcp_config_path: Optional explicit path to an MCP config JSON.
        no_mcp: When True, skip all MCP loading.
        trust_project_mcp: Optional whole-config trust override.
        include_subagents: Whether to also discover and preload subagent-scoped
            MCP servers in the same deduplicated batch. Defaults to True outside
            pytest runs.
        project_root: Optional working directory or project root for discovery.

    Returns:
        List of MCPServerInfo entries for discovered servers.
    """
    if no_mcp:
        return []

    from talkops.mcp.discovery import (
        MCPDiscovery,
        _load_mcp_json,
        _parse_server_config,
    )
    from talkops.project_utils import ProjectContext

    resolved_project_root: Path | None = None
    if project_root is not None:
        try:
            project_context = ProjectContext.from_user_cwd(Path(project_root))
            resolved_project_root = (
                project_context.project_root or project_context.user_cwd
            )
        except OSError:
            resolved_project_root = Path(project_root)

    discovery = MCPDiscovery()
    try:
        config: dict[str, Any] = dict(
            await discovery.discover_and_sync_async(project_root=resolved_project_root)
        )
    except Exception as exc:
        logger.debug("Async discovery failed, falling back to sync: %s", exc)
        config = dict(discovery.discover(project_root=resolved_project_root))

    if mcp_config_path:
        try:
            path = Path(mcp_config_path)
            servers = _load_mcp_json(path)
            for name, srv_config in servers.items():
                if isinstance(srv_config, dict):
                    config[name] = _parse_server_config(name, srv_config, "cli")
        except Exception as e:
            logger.warning("Failed to load explicit MCP config %s: %s", mcp_config_path, e)

    top_level_names = list(config.keys())

    if include_subagents is None:
        include_subagents = not bool(os.environ.get("PYTEST_CURRENT_TEST"))

    if include_subagents:
        try:
            from talkops.plugins.adapters.mcp import discover_all_subagents_mcp_configs

            subagent_configs = discover_all_subagents_mcp_configs(
                project_root=resolved_project_root,
            )
            if subagent_configs:
                for s_name, s_cfg in subagent_configs.items():
                    config.setdefault(s_name, s_cfg)
        except Exception as exc:
            logger.debug("Subagent MCP config discovery during preload skipped: %s", exc)

    try:
        from talkops.mcp.session_manager import MCPSessionManager

        mgr = (
            MCPSessionManager.get_instance(config)
            if config
            else MCPSessionManager._instance
        )
        if mgr is not None:
            await mgr.async_sync_active_configs(
                config,
                purge_missing=True,
                purge_subagents=bool(include_subagents),
            )
    except Exception as exc:
        logger.debug("MCPSessionManager sync during preload skipped: %s", exc)

    prune_stale_mcp_server_state(
        config.keys(),
        prune_subagents=bool(include_subagents),
    )

    if not config:
        return []

    await preload_mcp_metadata(config)
    return [
        _PROBED_CACHE[name]
        for name in top_level_names
        if name in _PROBED_CACHE
    ]


__all__ = [
    "_PROGRESS_CALLBACK",
    "_connection_fingerprint",
    "_mcp_tool_name",
    "build_mcp_server_info_from_raw_tools",
    "clear_cached_mcp_server_infos",
    "evict_cached_mcp_server_info",
    "evict_failed_mcp_server_infos",
    "format_mcp_status_response",
    "get_cached_mcp_server_infos",
    "get_mcp_server_state_dir",
    "is_mcp_server_info_cached",
    "preload_mcp_metadata",
    "preload_mcp_server_info",
    "probe_one_mcp_server",
    "prune_stale_mcp_server_state",
    "read_mcp_tool_schema",
    "set_cached_mcp_server_info",
    "wait_for_inflight_mcp_probes",
    "write_mcp_server_state",
]



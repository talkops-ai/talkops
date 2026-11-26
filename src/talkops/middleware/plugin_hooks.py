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

"""Scoped blocking PreToolUse / PostToolUse / permissions.deny middleware for plugins."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from langchain.agents.middleware.human_in_the_loop import (
    ActionRequest,
    HITLRequest,
    ReviewConfig,
)
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ResponseT,
)
from langchain_core.messages import ToolMessage
from langgraph.types import Command, interrupt

from talkops.hooks.env import sanitize_hook_environ
from talkops.hooks.tools import to_wire_tool_input, to_wire_tool_name
from talkops.middleware.registry import register_middleware
from talkops.plugins.substitution import plugin_environment, substitute_json
from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from langchain_core.tools import BaseTool
    from langgraph.prebuilt.tool_node import ToolCallRequest

logger = get_logger(__name__)

_DEFAULT_HOOK_TIMEOUT_SECONDS = 5.0
_PERMISSION_RULE_RE = re.compile(r"^([A-Za-z0-9_.*:-]+)(?:\((.*)\))?$")
_FILE_WIRE_TOOLS = frozenset({"Read", "Write", "Edit", "MultiEdit"})


@dataclass(frozen=True, slots=True)
class PluginCommandHook:
    """Single command hook bound to a plugin root and tool lifecycle event."""

    plugin_root: Path
    plugin_name: str
    plugin_data_dir: Path | None
    event: Literal["PreToolUse", "PostToolUse"]
    matcher: re.Pattern[str] | None
    command: tuple[str, ...]
    timeout_seconds: float = _DEFAULT_HOOK_TIMEOUT_SECONDS

    def matches_tool(self, wire_tool_name: str) -> bool:
        """Return whether this hook applies to `wire_tool_name`."""
        if self.matcher is None:
            return True
        return self.matcher.search(wire_tool_name) is not None


@dataclass(frozen=True, slots=True)
class PluginPermissionDenyRule:
    """Declarative `permissions.deny` rule from a plugin's `hooks.json`."""

    plugin_root: Path
    plugin_name: str
    raw_rule: str
    tool_pattern: str
    arg_pattern: str | None


@dataclass(frozen=True, slots=True)
class PluginHooksSpec:
    """Parsed blocking hooks and permission deny rules for one or more plugins."""

    pre_tool_hooks: tuple[PluginCommandHook, ...] = ()
    post_tool_hooks: tuple[PluginCommandHook, ...] = ()
    deny_rules: tuple[PluginPermissionDenyRule, ...] = ()

    @property
    def is_empty(self) -> bool:
        """Return True when no blocking hooks or deny rules are configured."""
        return not (self.pre_tool_hooks or self.post_tool_hooks or self.deny_rules)


@dataclass(frozen=True, slots=True)
class _HookEvalResult:
    """Aggregated outcome of running command hooks for a tool call."""

    decision: Literal["allow", "deny", "ask"] = "allow"
    reason: str = ""
    additional_context: tuple[str, ...] = ()


def _compile_matcher(raw_matcher: object) -> re.Pattern[str] | None:
    if not isinstance(raw_matcher, str):
        return None
    cleaned = raw_matcher.strip()
    if not cleaned or cleaned == "*":
        return None
    try:
        return re.compile(f"^(?:{cleaned})$")
    except re.error as exc:
        logger.warning("Invalid plugin hook matcher regex %r: %s", cleaned, exc)
        return None


def _parse_deny_rule(
    raw_rule: str,
    *,
    plugin_root: Path,
    plugin_name: str,
) -> PluginPermissionDenyRule | None:
    stripped = raw_rule.strip()
    if not stripped:
        return None
    match = _PERMISSION_RULE_RE.match(stripped)
    if match is None:
        return None
    tool_pat = match.group(1).strip()
    arg_pat = match.group(2)
    return PluginPermissionDenyRule(
        plugin_root=plugin_root,
        plugin_name=plugin_name,
        raw_rule=stripped,
        tool_pattern=tool_pat,
        arg_pattern=arg_pat.strip() if isinstance(arg_pat, str) else None,
    )


def _discover_plugin_hook_files(
    plugin_root: Path,
    explicit_files: Sequence[Path] = (),
) -> list[Path]:
    seen: set[Path] = set()
    ordered: list[Path] = []
    for candidate in (
        *explicit_files,
        plugin_root / "hooks" / "hooks.json",
        plugin_root / "hooks.json",
    ):
        resolved = candidate.resolve(strict=False)
        if resolved not in seen and candidate.is_file():
            seen.add(resolved)
            ordered.append(candidate)
    return ordered


def load_plugin_hooks_spec(
    plugin_root: Path,
    *,
    plugin_name: str = "",
    plugin_data_dir: Path | None = None,
    project_dir: Path | None = None,
    explicit_files: Sequence[Path] = (),
) -> PluginHooksSpec:
    """Load `PreToolUse`, `PostToolUse`, and `permissions.deny` rules for a plugin."""
    effective_name = plugin_name or plugin_root.name
    pre_hooks: list[PluginCommandHook] = []
    post_hooks: list[PluginCommandHook] = []
    deny_rules: list[PluginPermissionDenyRule] = []

    for hooks_file in _discover_plugin_hook_files(plugin_root, explicit_files):
        try:
            raw_json = json.loads(hooks_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read plugin hooks file %s: %s", hooks_file, exc)
            continue
        if not isinstance(raw_json, dict):
            continue

        substituted = substitute_json(
            raw_json,
            plugin_root=plugin_root,
            plugin_data=plugin_data_dir,
            project_dir=project_dir,
        )
        if not isinstance(substituted, dict):
            continue

        hooks_section = substituted.get("hooks")
        if not isinstance(hooks_section, dict):
            hooks_section = substituted

        for event_name, target_list in (
            ("PreToolUse", pre_hooks),
            ("PostToolUse", post_hooks),
        ):
            entries = hooks_section.get(event_name)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                matcher = _compile_matcher(entry.get("matcher"))
                inner_hooks = entry.get("hooks")
                hook_defs = inner_hooks if isinstance(inner_hooks, list) else [entry]
                for hook_def in hook_defs:
                    if not isinstance(hook_def, dict):
                        continue
                    hook_type = str(hook_def.get("type") or "command").strip().lower()
                    if hook_type != "command":
                        continue
                    raw_cmd = hook_def.get("command")
                    cmd_tuple: tuple[str, ...]
                    if isinstance(raw_cmd, str) and raw_cmd.strip():
                        cmd_tuple = ("sh", "-c", raw_cmd.strip())
                    elif isinstance(raw_cmd, list) and raw_cmd and all(isinstance(x, str) for x in raw_cmd):
                        cmd_tuple = tuple(raw_cmd)
                    else:
                        continue
                    raw_timeout = hook_def.get("timeout", _DEFAULT_HOOK_TIMEOUT_SECONDS)
                    timeout_sec = (
                        float(raw_timeout)
                        if isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool) and raw_timeout > 0
                        else _DEFAULT_HOOK_TIMEOUT_SECONDS
                    )
                    target_list.append(
                        PluginCommandHook(
                            plugin_root=plugin_root,
                            plugin_name=effective_name,
                            plugin_data_dir=plugin_data_dir,
                            event=event_name,  # type: ignore[arg-type]
                            matcher=matcher,
                            command=cmd_tuple,
                            timeout_seconds=timeout_sec,
                        )
                    )

        permissions = substituted.get("permissions")
        if isinstance(permissions, dict):
            raw_deny = permissions.get("deny")
            if isinstance(raw_deny, list):
                for item in raw_deny:
                    if isinstance(item, str):
                        parsed = _parse_deny_rule(
                            item,
                            plugin_root=plugin_root,
                            plugin_name=effective_name,
                        )
                        if parsed is not None:
                            deny_rules.append(parsed)

    seen_deny_rules = {r.raw_rule for r in deny_rules}
    for manifest_candidate in (
        plugin_root / ".claude-plugin" / "plugin.json",
        plugin_root / "plugin.json",
    ):
        if not manifest_candidate.is_file():
            continue
        try:
            manifest_json = json.loads(manifest_candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest_json, dict):
            continue
        manifest_perms = manifest_json.get("permissions")
        if not isinstance(manifest_perms, dict):
            continue
        raw_manifest_deny = manifest_perms.get("deny")
        if not isinstance(raw_manifest_deny, list):
            continue
        for item in raw_manifest_deny:
            if isinstance(item, str) and item.strip() not in seen_deny_rules:
                parsed = _parse_deny_rule(
                    item,
                    plugin_root=plugin_root,
                    plugin_name=effective_name,
                )
                if parsed is not None:
                    seen_deny_rules.add(parsed.raw_rule)
                    deny_rules.append(parsed)

    return PluginHooksSpec(
        pre_tool_hooks=tuple(pre_hooks),
        post_tool_hooks=tuple(post_hooks),
        deny_rules=tuple(deny_rules),
    )


def merge_plugin_hooks_specs(specs: Sequence[PluginHooksSpec]) -> PluginHooksSpec:
    """Combine multiple `PluginHooksSpec` objects while preserving order."""
    pre: list[PluginCommandHook] = []
    post: list[PluginCommandHook] = []
    deny: list[PluginPermissionDenyRule] = []
    for spec in specs:
        pre.extend(spec.pre_tool_hooks)
        post.extend(spec.post_tool_hooks)
        deny.extend(spec.deny_rules)
    return PluginHooksSpec(
        pre_tool_hooks=tuple(pre),
        post_tool_hooks=tuple(post),
        deny_rules=tuple(deny),
    )


def _glob_path_matches(candidate_posix: str, pattern_posix: str) -> bool:
    """Match a POSIX path against a glob pattern supporting zero-or-more `**/` segments."""
    patterns_to_try = {pattern_posix}
    if pattern_posix.startswith("**/"):
        patterns_to_try.add(pattern_posix[3:])
    if "/**/" in pattern_posix:
        patterns_to_try.add(pattern_posix.replace("/**/", "/"))
    for pat in patterns_to_try:
        if fnmatch.fnmatchcase(candidate_posix, pat):
            return True
    return False


def _matches_permission_deny_rule(
    rule: PluginPermissionDenyRule,
    *,
    native_tool_name: str,
    wire_tool_name: str,
    wire_input: Mapping[str, Any],
    raw_args: Mapping[str, Any],
    cwd: Path,
) -> bool:
    if not (
        fnmatch.fnmatchcase(wire_tool_name, rule.tool_pattern)
        or fnmatch.fnmatchcase(native_tool_name, rule.tool_pattern)
    ):
        return False

    if rule.arg_pattern is None:
        return True

    arg_pat = rule.arg_pattern
    if wire_tool_name in _FILE_WIRE_TOOLS:
        raw_path = (
            wire_input.get("file_path")
            or raw_args.get("file_path")
            or raw_args.get("path")
        )
        if not isinstance(raw_path, str) or not raw_path.strip():
            return False
        raw_path_str = raw_path.strip()

        if arg_pat.startswith("~"):
            expanded_pat = str(Path(arg_pat).expanduser()).replace("\\", "/")
            expanded_target = Path(raw_path_str).expanduser()
            target_abs = (
                expanded_target.resolve(strict=False)
                if expanded_target.is_absolute()
                else (cwd / expanded_target).resolve(strict=False)
            )
            return _glob_path_matches(target_abs.as_posix(), expanded_pat)

        clean_pat = arg_pat.removeprefix("./").lstrip("/")
        cwd_abs = cwd.resolve(strict=False)
        expanded_target = Path(raw_path_str).expanduser()
        target_abs = (
            expanded_target.resolve(strict=False)
            if expanded_target.is_absolute()
            else (cwd_abs / expanded_target).resolve(strict=False)
        )

        candidates: list[str] = []
        try:
            candidates.append(target_abs.relative_to(cwd_abs).as_posix())
        except ValueError:
            pass

        # Support virtual root paths used by DeepAgents FilesystemBackend (e.g. "/.env", "/certs/server.pem")
        virtual_rel = raw_path_str.replace("\\", "/").removeprefix("./").lstrip("/")
        if virtual_rel and virtual_rel not in candidates:
            candidates.append(virtual_rel)

        # Secret filename patterns without directory separators (e.g. ".env", ".env.*", "**/*.pem")
        # also match the basename regardless of directory depth.
        if "/" not in clean_pat or clean_pat.startswith("**/"):
            basename = target_abs.name or Path(raw_path_str).name
            if basename and basename not in candidates:
                candidates.append(basename)

        return any(_glob_path_matches(candidate, clean_pat) for candidate in candidates)

    if wire_tool_name == "Bash":
        command = str(wire_input.get("command") or raw_args.get("command") or "")
        return fnmatch.fnmatchcase(command, arg_pat)

    serialized = json.dumps(dict(wire_input), sort_keys=True)
    return fnmatch.fnmatchcase(serialized, arg_pat)


def _extract_mcp_server_from_tool(tool: object | None) -> str | None:
    if tool is None:
        return None
    metadata = getattr(tool, "metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    for key in ("_mcp_server", "mcp_server", "mcp_server_name", "server_name"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _base_mcp_server_name(scoped_server: str) -> str:
    if scoped_server.startswith(("plugin__", "subagent__")):
        parts = scoped_server.split("__", 2)
        if len(parts) == 3 and parts[2]:
            return parts[2]
    return scoped_server


def _project_plugin_wire_call(
    *,
    call_name: str,
    raw_args: Mapping[str, Any],
    mcp_server: str | None,
    plugin_name: str,
) -> tuple[str, dict[str, Any]]:
    """Project a TalkOps tool call to the Claude/Codex plugin hook wire format."""
    if call_name in {"list_resources", "read_resource"}:
        srv_arg = str(
            raw_args.get("server_name")
            or raw_args.get("ServerName")
            or raw_args.get("server")
            or mcp_server
            or ""
        ).strip()
        base_srv = _base_mcp_server_name(srv_arg) if srv_arg else "mcp"
        if plugin_name:
            return f"mcp__plugin_{plugin_name}_{base_srv}__{call_name}", dict(raw_args)
        return f"mcp__{base_srv}__{call_name}", dict(raw_args)

    if call_name == "call_mcp_tool":
        srv_arg = str(
            raw_args.get("server_name")
            or raw_args.get("ServerName")
            or raw_args.get("server")
            or mcp_server
            or ""
        ).strip()
        tool_arg = str(
            raw_args.get("tool_name")
            or raw_args.get("ToolName")
            or raw_args.get("tool")
            or ""
        ).strip()
        if ":" in tool_arg and not srv_arg:
            srv_arg, tool_arg = tool_arg.split(":", 1)
            srv_arg = srv_arg.strip()
            tool_arg = tool_arg.strip()
        elif ":" in tool_arg:
            tool_arg = tool_arg.split(":", 1)[1].strip()
        inner_args = (
            raw_args.get("arguments")
            or raw_args.get("Arguments")
            or raw_args.get("args")
        )
        projected_args = dict(inner_args) if isinstance(inner_args, Mapping) else dict(raw_args)
        base_srv = _base_mcp_server_name(srv_arg) if srv_arg else "mcp"
        effective_tool = tool_arg or call_name
        if plugin_name:
            return f"mcp__plugin_{plugin_name}_{base_srv}__{effective_tool}", projected_args
        return f"mcp__{base_srv}__{effective_tool}", projected_args

    if call_name.startswith("mcp__"):
        return call_name, dict(raw_args)

    if mcp_server:
        base_srv = _base_mcp_server_name(mcp_server)
        raw_tool = call_name
        for prefix in (f"{mcp_server}_", f"{base_srv}_"):
            if raw_tool.startswith(prefix):
                raw_tool = raw_tool[len(prefix) :]
                break
        if plugin_name:
            return f"mcp__plugin_{plugin_name}_{base_srv}__{raw_tool}", dict(raw_args)
        return f"mcp__{base_srv}__{raw_tool}", dict(raw_args)

    wire_name = to_wire_tool_name(call_name)
    wire_input = to_wire_tool_input(call_name, dict(raw_args))
    return wire_name, dict(wire_input)


def _parse_hook_stdout(
    *,
    returncode: int,
    stdout_text: str,
    stderr_text: str,
) -> _HookEvalResult:
    if returncode == 2:
        reason = stderr_text.strip() or stdout_text.strip() or "Blocked by plugin hook (exit code 2)."
        return _HookEvalResult(decision="deny", reason=reason)

    if returncode != 0:
        logger.warning(
            "Plugin hook exited with non-blocking error code %d: %s",
            returncode,
            stderr_text.strip() or stdout_text.strip(),
        )
        return _HookEvalResult()

    stripped = stdout_text.strip()
    if not stripped:
        return _HookEvalResult()

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return _HookEvalResult()

    if not isinstance(parsed, dict):
        return _HookEvalResult()

    extras: list[str] = []
    decision: Literal["allow", "deny", "ask"] = "allow"
    reason = ""

    top_decision = str(parsed.get("decision") or "").strip().lower()
    if top_decision in {"deny", "block"}:
        decision = "deny"
        reason = str(parsed.get("reason") or parsed.get("stopReason") or "").strip()
    elif top_decision == "ask":
        decision = "ask"
        reason = str(parsed.get("reason") or "").strip()

    if parsed.get("continue") is False and decision == "allow":
        decision = "deny"
        reason = str(parsed.get("stopReason") or parsed.get("reason") or "Stopped by plugin hook.").strip()

    specific = parsed.get("hookSpecificOutput")
    if isinstance(specific, dict):
        perm = str(specific.get("permissionDecision") or "").strip().lower()
        perm_reason = str(specific.get("permissionDecisionReason") or "").strip()
        if perm in {"deny", "block"}:
            decision = "deny"
            reason = perm_reason or reason
        elif perm == "ask" and decision != "deny":
            decision = "ask"
            reason = perm_reason or reason
        add_ctx = specific.get("additionalContext")
        if isinstance(add_ctx, str) and add_ctx.strip():
            extras.append(add_ctx.strip())

    top_ctx = parsed.get("additionalContext")
    if isinstance(top_ctx, str) and top_ctx.strip() and top_ctx.strip() not in extras:
        extras.append(top_ctx.strip())

    return _HookEvalResult(
        decision=decision,
        reason=reason,
        additional_context=tuple(extras),
    )


def _merge_tool_message_notice(
    result: ToolMessage | Command[Any],
    notices: Sequence[str],
    tool_call_id: str,
) -> ToolMessage | Command[Any]:
    if not notices:
        return result
    formatted = "\n\n[Plugin Hook Notice]\n" + "\n\n".join(notices)
    if isinstance(result, ToolMessage):
        existing = result.content if isinstance(result.content, str) else str(result.content)
        return result.model_copy(update={"content": f"{existing}{formatted}"})
    update = getattr(result, "update", None)
    if not isinstance(update, Mapping):
        return result
    raw_msgs = update.get("messages")
    if not isinstance(raw_msgs, list):
        return result
    updated_msgs: list[Any] = []
    changed = False
    for msg in raw_msgs:
        if isinstance(msg, ToolMessage) and msg.tool_call_id == tool_call_id:
            existing = msg.content if isinstance(msg.content, str) else str(msg.content)
            updated_msgs.append(msg.model_copy(update={"content": f"{existing}{formatted}"}))
            changed = True
        else:
            updated_msgs.append(msg)
    if not changed:
        return result
    return replace(result, update={**update, "messages": updated_msgs})


@register_middleware(name="plugin_hooks")
class PluginHooksMiddleware(AgentMiddleware[AgentState[Any], ContextT, ResponseT]):
    """Synchronously enforce plugin `permissions.deny`, `PreToolUse`, and `PostToolUse` hooks."""

    def __init__(
        self,
        *,
        spec: PluginHooksSpec | None = None,
        cwd: Path | None = None,
        agent_type: str = "",
        plugin_name: str = "",
        mcp_tools: Sequence[BaseTool] = (),
    ) -> None:
        """Initialize PluginHooksMiddleware."""
        self._spec = spec if spec is not None else PluginHooksSpec()
        self._cwd = cwd or Path.cwd()
        short_agent = agent_type.rsplit(":", 1)[-1] if agent_type else ""
        if "@" in short_agent:
            short_agent = short_agent.split("@", 1)[0]
        self._agent_type = short_agent
        self._plugin_name = plugin_name or short_agent
        self._mcp_servers: dict[str, str] = {
            name: server
            for tool in mcp_tools
            if (name := getattr(tool, "name", None))
            and isinstance(name, str)
            and (server := _extract_mcp_server_from_tool(tool)) is not None
        }

    @property
    def spec(self) -> PluginHooksSpec:
        """Return the active `PluginHooksSpec`."""
        return self._spec

    def _build_stdin_payload(
        self,
        *,
        event: Literal["PreToolUse", "PostToolUse"],
        wire_name: str,
        wire_input: Mapping[str, Any],
        tool_response: str | None = None,
    ) -> bytes:
        payload: dict[str, Any] = {
            "hook_event_name": event,
            "tool_name": wire_name,
            "tool_input": dict(wire_input),
            "cwd": str(self._cwd),
        }
        if self._agent_type:
            payload["agent_type"] = self._agent_type
        if tool_response is not None:
            payload["tool_response"] = tool_response
        return json.dumps(payload).encode("utf-8")

    def _build_hook_env(self, hook: PluginCommandHook) -> dict[str, str]:
        base = sanitize_hook_environ()
        plugin_vars = plugin_environment(
            plugin_root=hook.plugin_root,
            plugin_data=hook.plugin_data_dir,
            project_dir=self._cwd,
        )
        return {
            **base,
            **plugin_vars,
            "TALKOPS_HOOK_EVENT": hook.event,
        }

    def _run_hook_sync(
        self,
        hook: PluginCommandHook,
        stdin_bytes: bytes,
    ) -> _HookEvalResult:
        try:
            completed = subprocess.run(
                list(hook.command),
                input=stdin_bytes,
                capture_output=True,
                env=self._build_hook_env(hook),
                cwd=str(self._cwd) if self._cwd.is_dir() else None,
                timeout=hook.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "Plugin %s %s hook timed out after %.1fs",
                hook.plugin_name,
                hook.event,
                hook.timeout_seconds,
            )
            return _HookEvalResult()
        except OSError as exc:
            logger.warning("Failed to execute plugin hook %s: %s", hook.command, exc)
            return _HookEvalResult()

        return _parse_hook_stdout(
            returncode=completed.returncode,
            stdout_text=completed.stdout.decode("utf-8", errors="replace"),
            stderr_text=completed.stderr.decode("utf-8", errors="replace"),
        )

    async def _run_hook_async(
        self,
        hook: PluginCommandHook,
        stdin_bytes: bytes,
    ) -> _HookEvalResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *hook.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_hook_env(hook),
                cwd=str(self._cwd) if self._cwd.is_dir() else None,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(input=stdin_bytes),
                    timeout=hook.timeout_seconds,
                )
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                logger.warning(
                    "Plugin %s %s hook timed out after %.1fs",
                    hook.plugin_name,
                    hook.event,
                    hook.timeout_seconds,
                )
                return _HookEvalResult()
        except OSError as exc:
            logger.warning("Failed to execute async plugin hook %s: %s", hook.command, exc)
            return _HookEvalResult()

        return _parse_hook_stdout(
            returncode=proc.returncode or 0,
            stdout_text=stdout_bytes.decode("utf-8", errors="replace"),
            stderr_text=stderr_bytes.decode("utf-8", errors="replace"),
        )

    def _extract_call_details(
        self,
        request: ToolCallRequest,
    ) -> tuple[str, str, dict[str, Any], str | None]:
        tool_call = request.tool_call
        call_id = str(tool_call.get("id") or "")
        call_name = str(tool_call.get("name") or "")
        raw_args_obj = tool_call.get("args")
        raw_args = (
            {str(k): v for k, v in raw_args_obj.items()}
            if isinstance(raw_args_obj, Mapping)
            else {}
        )
        mcp_server = _extract_mcp_server_from_tool(getattr(request, "tool", None)) or self._mcp_servers.get(call_name)
        return call_id, call_name, raw_args, mcp_server

    def _check_deny_rules(
        self,
        *,
        call_id: str,
        call_name: str,
        raw_args: Mapping[str, Any],
        mcp_server: str | None,
    ) -> ToolMessage | None:
        if not self._spec.deny_rules:
            return None
        default_wire_name, default_wire_input = _project_plugin_wire_call(
            call_name=call_name,
            raw_args=raw_args,
            mcp_server=mcp_server,
            plugin_name=self._plugin_name,
        )
        for rule in self._spec.deny_rules:
            if _matches_permission_deny_rule(
                rule,
                native_tool_name=call_name,
                wire_tool_name=default_wire_name,
                wire_input=default_wire_input,
                raw_args=raw_args,
                cwd=self._cwd,
            ):
                return ToolMessage(
                    content=(
                        f"[PLUGIN_PERMISSION_DENIED] {default_wire_name} blocked by plugin "
                        f"'{rule.plugin_name}' rule `{rule.raw_rule}`."
                    ),
                    name=call_name,
                    tool_call_id=call_id,
                    status="error",
                )
        return None

    def _resolve_ask_decision(
        self,
        *,
        call_id: str,
        call_name: str,
        raw_args: Mapping[str, Any],
        reason: str,
    ) -> ToolMessage | None:
        description = reason or f"Plugin hook requested operator approval for `{call_name}`."
        try:
            response = interrupt(
                HITLRequest(
                    action_requests=[
                        ActionRequest(
                            name=call_name,
                            args=dict(raw_args),
                            description=description,
                        )
                    ],
                    review_configs=[
                        ReviewConfig(
                            action_name=call_name,
                            allowed_decisions=["approve", "reject"],
                        )
                    ],
                )
            )
        except RuntimeError:
            return ToolMessage(
                content=f"[PLUGIN_HOOK_APPROVAL_REQUIRED] {description}",
                name=call_name,
                tool_call_id=call_id,
                status="error",
            )

        decisions = response.get("decisions", ()) if isinstance(response, Mapping) else ()
        first = decisions[0] if isinstance(decisions, Sequence) and decisions else None
        if isinstance(first, Mapping) and first.get("type") == "approve":
            return None
        reject_msg = (
            str(first.get("message") or "").strip()
            if isinstance(first, Mapping)
            else ""
        )
        return ToolMessage(
            content=f"[PLUGIN_HOOK_BLOCKED] {reject_msg or description}",
            name=call_name,
            tool_call_id=call_id,
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """Enforce `permissions.deny`, `PreToolUse`, and `PostToolUse` around sync tool calls."""
        call_id, call_name, raw_args, mcp_server = self._extract_call_details(request)

        denied = self._check_deny_rules(
            call_id=call_id,
            call_name=call_name,
            raw_args=raw_args,
            mcp_server=mcp_server,
        )
        if denied is not None:
            return denied

        notices: list[str] = []
        for hook in self._spec.pre_tool_hooks:
            wire_name, wire_input = _project_plugin_wire_call(
                call_name=call_name,
                raw_args=raw_args,
                mcp_server=mcp_server,
                plugin_name=hook.plugin_name or self._plugin_name,
            )
            if not hook.matches_tool(wire_name):
                continue
            outcome = self._run_hook_sync(
                hook,
                self._build_stdin_payload(
                    event="PreToolUse",
                    wire_name=wire_name,
                    wire_input=wire_input,
                ),
            )
            notices.extend(outcome.additional_context)
            if outcome.decision == "deny":
                reason = outcome.reason or f"Blocked by plugin '{hook.plugin_name}' PreToolUse hook."
                return ToolMessage(
                    content=f"[PLUGIN_HOOK_BLOCKED] {reason}",
                    name=call_name,
                    tool_call_id=call_id,
                    status="error",
                )
            if outcome.decision == "ask":
                blocked = self._resolve_ask_decision(
                    call_id=call_id,
                    call_name=call_name,
                    raw_args=raw_args,
                    reason=outcome.reason,
                )
                if blocked is not None:
                    return blocked

        result = handler(request)

        if self._spec.post_tool_hooks:
            response_text = (
                result.content
                if isinstance(result, ToolMessage) and isinstance(result.content, str)
                else ""
            )
            for hook in self._spec.post_tool_hooks:
                wire_name, wire_input = _project_plugin_wire_call(
                    call_name=call_name,
                    raw_args=raw_args,
                    mcp_server=mcp_server,
                    plugin_name=hook.plugin_name or self._plugin_name,
                )
                if not hook.matches_tool(wire_name):
                    continue
                post_outcome = self._run_hook_sync(
                    hook,
                    self._build_stdin_payload(
                        event="PostToolUse",
                        wire_name=wire_name,
                        wire_input=wire_input,
                        tool_response=response_text,
                    ),
                )
                notices.extend(post_outcome.additional_context)

        return _merge_tool_message_notice(result, notices, call_id)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Enforce `permissions.deny`, `PreToolUse`, and `PostToolUse` around async tool calls."""
        call_id, call_name, raw_args, mcp_server = self._extract_call_details(request)

        denied = self._check_deny_rules(
            call_id=call_id,
            call_name=call_name,
            raw_args=raw_args,
            mcp_server=mcp_server,
        )
        if denied is not None:
            return denied

        notices: list[str] = []
        for hook in self._spec.pre_tool_hooks:
            wire_name, wire_input = _project_plugin_wire_call(
                call_name=call_name,
                raw_args=raw_args,
                mcp_server=mcp_server,
                plugin_name=hook.plugin_name or self._plugin_name,
            )
            if not hook.matches_tool(wire_name):
                continue
            outcome = await self._run_hook_async(
                hook,
                self._build_stdin_payload(
                    event="PreToolUse",
                    wire_name=wire_name,
                    wire_input=wire_input,
                ),
            )
            notices.extend(outcome.additional_context)
            if outcome.decision == "deny":
                reason = outcome.reason or f"Blocked by plugin '{hook.plugin_name}' PreToolUse hook."
                return ToolMessage(
                    content=f"[PLUGIN_HOOK_BLOCKED] {reason}",
                    name=call_name,
                    tool_call_id=call_id,
                    status="error",
                )
            if outcome.decision == "ask":
                blocked = self._resolve_ask_decision(
                    call_id=call_id,
                    call_name=call_name,
                    raw_args=raw_args,
                    reason=outcome.reason,
                )
                if blocked is not None:
                    return blocked

        result = await handler(request)

        if self._spec.post_tool_hooks:
            response_text = (
                result.content
                if isinstance(result, ToolMessage) and isinstance(result.content, str)
                else ""
            )
            for hook in self._spec.post_tool_hooks:
                wire_name, wire_input = _project_plugin_wire_call(
                    call_name=call_name,
                    raw_args=raw_args,
                    mcp_server=mcp_server,
                    plugin_name=hook.plugin_name or self._plugin_name,
                )
                if not hook.matches_tool(wire_name):
                    continue
                post_outcome = await self._run_hook_async(
                    hook,
                    self._build_stdin_payload(
                        event="PostToolUse",
                        wire_name=wire_name,
                        wire_input=wire_input,
                        tool_response=response_text,
                    ),
                )
                notices.extend(post_outcome.additional_context)

        return _merge_tool_message_notice(result, notices, call_id)


__all__ = [
    "PluginCommandHook",
    "PluginHooksMiddleware",
    "PluginHooksSpec",
    "PluginPermissionDenyRule",
    "load_plugin_hooks_spec",
    "merge_plugin_hooks_specs",
]

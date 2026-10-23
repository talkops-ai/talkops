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

"""Transactional Non-Regression (TNR) Safety & Mandatory Undo Middleware.

Implements the STRATUS (NeurIPS) Transactional Non-Regression specification:
1. Writer Exclusivity (A-Lock): Resource-scoped concurrency lock preventing
   conflicting concurrent mutations while tracking Closing Action Hygiene.
2. Mandatory Undo Operator (alpha_U): Pre-mutation snapshots and inverse command
   synthesis for file edits, destructive shell commands, and Kubernetes/IaC mutations.
3. Monotonic Severity Non-Regression: Verifies mu(s_post) <= mu(s_0^e) after each
   mutating transaction and automatically executes alpha_U rollback on regression.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from talkops.middleware.registry import register_middleware
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

_FILE_MUTATION_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "write_to_file",
        "edit_file",
        "replace_file_content",
        "multi_replace_file_content",
        "delete_file",
    }
)

_SHELL_TOOLS: frozenset[str] = frozenset(
    {
        "execute",
        "run_command",
        "bash",
        "sh",
        "terminal",
    }
)

_MUTATING_K8S_VERBS: frozenset[str] = frozenset(
    {
        "apply",
        "patch",
        "scale",
        "set",
        "delete",
        "replace",
        "drain",
        "cordon",
        "taint",
    }
)

_REGRESSION_OUTPUT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"exit code[:\s]+[1-9]\d*", re.IGNORECASE),
    re.compile(r"error from server\s*\(", re.IGNORECASE),
    re.compile(r"CrashLoopBackOff|ImagePullBackOff|ErrImagePull|OOMKilled"),
    re.compile(r"validation failed|syntax error|unchanged \(dry run\)|conflict", re.IGNORECASE),
    re.compile(r"terraform.*error:", re.IGNORECASE),
)


@dataclass
class UndoSnapshot:
    """Captured pre-mutation state and inverse operator (alpha_U) for a transaction."""

    resource_key: str
    tool_name: str
    file_existed: bool = False
    file_path: Path | None = None
    backup_bytes: bytes | None = None
    backup_dir_map: dict[Path, bytes] = field(default_factory=dict)
    inverse_shell_cmd: str | None = None
    cwd: Path | None = None


class ALockManager:
    """Resource-scoped readers-writer / writer-exclusivity lock manager (A-Lock)."""

    def __init__(self) -> None:
        """Initialize ALockManager."""
        self._guard = threading.Lock()
        self._sync_locks: dict[str, threading.RLock] = {}
        self._async_locks: dict[str, asyncio.Lock] = {}
        self._active_holders: dict[str, int] = {}

    def acquire_sync(self, resource_key: str) -> None:
        """Execute acquire sync on ALockManager."""
        with self._guard:
            lock = self._sync_locks.setdefault(resource_key, threading.RLock())
        lock.acquire()
        with self._guard:
            self._active_holders[resource_key] = self._active_holders.get(resource_key, 0) + 1

    def release_sync(self, resource_key: str) -> None:
        """Execute release sync on ALockManager."""
        with self._guard:
            lock = self._sync_locks.get(resource_key)
            if resource_key in self._active_holders:
                self._active_holders[resource_key] = max(0, self._active_holders[resource_key] - 1)
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass

    async def acquire_async(self, resource_key: str) -> None:
        """Execute acquire async on ALockManager."""
        with self._guard:
            lock = self._async_locks.setdefault(resource_key, asyncio.Lock())
        await lock.acquire()
        with self._guard:
            self._active_holders[resource_key] = self._active_holders.get(resource_key, 0) + 1

    def release_async(self, resource_key: str) -> None:
        """Execute release async on ALockManager."""
        with self._guard:
            lock = self._async_locks.get(resource_key)
            if resource_key in self._active_holders:
                self._active_holders[resource_key] = max(0, self._active_holders[resource_key] - 1)
        if lock is not None and lock.locked():
            lock.release()

    def active_lock_count(self) -> int:
        """Execute active lock count on ALockManager."""
        with self._guard:
            return sum(v for v in self._active_holders.values() if v > 0)


def _extract_tool_call(request: ToolCallRequest) -> tuple[str, dict[str, Any], str]:
    """Extract (tool_name, args_dict, tool_call_id) from a ToolCallRequest."""
    tc = getattr(request, "tool_call", None)
    if isinstance(tc, Mapping):
        name = str(tc.get("name") or "")
        args = dict(tc.get("args") or {})
        tc_id = str(tc.get("id") or "")
        return name, args, tc_id
    name = str(getattr(tc, "name", None) or getattr(request, "name", "") or "")
    raw_args = getattr(tc, "args", None) or getattr(request, "args", {}) or {}
    args = dict(raw_args) if isinstance(raw_args, Mapping) else {}
    tc_id = str(getattr(tc, "id", None) or getattr(request, "id", "") or "")
    return name, args, tc_id


def _is_tnr_enabled(runtime: Any, default: bool = False) -> bool:
    if default:
        return True
    if os.environ.get("TALKOPS_SERVER_TNR", "").lower() == "true":
        return True
    ctx = getattr(runtime, "context", None)
    if isinstance(ctx, Mapping) and bool(ctx.get("tnr")):
        return True
    if ctx is not None and callable(getattr(ctx, "get", None)) and bool(ctx.get("tnr")):
        return True
    cfg = getattr(runtime, "config", None)
    if isinstance(cfg, Mapping):
        configurable = cfg.get("configurable")
        if isinstance(configurable, Mapping) and bool(configurable.get("tnr")):
            return True
    return False


def _emit_tnr_event(runtime: Any, payload: dict[str, Any]) -> None:
    event = {"type": "tnr_transaction", "timestamp": time.time(), **payload}
    writer = getattr(runtime, "stream_writer", None)
    if callable(writer):
        try:
            writer(event)
            return
        except Exception:
            pass
    try:
        from langgraph.config import get_stream_writer

        sw = get_stream_writer()
        if callable(sw):
            sw(event)
    except Exception:
        pass


def _split_shell_subcommands(cmd: str) -> list[list[str]]:
    """Split a compound shell command (separated by ;, &&, ||) into token lists."""
    segments = re.split(r"\s*(?:;|&&|\|\|)\s*", cmd)
    result: list[list[str]] = []
    for seg in segments:
        seg_str = seg.strip()
        if not seg_str:
            continue
        try:
            tokens = shlex.split(seg_str)
        except ValueError:
            tokens = seg_str.split()
        # Strip leading VAR=val assignments
        while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
            tokens = tokens[1:]
        if tokens:
            result.append(tokens)
    return result


def synthesize_k8s_inverse_command(cmd: str) -> tuple[str | None, str]:
    """Synthesize a verified inverse kubectl/helm command (alpha_U) and resource key."""
    subcommands = _split_shell_subcommands(cmd)
    if not subcommands:
        return None, "shell:unknown"

    _K8S_WORKLOAD_KINDS = {
        "deployment": "deployment",
        "deploy": "deployment",
        "deployments": "deployment",
        "statefulset": "statefulset",
        "sts": "statefulset",
        "daemonset": "daemonset",
        "ds": "daemonset",
    }

    for tokens in subcommands:
        if tokens[0] == "kubectl" and len(tokens) >= 2:
            ns = "default"
            for i, tok in enumerate(tokens):
                if tok in {"-n", "--namespace"} and i + 1 < len(tokens):
                    ns = tokens[i + 1]
                elif tok.startswith("--namespace="):
                    ns = tok.split("=", 1)[1]

            verb = next((t for t in tokens[1:] if not t.startswith("-")), "")
            if verb in {"set", "patch", "apply", "replace", "scale"}:
                target = next(
                    (
                        t
                        for t in tokens[2:]
                        if "/" in t
                        and any(
                            t.startswith(k)
                            for k in (
                                "deployment/",
                                "deploy/",
                                "statefulset/",
                                "sts/",
                                "daemonset/",
                                "ds/",
                            )
                        )
                    ),
                    None,
                )
                if target is None:
                    # Check two-token form: `kubectl patch deployment checkout ...`
                    for idx in range(1, len(tokens) - 1):
                        kind_norm = _K8S_WORKLOAD_KINDS.get(tokens[idx].lower())
                        if kind_norm and not tokens[idx + 1].startswith("-"):
                            target = f"{kind_norm}/{tokens[idx + 1]}"
                            break
                if target:
                    return f"kubectl rollout undo {target} -n {ns}", f"k8s:{ns}/{target}"
                if verb == "scale":
                    fallback = next((t for t in tokens[2:] if "/" in t or not t.startswith("-")), "deployment")
                    return f"kubectl rollout undo {fallback} -n {ns}", f"k8s:{ns}/{fallback}"
                return f"kubectl rollout undo deployment -n {ns}", f"k8s:{ns}/{verb}"

        if tokens[0] == "helm" and len(tokens) >= 3 and tokens[1] == "upgrade":
            release = tokens[2]
            return f"helm rollback {release}", f"helm:{release}"

    return None, "shell:workspace"


def is_mutating_tool_call(tool_name: str, args: Mapping[str, Any]) -> bool:
    """Return True if a tool call mutates files, cluster state, or infrastructure."""
    lower_name = tool_name.lower()
    if lower_name in _FILE_MUTATION_TOOLS:
        return True
    if lower_name in _SHELL_TOOLS:
        cmd = str(args.get("command") or args.get("cmd") or args.get("CommandLine") or "").strip()
        if not cmd:
            return False
        subcommands = _split_shell_subcommands(cmd)
        if not subcommands:
            return False
        for tokens in subcommands:
            first = tokens[0]
            if first in {"rm", "mv", "cp", "chmod", "chown", "sed", "tee", "dd"}:
                return True
            if first == "kubectl" and any(t in _MUTATING_K8S_VERBS for t in tokens[1:]):
                return True
            if first == "helm" and any(
                t in {"install", "upgrade", "uninstall", "delete", "rollback"} for t in tokens[1:]
            ):
                return True
            if first in {"terraform", "tofu"} and any(t in {"apply", "destroy", "import"} for t in tokens[1:]):
                return True
            if first == "git" and any(
                t in {"reset", "checkout", "clean", "revert", "commit", "push"} for t in tokens[1:]
            ):
                return True
        # Check non-devnull/non-tmp file redirection
        if re.search(r"(?:^|[^0-9&])>>?\s*(?!/dev/null|/tmp/|\"?\$out\"?|'\$out'|&)", cmd):
            return True
    return False


@register_middleware(name="tnr_safety")
class TransactionalNonRegressionMiddleware(AgentMiddleware[Any, Any, Any]):
    """STRATUS Transactional Non-Regression (TNR) middleware with A-Lock and alpha_U undo."""

    def __init__(
        self,
        *,
        working_dir: str | Path | None = None,
        enabled_by_default: bool = False,
        severity_evaluator: Callable[[ToolCallRequest, ToolMessage | Command[Any]], float] | None = None,
        initial_severity: float = 0.5,
    ) -> None:
        """Initialize TransactionalNonRegressionMiddleware."""
        super().__init__()
        self.working_dir = Path(working_dir).resolve() if working_dir else Path.cwd().resolve()
        self.enabled_by_default = enabled_by_default
        self.severity_evaluator = severity_evaluator
        self.initial_severity = initial_severity
        self.lock_manager = ALockManager()

    def _prepare_snapshot(self, tool_name: str, args: Mapping[str, Any]) -> UndoSnapshot:
        lower_name = tool_name.lower()
        if lower_name in _FILE_MUTATION_TOOLS:
            raw_path = str(
                args.get("file_path")
                or args.get("path")
                or args.get("TargetFile")
                or ""
            ).strip()
            if raw_path:
                p = Path(raw_path)
                if not p.is_absolute():
                    p = (self.working_dir / p).resolve()
                existed = p.exists() and p.is_file()
                backup_bytes = p.read_bytes() if existed else None
                return UndoSnapshot(
                    resource_key=f"file:{p}",
                    tool_name=tool_name,
                    file_existed=existed,
                    file_path=p,
                    backup_bytes=backup_bytes,
                    cwd=self.working_dir,
                )

        if lower_name in _SHELL_TOOLS:
            cmd = str(args.get("command") or args.get("cmd") or args.get("CommandLine") or "").strip()
            try:
                tokens = shlex.split(cmd)
            except ValueError:
                tokens = cmd.split()

            if tokens and tokens[0] == "rm":
                backup_map: dict[Path, bytes] = {}
                for tok in tokens[1:]:
                    if tok.startswith("-"):
                        continue
                    cand = Path(tok)
                    if not cand.is_absolute():
                        cand = (self.working_dir / cand).resolve()
                    if cand.exists() and cand.is_file():
                        backup_map[cand] = cand.read_bytes()
                return UndoSnapshot(
                    resource_key="shell:rm",
                    tool_name=tool_name,
                    backup_dir_map=backup_map,
                    cwd=self.working_dir,
                )

            inv_cmd, res_key = synthesize_k8s_inverse_command(cmd)
            return UndoSnapshot(
                resource_key=res_key,
                tool_name=tool_name,
                inverse_shell_cmd=inv_cmd,
                cwd=self.working_dir,
            )

        return UndoSnapshot(resource_key=f"tool:{tool_name}", tool_name=tool_name, cwd=self.working_dir)

    def _evaluate_post_severity(
        self,
        request: ToolCallRequest,
        result: ToolMessage | Command[Any],
    ) -> float:
        """Compute post-transaction severity mu(s_post). Returns > initial_severity on regression."""
        if self.severity_evaluator is not None:
            return float(self.severity_evaluator(request, result))

        if isinstance(result, ToolMessage):
            status = getattr(result, "status", None)
            if status == "error":
                return 1.0
            content = str(getattr(result, "content", "") or "")
            for pattern in _REGRESSION_OUTPUT_PATTERNS:
                if pattern.search(content):
                    return 1.0
        return self.initial_severity

    def _execute_undo(self, snapshot: UndoSnapshot) -> tuple[bool, str]:
        """Execute alpha_U inverse operator to restore pre-mutation state."""
        try:
            if snapshot.file_path is not None:
                if snapshot.file_existed and snapshot.backup_bytes is not None:
                    snapshot.file_path.parent.mkdir(parents=True, exist_ok=True)
                    snapshot.file_path.write_bytes(snapshot.backup_bytes)
                    return True, f"Restored file {snapshot.file_path.name} to pre-mutation snapshot"
                if not snapshot.file_existed and snapshot.file_path.exists():
                    snapshot.file_path.unlink(missing_ok=True)
                    return True, f"Removed newly created file {snapshot.file_path.name}"

            if snapshot.backup_dir_map:
                for path, data in snapshot.backup_dir_map.items():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                return True, f"Restored {len(snapshot.backup_dir_map)} deleted file(s) from TNR backup"

            if snapshot.inverse_shell_cmd:
                proc = subprocess.run(
                    snapshot.inverse_shell_cmd,
                    shell=True,
                    cwd=str(snapshot.cwd or self.working_dir),
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if proc.returncode == 0:
                    return True, f"Executed inverse command: `{snapshot.inverse_shell_cmd}`"
                return False, f"Inverse command `{snapshot.inverse_shell_cmd}` exited {proc.returncode}"

            return True, "No external state mutation persisted"
        except Exception as exc:
            logger.warning("TNR undo operator failed: %s", exc, exc_info=True)
            return False, f"Undo operator exception: {exc}"

    @staticmethod
    def _append_rollback_notice(
        result: ToolMessage | Command[Any],
        note: str,
    ) -> ToolMessage | Command[Any]:
        if isinstance(result, ToolMessage):
            orig = str(result.content or "")
            updated_content = f"{orig}\n\n[TNR Safety Rollback (α_U)]: {note}".strip()
            return ToolMessage(
                content=updated_content,
                tool_call_id=result.tool_call_id,
                name=result.name,
                status=getattr(result, "status", None),
                additional_kwargs=dict(getattr(result, "additional_kwargs", {}) or {}),
            )
        return result

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """Intercept and wrap synchronous tool execution."""
        runtime = getattr(request, "runtime", None)
        if not _is_tnr_enabled(runtime, self.enabled_by_default):
            return handler(request)

        tool_name, args, _ = _extract_tool_call(request)
        if not is_mutating_tool_call(tool_name, args):
            return handler(request)

        snapshot = self._prepare_snapshot(tool_name, args)
        self.lock_manager.acquire_sync(snapshot.resource_key)
        _emit_tnr_event(
            runtime,
            {
                "status": "lock_acquired",
                "resource": snapshot.resource_key,
                "tool_name": tool_name,
            },
        )
        try:
            result = handler(request)
            post_severity = self._evaluate_post_severity(request, result)
            if post_severity > self.initial_severity:
                undo_ok, undo_msg = self._execute_undo(snapshot)
                status = "rolled_back" if undo_ok else "violation"
                _emit_tnr_event(
                    runtime,
                    {
                        "status": status,
                        "resource": snapshot.resource_key,
                        "tool_name": tool_name,
                        "initial_severity": self.initial_severity,
                        "post_severity": post_severity,
                        "undo_message": undo_msg,
                    },
                )
                return self._append_rollback_notice(result, undo_msg)

            _emit_tnr_event(
                runtime,
                {
                    "status": "committed",
                    "resource": snapshot.resource_key,
                    "tool_name": tool_name,
                    "initial_severity": self.initial_severity,
                    "post_severity": post_severity,
                },
            )
            return result
        finally:
            self.lock_manager.release_sync(snapshot.resource_key)
            _emit_tnr_event(
                runtime,
                {
                    "status": "lock_released",
                    "resource": snapshot.resource_key,
                    "tool_name": tool_name,
                },
            )

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Intercept and wrap asynchronous tool execution."""
        runtime = getattr(request, "runtime", None)
        if not _is_tnr_enabled(runtime, self.enabled_by_default):
            return await handler(request)

        tool_name, args, _ = _extract_tool_call(request)
        if not is_mutating_tool_call(tool_name, args):
            return await handler(request)

        snapshot = self._prepare_snapshot(tool_name, args)
        await self.lock_manager.acquire_async(snapshot.resource_key)
        _emit_tnr_event(
            runtime,
            {
                "status": "lock_acquired",
                "resource": snapshot.resource_key,
                "tool_name": tool_name,
            },
        )
        try:
            result = await handler(request)
            post_severity = self._evaluate_post_severity(request, result)
            if post_severity > self.initial_severity:
                undo_ok, undo_msg = self._execute_undo(snapshot)
                status = "rolled_back" if undo_ok else "violation"
                _emit_tnr_event(
                    runtime,
                    {
                        "status": status,
                        "resource": snapshot.resource_key,
                        "tool_name": tool_name,
                        "initial_severity": self.initial_severity,
                        "post_severity": post_severity,
                        "undo_message": undo_msg,
                    },
                )
                return self._append_rollback_notice(result, undo_msg)

            _emit_tnr_event(
                runtime,
                {
                    "status": "committed",
                    "resource": snapshot.resource_key,
                    "tool_name": tool_name,
                    "initial_severity": self.initial_severity,
                    "post_severity": post_severity,
                },
            )
            return result
        finally:
            self.lock_manager.release_async(snapshot.resource_key)
            _emit_tnr_event(
                runtime,
                {
                    "status": "lock_released",
                    "resource": snapshot.resource_key,
                    "tool_name": tool_name,
                },
            )


__all__ = [
    "ALockManager",
    "TransactionalNonRegressionMiddleware",
    "UndoSnapshot",
    "is_mutating_tool_call",
    "synthesize_k8s_inverse_command",
]

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

"""Event hook dispatch for talkops.

Hooks allow external tools to react to agent events. Configured in
``~/.talkops/hooks.json``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from talkops.utils.logger import get_logger

logger = get_logger(__name__)

HOOK_SUBPROCESS_TIMEOUT = 5
"""Seconds before a hook subprocess is killed."""

HOOK_TOOL_OUTPUT_LIMIT = 2000
"""Max chars kept in tool_output payload fields."""

# Standard events
STANDARD_EVENTS = frozenset(
    {
        "session.start",
        "session.end",
        "user.prompt",
        "tool.use",
        "tool.result",
        "tool.error",
        "task.complete",
        "user.name.set",
        "context.compact",
        "permission.request",
    }
)

# Cloud / DevOps events
DEVOPS_EVENTS = frozenset(
    {
        "terraform.plan",
        "terraform.apply",
        "helm.install",
        "kubectl.apply",
        "deploy.start",
        "deploy.complete",
        "aws.resource.create",
        "aws.resource.update",
        "aws.resource.delete",
        "webhook.received",
        "webhook.completed",
    }
)

ALL_KNOWN_EVENTS = STANDARD_EVENTS | DEVOPS_EVENTS


@dataclass(frozen=True)
class HookConfig:
    """A single hook definition from ``hooks.json``."""

    command: list[str]
    """Subprocess command to execute."""

    events: frozenset[str]
    """Events this hook listens to. Empty -> all events."""

    def matches(self, event: str) -> bool:
        """Return True if this hook should fire for event."""
        return len(self.events) == 0 or event in self.events


_hooks: list[HookConfig] = []
_pending_tasks: set[asyncio.Task[None]] = set()
_loaded = False


def _default_hooks_path() -> Path:
    from talkops.config.paths import HOOKS_PATH
    return HOOKS_PATH


def load_hooks(path: Path | None = None) -> list[HookConfig]:
    """Parse hooks.json and populate the module-level hook list."""
    global _hooks, _loaded

    parsed: list[HookConfig] = []

    def _parse_hooks_data(
        data: dict[str, Any],
        source_name: str,
        *,
        skip_events: frozenset[str] = frozenset(),
    ) -> None:
        raw_hooks = data.get("hooks", data)
        if isinstance(raw_hooks, list):
            for entry in raw_hooks:
                if not isinstance(entry, dict):
                    continue
                cmd = entry.get("command")
                if isinstance(cmd, str) and cmd.strip():
                    cmd = ["sh", "-c", cmd.strip()]
                if not isinstance(cmd, list) or not cmd:
                    logger.warning("Skipping hook with invalid command from %s: %r", source_name, entry)
                    continue
                events_raw = entry.get("events", [])
                if not isinstance(events_raw, list):
                    events_raw = []
                filtered_events = frozenset(str(e) for e in events_raw if str(e) not in skip_events)
                if events_raw and not filtered_events:
                    continue
                parsed.append(
                    HookConfig(
                        command=cmd,
                        events=filtered_events,
                    )
                )
        elif isinstance(raw_hooks, dict):
            # Also support Antigravity / Claude Code event-dict hooks format
            for event_name, matchers in raw_hooks.items():
                if str(event_name) in skip_events:
                    continue
                if not isinstance(matchers, list):
                    continue
                for matcher_item in matchers:
                    if not isinstance(matcher_item, dict):
                        continue
                    hook_defs = matcher_item.get("hooks")
                    items = hook_defs if isinstance(hook_defs, list) else [matcher_item]
                    for hook_def in items:
                        if not isinstance(hook_def, dict):
                            continue
                        cmd = hook_def.get("command")
                        if isinstance(cmd, str) and cmd.strip():
                            cmd = ["sh", "-c", cmd.strip()]
                        if not isinstance(cmd, list) or not cmd:
                            continue
                        parsed.append(
                            HookConfig(
                                command=cmd,
                                events=frozenset([str(event_name)]),
                            )
                        )

    hooks_file = path or _default_hooks_path()
    if hooks_file.is_file():
        try:
            data = json.loads(hooks_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _parse_hooks_data(data, str(hooks_file))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to parse hooks config %s: %s", hooks_file, exc)

    # Discover and merge plugin hooks
    try:
        from talkops.plugins.discovery import discover_plugins
        from talkops.plugins.substitution import substitute_json

        plugins = discover_plugins().plugins
        for plugin in plugins:
            hook_candidates: list[Path] = []
            for hp in getattr(plugin.inventory, "hooks_files", ()):
                if hp not in hook_candidates:
                    hook_candidates.append(hp)
            for candidate in (plugin.root / "hooks.json", plugin.root / "hooks" / "hooks.json"):
                if candidate not in hook_candidates:
                    hook_candidates.append(candidate)

            for plugin_hooks_file in hook_candidates:
                if plugin_hooks_file.is_file():
                    try:
                        p_data = json.loads(plugin_hooks_file.read_text(encoding="utf-8"))
                        if isinstance(p_data, dict):
                            p_data = substitute_json(
                                p_data,
                                plugin_root=plugin.root,
                                plugin_data=plugin.data_dir,
                            )
                            _parse_hooks_data(
                                p_data,
                                f"plugin:{plugin.plugin_id}",
                                skip_events=frozenset({"PreToolUse", "PostToolUse"}),
                            )
                    except (json.JSONDecodeError, OSError) as exc:
                        logger.warning("Failed to parse plugin hooks config %s: %s", plugin_hooks_file, exc)
    except Exception as exc:
        logger.debug("Plugin hook discovery skipped: %s", exc)

    _hooks = parsed
    _loaded = True
    return _hooks


def _ensure_loaded() -> list[HookConfig]:
    if not _loaded:
        load_hooks()
    return _hooks


def _sanitise_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    if "tool_output" in out and isinstance(out["tool_output"], str):
        if len(out["tool_output"]) > HOOK_TOOL_OUTPUT_LIMIT:
            out["tool_output"] = out["tool_output"][:HOOK_TOOL_OUTPUT_LIMIT] + "…[truncated]"
    return out


async def _run_hook(hook: HookConfig, event: str, payload: dict[str, Any]) -> None:
    from talkops.hooks.env import sanitize_hook_environ

    env = {**sanitize_hook_environ(), "TALKOPS_HOOK_EVENT": event}
    json_payload = json.dumps(_sanitise_payload(payload))

    try:
        proc = await asyncio.create_subprocess_exec(
            *hook.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
        _, stderr = await asyncio.wait_for(
            proc.communicate(input=json_payload.encode()),
            timeout=HOOK_SUBPROCESS_TIMEOUT,
        )
        if proc.returncode != 0:
            logger.debug(
                "Hook %s exited %d for event %s: %s",
                hook.command[0],
                proc.returncode,
                event,
                stderr.decode(errors="replace")[:200],
            )
    except asyncio.TimeoutError:
        logger.debug("Hook %s timed out after %ds for event %s", hook.command[0], HOOK_SUBPROCESS_TIMEOUT, event)
        with suppress_os_errors():
            proc.kill()  # type: ignore[possibly-undefined]
    except OSError as exc:
        logger.debug("Hook %s failed to start: %s", hook.command[0], exc)


class suppress_os_errors:
    """Represent suppress os errors."""
    def __enter__(self) -> None:
        """Enter the runtime context related to this object."""
        pass

    def __exit__(self, exc_type: type | None, *_: object) -> bool:
        """Exit the runtime context and handle any exception."""
        return exc_type is not None and issubclass(exc_type, OSError)


async def dispatch_hook(event: str, payload: dict[str, Any]) -> None:
    """Dispatch event to all matching hooks and await completion."""
    hooks = _ensure_loaded()
    tasks = [_run_hook(h, event, payload) for h in hooks if h.matches(event)]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def dispatch_hook_fire_and_forget(event: str, payload: dict[str, Any]) -> None:
    """Dispatch event without waiting."""
    hooks = _ensure_loaded()
    matching = [h for h in hooks if h.matches(event)]
    if not matching:
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    for hook in matching:
        task = loop.create_task(_run_hook(hook, event, payload))
        _pending_tasks.add(task)
        task.add_done_callback(_pending_tasks.discard)


async def drain_pending_hooks() -> None:
    """Wait for all fire-and-forget hooks to complete."""
    if _pending_tasks:
        await asyncio.gather(*_pending_tasks, return_exceptions=True)
        _pending_tasks.clear()


__all__ = [
    "ALL_KNOWN_EVENTS",
    "DEVOPS_EVENTS",
    "HOOK_SUBPROCESS_TIMEOUT",
    "HOOK_TOOL_OUTPUT_LIMIT",
    "HookConfig",
    "STANDARD_EVENTS",
    "dispatch_hook",
    "dispatch_hook_fire_and_forget",
    "drain_pending_hooks",
    "load_hooks",
]

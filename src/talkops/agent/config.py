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

"""Agent mode discovery, configuration, and default persistence for TalkOps."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from talkops.config.paths import STATE_DIR as _STATE_DIR
from talkops.config.toml_config import (
    clear_default_agent as clear_default_agent,
)
from talkops.config.toml_config import (
    load_default_agent as _toml_load_default_agent,
)
from talkops.config.toml_config import (
    load_recent_agent as load_recent_agent,
)
from talkops.config.toml_config import (
    save_default_agent as save_default_agent,
)
from talkops.config.toml_config import (
    save_recent_agent as save_recent_agent,
)
from talkops.utils.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_AGENT_FILE = _STATE_DIR / "default_agent.json"

AVAILABLE_AGENTS: list[str] = [
    "agent",
    "conversation_history",
]


def get_available_agent_names() -> list[str]:
    """Return the list of available agent modes and discovered custom agents."""
    from talkops.config import paths as config_paths

    names = list(AVAILABLE_AGENTS)
    search_dirs: list[Path] = [
        config_paths.user_agents_dir(),
        Path.cwd() / ".talkops" / "agents",
    ]
    for base in search_dirs:
        if not base.is_dir():
            continue
        try:
            for child in sorted(base.iterdir()):
                if child.is_dir() and child.name not in names:
                    if (child / "AGENTS.md").is_file():
                        names.append(child.name)
        except OSError:
            pass
    return names


def load_default_agent() -> str | None:
    """Load the persisted default agent name from config.toml or fallbacks."""
    default_agent = _toml_load_default_agent()
    if default_agent:
        return default_agent
    recent_agent = load_recent_agent()
    if recent_agent:
        return recent_agent
    try:
        if _DEFAULT_AGENT_FILE.exists():
            data = json.loads(_DEFAULT_AGENT_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "agent" in data:
                return str(data["agent"])
    except Exception:
        pass
    return None


@dataclass
class AgentContext:
    """Runtime context passed through the agent lifecycle."""

    model: str = ""
    thread_id: str = ""
    turn_id: str = ""
    approval_mode: str = "manual"
    working_dir: str = "."
    interactive: bool = True
    auto_approve: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(init=False)
class CLIContextSchema:
    """Standard context schema for the TalkOps LangGraph execution graph."""

    model: str | None = None
    model_params: dict[str, Any] = field(default_factory=dict)
    profile_overrides: dict[str, Any] = field(default_factory=dict)
    model_context_limit: int | None = None
    approval_mode: str = "manual"
    auto_approve: bool = False
    smart: bool = False
    approval_mode_key: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    offload_tool_call_id: str | None = None
    hooks_snapshot_id: str | None = None
    hooks_server_events: list[str] = field(default_factory=list)
    prompt_id: str | None = None
    reasoning_effort: str | None = None
    cwd: str | None = None
    working_dir: str | None = None
    interactive: bool = True
    workspace: dict[str, Any] = field(default_factory=dict)
    cloud_provider: str | None = None
    cloud_target: str | None = None
    cloud_execution_mode: str = "read_only"
    aws_profile: str | None = None
    aws_region: str | None = None
    source_addon: str | None = None
    webhook_provider: str | None = None
    webhook_event_type: str | None = None
    webhook_incident_id: str | None = None
    channel: str | None = None
    webhook_sre_mode: bool | None = None
    _btw_side_question: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        model: str | None = None,
        model_params: dict[str, Any] | None = None,
        profile_overrides: dict[str, Any] | None = None,
        model_context_limit: int | None = None,
        approval_mode: str = "manual",
        auto_approve: bool = False,
        smart: bool = False,
        approval_mode_key: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        offload_tool_call_id: str | None = None,
        hooks_snapshot_id: str | None = None,
        hooks_server_events: list[str] | None = None,
        prompt_id: str | None = None,
        reasoning_effort: str | None = None,
        cwd: str | None = None,
        working_dir: str | None = None,
        interactive: bool = True,
        workspace: dict[str, Any] | None = None,
        cloud_provider: str | None = None,
        cloud_target: str | None = None,
        cloud_execution_mode: str = "read_only",
        aws_profile: str | None = None,
        aws_region: str | None = None,
        source_addon: str | None = None,
        webhook_provider: str | None = None,
        webhook_event_type: str | None = None,
        webhook_incident_id: str | None = None,
        channel: str | None = None,
        webhook_sre_mode: bool | None = None,
        _btw_side_question: bool = False,
        extra: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize CLIContextSchema."""
        resolved_cwd = cwd or working_dir
        ws = dict(workspace) if isinstance(workspace, dict) else {}
        if not resolved_cwd and ws and isinstance(ws.get("cwd"), str):
            resolved_cwd = str(ws["cwd"])

        self.model = model
        self.model_params = dict(model_params) if isinstance(model_params, dict) else {}
        self.profile_overrides = dict(profile_overrides) if isinstance(profile_overrides, dict) else {}
        self.model_context_limit = (
            model_context_limit
            if isinstance(model_context_limit, int) and not isinstance(model_context_limit, bool)
            else None
        )
        self.approval_mode = approval_mode or "manual"
        self.auto_approve = bool(auto_approve)
        self.smart = bool(smart)
        self.approval_mode_key = approval_mode_key
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.offload_tool_call_id = offload_tool_call_id
        self.hooks_snapshot_id = hooks_snapshot_id
        self.hooks_server_events = list(hooks_server_events) if isinstance(hooks_server_events, list) else []
        self.prompt_id = prompt_id
        self.reasoning_effort = reasoning_effort
        self.cwd = resolved_cwd
        self.working_dir = working_dir or resolved_cwd
        self.interactive = bool(interactive)
        self.workspace = ws
        self.cloud_provider = cloud_provider
        self.cloud_target = cloud_target
        self.cloud_execution_mode = cloud_execution_mode or "read_only"
        self.aws_profile = aws_profile
        self.aws_region = aws_region
        self.source_addon = source_addon
        self.webhook_provider = webhook_provider
        self.webhook_event_type = webhook_event_type
        self.webhook_incident_id = webhook_incident_id
        self.channel = channel
        self.webhook_sre_mode = webhook_sre_mode if isinstance(webhook_sre_mode, bool) else None
        self._btw_side_question = bool(_btw_side_question)
        merged_extra = dict(extra) if isinstance(extra, dict) else {}
        if kwargs:
            merged_extra.update(kwargs)
        self.extra = merged_extra

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-compatible lookup across schema attributes and extra payload."""
        if hasattr(self, key):
            val = getattr(self, key)
            return default if val is None else val
        return self.extra.get(key, default)

    def __getitem__(self, key: str) -> Any:
        """Return the item for the given key or index."""
        if hasattr(self, key):
            return getattr(self, key)
        return self.extra[key]

    def __contains__(self, key: object) -> bool:
        """Return whether the given item is present."""
        if not isinstance(key, str):
            return False
        return (hasattr(self, key) and getattr(self, key) is not None) or (key in self.extra)

    @classmethod
    def from_payload(cls, payload: object) -> CLIContextSchema | None:
        """Coerce a run's `context=` payload into this schema."""
        if isinstance(payload, cls):
            return payload
        if hasattr(payload, "__dict__") and not isinstance(payload, dict):
            payload = dict(payload.__dict__)
        if not isinstance(payload, dict):
            return None
        data = dict(payload)

        def _str(key: str) -> str | None:
            val = data.get(key)
            return val if isinstance(val, str) else None

        def _mapping(key: str) -> dict[str, Any]:
            val = data.get(key)
            return dict(val) if isinstance(val, dict) else {}

        raw_limit = data.get("model_context_limit")
        limit = raw_limit if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) else None

        raw_events = data.get("hooks_server_events")
        events = [e for e in raw_events if isinstance(e, str)] if isinstance(raw_events, list) else []

        raw_auto = data.get("auto_approve")
        auto_approve = raw_auto if isinstance(raw_auto, bool) else False

        raw_mode = _str("approval_mode")
        raw_smart = data.get("smart")
        smart = raw_smart if isinstance(raw_smart, bool) else (raw_mode == "smart" if raw_mode else False)
        if not raw_mode:
            if smart:
                raw_mode = "smart"
            elif auto_approve:
                raw_mode = "auto"
            else:
                raw_mode = "manual"

        cwd = _str("cwd") or _str("working_dir")
        workspace = _mapping("workspace")
        if not cwd and workspace and isinstance(workspace.get("cwd"), str):
            cwd = str(workspace["cwd"])

        known_keys = {
            "model",
            "model_params",
            "profile_overrides",
            "model_context_limit",
            "approval_mode",
            "auto_approve",
            "smart",
            "approval_mode_key",
            "thread_id",
            "turn_id",
            "offload_tool_call_id",
            "hooks_snapshot_id",
            "hooks_server_events",
            "prompt_id",
            "reasoning_effort",
            "cwd",
            "working_dir",
            "interactive",
            "workspace",
            "cloud_provider",
            "cloud_target",
            "cloud_execution_mode",
            "aws_profile",
            "aws_region",
            "source_addon",
            "webhook_provider",
            "webhook_event_type",
            "webhook_incident_id",
            "channel",
            "webhook_sre_mode",
            "_btw_side_question",
            "extra",
        }
        extra = _mapping("extra")
        for k, v in data.items():
            if k not in known_keys:
                extra[k] = v

        raw_interactive = data.get("interactive")
        interactive = raw_interactive if isinstance(raw_interactive, bool) else True

        raw_sre_mode = data.get("webhook_sre_mode")
        sre_mode = raw_sre_mode if isinstance(raw_sre_mode, bool) else None

        return cls(
            model=_str("model"),
            model_params=_mapping("model_params"),
            profile_overrides=_mapping("profile_overrides"),
            model_context_limit=limit,
            approval_mode=raw_mode,
            auto_approve=auto_approve,
            smart=smart,
            approval_mode_key=_str("approval_mode_key"),
            thread_id=_str("thread_id"),
            turn_id=_str("turn_id"),
            offload_tool_call_id=_str("offload_tool_call_id"),
            hooks_snapshot_id=_str("hooks_snapshot_id"),
            hooks_server_events=events,
            prompt_id=_str("prompt_id"),
            reasoning_effort=_str("reasoning_effort"),
            cwd=cwd,
            working_dir=_str("working_dir") or cwd,
            interactive=interactive,
            workspace=workspace,
            cloud_provider=_str("cloud_provider"),
            cloud_target=_str("cloud_target"),
            cloud_execution_mode=_str("cloud_execution_mode") or "read_only",
            aws_profile=_str("aws_profile"),
            aws_region=_str("aws_region"),
            source_addon=_str("source_addon"),
            webhook_provider=_str("webhook_provider"),
            webhook_event_type=_str("webhook_event_type"),
            webhook_incident_id=_str("webhook_incident_id"),
            channel=_str("channel"),
            webhook_sre_mode=sre_mode,
            _btw_side_question=bool(data.get("_btw_side_question")),
            extra=extra,
        )


AgentContextSchema = CLIContextSchema

__all__ = [
    "AVAILABLE_AGENTS",
    "AgentContext",
    "AgentContextSchema",
    "CLIContextSchema",
    "clear_default_agent",
    "get_available_agent_names",
    "load_default_agent",
    "load_recent_agent",
    "save_default_agent",
    "save_recent_agent",
]

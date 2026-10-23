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

"""``WebhookSREMiddleware`` — Dynamic Autonomous SRE & Incident RCA prompt middleware.

When the TalkOps central server (`talkops serve`) is triggered via an inbound
webhook (Alertmanager, PagerDuty, Datadog, CloudWatch, GitHub/GitLab, custom)
or an incident response channel (Slack, MS Teams, Discord, Telegram, Opsgenie,
ServiceNow), this middleware swaps the base system prompt from the general
Dual-Engine Coding + DevOps prompt to the dedicated **Autonomous SRE & Cloud
Incident Root Cause Analysis (RCA)** prompt (`webhook_sre_prompt.md`).

All other middlewares (`FilesystemMiddleware`, `SummarizationMiddleware`,
`ConfigurableModelMiddleware`, `MemoryMiddleware`, `PluginSkillsMiddleware`,
`CustomCommandsMiddleware`, `CodeInterpreterMiddleware`, `LocalContextMiddleware`,
`BackgroundCommandMiddleware`, `SubagentsMiddleware`, `UnifiedSystemMessageMiddleware`),
skills, subagent delegation (`task` & `js_eval`), and safety guardrails remain
attached in the exact same order.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import SystemMessage

from talkops.middleware.registry import register_middleware
from talkops.prompts.system import get_webhook_sre_system_prompt
from talkops.state.webhook_sre_channels import WebhookSREChannels
from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from langgraph.runtime import Runtime

logger = get_logger(__name__)

# Recognised addon / source identifiers that represent webhook or incident channel triggers
_WEBHOOK_AND_CHANNEL_SOURCES: frozenset[str] = frozenset(
    {
        "webhook",
        "webhooks",
        "channel",
        "channels",
        "chatops",
        "incident",
        "alertmanager",
        "prometheus",
        "pagerduty",
        "datadog",
        "cloudwatch",
        "opsgenie",
        "servicenow",
        "slack",
        "teams",
        "msteams",
        "discord",
        "telegram",
    }
)

# Regex matching the header emitted by `NormalizedWebhookEvent.format_agent_prompt()`
_WEBHOOK_ALERT_HEADER_RE = re.compile(
    r"^\[WEBHOOK ALERT\]\s*Provider:\s*(?P<provider>[^|\n]+?)(?:\s*\|\s*Event:\s*(?P<event_type>[^\n]+))?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Markers injected by core/downstream middlewares after the base system prompt
_DOWNSTREAM_PROMPT_MARKERS: tuple[str, ...] = (
    "\n\n## Shell paths vs. virtual paths",
    "\n\n<conversation_transcript>",
    "\n\n<agent_memory>",
    "\n\n## Skills System",
    "\n\n<custom_commands>",
    "\n\n### Interpreter",
    "\n\n## Local Context",
    "\n\n## Non-Blocking Background Command Execution",
    "\n\n## Subagent Delegation & Orchestration Architecture",
)


def _extract_message_text(msg: object) -> str:
    """Extract plain text from a LangChain message object or dict."""
    if isinstance(msg, Mapping):
        role = str(msg.get("type") or msg.get("role") or "").lower()
        content = msg.get("content", "")
    else:
        role = str(getattr(msg, "type", "") or getattr(msg, "role", "")).lower()
        content = getattr(msg, "content", "")

    if role not in {"human", "user", "humanmessage"}:
        return ""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return ""


def _ctx_lookup(ctx: Any, key: str) -> Any:
    """Safely look up *key* on a dict, `CLIContextSchema`, or object."""
    if ctx is None:
        return None
    if isinstance(ctx, Mapping):
        return ctx.get(key)
    getter = getattr(ctx, "get", None)
    if callable(getter):
        try:
            return getter(key)
        except Exception:
            pass
    val = getattr(ctx, key, None)
    if val is not None:
        return val
    extra = getattr(ctx, "extra", None)
    if isinstance(extra, Mapping):
        return extra.get(key)
    return None


def extract_webhook_metadata(
    state: Any = None,
    runtime: Any = None,
    messages: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Extract normalized webhook/channel metadata from state, runtime context/config, and messages."""
    meta: dict[str, Any] = {}

    if isinstance(state, Mapping):
        persisted = state.get("_webhook_metadata")
        if isinstance(persisted, Mapping):
            meta.update({k: v for k, v in persisted.items() if v is not None})

    ctx = getattr(runtime, "context", None) if runtime is not None else None
    cfg = getattr(runtime, "config", None) if runtime is not None else None
    raw_meta = cfg.get("metadata") if isinstance(cfg, Mapping) else None
    cfg_meta: Mapping[str, Any] = raw_meta if isinstance(raw_meta, Mapping) else {}
    raw_conf = cfg.get("configurable") if isinstance(cfg, Mapping) else None
    cfg_conf: Mapping[str, Any] = raw_conf if isinstance(raw_conf, Mapping) else {}

    provider = (
        _ctx_lookup(ctx, "webhook_provider")
        or cfg_meta.get("provider")
        or cfg_meta.get("webhook_provider")
        or cfg_conf.get("webhook_provider")
    )
    event_type = (
        _ctx_lookup(ctx, "webhook_event_type")
        or cfg_meta.get("event_type")
        or cfg_meta.get("webhook_event_type")
        or cfg_conf.get("webhook_event_type")
    )
    incident_id = (
        _ctx_lookup(ctx, "webhook_incident_id")
        or cfg_meta.get("incident_id")
        or cfg_meta.get("webhook_incident_id")
        or cfg_conf.get("webhook_incident_id")
    )
    source_addon = (
        _ctx_lookup(ctx, "source_addon")
        or cfg_meta.get("source_addon")
        or cfg_conf.get("source_addon")
    )
    source = (
        _ctx_lookup(ctx, "source")
        or cfg_meta.get("source")
        or cfg_conf.get("source")
    )
    owner = (
        _ctx_lookup(ctx, "owner")
        or cfg_meta.get("owner")
        or cfg_conf.get("owner")
    )
    channel = (
        _ctx_lookup(ctx, "channel")
        or cfg_meta.get("channel")
        or cfg_conf.get("channel")
    )
    cloud_provider = _ctx_lookup(ctx, "cloud_provider") or cfg_conf.get("cloud_provider")

    if provider and isinstance(provider, str):
        meta["webhook_provider"] = provider.strip()
    if event_type and isinstance(event_type, str):
        meta["webhook_event_type"] = event_type.strip()
    if incident_id and isinstance(incident_id, str):
        meta["webhook_incident_id"] = incident_id.strip()
    if source_addon and isinstance(source_addon, str):
        meta["source_addon"] = source_addon.strip()
    if source and isinstance(source, str):
        meta["source"] = source.strip()
    if owner and isinstance(owner, str):
        meta["owner"] = owner.strip()
    if channel and isinstance(channel, str):
        meta["channel"] = channel.strip()
    if cloud_provider and isinstance(cloud_provider, str):
        meta["cloud_provider"] = cloud_provider.strip()

    # Inspect human messages for `[WEBHOOK ALERT] Provider: ... | Event: ...`
    all_msgs: list[Any] = []
    if isinstance(state, Mapping) and isinstance(state.get("messages"), Sequence):
        all_msgs.extend(state["messages"])
    if messages:
        all_msgs.extend(messages)

    for msg in all_msgs:
        text = _extract_message_text(msg).strip()
        if not text:
            continue
        match = _WEBHOOK_ALERT_HEADER_RE.search(text)
        if match:
            meta["has_webhook_alert_prompt"] = True
            if "webhook_provider" not in meta and match.group("provider"):
                meta["webhook_provider"] = match.group("provider").strip().lower()
            if "webhook_event_type" not in meta and match.group("event_type"):
                meta["webhook_event_type"] = match.group("event_type").strip()
            break
        if text.startswith(("[WEBHOOK ALERT]", "[INCIDENT ALERT]", "[CHANNEL ALERT]")):
            meta["has_webhook_alert_prompt"] = True
            break

    return meta


def is_webhook_sre_turn(
    state: Any = None,
    runtime: Any = None,
    messages: Sequence[Any] | None = None,
) -> bool:
    """Return True when the current execution turn is triggered via a webhook or incident channel."""
    if isinstance(state, Mapping):
        if state.get("_webhook_sre_mode") is True:
            return True
        if state.get("_webhook_sre_mode") is False:
            return False

    ctx = getattr(runtime, "context", None) if runtime is not None else None
    if ctx is not None:
        explicit_flag = _ctx_lookup(ctx, "webhook_sre_mode")
        if explicit_flag is None:
            explicit_flag = _ctx_lookup(ctx, "is_webhook")
        if isinstance(explicit_flag, bool):
            return explicit_flag

    meta = extract_webhook_metadata(state=state, runtime=runtime, messages=messages)
    if meta.get("has_webhook_alert_prompt"):
        return True
    if meta.get("webhook_provider") or meta.get("webhook_event_type") or meta.get("webhook_incident_id"):
        return True
    if meta.get("channel"):
        return True

    for key in ("source_addon", "source", "owner"):
        val = str(meta.get(key) or "").strip().lower()
        if val in _WEBHOOK_AND_CHANNEL_SOURCES:
            return True

    return False


def swap_system_message_to_sre(
    system_message: SystemMessage | None,
    sre_prompt: str,
) -> SystemMessage:
    """Replace the base system prompt in *system_message* with *sre_prompt*.

    Preserves all content blocks and sections appended by core and downstream
    middlewares (`## Shell paths vs. virtual paths`, `<conversation_transcript>`,
    `<agent_memory>`, `## Skills System`, `<custom_commands>`, `### Interpreter`,
    `## Local Context`, `## Non-Blocking Background Command Execution`, and
    `## Subagent Delegation & Orchestration Architecture`).
    """
    clean_sre = sre_prompt.strip()
    if system_message is None:
        return SystemMessage(content_blocks=[{"type": "text", "text": clean_sre}])

    blocks: list[Any] = list(getattr(system_message, "content_blocks", None) or [])
    if len(blocks) > 1:
        first_block: dict[str, Any] = dict(blocks[0]) if isinstance(blocks[0], Mapping) else {"type": "text"}
        first_text = str(first_block.get("text", ""))
        # Check if any downstream marker was merged into block 0
        earliest_idx: int | None = None
        for marker in _DOWNSTREAM_PROMPT_MARKERS:
            idx = first_text.find(marker)
            if idx != -1 and (earliest_idx is None or idx < earliest_idx):
                earliest_idx = idx
        if earliest_idx is not None:
            first_block["text"] = clean_sre + first_text[earliest_idx:]
        else:
            first_block["text"] = clean_sre
        blocks[0] = first_block
        return SystemMessage(content_blocks=blocks)

    # Single-block or string-backed SystemMessage: preserve everything from the first
    # downstream middleware marker onward.
    raw_text = (
        str(blocks[0].get("text", ""))
        if blocks and isinstance(blocks[0], Mapping)
        else str(getattr(system_message, "content", "") or "")
    )
    earliest_marker_idx: int | None = None
    for marker in _DOWNSTREAM_PROMPT_MARKERS:
        idx = raw_text.find(marker)
        if idx != -1 and (earliest_marker_idx is None or idx < earliest_marker_idx):
            earliest_marker_idx = idx

    if earliest_marker_idx is not None:
        combined = clean_sre + raw_text[earliest_marker_idx:]
    else:
        combined = clean_sre

    if blocks and isinstance(blocks[0], Mapping):
        updated_block: dict[str, Any] = dict(blocks[0])
        updated_block["text"] = combined
        updated_blocks: list[Any] = [updated_block]
        return SystemMessage(content_blocks=updated_blocks)
    return SystemMessage(content=combined)


@register_middleware(name="webhook_sre")
class WebhookSREMiddleware(AgentMiddleware[WebhookSREChannels, Any]):
    """Dynamically swap the base system prompt to Autonomous SRE / Incident RCA mode on webhook & channel runs."""

    state_schema = WebhookSREChannels

    def __init__(
        self,
        *,
        assistant_id: str = "talkops",
        cwd: str | Path | None = None,
        fs_tools: Sequence[str] | None = None,
        model_name: str | None = None,
        model_provider: str | None = None,
        model_context_limit: int | None = None,
        model_unsupported_modalities: frozenset[str] = frozenset(),
        cloud_provider: str = "aws",
        sandbox_type: str | None = "local",
        has_search: bool = True,
        sre_system_prompt: str | None = None,
    ) -> None:
        """Initialize WebhookSREMiddleware."""
        self._assistant_id = assistant_id
        self._cwd = cwd
        self._fs_tools = fs_tools
        self._model_name = model_name
        self._model_provider = model_provider
        self._model_context_limit = model_context_limit
        self._model_unsupported_modalities = model_unsupported_modalities
        self._cloud_provider = cloud_provider
        self._sandbox_type = sandbox_type
        self._has_search = has_search
        self._explicit_sre_prompt = sre_system_prompt

    def _build_sre_prompt(
        self,
        state: Any = None,
        runtime: Any = None,
        messages: Sequence[Any] | None = None,
    ) -> str:
        if self._explicit_sre_prompt:
            return self._explicit_sre_prompt

        meta = extract_webhook_metadata(state=state, runtime=runtime, messages=messages)
        ctx = getattr(runtime, "context", None) if runtime is not None else None
        effective_cwd = _ctx_lookup(ctx, "cwd") or _ctx_lookup(ctx, "working_dir") or self._cwd
        effective_cloud = meta.get("cloud_provider") or self._cloud_provider or "aws"

        return get_webhook_sre_system_prompt(
            assistant_id=self._assistant_id,
            interactive=False,
            cwd=effective_cwd,
            fs_tools=self._fs_tools,
            model_name=self._model_name,
            model_provider=self._model_provider,
            model_context_limit=self._model_context_limit,
            model_unsupported_modalities=self._model_unsupported_modalities,
            cloud_provider=effective_cloud,
            sandbox_type=self._sandbox_type,
            has_search=self._has_search,
            webhook_provider=meta.get("webhook_provider"),
            webhook_event_type=meta.get("webhook_event_type"),
            webhook_incident_id=meta.get("webhook_incident_id"),
            source_addon=meta.get("source_addon") or meta.get("source"),
            channel=meta.get("channel"),
        )

    def before_agent(
        self,
        state: WebhookSREChannels,
        runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        """Detect webhook/channel runs at turn start and persist SRE mode in thread state."""
        if not is_webhook_sre_turn(state, runtime):
            return None
        meta = extract_webhook_metadata(state=state, runtime=runtime)
        updates: dict[str, Any] = {}
        if state.get("_webhook_sre_mode") is not True:
            updates["_webhook_sre_mode"] = True
        if meta and state.get("_webhook_metadata") != meta:
            updates["_webhook_metadata"] = meta
        return updates or None

    async def abefore_agent(
        self,
        state: WebhookSREChannels,
        runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        """Asynchronously detect webhook/channel runs at turn start and persist SRE mode in thread state."""
        return self.before_agent(state, runtime)

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | ExtendedModelResponse[Any]:
        """Swap the base system prompt to Autonomous SRE / RCA mode when active."""
        runtime = getattr(request, "runtime", None)
        messages = getattr(request, "messages", None)
        if is_webhook_sre_turn(request.state, runtime, messages):
            sre_prompt = self._build_sre_prompt(request.state, runtime, messages)
            swapped_msg = swap_system_message_to_sre(request.system_message, sre_prompt)
            request = request.override(system_message=swapped_msg)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | ExtendedModelResponse[Any]:
        """Asynchronously swap the base system prompt to Autonomous SRE / RCA mode when active."""
        runtime = getattr(request, "runtime", None)
        messages = getattr(request, "messages", None)
        if is_webhook_sre_turn(request.state, runtime, messages):
            sre_prompt = self._build_sre_prompt(request.state, runtime, messages)
            swapped_msg = swap_system_message_to_sre(request.system_message, sre_prompt)
            request = request.override(system_message=swapped_msg)
        return await handler(request)


__all__ = [
    "WebhookSREMiddleware",
    "extract_webhook_metadata",
    "is_webhook_sre_turn",
    "swap_system_message_to_sre",
]

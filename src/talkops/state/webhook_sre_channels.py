"""State channels for Webhook & Incident Channel Autonomous SRE / RCA execution."""

from __future__ import annotations

from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.types import AgentState, PrivateStateAttr


class WebhookSREChannels(AgentState):
    """Private state channels persisted when a thread is triggered via a webhook or incident channel."""

    _webhook_sre_mode: NotRequired[Annotated[bool | None, PrivateStateAttr]]
    """True when the conversation thread is operating in Autonomous SRE / Incident RCA mode."""

    _webhook_metadata: NotRequired[Annotated[dict[str, Any] | None, PrivateStateAttr]]
    """Normalized webhook or incident channel metadata (provider, event_type, incident_id, channel, source_addon)."""


__all__ = ["WebhookSREChannels"]

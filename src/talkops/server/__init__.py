"""LangGraph dev server integration package."""

from typing import TYPE_CHECKING, Any

from talkops._constants import SERVER_ENV_PREFIX
from talkops.server._server_config import ServerConfig
from talkops.server.server import ServerProcess, find_free_port, generate_langgraph_json

if TYPE_CHECKING:
    from talkops.server.asgi import create_app, create_server_app
    from talkops.server.webhooks_app import (
        create_webhooks_app,
        get_webhook_dispatcher,
        set_webhook_dispatcher,
    )

__all__ = [
    "SERVER_ENV_PREFIX",
    "ServerConfig",
    "ServerProcess",
    "create_app",
    "create_server_app",
    "create_webhooks_app",
    "find_free_port",
    "generate_langgraph_json",
    "get_webhook_dispatcher",
    "set_webhook_dispatcher",
]


def __getattr__(name: str) -> Any:
    if name in ("create_webhooks_app", "get_webhook_dispatcher", "set_webhook_dispatcher"):
        from talkops.server import webhooks_app

        return getattr(webhooks_app, name)
    if name in ("create_app", "create_server_app"):
        from talkops.server import asgi

        return getattr(asgi, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


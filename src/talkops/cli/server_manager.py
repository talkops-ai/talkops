"""Server lifecycle manager for TalkOps CLI."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack, contextmanager
import os
from pathlib import Path
from typing import Any, Generator

from talkops.server import ServerConfig, ServerProcess
from talkops.ui.remote_client import InProcessAgent, RemoteAgent
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


def _use_in_process_server(port: int = 0, explicit: bool | None = None) -> bool:
    """Return True if TalkOps should run the LangGraph backend in-process."""
    if explicit is not None:
        return bool(explicit)
    if port != 0:
        return False
    raw = os.environ.get("TALKOPS_IN_PROCESS_SERVER", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


class InProcessServerHandle:
    """In-process backend lifecycle handle matching ``ServerProcess``'s interface.

    Compiles and runs the LangGraph ``Pregel`` graph, SQLite checkpointer, and
    ``InMemoryStore`` directly inside the CLI process without spawning a separate
    ``langgraph dev`` HTTP server subprocess.
    """

    is_in_process: bool = True

    def __init__(
        self,
        config: ServerConfig,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.config = config
        self.host = host
        self.port = port
        self.url = "inprocess://local"
        self.process: Any = None
        self.graph: Any = None
        self.backend: Any = None
        self.checkpointer: Any = None
        self.store: Any = None
        self._exit_stack: AsyncExitStack | None = None

    def _apply_config_to_env(self) -> None:
        for key, value in self.config.to_env().items():
            if value is not None:
                os.environ[key] = str(value)

    async def astart(self, timeout: float = 60.0) -> str:
        """Initialize the SQLite checkpointer, store, and compiled graph in-process."""
        del timeout
        self._apply_config_to_env()

        if "PYTEST_CURRENT_TEST" not in os.environ or os.environ.get("TALKOPS_ENFORCE_CLI_AUTH_IN_TESTS") == "1":
            from talkops.auth.storage import get_credential_storage
            from talkops.server.auth import resolve_user_from_headers
            from talkops.state.remote_session import (
                _is_jwt_expired,
                _looks_like_jwt,
                exchange_api_key_for_temporary_token,
                start_background_token_refresh_task,
            )

            stored = get_credential_storage().load_session()
            raw_cfg_token = (
                self.config.api_key
                or os.environ.get("TALKOPS_API_KEY", "").strip()
                or os.environ.get("TALKOPS_SERVER_API_KEY", "").strip()
            )
            long_lived_api_key = ""
            if stored is not None and stored.api_key and not _looks_like_jwt(stored.api_key):
                long_lived_api_key = stored.api_key.strip()
            elif raw_cfg_token and not _looks_like_jwt(raw_cfg_token):
                long_lived_api_key = raw_cfg_token.strip()

            token = raw_cfg_token
            if stored is not None:
                if stored.access_token and stored.is_access_token_valid() and (
                    not token
                    or token == stored.api_key
                    or token == stored.access_token
                    or (_looks_like_jwt(token) and _is_jwt_expired(token))
                ):
                    token = stored.access_token.strip()
                elif (not token or (_looks_like_jwt(token) and _is_jwt_expired(token))) and long_lived_api_key:
                    token = long_lived_api_key

            if not token:
                raise PermissionError(
                    "401 Unauthorized: Control Plane token required. Run 'talkops auth login' first."
                )

            cp_url = os.environ.get("TALKOPS_CONTROL_PLANE_URL", "").strip() or (
                stored.control_plane_url if stored else ""
            )

            if not _looks_like_jwt(token) or _is_jwt_expired(token):
                key_for_exchange = long_lived_api_key or token
                if cp_url and not _looks_like_jwt(key_for_exchange):
                    exchanged = await exchange_api_key_for_temporary_token(
                        None,
                        api_key=key_for_exchange,
                        user_id=(stored.user_id if stored else None),
                        control_plane_url=cp_url,
                        force_refresh=True,
                    )
                    if exchanged:
                        token = exchanged
                        self.config.api_key = exchanged
                        os.environ["TALKOPS_API_KEY"] = exchanged
                    elif long_lived_api_key:
                        token = long_lived_api_key

            try:
                await resolve_user_from_headers({"authorization": f"Bearer {token}"}, path="/threads")
            except Exception as auth_exc:
                if long_lived_api_key:
                    refreshed = None
                    if cp_url:
                        refreshed = await exchange_api_key_for_temporary_token(
                            None,
                            api_key=long_lived_api_key,
                            user_id=(stored.user_id if stored else None),
                            control_plane_url=cp_url,
                            force_refresh=True,
                        )
                    fallback_token = refreshed or long_lived_api_key
                    self.config.api_key = fallback_token
                    os.environ["TALKOPS_API_KEY"] = fallback_token
                    await resolve_user_from_headers(
                        {"authorization": f"Bearer {fallback_token}", "x-api-key": long_lived_api_key},
                        path="/threads",
                    )
                else:
                    raise auth_exc

            start_background_token_refresh_task()

        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception:
                pass

        stack = AsyncExitStack()
        self._exit_stack = stack

        try:
            from langgraph.store.memory import InMemoryStore
            from talkops.state.session import TalkOpsCheckpointer, get_db_path

            db_path = str(get_db_path())
            os.environ["TALKOPS_SERVER_DB_PATH"] = db_path
            self.checkpointer = await stack.enter_async_context(
                TalkOpsCheckpointer.from_conn_string(db_path)
            )
            self.store = InMemoryStore()
        except Exception as exc:
            logger.warning("In-process checkpointer init fallback: %s", exc)
            from langgraph.checkpoint.memory import InMemorySaver
            from langgraph.store.memory import InMemoryStore

            self.checkpointer = InMemorySaver()
            self.store = InMemoryStore()

        from talkops.server.server_graph import _build_default_runtime_async

        logger.info(
            "Starting in-process agent runtime (cwd=%s, model=%s)",
            self.config.cwd,
            self.config.model,
        )
        runtime = await _build_default_runtime_async(
            checkpointer=self.checkpointer,
            store=self.store,
        )
        self.graph = runtime.agent
        self.backend = runtime.backend
        logger.info("In-process agent graph 'agent' is ready (%s)", self.url)
        return self.url

    def start(self, timeout: float = 60.0) -> str:
        """Synchronous compatibility wrapper around ``astart``."""
        return asyncio.run(self.astart(timeout=timeout))

    async def astop(self) -> None:
        """Close the in-process checkpointer context cleanly."""
        stack = self._exit_stack
        self._exit_stack = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as exc:
                logger.debug("Error closing InProcessServerHandle stack: %s", exc)

    def stop(self) -> None:
        """Stop the in-process handle and schedule async checkpointer teardown if needed."""
        stack = self._exit_stack
        self._exit_stack = None
        if stack is None:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(stack.aclose())
        except RuntimeError:
            try:
                asyncio.run(stack.aclose())
            except Exception:
                pass

    async def restart(self, timeout: float = 60.0) -> str:
        """Rebuild the in-process agent graph (used by `/reload` and `/restart`)."""
        await self.astop()
        return await self.astart(timeout=timeout)

    def create_client(self) -> InProcessAgent:
        """Return an ``InProcessAgent`` bound to the compiled graph."""
        return InProcessAgent(
            self.graph,
            checkpointer=self.checkpointer,
            store=self.store,
            graph_name="agent",
            cwd=self.config.cwd,
        )


@contextmanager
def server_session(config: ServerConfig) -> Generator[RemoteAgent, None, None]:
    """Connect to a remote server URL (if configured) or launch a local server subprocess."""
    if getattr(config, "server_url", None):
        from talkops.state.remote_session import set_remote_client_context

        set_remote_client_context(
            str(config.server_url),
            api_key=getattr(config, "api_key", None),
            user_id=getattr(config, "user_id", None),
        )
        agent = RemoteAgent(
            str(config.server_url),
            api_key=getattr(config, "api_key", None),
            user_id=getattr(config, "user_id", None),
            graph_name="agent",
        )
        yield agent
        return

    proc = ServerProcess(config)
    try:
        url = proc.start()
        agent = RemoteAgent(url)
        yield agent
    finally:
        proc.stop()


async def start_server_and_get_agent(
    *,
    assistant_id: str = "talkops",
    model_name: str | None = None,
    interactive: bool = True,
    auto_approve: bool = False,
    cwd: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    on_spawn: Callable[[Any], None] | None = None,
    in_process: bool | None = None,
    **kwargs: Any,
) -> tuple[RemoteAgent, Any]:
    """Start the TalkOps backend (in-process by default, subprocess, or remote server) and return a client."""
    from talkops.project_utils import ProjectContext

    project_context = (
        ProjectContext.from_user_cwd(Path(cwd))
        if cwd is not None
        else ProjectContext.from_user_cwd(Path.cwd())
    )
    resolved_model = model_name or kwargs.pop("model", None)
    if not resolved_model:
        try:
            from talkops.model.factory import _get_default_model_spec

            resolved_model = _get_default_model_spec()
        except Exception:
            resolved_model = None

    config = ServerConfig.from_cli_args(
        project_context=project_context,
        assistant_id=assistant_id,
        model=resolved_model,
        interactive=interactive,
        approval_mode="never" if auto_approve else "auto",
        **kwargs,
    )

    if config.server_url:
        from talkops.state.remote_session import set_remote_client_context

        set_remote_client_context(
            config.server_url,
            api_key=config.api_key,
            user_id=config.user_id,
        )
        agent = RemoteAgent(
            url=config.server_url,
            api_key=config.api_key,
            user_id=config.user_id,
            graph_name="agent",
        )
        return agent, None

    # Schedule background database maintenance on launch (DV-08)
    try:
        from talkops.state.session import SessionManager

        loop = asyncio.get_running_loop()
        loop.create_task(SessionManager.maintain_database())
    except Exception:
        pass

    if _use_in_process_server(port=port, explicit=in_process):
        handle = InProcessServerHandle(config, host=host, port=port)
        if on_spawn is not None:
            try:
                on_spawn(handle)
            except Exception:
                pass
        await handle.astart()
        return handle.create_client(), handle

    server = ServerProcess(config, host=host, port=port)
    if on_spawn is not None:
        try:
            on_spawn(server)
        except Exception:
            pass

    url = await server.astart()
    agent = RemoteAgent(
        url=url,
        api_key=config.api_key,
        user_id=config.user_id,
        graph_name="agent",
    )
    return agent, server



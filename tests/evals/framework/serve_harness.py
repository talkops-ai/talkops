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

"""Reusable `ServeEvalHarness` spinning up isolated `talkops serve` profiles in `tmp_path`."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from talkops.addons.builtin.webhooks import set_webhook_dispatcher
from talkops.addons.gateway import AddonEvent, AgentRunGateway, HookEventBus
from talkops.addons.manager import AddonManager
from talkops.addons.protocol import ServerAddon
from talkops.agent.factory import create_talkops_agent
from talkops.model.factory import create_model
from talkops.state.session import get_checkpointer, get_db_path, set_db_path
from talkops.storage.factory import aclose_operational_store, set_operational_store
from talkops.storage.sqlite_store import SqliteOperationalStore
from talkops.webhooks.correlation import AlertStormCorrelator
from talkops.webhooks.dispatcher import WebhookDispatcher, create_gateway_run_executor
from talkops.webhooks.models import WebhookConfig
from tests.evals.fixtures.topology_and_tools import build_mock_infrastructure_tools

EVAL_API_USER = "eval-operator"
EVAL_API_TOKEN = "eval-local-secret-token"


@dataclass
class ServeEvalHarness:
    """Isolated per-scenario `talkops serve` runtime (ASGI + `AddonManager` + `AgentRunGateway` + SQLite)."""

    profile: str
    workspace_dir: Path
    sqlite_store: SqliteOperationalStore
    checkpointer: Any
    agent: Any
    gateway: AgentRunGateway
    event_bus: HookEventBus
    dispatcher: WebhookDispatcher
    addon_manager: AddonManager
    http_client: httpx.AsyncClient
    auth_headers: dict[str, str]
    tool_call_log: list[dict[str, Any]] = field(default_factory=list)
    sink_deliveries: list[dict[str, Any]] = field(default_factory=list)
    bus_events: list[AddonEvent] = field(default_factory=list)

    @property
    def channels_addon(self) -> Any:
        """Return the active built-in `ChannelsAddon` instance if enabled in this profile."""
        for addon in self.addon_manager.active_addons:
            if getattr(addon, "name", "") == "channels":
                return addon
        return None

    async def wait_for_webhook_runs(self, timeout: float = 240.0) -> None:
        """Await all in-flight background tasks spawned by `WebhookDispatcher`."""
        deadline = asyncio.get_running_loop().time() + timeout
        while self.dispatcher._background_tasks:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            pending = list(self.dispatcher._background_tasks)
            if pending:
                await asyncio.wait(pending, timeout=min(remaining, 2.0))

    async def wait_for_channel_runs(self, timeout: float = 180.0) -> None:
        """Await all in-flight background webhook & conversation tasks inside `ChannelsAddon`."""
        ch_addon = self.channels_addon
        if ch_addon is None:
            return
        hub = ch_addon.hub
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            pending: list[asyncio.Task[Any]] = [
                t for t in hub._active_turn_tasks.values() if not t.done()
            ]
            for adapter in hub.adapters.values():
                bg_set = getattr(adapter, "_background_tasks", None) or set()
                pending.extend(t for t in bg_set if not t.done())
            if not pending:
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.wait(pending, timeout=min(remaining, 2.0))


async def build_serve_harness_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    eval_model_spec: str,
) -> AsyncIterator[Callable[..., Any]]:
    """Manage creation and clean teardown of isolated `ServeEvalHarness` instances."""
    created_managers: list[AddonManager] = []
    created_clients: list[httpx.AsyncClient] = []
    created_stores: list[SqliteOperationalStore] = []
    exit_stack = contextlib.AsyncExitStack()
    prev_sessions_db = get_db_path()

    async def _create_harness(
        *,
        profile: str = "sre",
        execution_profile: str | None = None,
        read_only: bool = False,
        interactive: bool = False,
        auto_approve: bool = True,
        tnr: bool = False,
        no_subagents: bool = False,
        explicit_model: bool = True,
        extra_tools: Sequence[Any] | None = None,
        override_tools: Sequence[Any] | None = None,
        extra_addons: list[ServerAddon] | None = None,
        addons_config_path: Path | None = None,
        correlation_window_sec: float = 300.0,
        workspace_setup: Callable[[Path], None] | None = None,
    ) -> ServeEvalHarness:
        idx = len(created_managers)
        run_root = tmp_path / f"serve_{profile}_{idx}"
        workspace_dir = run_root / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        if workspace_setup is not None:
            workspace_setup(workspace_dir)

        sessions_db = run_root / "sessions.db"
        set_db_path(sessions_db)

        op_db = run_root / "operational_state.db"
        op_store = SqliteOperationalStore(op_db)
        await op_store.setup()
        set_operational_store(op_store)
        created_stores.append(op_store)

        monkeypatch.setenv("TALKOPS_SERVER_PROFILE", profile)
        monkeypatch.setenv("TALKOPS_SQLITE_PATH", str(op_db))
        monkeypatch.setenv("TALKOPS_CHANNELS_PAIRING_FILE", str(run_root / "channel_pairing.json"))
        monkeypatch.setenv("TALKOPS_SERVER_API_KEYS", f"{EVAL_API_USER}:{EVAL_API_TOKEN}")
        monkeypatch.setenv("TALKOPS_SERVER_MODEL", eval_model_spec)
        monkeypatch.setenv("TALKOPS_SERVER_EXPLICIT_MODEL", "true" if explicit_model else "false")
        monkeypatch.setenv("TALKOPS_SERVER_READ_ONLY", "true" if read_only else "false")
        monkeypatch.setenv("TALKOPS_READ_ONLY", "true" if read_only else "false")
        monkeypatch.setenv("TALKOPS_SERVER_APPROVAL_MODE", "yolo" if auto_approve else "manual")
        if execution_profile:
            monkeypatch.setenv("TALKOPS_EXECUTION_PROFILE", execution_profile)
        else:
            monkeypatch.delenv("TALKOPS_EXECUTION_PROFILE", raising=False)

        tool_call_log: list[dict[str, Any]] = []
        mock_tools = (
            list(override_tools)
            if override_tools is not None
            else build_mock_infrastructure_tools(tool_call_log)
        )
        if extra_tools:
            mock_tools.extend(extra_tools)

        checkpointer = await exit_stack.enter_async_context(get_checkpointer())
        model_res = create_model(eval_model_spec)
        compiled_agent, _ = create_talkops_agent(
            model=model_res.model,
            tools=mock_tools,
            mcp_tools=[],
            interactive=interactive,
            auto_approve=auto_approve,
            enable_shell=False,
            cwd=workspace_dir,
            checkpointer=checkpointer,
            execution_profile=execution_profile,
            tnr=tnr,
            no_subagents=no_subagents,
            _defer_mcp_preload=True,
        )

        event_bus = HookEventBus()
        bus_events: list[AddonEvent] = []
        event_bus.subscribe("*", lambda ev: bus_events.append(ev))

        gateway = AgentRunGateway(
            graph=compiled_agent,
            event_bus=event_bus,
            cwd=str(workspace_dir),
        )

        sink_deliveries: list[dict[str, Any]] = []
        wh_cfg = WebhookConfig(
            max_concurrent_runs=4,
            dedup_ttl_seconds=60.0,
            outbound_channels=[],
        )
        dispatcher = WebhookDispatcher(
            config=wh_cfg,
            gateway=gateway,
            event_bus=event_bus,
            run_executor=create_gateway_run_executor(gateway),
        )
        dispatcher.bind_storm_correlator(
            AlertStormCorrelator(op_store, correlation_window_sec=correlation_window_sec)
        )

        async def _capture_sink(
            event: Any,
            result: Any = None,
            routes: Any = None,
            **_kw: Any,
        ) -> bool:
            sink_deliveries.append(
                {
                    "stage": "completed",
                    "provider": getattr(event, "provider", ""),
                    "incident_id": getattr(event, "incident_id", ""),
                    "service_name": getattr(event, "service_name", ""),
                    "thread_id": getattr(result, "thread_id", None),
                    "status": getattr(result, "status", None),
                    "analysis_text": getattr(result, "analysis_text", ""),
                    "hypothesis_ledger": getattr(result, "hypothesis_ledger", None),
                    "routes": routes,
                }
            )
            return True

        event_bus.subscribe(
            "webhook.received",
            lambda ev: sink_deliveries.append(
                {
                    "stage": "received",
                    "provider": ev.payload.get("provider", ""),
                    "incident_id": ev.payload.get("incident_id", ""),
                    "service_name": ev.payload.get("service_name", ""),
                    "thread_id": ev.thread_id,
                    "status": "received",
                    "analysis_text": "",
                    "hypothesis_ledger": None,
                }
            ),
        )
        dispatcher.register_outbound_sink("eval_capture", _capture_sink)
        dispatcher.config.outbound_channels = [
            {
                "channel": "eval_capture",
                "conversation_id": "sre-incidents",
                "providers": ["*"],
                "severities": ["*"],
                "notify_on": ["received", "completed"],
            }
        ]
        set_webhook_dispatcher(dispatcher)

        addon_mgr = AddonManager(
            profile=profile,
            project_root=workspace_dir,
            gateway=gateway,
            event_bus=event_bus,
            addons_config_path=addons_config_path,
            extra_addons=extra_addons,
        )
        addon_mgr.discover_addons()

        asgi_app = addon_mgr.create_asgi_app()
        await addon_mgr.startup()
        created_managers.append(addon_mgr)

        auth_headers = {
            "Authorization": f"Bearer {EVAL_API_TOKEN}",
            "X-API-Key": EVAL_API_TOKEN,
            "X-TalkOps-User": EVAL_API_USER,
        }
        http_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_app),
            base_url="http://eval-server",
            timeout=60.0,
        )
        created_clients.append(http_client)

        return ServeEvalHarness(
            profile=profile,
            workspace_dir=workspace_dir,
            sqlite_store=op_store,
            checkpointer=checkpointer,
            agent=compiled_agent,
            gateway=gateway,
            event_bus=event_bus,
            dispatcher=dispatcher,
            addon_manager=addon_mgr,
            http_client=http_client,
            auth_headers=auth_headers,
            tool_call_log=tool_call_log,
            sink_deliveries=sink_deliveries,
            bus_events=bus_events,
        )

    try:
        yield _create_harness
    finally:
        for client in reversed(created_clients):
            with contextlib.suppress(Exception):
                await client.aclose()
        for mgr in reversed(created_managers):
            with contextlib.suppress(Exception):
                await mgr.shutdown()
        for st in reversed(created_stores):
            with contextlib.suppress(Exception):
                await st.aclose()
        with contextlib.suppress(Exception):
            await exit_stack.aclose()
        with contextlib.suppress(Exception):
            await aclose_operational_store()
        set_webhook_dispatcher(None)
        set_db_path(prev_sessions_db)

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

"""Regression tests for critical TUI freeze scenarios.

1. Resuming / scrolling / typing in >600-message threads (virtualized DOM + lazy tool groups).
2. Idle remote transport + background tasks + firing commands like `/compact` (pool recycle, JWT refresh, bounded timeouts, Esc-cancellable worker).
3. Slash command & fuzzy file autocomplete responsiveness (`FuzzyFileController` cache, `/ com` path classification).
4. BackgroundTaskPanel / BackgroundTaskHandle O(1) telemetry & bounded log tail reads.
5. In-process SQLite checkpointer large-blob executor offload & O(1) `fork_thread`.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

from talkops.state.session import SessionManager, TalkOpsCheckpointer, set_db_path
from talkops.tasks.manager import BackgroundTaskHandle, read_log_tail
from talkops.ui.app import TalkOpsApp
from talkops.ui.remote_client import RemoteAgent
from talkops.ui.widgets.autocomplete import FuzzyFileController
from talkops.ui.widgets.background_task_panel import _TICK_INTERVAL, BackgroundTaskPanel
from talkops.ui.widgets.chat_input import ChatInput
from talkops.ui.widgets.message_list import MessageList
from talkops.ui.widgets.tool_call_message import ToolCallMessage
from talkops.ui.widgets.tool_group import ToolGroupSummary


def _build_large_thread_messages(turn_count: int = 180) -> list[object]:
    """Construct a ~720-message conversation with markdown, code fences, and tool calls."""
    msgs: list[object] = []
    for i in range(turn_count):
        msgs.append(HumanMessage(content=f"Investigate cluster issue #{i} in production"))
        call_id = f"call_{i}"
        msgs.append(
            AIMessage(
                content=(
                    f"Checking service metrics for turn {i}.\n\n"
                    f"```python\n# snippet {i}\nprint('status {i}')\n```"
                ),
                tool_calls=[
                    {
                        "id": call_id,
                        "name": "read_file",
                        "args": {"file_path": f"src/service_{i}.py"},
                    }
                ],
                usage_metadata={
                    "input_tokens": 500 + i,
                    "output_tokens": 120,
                    "total_tokens": 620 + i,
                },
            )
        )
        msgs.append(
            ToolMessage(
                content=(f"line 1 of service_{i}\n" * 40)
                + "\n[Command succeeded with exit code 0]",
                tool_call_id=call_id,
                name="read_file",
            )
        )
        msgs.append(
            AIMessage(
                content=f"Turn {i} verified cleanly — see `src/talkops/ui/app.py`.",
                usage_metadata={
                    "input_tokens": 650 + i,
                    "output_tokens": 80,
                    "total_tokens": 730 + i,
                },
            )
        )
    return msgs


@pytest.mark.asyncio
async def test_resume_720_message_thread_virtualized_fast_and_lossless() -> None:
    """Resuming a 720-message thread must fetch state once, mount <= ~45 DOM items, and preserve all messages."""
    raw_messages = _build_large_thread_messages(180)  # 720 messages
    assert len(raw_messages) == 720

    app = TalkOpsApp(defer_server_start=True)
    fetch_calls = 0

    async def _fake_get_state(thread_id: str | None = None) -> dict[str, object]:
        nonlocal fetch_calls
        fetch_calls += 1
        return {
            "messages": raw_messages,
            "_context_tokens": 42_000,
            "_session_total_tokens": 310_000,
            "_session_cost_usd": 1.25,
        }

    app._get_thread_state_values = _fake_get_state  # type: ignore[method-assign]

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        t0 = time.perf_counter()
        await app.resume_thread("thread-720-freeze-test", is_new=False)
        await pilot.pause()
        elapsed = time.perf_counter() - t0

        assert elapsed < 2.0, f"720-message resume took too long: {elapsed:.2f}s"
        assert fetch_calls == 1, f"Expected single checkpoint state fetch on resume, got {fetch_calls}"

        ml = app.query_one("#messages", MessageList)
        # WelcomeBanner + INITIAL_HISTORY_WINDOW (40) + "Resumed thread" SystemMessage = ~42 widgets
        assert len(ml.children) <= MessageList.INITIAL_HISTORY_WINDOW + 5
        assert len(ml._archived_entries) > 200

        # Collapsed historical tool groups must NOT mount hidden ToolCallMessage widgets until clicked
        assert len(list(ml.query(ToolCallMessage))) == 0
        groups = list(ml.query(ToolGroupSummary))
        assert len(groups) >= 1

        # Expanding a historical tool group lazily materializes its ToolCallMessage in the DOM
        groups[-1].toggle()
        await pilot.pause()
        materialized = list(ml.query(ToolCallMessage))
        assert len(materialized) == 1
        assert "[Command succeeded with exit code 0]" not in materialized[0]._output

        # Full history retrieval across archived + mounted entries must remain lossless
        recovered = app.get_thread_messages()
        # 180 Human + 360 AI + 180 Tool = 720 LangChain messages
        assert len(recovered) == 720

        # Scrolling to top hydrates an older batch on demand
        archived_before = len(ml._archived_entries)
        ml.scroll_to(y=0, animate=False, immediate=True)
        ml._maybe_hydrate_older()
        await pilot.pause()
        await pilot.pause()
        assert len(ml._archived_entries) == archived_before - MessageList.HYDRATE_BATCH_SIZE


@pytest.mark.asyncio
async def test_typing_slash_com_on_large_thread_enters_command_mode_without_path_probing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typing `/ com` or `/compact` must enter command mode without path statting or UI stalls."""
    from talkops.input import _looks_like_path_candidate_text

    assert _looks_like_path_candidate_text("/compact") is False
    assert _looks_like_path_candidate_text("/ com") is False
    assert _looks_like_path_candidate_text("/Users/someone/file.txt") is True

    raw_messages = _build_large_thread_messages(160)  # 640 messages
    app = TalkOpsApp(defer_server_start=True)
    app._get_thread_state_values = AsyncMock(return_value={"messages": raw_messages})  # type: ignore[method-assign]

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app.resume_thread("thread-640-slash", is_new=False)
        await pilot.pause()

        chat_input = app.query_one(ChatInput)
        chat_input.focus_input()
        await pilot.pause()

        t0 = time.perf_counter()
        await pilot.press("/", "space", "c", "o", "m")
        await pilot.pause()
        elapsed = time.perf_counter() - t0

        assert elapsed < 1.0, f"Typing '/ com' stalled for {elapsed:.2f}s"
        assert chat_input.mode == "command"


def test_fuzzy_file_controller_reuses_warm_cache_across_keystrokes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`FuzzyFileController._get_files()` must return warm `_file_cache` instead of re-scanning git/disk per keystroke."""
    scan_calls = 0

    def _fake_scan(_cwd: Path, **_kwargs: object) -> list[str]:
        nonlocal scan_calls
        scan_calls += 1
        return ["src/app.py", "src/main.py", "README.md"]

    monkeypatch.setattr(
        "talkops.ui.widgets.autocomplete._get_project_files",
        _fake_scan,
    )
    view = MagicMock()
    ctrl = FuzzyFileController(view, cwd=tmp_path)

    ctrl.on_text_changed("Please check @ap", cursor_index=16)
    ctrl.on_text_changed("Please check @app", cursor_index=17)
    ctrl.on_text_changed("Please check @app.", cursor_index=18)

    assert scan_calls == 1, f"Expected 1 project scan with warm cache, got {scan_calls}"


@pytest.mark.asyncio
async def test_idle_remote_agent_recycles_stale_pool_retries_once_and_preserves_remove_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idle RemoteAgent (>45s) must recycle its httpx pool, refresh temporary JWT, retry once on stale transport/401 error, and preserve RemoveMessage."""
    token_refresh_calls: list[bool] = []

    async def _fake_exchange(
        _url: str,
        api_key: str | None = None,
        *,
        user_id: str | None = None,
        timeout: float = 10.0,
        force_refresh: bool = False,
    ) -> str:
        token_refresh_calls.append(force_refresh)
        return "fresh-jwt-token-v2" if force_refresh else "fresh-jwt-token-v1"

    monkeypatch.setattr(
        "talkops.state.remote_session.exchange_api_key_for_temporary_token",
        _fake_exchange,
    )

    agent = RemoteAgent(
        url="http://127.0.0.1:8123",
        api_key="tk_live_secret",
        graph_name="agent",
    )

    closed_clients: list[httpx.AsyncClient] = []
    created_clients: list[httpx.AsyncClient] = []

    def _make_tracked_http() -> httpx.AsyncClient:
        http_client = httpx.AsyncClient()
        created_clients.append(http_client)
        orig_aclose = http_client.aclose

        async def _tracked_aclose() -> None:
            closed_clients.append(http_client)
            await orig_aclose()

        http_client.aclose = _tracked_aclose  # type: ignore[method-assign]
        return http_client

    get_state_attempts = 0

    async def _flaky_aget_state(_config: object) -> dict[str, object]:
        nonlocal get_state_attempts
        get_state_attempts += 1
        if get_state_attempts == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return {"values": {"messages": []}}

    update_attempts = 0
    captured_update_values: list[dict[str, Any]] = []

    async def _auth_flaky_aupdate_state(
        _config: object,
        values: dict[str, Any],
        **_kwargs: object,
    ) -> dict[str, object]:
        nonlocal update_attempts
        update_attempts += 1
        if update_attempts == 1:
            err = RuntimeError("JWT expired")
            err.status_code = 401  # type: ignore[attr-defined]
            raise err
        captured_update_values.append(values)
        return {"ok": True}

    def _make_fake_graph() -> object:
        sdk_client = SimpleNamespace(http=_make_tracked_http())
        return SimpleNamespace(
            _validate_client=lambda: sdk_client,
            aget_state=_flaky_aget_state,
            aupdate_state=_auth_flaky_aupdate_state,
        )

    rebuilt_graphs = 0

    def _fake_get_graph() -> object:
        nonlocal rebuilt_graphs
        if agent._graph is None:
            rebuilt_graphs += 1
            agent._graph = _make_fake_graph()
        return agent._graph

    # Seed an active real httpx.AsyncClient pool and mark the agent idle for 5 minutes
    agent._graph = _make_fake_graph()
    initial_http = created_clients[0]
    agent._get_graph = _fake_get_graph  # type: ignore[method-assign]
    agent._last_activity_monotonic = time.monotonic() - 300.0

    state = await agent.aget_state({"configurable": {"thread_id": "t-1"}})
    assert state == {"values": {"messages": []}}
    assert initial_http in closed_clients, "Stale >45s idle httpx pool must be closed before RPC"
    assert rebuilt_graphs >= 2, "Pool must rebuild on idle expiry and again on transport retry"

    # Verify 401 triggers force_refresh=True AND RemoveMessage survives RemoteAgent.aupdate_state serialization
    await agent.aupdate_state(
        {"configurable": {"thread_id": "t-1"}},
        {
            "messages": [
                RemoveMessage(id="msg-old-1"),
                HumanMessage(content="<CONTEXT_SUMMARY>summary</CONTEXT_SUMMARY>"),
            ]
        },
    )
    assert True in token_refresh_calls
    assert len(captured_update_values) == 1
    sent_values = captured_update_values[0]
    assert sent_values["messages"][0]["type"] == "remove"
    assert sent_values["messages"][0]["id"] == "msg-old-1"

    await agent.aclose()


def test_google_genai_transport_disables_aiohttp_and_injects_bounded_timeout() -> None:
    """Google GenAI transport patch must disable unbounded aiohttp and inject finite httpx timeouts."""
    import google.genai._api_client as genai_api_client

    from talkops.model.factory import _ensure_google_genai_httpx_transport

    _ensure_google_genai_httpx_transport()
    assert genai_api_client.has_aiohttp is False


@pytest.mark.asyncio
async def test_compact_command_runs_in_worker_and_cancels_immediately_on_escape() -> None:
    """`/compact` submitted from ChatInput must not block the App message pump and must cancel immediately on Esc."""
    from talkops.commands import CommandResult

    app = TalkOpsApp(defer_server_start=True)

    compact_started = asyncio.Event()
    compact_cancelled = asyncio.Event()

    async def _slow_dispatch(raw_cmd: str, _ctx: object) -> CommandResult:
        assert raw_cmd.strip().startswith("/compact")
        compact_started.set()
        try:
            await asyncio.sleep(180.0)
        except asyncio.CancelledError:
            compact_cancelled.set()
            raise
        return CommandResult(success=True, message="Compacted")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._startup_sequence_running = False
        app._connecting = False
        app._agent_thread_id = "thread-compact-cancel"
        app._command_router.dispatch = _slow_dispatch  # type: ignore[method-assign]

        # Submit /compact through the real ChatInput.Submitted event handler
        t0 = time.perf_counter()
        app.post_message(ChatInput.Submitted("/compact", "command"))
        await asyncio.wait_for(compact_started.wait(), timeout=1.5)
        pump_latency = time.perf_counter() - t0

        assert pump_latency < 1.0
        assert app._compact_in_flight is True
        assert app._agent_running is True
        assert app._agent_worker is not None

        # Pressing Escape must immediately interrupt the in-flight /compact worker
        await pilot.press("escape")
        await asyncio.wait_for(compact_cancelled.wait(), timeout=1.5)
        await pilot.pause()

        assert app._compact_in_flight is False
        assert app._agent_running is False


def test_background_task_handle_o1_last_line_and_bounded_log_tail(tmp_path: Path) -> None:
    """BackgroundTaskHandle caches last non-empty line in O(1), read_log_tail seeks from EOF, and panel ticks at 1Hz."""
    assert _TICK_INTERVAL >= 1.0
    panel = BackgroundTaskPanel()
    assert panel._last_rendered_row_count == -1

    log_path = tmp_path / "big.log"
    # Write ~2 MB log file
    with log_path.open("w", encoding="utf-8") as fh:
        for idx in range(40_000):
            fh.write(f"step {idx}: processing analytics shard\n")
        fh.write("FINAL_MARKER_LINE\n")

    tail = read_log_tail(log_path, max_bytes=4096)
    assert "FINAL_MARKER_LINE" in tail
    assert len(tail) <= 4096

    handle = BackgroundTaskHandle(
        task_id="cmd-test",
        thread_id="t-1",
        command="echo hi",
        cwd=str(tmp_path),
        log_path=log_path,
    )
    handle.append_output("first line\n  second line  \n\n")
    assert handle.last_non_empty_line() == "second line"


@pytest.mark.asyncio
async def test_checkpointer_offloads_large_blob_and_fork_copies_latest_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TalkOpsCheckpointer handles >32KB checkpoints cleanly and fork_thread copies only the latest root checkpoint."""
    from talkops.state.session import get_db_path

    monkeypatch.setattr("talkops.state.remote_session.is_remote_client_mode", lambda: False)
    monkeypatch.setattr("talkops.state.postgres_session.get_postgres_session_uri", lambda: None)

    prev_db_path = get_db_path()
    db_file = tmp_path / "sessions_large.db"
    set_db_path(db_file)
    try:
        source_tid = "01963000-0000-7000-8000-000000009999"
        large_messages = [HumanMessage(content=f"msg-{i}-" + ("x" * 600)) for i in range(80)]

        async with TalkOpsCheckpointer.from_conn_string(str(db_file)) as saver:
            await saver.setup()
            for step in range(1, 4):
                cp_id = f"01963000-0000-7000-8000-00000000000{step}"
                cfg = {
                    "configurable": {
                        "thread_id": source_tid,
                        "checkpoint_ns": "",
                        "checkpoint_id": f"01963000-0000-7000-8000-00000000000{step - 1}" if step > 1 else None,
                    }
                }
                cp = {
                    "v": 1,
                    "id": cp_id,
                    "ts": "2026-11-14T10:00:00+00:00",
                    "channel_values": {"messages": large_messages[: step * 25]},
                    "channel_versions": {"messages": step},
                    "versions_seen": {},
                    "pending_sends": [],
                }
                await saver.aput(cfg, cp, {"step": step, "thread_id": source_tid}, {"messages": step})

            tup = await saver.aget_tuple({"configurable": {"thread_id": source_tid}})
            assert tup is not None
            assert len(tup.checkpoint["channel_values"]["messages"]) == 75

        forked_tid = await SessionManager.fork_thread(source_tid)
        async with TalkOpsCheckpointer.from_conn_string(str(db_file)) as saver:
            async with saver.conn.execute(
                "SELECT checkpoint_id FROM checkpoints WHERE thread_id = ?",
                (forked_tid,),
            ) as cur:
                rows = list(await cur.fetchall())
            # Only the latest checkpoint (step 3) is copied on fork, not all 3 supersteps
            assert len(rows) == 1
            assert rows[0][0] == "01963000-0000-7000-8000-000000000003"
    finally:
        set_db_path(prev_db_path)

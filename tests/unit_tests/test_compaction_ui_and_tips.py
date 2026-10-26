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

"""Unit tests for Compaction TUI visibility, <slash_commands> prompt section, and TipsManager."""

from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from talkops.commands.registry import CommandRegistry
from talkops.middleware.compaction import (
    CLICompactionMiddleware,
    TalkOpsSummarizationMiddleware,
    generate_llm_continuation_summary,
)
from talkops.prompts.system import build_slash_commands_section, get_base_system_prompt
from talkops.ui.controllers.thread_controller import (
    ThreadController,
    _extract_compaction_stats,
    _is_compaction_summary_message,
)
from talkops.ui.textual_adapter import (
    TextualAdapter,
    _is_auto_mode_classifier_chunk,
    _TurnStreamState,
)
from talkops.ui.tips import (
    TipsManager,
    has_inline_slash_recommendation,
)
from talkops.ui.widgets.messages import CompactionMessage, TurnSummary

# ── 1. CompactionMessage & TurnSummary Widget Tests ──────────────────────────


def test_compaction_message_header_and_stats_formatting() -> None:
    """Verify compaction message header and stats formatting."""
    widget = CompactionMessage(
        steps_summarized=18,
        pre_tokens=172_400,
        post_tokens=26_800,
        summary_tokens=4_200,
        duration_seconds=3.14,
    )
    header = widget.header_text(80)
    assert "Conversation compacted" in header
    assert len(header) == 80

    stats = widget.stats_text()
    assert "18 steps summarized" in stats
    assert "context 172.4K" in stats
    assert "~26.8K" in stats
    assert "(4.2K summary)" in stats
    assert "took 3.1s" in stats

    rendered = widget.render().plain
    assert "Conversation compacted" in rendered
    assert "18 steps summarized" in rendered


def test_compaction_message_singular_step_and_empty_stats() -> None:
    """Verify compaction message singular step and empty stats."""
    single = CompactionMessage(steps_summarized=1, summary_tokens=500)
    stats = single.stats_text()
    assert "1 step summarized" in stats
    assert "500-token summary" in stats

    empty = CompactionMessage()
    assert empty.stats_text() == ""
    assert "Conversation compacted" in empty.render().plain


def test_turn_summary_with_and_without_tip() -> None:
    """Verify turn summary with and without tip."""
    without_tip = TurnSummary(elapsed_seconds=2.5, tool_count=2, tokens=1500, cost_usd=0.01)
    assert without_tip.summary_text() == "Done in 2.5s · 2 tools · 1.5K tokens · $0.01"
    assert "Tip:" not in without_tip.render().plain

    with_tip = TurnSummary(
        elapsed_seconds=2.5,
        tool_count=2,
        tokens=1500,
        cost_usd=0.01,
        tip="Use /compact to summarize conversation history.",
    )
    # summary_text() remains strictly unchanged for backwards compatibility
    assert with_tip.summary_text() == "Done in 2.5s · 2 tools · 1.5K tokens · $0.01"
    rendered_plain = with_tip.render().plain
    assert "Done in 2.5s" in rendered_plain
    assert "Tip: Use /compact to summarize conversation history." in rendered_plain


# ── 2. Summarization lc_source Tagging & Stream Filtering ───────────────────


def test_summarization_chunk_is_filtered_from_transcript() -> None:
    """Verify summarization chunk is filtered from transcript."""
    assert _is_auto_mode_classifier_chunk({"lc_source": "summarization"}) is True
    assert _is_auto_mode_classifier_chunk({"lc_source": "auto_mode_classifier"}) is True
    assert _is_auto_mode_classifier_chunk({"lc_source": "conversation_title_generator"}) is True
    assert _is_auto_mode_classifier_chunk({"lc_source": "main_agent"}) is False
    assert _is_auto_mode_classifier_chunk(None) is False


def test_generate_llm_continuation_summary_passes_summarization_lc_source() -> None:
    """Verify generate llm continuation summary passes summarization lc source."""
    captured_configs: list[Any] = []

    class DummyModel:
        """Test suite or helper class for dummy model."""
        def invoke(self, messages: Any, config: Any = None) -> Any:
            """Provide invoke test helper or fixture."""
            captured_configs.append(config)
            return AIMessage(content="<summary>Compact summary</summary>")

    dummy_model: Any = DummyModel()
    summary = generate_llm_continuation_summary(
        dummy_model,
        [HumanMessage(content="Deploy EKS cluster"), AIMessage(content="Working on it")],
    )
    assert "<summary>Compact summary</summary>" in summary
    assert len(captured_configs) == 1
    assert captured_configs[0] == {"metadata": {"lc_source": "summarization"}}


# ── 3. Middleware Compaction Stream Events & Stats ───────────────────────────


def test_talkops_summarization_middleware_emits_compaction_events() -> None:
    """Verify talkops summarization middleware emits compaction events."""
    emitted_events: list[dict[str, Any]] = []
    runtime = SimpleNamespace(
        context={},
        stream_writer=lambda payload: emitted_events.append(payload),
    )

    class FakeBackend:
        """Test suite or helper class for fake backend."""
        def download_files(self, paths: list[str]) -> list[Any]:
            """Provide download files test helper or fixture."""
            return [SimpleNamespace(content=None, error="file_not_found")]

        def write(self, path: str, content: str) -> Any:
            """Provide write test helper or fixture."""
            return SimpleNamespace(error=None)

    class DummyModel:
        """Test suite or helper class for dummy model."""
        _llm_type = "openai-chat"
        profile = {"max_input_tokens": 128_000}

        def with_retry(self, **kwargs: Any) -> Any:
            """Provide with retry test helper or fixture."""
            return self

        def invoke(self, messages: Any, config: Any = None) -> Any:
            """Provide invoke test helper or fixture."""
            return AIMessage(content="<summary>Summarized 3 steps</summary>")

    mw = TalkOpsSummarizationMiddleware(
        model=DummyModel(),
        backend=FakeBackend(),
        trigger=("messages", 3),
        keep=("messages", 1),
    )
    msgs = [
        HumanMessage(content="Step 1 " * 40, id="m1"),
        AIMessage(
            content="Step 2 " * 40,
            id="m2",
            usage_metadata={"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500},
        ),
        HumanMessage(content="Step 3 " * 40, id="m3"),
        AIMessage(
            content="Step 4 " * 40,
            id="m4",
            usage_metadata={"input_tokens": 1800, "output_tokens": 400, "total_tokens": 2200},
        ),
    ]
    state: dict[str, Any] = {"messages": msgs, "thread_id": "test-thread"}
    base_req_fields = {
        "state": state,
        "runtime": runtime,
        "messages": msgs,
        "system_message": None,
        "model": DummyModel(),
        "model_settings": {},
        "tools": [],
    }
    req: Any = SimpleNamespace(
        **base_req_fields,
        override=lambda **kw: SimpleNamespace(**{**base_req_fields, **kw}),
    )
    handler_fn: Any = lambda r: SimpleNamespace(result=[AIMessage(content="ok")])
    res: Any = mw.wrap_model_call(req, handler_fn)
    assert [e["type"] for e in emitted_events] == ["compaction_start", "compaction_end"]

    end_evt = emitted_events[1]
    assert end_evt["steps_summarized"] == 3
    assert end_evt["pre_tokens"] >= 2200
    assert end_evt["post_tokens"] > 0
    assert end_evt["summary_tokens"] > 0
    assert end_evt["duration_seconds"] >= 0.0

    # Check that the replacement summary message carries compaction_stats metadata
    event_state = res.command.update.get("_summarization_event")
    assert isinstance(event_state, dict)
    summary_human = event_state["summary_message"]
    assert summary_human.additional_kwargs.get("lc_source") == "summarization"
    assert summary_human.additional_kwargs.get("compaction_stats", {}).get("steps_summarized") == 3


def test_cli_compaction_middleware_emits_compaction_events() -> None:
    """Verify cli compaction middleware emits compaction events."""
    emitted_events: list[dict[str, Any]] = []
    runtime = SimpleNamespace(
        context={},
        stream_writer=lambda payload: emitted_events.append(payload),
    )

    class FakeBackend:
        """Test suite or helper class for fake backend."""
        def download_files(self, paths: list[str]) -> list[Any]:
            """Provide download files test helper or fixture."""
            return [SimpleNamespace(content=None, error="file_not_found")]

        def write(self, path: str, content: str) -> Any:
            """Provide write test helper or fixture."""
            return SimpleNamespace(error=None)

    class DummyModel:
        """Test suite or helper class for dummy model."""
        _llm_type = "openai-chat"
        profile = {"max_input_tokens": 128_000}

        def with_retry(self, **kwargs: Any) -> Any:
            """Provide with retry test helper or fixture."""
            return self

        def invoke(self, messages: Any, config: Any = None) -> Any:
            """Provide invoke test helper or fixture."""
            return AIMessage(content="<summary>CLI compacted</summary>")

    backend = FakeBackend()
    summarization = TalkOpsSummarizationMiddleware(
        model=DummyModel(),
        backend=backend,
        trigger=("messages", 3),
        keep=("messages", 1),
    )
    mw = CLICompactionMiddleware(summarization)
    msgs = [
        HumanMessage(content="Msg 1 " * 30, id="m1"),
        AIMessage(content="Msg 2 " * 30, id="m2"),
        HumanMessage(content="Msg 3 " * 30, id="m3"),
        AIMessage(content="Msg 4 " * 30, id="m4"),
    ]
    state: dict[str, Any] = {"messages": msgs}

    def handler(req: Any) -> Any:
        """Provide handler test helper or fixture."""
        return SimpleNamespace(result=[AIMessage(content="ok")])

    base_req_fields = {
        "state": state,
        "runtime": runtime,
        "messages": msgs,
        "system_message": None,
        "model": DummyModel(),
        "model_settings": {},
        "tools": [],
    }
    req: Any = SimpleNamespace(
        **base_req_fields,
        override=lambda **kw: SimpleNamespace(**{**base_req_fields, **kw}),
    )
    mw.wrap_model_call(req, handler)
    assert [e["type"] for e in emitted_events] == ["compaction_start", "compaction_end"]
    assert emitted_events[1]["steps_summarized"] == 3


# ── 4. TextualAdapter Compaction Event & Context Reset Tests ─────────────────


def test_textual_adapter_handles_compaction_start_and_end() -> None:
    """Verify textual adapter handles compaction start and end."""
    mounted_widgets: list[Any] = []
    spinner_states: list[str | None] = []
    status_bar = MagicMock()
    messages = MagicMock()
    messages.mount = MagicMock(side_effect=lambda w: mounted_widgets.append(w))
    app = SimpleNamespace(
        _context_tokens=175_000,
        _cumulative_session_tokens=240_000,
    )

    async def fake_set_spinner(text: str | None) -> None:
        """Provide fake set spinner test helper or fixture."""
        spinner_states.append(text)

    adapter = TextualAdapter(
        client=MagicMock(),
        assistant_id="agent",
        messages_widget=messages,
        status_bar=status_bar,
        set_spinner=fake_set_spinner,
        app=app,
    )
    stream_state = _TurnStreamState()
    stream_state.default_spinner = "Thinking"
    stream_state.turn_context_tokens = 175_000

    adapter._handle_custom_stream_event(
        {"type": "compaction_start"},
        is_main_agent=True,
        stream_state=stream_state,
    )
    assert adapter._pending_spinner_text == "Compacting conversation..."
    messages.finish_assistant_message.assert_called()

    adapter._handle_custom_stream_event(
        {
            "type": "compaction_end",
            "steps_summarized": 24,
            "pre_tokens": 175_000,
            "post_tokens": 28_500,
            "summary_tokens": 4_100,
            "duration_seconds": 2.8,
        },
        is_main_agent=True,
        stream_state=stream_state,
    )

    # Context tokens reset to post_tokens immediately
    assert stream_state.turn_context_tokens == 28_500
    assert app._context_tokens == 28_500
    status_bar.set_tokens.assert_called_with(240_000, context_tokens=28_500)
    assert adapter._pending_spinner_text == "Thinking"

    # CompactionMessage mounted in chat stream
    assert len(mounted_widgets) == 1
    banner = mounted_widgets[0]
    assert isinstance(banner, CompactionMessage)
    assert banner.steps_summarized == 24
    assert banner.pre_tokens == 175_000
    assert banner.post_tokens == 28_500
    assert banner.summary_tokens == 4_100


# ── 5. ThreadController History Rehydration Tests ────────────────────────────


@pytest.mark.asyncio
async def test_thread_controller_mounts_compaction_message_on_rehydrate() -> None:
    """Verify thread controller mounts compaction message on rehydrate."""
    summary_msg = HumanMessage(
        content="<CONTEXT_SUMMARY>\n<summary>Prior work</summary>\n</CONTEXT_SUMMARY>",
        additional_kwargs={
            "lc_source": "summarization",
            "compaction_stats": {
                "steps_summarized": 12,
                "pre_tokens": 160_000,
                "post_tokens": 22_000,
                "summary_tokens": 3_500,
                "duration_seconds": 2.4,
            },
        },
    )
    assert _is_compaction_summary_message(summary_msg, str(summary_msg.content))
    stats = _extract_compaction_stats(summary_msg)
    assert stats["steps_summarized"] == 12
    assert stats["pre_tokens"] == 160_000
    assert stats["post_tokens"] == 22_000

    mounted: list[Any] = []
    fake_container = MagicMock()
    fake_container.children = []
    fake_container._tool_calls = {}

    fake_app = SimpleNamespace(
        _reset_thread_usage=MagicMock(),
        _get_thread_state_values=AsyncMock(
            return_value={
                "messages": [
                    summary_msg,
                    HumanMessage(content="Continue deployment"),
                ]
            }
        ),
        _goal_rubric_payload_from_state=MagicMock(return_value={}),
        _restore_goal_rubric_state=MagicMock(),
        query_one=MagicMock(return_value=fake_container),
        _mount_message=AsyncMock(side_effect=lambda w: mounted.append(w)),
        _chat_input=None,
        _model="anthropic:claude-sonnet-4-5",
        _context_tokens=0,
        _session_cost_usd=0.0,
        _regroup_completed_tools=AsyncMock(),
    )

    ctrl = ThreadController(fake_app)  # type: ignore[arg-type]
    await ctrl.load_thread_history("thread-123")

    assert len(mounted) == 2
    assert isinstance(mounted[0], CompactionMessage)
    assert mounted[0].steps_summarized == 12


# ── 6. <slash_commands> System Prompt Section Tests ──────────────────────────


def test_slash_commands_section_in_system_prompt() -> None:
    """Verify slash commands section in system prompt."""
    CommandRegistry.reset()
    commands = CommandRegistry.get_instance().list_recommendable_commands()
    cmd_names = {name for name, _ in commands}
    assert "/goal" in cmd_names
    assert "/compact" in cmd_names
    assert "/review" in cmd_names
    assert "/memory" in cmd_names
    assert "/loop" in cmd_names

    section = build_slash_commands_section(interactive=True, is_subagent=False)
    assert "<slash_commands>" in section
    assert "</slash_commands>" in section
    assert "- /goal:" in section
    assert "- /compact:" in section

    # Non-interactive or subagent omits <slash_commands>
    assert build_slash_commands_section(interactive=False) == ""
    assert build_slash_commands_section(interactive=True, is_subagent=True) == ""

    interactive_prompt = get_base_system_prompt(interactive=True)
    assert "<slash_commands>" in interactive_prompt

    headless_prompt = get_base_system_prompt(interactive=False)
    assert "<slash_commands>" not in headless_prompt


# ── 7. TipsManager Contextual Eligibility & Probability Tests ────────────────


def test_has_inline_slash_recommendation_detection() -> None:
    """Verify has inline slash recommendation detection."""
    assert has_inline_slash_recommendation("You can use `/goal` to track this.") is True
    assert has_inline_slash_recommendation("Try /compact to free up space.") is True
    assert has_inline_slash_recommendation("Modified `/etc/hosts` and `/var/log/syslog`.") is False
    assert has_inline_slash_recommendation(None) is False


def test_tips_manager_contextual_selection_and_suppression() -> None:
    # Deterministic RNG that always rolls 0 (< len(eligible), so roll succeeds)
    """Verify tips manager contextual selection and suppression."""
    always_Zero_rng = MagicMock(spec=random.Random)
    always_Zero_rng.randrange.return_value = 0

    mgr = TipsManager(rng=always_Zero_rng, min_turns_between_tips=2)

    # 1. If assistant already recommended a slash command inline, suppress footer tip
    tip_suppressed = mgr.next_turn_tip(
        context_fraction=0.65,
        last_assistant_text="You can use the `/compact` command to summarize context.",
    )
    assert tip_suppressed is None

    # 2. High context fraction triggers /compact tip first (highest priority = 10)
    tip_compact = mgr.next_turn_tip(
        context_fraction=0.62,
        elapsed_seconds=5.0,
        tool_count=1,
    )
    assert tip_compact is not None
    assert "/compact" in tip_compact
    assert "compact_high_context" in mgr.shown_tip_ids

    # 3. Cooldown suppresses tip on the very next turn (turn_count delta == 1 < 2)
    tip_cooldown = mgr.next_turn_tip(
        context_fraction=0.20,
        elapsed_seconds=60.0,
        tool_count=6,
    )
    assert tip_cooldown is None

    # 4. Next turn after cooldown with many tools triggers /goal tip (priority = 8)
    tip_goal = mgr.next_turn_tip(
        context_fraction=0.20,
        elapsed_seconds=50.0,
        tool_count=6,
        goal_active=False,
    )
    assert tip_goal is not None
    assert "/goal" in tip_goal

    # 5. Recording `/review` as used prevents `review_after_tools` from being suggested
    mgr.record_command_used("/review --staged")
    assert "/review" in mgr.used_commands

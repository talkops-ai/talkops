"""Unit tests for multimodal media input (clipboard paste, dropped paths, @mentions) and status bar prompt-cache tracking."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from talkops.input import (
    MediaTracker,
    parse_pasted_file_paths,
    parse_pasted_path_payload,
)
from talkops.media_utils import (
    ImageData,
    VideoData,
    create_multimodal_content,
    get_image_from_path,
    get_media_from_path,
    get_video_from_path,
    is_media_path,
    strip_media_placeholders,
)
from talkops.middleware.cost_tracking import cache_token_counts
from talkops.ui.app import TalkOpsApp
from talkops.ui.controllers.thread_controller import ThreadController
from talkops.ui.textual_adapter import TextualAdapter, _TurnStreamState
from talkops.ui.widgets.chat_input import ChatInput
from talkops.ui.widgets.status import (
    CACHE_GOOD_PERCENT,
    CACHE_WARNING_PERCENT,
    StatusBar,
    cache_hit_style,
)

# Minimal valid 1x1 PNG bytes
_TINY_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


# ── 1. media_utils Tests ─────────────────────────────────────────────────────


def test_image_and_video_data_to_message_content() -> None:
    img = ImageData(base64_data="YWJj", format="png", placeholder="[image 1]")
    assert img.to_message_content() == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,YWJj"},
    }

    vid = VideoData(
        base64_data="eHl6", mime_type="video/mp4", placeholder="[video 1]"
    )
    vid_block = vid.to_message_content()
    assert vid_block["type"] == "video"
    assert vid_block["base64"] == "eHl6"
    assert vid_block["mime_type"] == "video/mp4"


def test_get_media_from_path_loads_image_and_video(tmp_path: Path) -> None:
    png_file = tmp_path / "screenshot.png"
    png_file.write_bytes(_TINY_PNG_BYTES)

    mp4_file = tmp_path / "demo.mp4"
    mp4_file.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    txt_file = tmp_path / "notes.txt"
    txt_file.write_text("hello")

    assert is_media_path(png_file) is True
    assert is_media_path(mp4_file) is True
    assert is_media_path(txt_file) is False

    img = get_image_from_path(png_file)
    assert isinstance(img, ImageData)
    assert img.format == "png"
    assert base64.b64decode(img.base64_data) == _TINY_PNG_BYTES

    vid = get_video_from_path(mp4_file)
    assert isinstance(vid, VideoData)
    assert vid.mime_type == "video/mp4"

    assert isinstance(get_media_from_path(png_file), ImageData)
    assert isinstance(get_media_from_path(mp4_file), VideoData)
    assert get_media_from_path(txt_file) is None


def test_strip_media_placeholders_and_create_multimodal_content() -> None:
    raw = "Please inspect [image 1] and [video 1] for errors"
    cleaned = strip_media_placeholders(raw)
    assert "[image 1]" not in cleaned
    assert "[video 1]" not in cleaned
    assert "Please inspect" in cleaned

    img = ImageData(base64_data="YWJj", format="png", placeholder="[image 1]")
    vid = VideoData(
        base64_data="eHl6", mime_type="video/mp4", placeholder="[video 1]"
    )
    blocks = create_multimodal_content(cleaned, [img], videos=[vid])
    assert blocks[0] == {"type": "text", "text": cleaned}
    assert blocks[1] == img.to_message_content()
    assert blocks[2]["type"] == "video"
    assert blocks[2]["base64"] == "eHl6"


# ── 2. MediaTracker & Pasted Path Parsing Tests ──────────────────────────────


def test_media_tracker_lifecycle_and_sync() -> None:
    tracker = MediaTracker()
    img1 = ImageData(base64_data="a1", format="png", placeholder="")
    img2 = ImageData(base64_data="a2", format="png", placeholder="")
    vid1 = VideoData(base64_data="v1", mime_type="video/mp4", placeholder="")

    p1 = tracker.add_image(img1)
    p2 = tracker.add_image(img2)
    pv1 = tracker.add_video(vid1)

    assert p1 == "[image 1]"
    assert p2 == "[image 2]"
    assert pv1 == "[video 1]"
    assert len(tracker.get_images()) == 2
    assert len(tracker.get_videos()) == 1

    snap = tracker.snapshot()

    # Sync when [image 1] is removed from text
    tracker.sync_to_text("Look at [image 2] and [video 1]")
    assert [i.placeholder for i in tracker.get_images()] == ["[image 2]"]
    assert [v.placeholder for v in tracker.get_videos()] == ["[video 1]"]

    # Restore from snapshot
    tracker.restore(snap)
    assert len(tracker.get_images()) == 2
    assert len(tracker.get_videos()) == 1


def test_parse_pasted_file_paths_handles_spaces_quotes_and_file_urls(
    tmp_path: Path,
) -> None:
    f1 = tmp_path / "my image.png"
    f1.write_bytes(_TINY_PNG_BYTES)
    f2 = tmp_path / "second.png"
    f2.write_bytes(_TINY_PNG_BYTES)

    # Quoted path
    assert parse_pasted_file_paths(f"'{f1}'") == [f1.resolve()]

    # Escaped spaces
    escaped = str(f1).replace(" ", r"\ ")
    assert parse_pasted_file_paths(escaped) == [f1.resolve()]

    # file:// URI
    assert parse_pasted_file_paths(f1.as_uri()) == [f1.resolve()]

    # Mixed payload with trailing text (when allow_leading_path=True)
    mixed_text = f"'{f1}' explain this diagram"
    payload = parse_pasted_path_payload(mixed_text, allow_leading_path=True)
    assert payload is not None
    assert payload.paths == [f1.resolve()]
    assert "explain this diagram" in mixed_text[payload.token_end :]


# ── 3. ChatInput Clipboard Paste & Atomic Placeholder Deletion ───────────────


@pytest.mark.asyncio
async def test_chat_input_paste_clipboard_image_and_attachment_bar() -> None:
    from talkops.ui.widgets.chat_input import MediaAttachmentBar

    app = TalkOpsApp(defer_server_start=True)
    async with app.run_test() as pilot:
        chat_input = app.query_one("#input-area", ChatInput)
        media_bar = app.query_one("#media-attachment-bar", MediaAttachmentBar)
        tracker = app._image_tracker
        assert tracker is not None

        fake_img = ImageData(
            base64_data="YWJj",
            format="png",
            placeholder="[image 1]",
            source="clipboard",
            size_bytes=137_216,  # 134 KB
        )
        with patch(
            "talkops.media_utils.get_clipboard_image",
            return_value=fake_img,
        ):
            ok = await chat_input.paste_clipboard_image()
        await pilot.pause()

        assert ok is True
        # Text input remains clean (no inline [image 1] token)
        assert chat_input.value == ""
        assert len(tracker.get_images()) == 1

        # Collapsed Antigravity CLI-style media bar above input
        collapsed_text = media_bar.render().plain
        assert "▸ 📎 1 media attached (clipboard, 134 KB, image/png)" in collapsed_text
        assert "(ctrl+o to expand)" in collapsed_text

        # Pressing Ctrl+O expands the attachment bar
        await pilot.press("ctrl+o")
        await pilot.pause()
        expanded_text = media_bar.render().plain
        assert "▾ 📎 1 media attached" in expanded_text
        assert "1. clipboard (134 KB, image/png)" in expanded_text

        # Pressing backspace when input text is empty removes the attached media
        await pilot.press("backspace")
        await pilot.pause()

        assert len(tracker.get_images()) == 0
        assert media_bar.has_class("-visible") is False


@pytest.mark.asyncio
async def test_chat_input_dropped_image_path_attaches_to_media_bar(
    tmp_path: Path,
) -> None:
    from talkops.ui.widgets.chat_input import MediaAttachmentBar

    img_path = tmp_path / "arch.png"
    img_path.write_bytes(_TINY_PNG_BYTES)

    app = TalkOpsApp(defer_server_start=True)
    async with app.run_test() as pilot:
        chat_input = app.query_one("#input-area", ChatInput)
        media_bar = app.query_one("#media-attachment-bar", MediaAttachmentBar)
        tracker = app._image_tracker

        handled = chat_input.handle_external_paste(str(img_path))
        await pilot.pause()

        assert handled is True
        assert chat_input.value == ""
        assert len(tracker.get_images()) == 1
        rendered_bar = media_bar.render().plain
        assert "▸ 📎 1 media attached (arch.png," in rendered_bar
        assert "image/png)" in rendered_bar



# ── 4. TextualAdapter Multimodal Stream Input Preparation ────────────────────


def test_textual_adapter_prepare_stream_input_with_tracked_and_mentioned_media(
    tmp_path: Path,
) -> None:
    tracker = MediaTracker()
    pasted_img = ImageData(
        base64_data="cGFzdGVk", format="png", placeholder="[image 1]"
    )
    tracker.add_image(pasted_img)

    mentioned_img_path = tmp_path / "diagram.png"
    mentioned_img_path.write_bytes(_TINY_PNG_BYTES)

    adapter = TextualAdapter(
        client=MagicMock(),
        assistant_id="talkops",
        image_tracker=tracker,
    )

    with patch(
        "talkops.input.parse_file_mentions",
        return_value=("Compare [image 1] with @diagram.png", [mentioned_img_path]),
    ):
        stream_input, _turn_id = adapter._prepare_stream_input(
            "Compare [image 1] with @diagram.png",
            graph_input=None,
            message_kwargs=None,
        )

    messages = stream_input["messages"]
    assert len(messages) == 1
    human_msg = messages[0]
    assert isinstance(human_msg, HumanMessage)
    assert isinstance(human_msg.content, list)

    image_blocks = [
        b
        for b in human_msg.content
        if isinstance(b, dict) and b.get("type") == "image_url"
    ]
    # Both the pasted clipboard image and the @mentioned image file are attached
    assert len(image_blocks) == 2
    # Tracker is cleared after preparing the turn input
    assert len(tracker.get_images()) == 0


# ── 5. StatusBar Prompt-Cache Segment & Hit Rate Rendering ───────────────────


def test_status_bar_cache_segment_rendering_and_thresholds() -> None:
    assert cache_hit_style(10) == "$error"
    assert cache_hit_style(CACHE_WARNING_PERCENT) == "$warning"
    assert cache_hit_style(CACHE_GOOD_PERCENT) == "dim"

    bar = StatusBar()
    mock_display = MagicMock()
    bar.query_one = MagicMock(return_value=mock_display)

    bar.set_context_limit(200_000)
    bar.set_tokens(50_000, context_tokens=20_000)
    bar.set_cost(0.05)

    # 1. With zero cache tokens, no cache segment is rendered
    rendered_no_cache = mock_display.update.call_args[0][0].plain
    assert "cache" not in rendered_no_cache
    assert "50.0K tok · $0.05" in rendered_no_cache

    # 2. With 80% cache hit rate (8,000 read out of 10,000 total input tokens)
    bar.set_cache_tokens(8_000, 2_000, input_tokens=10_000)
    rendered_with_cache = mock_display.update.call_args[0][0].plain
    assert "cache 80% hit" in rendered_with_cache
    assert "8.0K read" in rendered_with_cache
    assert "2.0K write" in rendered_with_cache

    # 3. With cache timing indicator
    bar.set_cache_timing(
        written_at="12:00", expires_at="12:05", confidence="inferred"
    )
    rendered_with_timing = mock_display.update.call_args[0][0].plain
    assert "written 12:00" in rendered_with_timing
    assert "exp ~12:05" in rendered_with_timing


def test_cost_tracking_cache_token_counts_and_adapter_stream_sync() -> None:
    usage = {
        "input_tokens": 12_000,
        "output_tokens": 500,
        "input_token_details": {
            "cache_read": 9_000,
            "cache_creation": 3_000,
            "ephemeral_5m_input_tokens": 3_000,
        },
    }
    c_read, cw_tuple = cache_token_counts(usage)
    assert c_read == 9_000
    assert cw_tuple == (0, 3_000, 0)
    assert sum(cw_tuple) == 3_000

    app = TalkOpsApp(defer_server_start=True)
    status_bar_mock = MagicMock()
    adapter = TextualAdapter(
        client=MagicMock(),
        assistant_id="talkops",
        app=app,
        status_bar=status_bar_mock,
    )
    stream_state = _TurnStreamState()

    adapter._handle_custom_stream_event(
        {
            "type": "session_cost",
            "total": 0.08,
            "total_tokens": 12_500,
            "input_tokens": 12_000,
            "cache_read_tokens": 9_000,
            "cache_write_tokens": 3_000,
            "pricing_ok": True,
        },
        is_main_agent=True,
        stream_state=stream_state,
    )

    assert app._session_cache_input_tokens == 12_000
    assert app._session_cache_read_tokens == 9_000
    assert app._session_cache_write_tokens == 3_000
    status_bar_mock.set_cache_tokens.assert_called_with(
        9_000, 3_000, input_tokens=12_000
    )


@pytest.mark.asyncio
async def test_thread_controller_restores_multimodal_placeholders_and_cache_tokens() -> None:
    mounted = []
    fake_container = MagicMock()
    fake_container.children = []
    fake_container._tool_calls = {}
    status_bar_mock = MagicMock()

    multimodal_human = HumanMessage(
        content=[
            {"type": "text", "text": "What is in this screenshot?"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,YWJj"},
            },
        ]
    )
    ai_reply = AIMessage(
        content="It shows a healthy cluster.",
        usage_metadata={
            "input_tokens": 10_000,
            "output_tokens": 200,
            "total_tokens": 10_200,
            "input_token_details": {
                "cache_read": 7_500,
                "cache_creation": 2_500,
            },
        },
    )

    fake_app = SimpleNamespace(
        _reset_thread_usage=MagicMock(),
        _get_thread_state_values=AsyncMock(
            return_value={
                "messages": [multimodal_human, ai_reply],
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
        _session_cache_input_tokens=0,
        _session_cache_read_tokens=0,
        _session_cache_write_tokens=0,
        _status_bar=status_bar_mock,
        _regroup_completed_tools=AsyncMock(),
    )

    ctrl = ThreadController(fake_app)  # type: ignore[arg-type]
    await ctrl.load_thread_history("thread-multimodal")

    assert len(mounted) == 2
    assert "What is in this screenshot? [image 1]" in mounted[0]._raw_content
    status_bar_mock.set_cache_tokens.assert_called_once_with(
        7_500, 2_500, input_tokens=10_000
    )

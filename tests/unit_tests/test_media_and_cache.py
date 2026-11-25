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

"""Comprehensive unit tests for LangChain DeepAgents multimodal input/output and prompt-cache tracking."""

from __future__ import annotations

import base64
import io
import json
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.content import is_data_content_block
from PIL import Image

from talkops.input import (
    MediaTracker,
    parse_pasted_file_paths,
    parse_pasted_path_payload,
)
from talkops.media_utils import (
    AudioData,
    DocumentData,
    FileData,
    ImageData,
    VideoData,
    classify_media_extension,
    compress_image_bytes,
    create_multimodal_content,
    get_audio_from_path,
    get_clipboard_media,
    get_document_from_path,
    get_file_from_path,
    get_image_from_path,
    get_media_from_path,
    get_video_from_path,
    is_media_path,
    strip_media_placeholders,
    triage_ogg_mime_type,
    validate_binary_media,
    validate_isobmff_container,
    validate_webm_container,
)
from talkops.middleware.compaction import (
    get_user_uploaded_media_dir,
    sync_messages_to_transcript,
)
from talkops.middleware.cost_tracking import cache_token_counts
from talkops.ui.app import TalkOpsApp
from talkops.ui.controllers.thread_controller import ThreadController
from talkops.ui.textual_adapter import (
    TextualAdapter,
    _format_tool_message_content,
    _TurnStreamState,
)
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


def _make_isobmff_bytes(brand: bytes = b"mp42", *, include_moov: bool = True) -> bytes:
    """Build a minimal ISOBMFF (MP4/MOV/M4A) container with optional `moov` and `mdat` atoms."""
    ftyp_payload = brand + b"\x00\x00\x02\x00" + brand + b"isom"
    ftyp_box = struct.pack(">I", 8 + len(ftyp_payload)) + b"ftyp" + ftyp_payload
    mdat_box = struct.pack(">I", 16) + b"mdat" + b"\x00" * 8
    if not include_moov:
        return ftyp_box + mdat_box
    moov_box = struct.pack(">I", 16) + b"moov" + b"\x00" * 8
    return ftyp_box + moov_box + mdat_box


_VALID_MP4_BYTES = _make_isobmff_bytes(b"mp42", include_moov=True)
_VALID_MOV_BYTES = _make_isobmff_bytes(b"qt  ", include_moov=True)
_VALID_M4A_BYTES = _make_isobmff_bytes(b"M4A ", include_moov=True)
_UNFINALIZED_MP4_BYTES = _make_isobmff_bytes(b"mp42", include_moov=False)
_VALID_WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
_VALID_MP3_BYTES = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64\x00\x00\x00\x00"
_VALID_FLAC_BYTES = b"fLaC\x00\x00\x00\x22" + b"\x00" * 34
_VALID_AAC_BYTES = b"\xff\xf1\x50\x80\x02\x1f\xfc" + b"\x00" * 16
_VALID_AIFF_BYTES = b"FORM\x00\x00\x00\x12AIFFCOMM\x00\x00\x00\x02\x00\x01"
_VALID_OGG_OPUS_BYTES = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x13OpusHead\x01\x02\x00\x0f\x80>\x00\x00\x00\x00\x00"
_VALID_OGG_THEORA_BYTES = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x2a\x80theora\x03\x02\x01" + b"\x00" * 32
_VALID_PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
_VALID_PPTX_BYTES = b"PK\x03\x04\x14\x00\x06\x00\x08\x00\x00\x00!\x00" + b"\x00" * 32


# ── 1. Canonical LangChain ContentBlocks & media_utils Tests ─────────────────


def test_all_modalities_emit_canonical_langchain_content_blocks() -> None:
    """Verify ImageData, VideoData, AudioData, and FileData emit canonical LangChain ContentBlocks."""
    img = ImageData(base64_data="YWJj", format="png", placeholder="[image 1]", source="arch.png")
    img_block = img.to_message_content()
    assert img_block["type"] == "image"
    assert img_block["base64"] == "YWJj"
    assert img_block["mime_type"] == "image/png"
    assert is_data_content_block(img_block) is True
    assert img.to_message_content() == img_block
    assert "arch.png ·" in img.expanded_label()

    vid = VideoData(base64_data="eHl6", mime_type="video/mp4", placeholder="[video 1]", source="demo.mp4")
    vid_block = vid.to_message_content()
    assert vid_block["type"] == "video"
    assert vid_block["base64"] == "eHl6"
    assert vid_block["mime_type"] == "video/mp4"
    assert is_data_content_block(vid_block) is True

    aud = AudioData(base64_data="bXAz", mime_type="audio/mpeg", placeholder="[audio 1]", source="song.mp3")
    aud_block = aud.to_message_content()
    assert aud_block["type"] == "audio"
    assert aud_block["base64"] == "bXAz"
    assert aud_block["mime_type"] == "audio/mpeg"
    assert is_data_content_block(aud_block) is True

    doc = FileData(
        base64_data="cGRm",
        mime_type="application/pdf",
        placeholder="[file 1]",
        source="runbook.pdf",
    )
    doc_block = doc.to_message_content()
    assert doc_block["type"] == "file"
    assert doc_block["base64"] == "cGRm"
    assert doc_block["mime_type"] == "application/pdf"
    assert doc_block["extras"]["filename"] == "runbook.pdf"
    assert is_data_content_block(doc_block) is True
    assert DocumentData is FileData


def test_get_media_from_path_loads_image_video_audio_and_document(tmp_path: Path) -> None:
    """Verify get_media_from_path loads image, video, audio/music, and PDF/Office documents."""
    png_file = tmp_path / "screenshot.png"
    png_file.write_bytes(_TINY_PNG_BYTES)

    mp4_file = tmp_path / "demo.mp4"
    mp4_file.write_bytes(_VALID_MP4_BYTES)

    mov_file = tmp_path / "clip.mov"
    mov_file.write_bytes(_VALID_MOV_BYTES)

    mp3_file = tmp_path / "music.mp3"
    mp3_file.write_bytes(_VALID_MP3_BYTES)

    wav_file = tmp_path / "voice.wav"
    wav_file.write_bytes(_VALID_WAV_BYTES)

    flac_file = tmp_path / "lossless.flac"
    flac_file.write_bytes(_VALID_FLAC_BYTES)

    aac_file = tmp_path / "track.aac"
    aac_file.write_bytes(_VALID_AAC_BYTES)

    aiff_file = tmp_path / "studio.aiff"
    aiff_file.write_bytes(_VALID_AIFF_BYTES)

    m4a_file = tmp_path / "podcast.m4a"
    m4a_file.write_bytes(_VALID_M4A_BYTES)

    opus_file = tmp_path / "memo.opus"
    opus_file.write_bytes(_VALID_OGG_OPUS_BYTES)

    pdf_file = tmp_path / "report.pdf"
    pdf_file.write_bytes(_VALID_PDF_BYTES)

    pptx_file = tmp_path / "slides.pptx"
    pptx_file.write_bytes(_VALID_PPTX_BYTES)

    txt_file = tmp_path / "notes.txt"
    txt_file.write_text("hello")

    for p in (
        png_file,
        mp4_file,
        mov_file,
        mp3_file,
        wav_file,
        flac_file,
        aac_file,
        aiff_file,
        m4a_file,
        opus_file,
        pdf_file,
        pptx_file,
    ):
        assert is_media_path(p) is True
    assert is_media_path(txt_file) is False

    assert classify_media_extension(png_file) == "image"
    assert classify_media_extension(mp4_file) == "video"
    assert classify_media_extension(mp3_file) == "audio"
    assert classify_media_extension(pdf_file) == "file"
    assert classify_media_extension(txt_file) == "text"

    img = get_image_from_path(png_file)
    assert isinstance(img, ImageData)
    assert img.format == "png"
    assert base64.b64decode(img.base64_data) == _TINY_PNG_BYTES

    vid = get_video_from_path(mp4_file)
    assert isinstance(vid, VideoData)
    assert vid.mime_type == "video/mp4"

    mov = get_video_from_path(mov_file)
    assert isinstance(mov, VideoData)
    assert mov.mime_type == "video/quicktime"

    aud_mp3 = get_audio_from_path(mp3_file)
    assert isinstance(aud_mp3, AudioData)
    assert aud_mp3.mime_type == "audio/mpeg"

    aud_wav = get_audio_from_path(wav_file)
    assert isinstance(aud_wav, AudioData)
    assert aud_wav.mime_type == "audio/wav"

    aud_flac = get_audio_from_path(flac_file)
    assert isinstance(aud_flac, AudioData)
    assert aud_flac.mime_type == "audio/flac"

    aud_aac = get_audio_from_path(aac_file)
    assert isinstance(aud_aac, AudioData)
    assert aud_aac.mime_type == "audio/aac"

    aud_aiff = get_audio_from_path(aiff_file)
    assert isinstance(aud_aiff, AudioData)
    assert aud_aiff.mime_type == "audio/aiff"

    aud_m4a = get_audio_from_path(m4a_file)
    assert isinstance(aud_m4a, AudioData)
    assert aud_m4a.mime_type == "audio/mp4"

    aud_opus = get_audio_from_path(opus_file)
    assert isinstance(aud_opus, AudioData)
    assert aud_opus.mime_type == "audio/opus"

    doc_pdf = get_file_from_path(pdf_file)
    assert isinstance(doc_pdf, FileData)
    assert doc_pdf.mime_type == "application/pdf"
    assert doc_pdf.filename == "report.pdf"

    doc_pptx = get_document_from_path(pptx_file)
    assert isinstance(doc_pptx, FileData)
    assert doc_pptx.mime_type == "application/vnd.openxmlformats-officedocument.presentationml.presentation"

    assert isinstance(get_media_from_path(png_file), ImageData)
    assert isinstance(get_media_from_path(mp4_file), VideoData)
    assert isinstance(get_media_from_path(mp3_file), AudioData)
    assert isinstance(get_media_from_path(pdf_file), FileData)
    assert get_media_from_path(txt_file) is None


def test_container_validation_moov_webm_ogg_and_animated_gif(tmp_path: Path) -> None:
    """Verify ISOBMFF moov atom validation, WebM block check, Ogg triage, animated GIF conversion, and image compression."""
    # 1. Unfinalized MP4 (has ftyp + mdat but no moov atom) is rejected
    assert validate_isobmff_container(_VALID_MP4_BYTES, require_moov=True) is True
    assert validate_isobmff_container(_UNFINALIZED_MP4_BYTES, require_moov=True) is False
    unfinalized_mp4 = tmp_path / "unfinalized.mp4"
    unfinalized_mp4.write_bytes(_UNFINALIZED_MP4_BYTES)
    assert get_video_from_path(unfinalized_mp4) is None

    # 2. WebM validation with and without media blocks
    webm_header_only = b"\x1a\x45\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x1f"
    webm_with_cluster = webm_header_only + b"\x1f\x43\xb6\x75\x01\x00\x00\x00\xa3\x00\x00"
    assert validate_webm_container(webm_header_only, require_media_blocks=False) is True
    assert validate_webm_container(webm_header_only, require_media_blocks=True) is False
    assert validate_webm_container(webm_with_cluster, require_media_blocks=True) is True

    # 3. Ogg audio vs. video triage
    assert triage_ogg_mime_type(_VALID_OGG_OPUS_BYTES) == "audio/ogg"
    assert triage_ogg_mime_type(_VALID_OGG_THEORA_BYTES) == "video/ogg"

    # 4. Animated GIF is automatically converted to static first-frame PNG
    frames = [Image.new("RGB", (4, 4), color=(255, 0, 0)), Image.new("RGB", (4, 4), color=(0, 255, 0))]
    gif_buf = io.BytesIO()
    frames[0].save(gif_buf, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    anim_gif_path = tmp_path / "anim.gif"
    anim_gif_path.write_bytes(gif_buf.getvalue())

    loaded_gif = get_image_from_path(anim_gif_path)
    assert isinstance(loaded_gif, ImageData)
    assert loaded_gif.format == "png"
    assert loaded_gif.mime_type == "image/png"
    decoded_png = Image.open(io.BytesIO(base64.b64decode(loaded_gif.base64_data)))
    assert decoded_png.format == "PNG"
    assert getattr(decoded_png, "is_animated", False) is False

    # 5. Image compression downscales images larger than max_side
    big_img = Image.new("RGB", (3000, 2000), color=(20, 40, 80))
    big_buf = io.BytesIO()
    big_img.save(big_buf, format="PNG")
    compressed_bytes, fmt = compress_image_bytes(big_buf.getvalue(), max_side=1024)
    assert fmt in {"png", "jpeg"}
    resized_img = Image.open(io.BytesIO(compressed_bytes))
    assert max(resized_img.size) <= 1024

    # 6. validate_binary_media rejects corrupted payloads
    assert validate_binary_media(b"not a pdf", ".pdf") is None
    assert validate_binary_media(_VALID_PDF_BYTES, ".pdf") == "application/pdf"


def test_clipboard_media_supports_finder_copied_files_across_all_modalities(
    tmp_path: Path,
) -> None:
    """Verify get_clipboard_media reads copied Finder files (furl) for photos, music, video, and PDFs."""
    mp3_file = tmp_path / "copied_song.mp3"
    mp3_file.write_bytes(_VALID_MP3_BYTES)

    pdf_file = tmp_path / "copied_spec.pdf"
    pdf_file.write_bytes(_VALID_PDF_BYTES)

    mp4_file = tmp_path / "copied_demo.mp4"
    mp4_file.write_bytes(_VALID_MP4_BYTES)

    with (
        patch("sys.platform", "darwin"),
        patch("talkops.media_utils._get_macos_clipboard_file_path", return_value=mp3_file),
    ):
        media_aud = get_clipboard_media()
        assert isinstance(media_aud, AudioData)
        assert media_aud.source == "copied_song.mp3"
        assert media_aud.mime_type == "audio/mpeg"

    with (
        patch("sys.platform", "darwin"),
        patch("talkops.media_utils._get_macos_clipboard_file_path", return_value=pdf_file),
    ):
        media_doc = get_clipboard_media()
        assert isinstance(media_doc, FileData)
        assert media_doc.source == "copied_spec.pdf"
        assert media_doc.mime_type == "application/pdf"

    with (
        patch("sys.platform", "darwin"),
        patch("talkops.media_utils._get_macos_clipboard_file_path", return_value=mp4_file),
    ):
        media_vid = get_clipboard_media()
        assert isinstance(media_vid, VideoData)
        assert media_vid.source == "copied_demo.mp4"
        assert media_vid.mime_type == "video/mp4"


def test_strip_media_placeholders_and_create_multimodal_content() -> None:
    """Verify strip_media_placeholders and create_multimodal_content handle image, video, audio, and file."""
    raw = "Please inspect [image 1], [video 1], [audio 1], and [file 1] for errors"
    cleaned = strip_media_placeholders(raw)
    for token in ("[image 1]", "[video 1]", "[audio 1]", "[file 1]"):
        assert token not in cleaned
    assert "Please inspect" in cleaned

    img = ImageData(base64_data="YWJj", format="png", placeholder="[image 1]")
    vid = VideoData(base64_data="eHl6", mime_type="video/mp4", placeholder="[video 1]")
    aud = AudioData(base64_data="bXAz", mime_type="audio/mpeg", placeholder="[audio 1]")
    doc = FileData(base64_data="cGRm", mime_type="application/pdf", placeholder="[file 1]", source="doc.pdf")

    blocks = create_multimodal_content(
        cleaned,
        images=[img],
        videos=[vid],
        audios=[aud],
        files=[doc],
    )
    assert blocks[0] == {"type": "text", "text": cleaned}
    assert blocks[1] == img.to_message_content()
    assert blocks[2] == vid.to_message_content()
    assert blocks[3] == aud.to_message_content()
    assert blocks[4] == doc.to_message_content()


# ── 2. MediaTracker & Pasted Path Parsing Tests ──────────────────────────────


def test_media_tracker_lifecycle_and_sync_all_modalities() -> None:
    """Verify MediaTracker tracks images, videos, audios, and files and syncs to text."""
    tracker = MediaTracker()
    img1 = ImageData(base64_data="a1", format="png", placeholder="")
    img2 = ImageData(base64_data="a2", format="png", placeholder="")
    vid1 = VideoData(base64_data="v1", mime_type="video/mp4", placeholder="")
    aud1 = AudioData(base64_data="m1", mime_type="audio/mpeg", placeholder="")
    doc1 = FileData(base64_data="f1", mime_type="application/pdf", placeholder="")

    p1 = tracker.add_image(img1)
    p2 = tracker.add_image(img2)
    pv1 = tracker.add_video(vid1)
    pa1 = tracker.add_audio(aud1)
    pf1 = tracker.add_file(doc1)

    assert p1 == "[image 1]"
    assert p2 == "[image 2]"
    assert pv1 == "[video 1]"
    assert pa1 == "[audio 1]"
    assert pf1 == "[file 1]"
    assert len(tracker.get_images()) == 2
    assert len(tracker.get_videos()) == 1
    assert len(tracker.get_audios()) == 1
    assert len(tracker.get_files()) == 1
    assert tracker.count() == 5

    snap = tracker.snapshot()

    # Sync when [image 1] and [audio 1] are removed from text
    tracker.sync_to_text("Look at [image 2], [video 1], and [file 1]")
    assert [i.placeholder for i in tracker.get_images()] == ["[image 2]"]
    assert [v.placeholder for v in tracker.get_videos()] == ["[video 1]"]
    assert tracker.get_audios() == []
    assert [f.placeholder for f in tracker.get_files()] == ["[file 1]"]

    # Restore from snapshot
    tracker.restore(snap)
    assert len(tracker.get_images()) == 2
    assert len(tracker.get_videos()) == 1
    assert len(tracker.get_audios()) == 1
    assert len(tracker.get_files()) == 1


def test_parse_pasted_file_paths_handles_spaces_quotes_and_file_urls(
    tmp_path: Path,
) -> None:
    """Verify parse pasted file paths handles spaces quotes and file urls."""
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


# ── 3. ChatInput Clipboard Paste, Drag-and-Drop & MediaAttachmentBar ─────────


@pytest.mark.asyncio
async def test_chat_input_paste_clipboard_media_and_attachment_bar() -> None:
    """Verify ChatInput clipboard paste and MediaAttachmentBar expand/collapse layout."""
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
            size_bytes=172_032,  # 168 KB
        )
        with patch(
            "talkops.media_utils.get_clipboard_media",
            return_value=fake_img,
        ):
            ok = await chat_input.paste_clipboard_image()
        await pilot.pause()

        assert ok is True
        assert chat_input.value == ""
        assert len(tracker.get_images()) == 1

        # Collapsed media attachment bar above input
        collapsed_text = media_bar.render().plain
        assert "▸ 📎 1 media attached (clipboard, 168 KB, image/png)" in collapsed_text
        assert "(ctrl+o to expand)" in collapsed_text

        # Pressing Ctrl+O expands the media attachment bar details
        await pilot.press("ctrl+o")
        await pilot.pause()
        expanded_text = media_bar.render().plain
        assert "▾ 📎 1 Media(s) attached" in expanded_text
        assert "1. clipboard · 168 KB · image/png" in expanded_text
        assert "(ctrl+o to collapse)" in expanded_text

        # Pressing backspace when input text is empty removes the attached media
        await pilot.press("backspace")
        await pilot.pause()

        assert len(tracker.get_images()) == 0
        assert media_bar.has_class("-visible") is False


@pytest.mark.asyncio
async def test_chat_input_dropped_multimodal_paths_attach_to_media_bar(
    tmp_path: Path,
) -> None:
    """Verify dropping image, audio, video, and PDF paths attaches all 4 modalities to MediaAttachmentBar."""
    from talkops.ui.widgets.chat_input import MediaAttachmentBar

    img_path = tmp_path / "arch.png"
    img_path.write_bytes(_TINY_PNG_BYTES)
    mp3_path = tmp_path / "theme.mp3"
    mp3_path.write_bytes(_VALID_MP3_BYTES)
    mp4_path = tmp_path / "demo.mp4"
    mp4_path.write_bytes(_VALID_MP4_BYTES)
    pdf_path = tmp_path / "spec.pdf"
    pdf_path.write_bytes(_VALID_PDF_BYTES)

    app = TalkOpsApp(defer_server_start=True)
    async with app.run_test() as pilot:
        chat_input = app.query_one("#input-area", ChatInput)
        media_bar = app.query_one("#media-attachment-bar", MediaAttachmentBar)
        tracker = app._image_tracker
        assert tracker is not None

        for p in (img_path, mp3_path, mp4_path, pdf_path):
            assert chat_input.handle_external_paste(str(p)) is True
            await pilot.pause()

        assert chat_input.value == ""
        assert len(tracker.get_images()) == 1
        assert len(tracker.get_audios()) == 1
        assert len(tracker.get_videos()) == 1
        assert len(tracker.get_files()) == 1

        await pilot.press("ctrl+o")
        await pilot.pause()
        expanded_text = media_bar.render().plain
        assert "▾ 📎 4 Media(s) attached" in expanded_text
        assert "arch.png" in expanded_text and "image/png" in expanded_text
        assert "theme.mp3" in expanded_text and "audio/mpeg" in expanded_text
        assert "demo.mp4" in expanded_text and "video/mp4" in expanded_text
        assert "spec.pdf" in expanded_text and "application/pdf" in expanded_text


# ── 4. TextualAdapter Multimodal Stream Input Preparation ────────────────────


def test_textual_adapter_prepare_stream_input_with_all_modalities(
    tmp_path: Path,
) -> None:
    """Verify TextualAdapter._prepare_stream_input builds canonical LangChain content_blocks for all modalities."""
    tracker = MediaTracker()
    pasted_img = ImageData(base64_data="cGFzdGVk", format="png", placeholder="[image 1]")
    pasted_aud = AudioData(base64_data="YXVkaW8=", mime_type="audio/mpeg", placeholder="[audio 1]")
    tracker.add_image(pasted_img)
    tracker.add_audio(pasted_aud)

    mentioned_img_path = tmp_path / "diagram.png"
    mentioned_img_path.write_bytes(_TINY_PNG_BYTES)
    mentioned_vid_path = tmp_path / "capture.mp4"
    mentioned_vid_path.write_bytes(_VALID_MP4_BYTES)
    mentioned_pdf_path = tmp_path / "runbook.pdf"
    mentioned_pdf_path.write_bytes(_VALID_PDF_BYTES)

    adapter = TextualAdapter(
        client=MagicMock(),
        assistant_id="talkops",
        image_tracker=tracker,
    )

    with patch(
        "talkops.input.parse_file_mentions",
        return_value=(
            "Analyze [image 1] [audio 1] with @diagram.png @capture.mp4 @runbook.pdf",
            [mentioned_img_path, mentioned_vid_path, mentioned_pdf_path],
        ),
    ):
        stream_input, _turn_id = adapter._prepare_stream_input(
            "Analyze [image 1] [audio 1] with @diagram.png @capture.mp4 @runbook.pdf",
            graph_input=None,
            message_kwargs=None,
        )

    messages = stream_input["messages"]
    assert len(messages) == 1
    human_msg = messages[0]
    assert isinstance(human_msg, HumanMessage)
    blocks = human_msg.content_blocks

    assert [b["type"] for b in blocks] == ["text", "image", "image", "video", "audio", "file"]
    assert "[image 1]" not in blocks[0]["text"]
    assert "[audio 1]" not in blocks[0]["text"]
    assert blocks[5]["mime_type"] == "application/pdf"
    assert blocks[5]["extras"]["filename"] == "runbook.pdf"

    # Tracker is cleared after preparing the turn input
    assert tracker.count() == 0


# ── 5. .user_uploaded Persistence & Transcript Media Logging ─────────────────


def test_user_uploaded_media_persistence_and_transcript_logging(tmp_path: Path) -> None:
    """Verify user-attached media is saved to .user_uploaded/ and logged in transcript.jsonl."""
    img = ImageData(
        base64_data=base64.b64encode(_TINY_PNG_BYTES).decode("ascii"),
        format="png",
        placeholder="[image 1]",
        source="clipboard",
    )
    aud = AudioData(
        base64_data=base64.b64encode(_VALID_MP3_BYTES).decode("ascii"),
        mime_type="audio/mpeg",
        placeholder="[audio 1]",
        source="song.mp3",
    )
    doc = FileData(
        base64_data=base64.b64encode(_VALID_PDF_BYTES).decode("ascii"),
        mime_type="application/pdf",
        placeholder="[file 1]",
        source="report.pdf",
    )

    human_msg = HumanMessage(
        content_blocks=create_multimodal_content(
            "Check this screenshot, song, and PDF",
            images=[img],
            audios=[aud],
            files=[doc],
        )
    )
    ai_msg = AIMessage(content="All three attachments look valid.")

    compact_path, full_path = sync_messages_to_transcript(
        [human_msg, ai_msg],
        thread_id="thread-media-test",
        data_dir=tmp_path,
    )

    upload_dir = get_user_uploaded_media_dir("thread-media-test", tmp_path)
    assert upload_dir.exists()
    uploaded_files = sorted(upload_dir.iterdir())
    assert len(uploaded_files) == 3
    assert [f.suffix for f in uploaded_files] == [".png", ".mp3", ".pdf"]
    assert uploaded_files[0].read_bytes() == _TINY_PNG_BYTES
    assert uploaded_files[1].read_bytes() == _VALID_MP3_BYTES
    assert uploaded_files[2].read_bytes() == _VALID_PDF_BYTES

    compact_records = [json.loads(line) for line in compact_path.read_text().splitlines() if line.strip()]
    full_records = [json.loads(line) for line in full_path.read_text().splitlines() if line.strip()]
    assert len(compact_records) == 2
    assert compact_records[0]["type"] == "USER_INPUT"
    assert compact_records[0]["content"] == "Check this screenshot, song, and PDF"
    assert len(compact_records[0]["media"]) == 3
    assert [m["mime_type"] for m in compact_records[0]["media"]] == [
        "image/png",
        "audio/mpeg",
        "application/pdf",
    ]
    assert compact_records[0]["media"] == full_records[0]["media"]


def test_multimodal_tool_message_formatting_and_unsupported_content_middleware() -> None:
    """Verify _format_tool_message_content and UnsupportedContentMiddleware for subagents."""
    from deepagents.middleware.unsupported_content import UnsupportedContentMiddleware

    tool_msg = ToolMessage(
        content_blocks=[
            {
                "type": "image",
                "base64": base64.b64encode(_TINY_PNG_BYTES).decode("ascii"),
                "mime_type": "image/png",
            }
        ],
        name="read_file",
        tool_call_id="call_1",
        additional_kwargs={"read_file_path": "/workspace/arch.png"},
    )
    formatted = _format_tool_message_content(tool_msg)
    assert "📎 Loaded image/png (arch.png," in formatted
    assert "iVBOR" not in formatted

    # Verify UnsupportedContentMiddleware replaces unsupported audio/image blocks when model.profile rejects them
    mw = UnsupportedContentMiddleware()
    text_only_model = SimpleNamespace(
        profile={
            "image_inputs": False,
            "audio_inputs": False,
            "video_inputs": False,
            "pdf_inputs": False,
        }
    )
    human_with_media = HumanMessage(
        content_blocks=[
            {"type": "text", "text": "Listen to this"},
            {"type": "audio", "base64": "bXAz", "mime_type": "audio/mpeg"},
        ]
    )
    filtered = mw._filter_message(human_with_media, model=text_only_model)  # type: ignore[arg-type]
    assert filtered.content_blocks[0]["type"] == "text"
    assert filtered.content_blocks[1]["type"] == "text"
    assert "does not support audio content" in filtered.content_blocks[1]["text"]


# ── 6. StatusBar Prompt-Cache Segment & ThreadController History Tests ───────


def test_status_bar_cache_segment_rendering_and_thresholds() -> None:
    """Verify status bar cache segment rendering and thresholds."""
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
    """Verify cost tracking cache token counts and adapter stream sync."""
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
async def test_thread_controller_restores_all_multimodal_placeholders_and_skips_synthetic_video_frames() -> None:
    """Verify ThreadController restores [image N], [video N], [audio N], [file N] and skips synthetic read_file frames."""
    mounted = []
    fake_container = MagicMock()
    fake_container.children = []
    fake_container._tool_calls = {}
    status_bar_mock = MagicMock()

    multimodal_human = HumanMessage(
        content=[
            {"type": "text", "text": "Inspect all four attachments:"},
            {"type": "image", "base64": "YWJj", "mime_type": "image/png"},
            {"type": "video", "base64": "eHl6", "mime_type": "video/mp4"},
            {"type": "audio", "base64": "bXAz", "mime_type": "audio/mpeg"},
            {"type": "file", "base64": "cGRm", "mime_type": "application/pdf"},
        ]
    )
    synthetic_video_frames = HumanMessage(
        content=[
            {"type": "text", "text": "Reading first 5s of demo.mp4 at 1.0 fps."},
            {"type": "image", "base64": "YWJj", "mime_type": "image/jpeg"},
        ],
        additional_kwargs={"read_file_media_result": True, "read_file_path": "/demo.mp4"},
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
                "messages": [multimodal_human, synthetic_video_frames, ai_reply],
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

    # Synthetic read_file_media_result message is skipped, leaving only the real UserMessage + AssistantMessage
    assert len(mounted) == 2
    assert (
        mounted[0]._raw_content
        == "Inspect all four attachments: [image 1] [video 1] [audio 1] [file 1]"
    )
    status_bar_mock.set_cache_tokens.assert_called_once_with(
        7_500, 2_500, input_tokens=10_000
    )

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

"""Utilities for handling image and video paste, clipboard detection, and multimodal content."""

from __future__ import annotations

import base64
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.messages.content import create_video_block
from PIL import Image

from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)

# Maximum file size in bytes (20 MB, safe across major providers)
MAX_MEDIA_BYTES = 20 * 1024 * 1024

# Supported image extensions
IMAGE_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".tiff",
        ".tif",
    }
)

# Supported video extensions
VIDEO_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".mov",
        ".webm",
    }
)

# Extension -> MIME type mapping for videos
VIDEO_MIME_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}

# Magic byte signatures for video formats
_WEBM_MAGIC = b"\x1a\x45\xdf\xa3"  # EBML header
# ISOBMFF (MP4/MOV) files have "ftyp" at byte offset 4
_FTYP_MARKER = b"ftyp"
_QUICKTIME_BRANDS = frozenset({b"qt  "})


def format_media_size(size_bytes: int) -> str:
    """Format byte count into human-readable size (e.g. '134 KB', '1.2 MB')."""
    if size_bytes <= 0:
        return "0 B"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{max(1, round(size_bytes / 1024))} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


@dataclass
class ImageData:
    """Represents a pasted or loaded image ready to be sent to the LLM."""

    base64_data: str
    format: str  # "png", "jpeg", "gif", "webp"
    placeholder: str  # Display text like "[image 1]" or "[image]"
    source: str = "clipboard"
    size_bytes: int = 0

    def __post_init__(self) -> None:
        """Validate and normalize fields after dataclass initialization."""
        if self.size_bytes <= 0 and self.base64_data:
            self.size_bytes = max(1, (len(self.base64_data.rstrip("=")) * 3) // 4)

    @property
    def mime_type(self) -> str:
        """Return the MIME type for this image (e.g. 'image/png')."""
        return f"image/{self.format}"

    def summary_label(self) -> str:
        """Return 'source, size, mime_type' (e.g. 'clipboard, 134 KB, image/png')."""
        return f"{self.source}, {format_media_size(self.size_bytes)}, {self.mime_type}"

    def to_message_content(self) -> dict[str, Any]:
        """Convert to LangChain multimodal message content format.

        Returns:
            Dict compatible with LangChain's HumanMessage content list
        """
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:image/{self.format};base64,{self.base64_data}",
            },
        }


@dataclass
class VideoData:
    """Represents a pasted or loaded video ready to be sent to the LLM."""

    base64_data: str
    mime_type: str  # "video/mp4", "video/quicktime", "video/webm"
    placeholder: str  # Display text like "[video 1]" or "[video]"
    source: str = "clipboard"
    size_bytes: int = 0

    def __post_init__(self) -> None:
        """Validate and normalize fields after dataclass initialization."""
        if self.size_bytes <= 0 and self.base64_data:
            self.size_bytes = max(1, (len(self.base64_data.rstrip("=")) * 3) // 4)

    def summary_label(self) -> str:
        """Return 'source, size, mime_type' (e.g. 'demo.mp4, 1.2 MB, video/mp4')."""
        return f"{self.source}, {format_media_size(self.size_bytes)}, {self.mime_type}"

    def to_message_content(self) -> dict[str, Any]:
        """Convert to LangChain VideoContentBlock format.

        Returns:
            VideoContentBlock compatible with LangChain's HumanMessage content list
        """
        return dict(
            create_video_block(
                base64=self.base64_data,
                mime_type=self.mime_type,
            )
        )


def get_clipboard_image() -> ImageData | None:
    """Attempt to read an image from the system clipboard.

    Supports macOS via `pngpaste` or `osascript` fallback.

    Returns:
        ImageData if an image is present in clipboard, None otherwise
    """
    if sys.platform == "darwin":
        return _get_macos_clipboard_image()
    if sys.platform.startswith("linux"):
        return _get_linux_clipboard_image()
    return None


def _get_linux_clipboard_image() -> ImageData | None:
    """Get clipboard image on Linux using xclip or wl-paste if available."""
    wl_paste_path = shutil.which("wl-paste")
    if wl_paste_path and os.environ.get("WAYLAND_DISPLAY"):
        try:
            result = subprocess.run(
                [wl_paste_path, "--type", "image/png"],
                capture_output=True,
                check=False,
                timeout=2,
            )
            if result.returncode == 0 and result.stdout:
                try:
                    Image.open(io.BytesIO(result.stdout))
                    base64_data = base64.b64encode(result.stdout).decode("utf-8")
                    return ImageData(
                        base64_data=base64_data,
                        format="png",
                        placeholder="[image]",
                    )
                except Exception:
                    pass
        except (subprocess.TimeoutExpired, OSError):
            pass

    xclip_path = shutil.which("xclip")
    if xclip_path and os.environ.get("DISPLAY"):
        try:
            result = subprocess.run(
                [xclip_path, "-selection", "clipboard", "-t", "image/png", "-o"],
                capture_output=True,
                check=False,
                timeout=2,
            )
            if result.returncode == 0 and result.stdout:
                try:
                    Image.open(io.BytesIO(result.stdout))
                    base64_data = base64.b64encode(result.stdout).decode("utf-8")
                    return ImageData(
                        base64_data=base64_data,
                        format="png",
                        placeholder="[image]",
                    )
                except Exception:
                    pass
        except (subprocess.TimeoutExpired, OSError):
            pass

    return None


def _get_macos_clipboard_image() -> ImageData | None:
    """Get clipboard image on macOS using pngpaste or osascript.

    First tries `pngpaste` (faster if installed), then falls back to `osascript`.

    Returns:
        ImageData if an image is in the clipboard, None otherwise
    """
    # Try pngpaste first (fast if installed)
    pngpaste_path = shutil.which("pngpaste")
    if pngpaste_path:
        try:
            result = subprocess.run(
                [pngpaste_path, "-"],
                capture_output=True,
                check=False,
                timeout=2,
            )
            if result.returncode == 0 and result.stdout:
                try:
                    Image.open(io.BytesIO(result.stdout))
                    base64_data = base64.b64encode(result.stdout).decode("utf-8")
                    return ImageData(
                        base64_data=base64_data,
                        format="png",  # pngpaste always outputs PNG
                        placeholder="[image]",
                    )
                except Exception:
                    logger.debug("Invalid image data from pngpaste", exc_info=True)
        except (subprocess.TimeoutExpired, OSError):
            logger.debug("pngpaste failed, falling back to osascript", exc_info=True)

    # Fallback: use osascript with a temp file
    return _get_clipboard_via_osascript()


def _get_clipboard_via_osascript() -> ImageData | None:
    """Get clipboard image via osascript using a temp file.

    osascript can't output raw binary to stdout, so we write to a temp file.

    Returns:
        ImageData if an image is in the clipboard, None otherwise
    """
    osascript_path = shutil.which("osascript")
    if not osascript_path:
        return None

    # Create a temp file for the image
    fd, temp_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)

    try:
        # First check if clipboard has PNG or TIFF data
        check_result = subprocess.run(
            [osascript_path, "-e", "clipboard info"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )

        if check_result.returncode != 0:
            return None

        clipboard_info = check_result.stdout.lower()
        if "pngf" not in clipboard_info and "tiff" not in clipboard_info:
            return None

        # Try to get PNG first, fall back to TIFF
        if "pngf" in clipboard_info:
            get_script = f"""
            set pngData to the clipboard as «class PNGf»
            set theFile to open for access POSIX file "{temp_path}" with write permission
            set eof of theFile to 0
            write pngData to theFile
            close access theFile
            return "ok"
            """
        else:
            get_script = f"""
            set tiffData to the clipboard as TIFF picture
            set theFile to open for access POSIX file "{temp_path}" with write permission
            set eof of theFile to 0
            write tiffData to theFile
            close access theFile
            return "ok"
            """

        result = subprocess.run(
            [osascript_path, "-e", get_script],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )

        if result.returncode != 0 or "ok" not in result.stdout:
            return None

        temp_path_obj = Path(temp_path)
        if not temp_path_obj.exists() or temp_path_obj.stat().st_size == 0:
            return None

        image_bytes = temp_path_obj.read_bytes()

        try:
            img = Image.open(io.BytesIO(image_bytes))
            output = io.BytesIO()
            img.save(output, format="PNG")
            png_bytes = output.getvalue()
            base64_data = base64.b64encode(png_bytes).decode("utf-8")
            return ImageData(
                base64_data=base64_data,
                format="png",
                placeholder="[image]",
            )
        except Exception:
            logger.debug("Failed to process clipboard image via PIL", exc_info=True)
            return None

    except (subprocess.TimeoutExpired, OSError):
        logger.debug("osascript clipboard check failed", exc_info=True)
        return None
    finally:
        try:
            Path(temp_path).unlink()
        except OSError:
            pass


def encode_image_to_base64(image_bytes: bytes) -> str:
    """Encode raw image bytes to a base64 string."""
    return base64.b64encode(image_bytes).decode("utf-8")


_PLACEHOLDER_RE = re.compile(r"\[(?:image|video)\s+\d+\]|\[(?:image|video)\]")
_MULTI_SPACE_RE = re.compile(r"[^\S\n]{2,}")
_BLANK_LINE_CLEANUP_RE = re.compile(r"[^\S\n]*\n[^\S\n]*")


def strip_media_placeholders(text: str) -> str:
    """Remove `[image N]` / `[video N]` display placeholders from prompt text.

    These tokens are UI-only badges inserted by `MediaTracker` so the user can
    see (and backspace-delete) attached media in the input box. The actual
    images/videos are sent as structured content blocks, so leaving the literal
    tokens in the text block is redundant and can confuse models into thinking
    the user is referring to a text label rather than the attached media.
    """
    cleaned = _PLACEHOLDER_RE.sub("", text)
    cleaned = _MULTI_SPACE_RE.sub(" ", cleaned)
    cleaned = _BLANK_LINE_CLEANUP_RE.sub("\n", cleaned)
    return cleaned.strip()


def create_multimodal_content(
    text: str,
    images: Sequence[ImageData] = (),
    videos: Sequence[VideoData] = (),
) -> list[dict[str, Any]]:
    """Create multimodal message content with text, images, and videos.

    Args:
        text: The text prompt (may contain `[image N]` / `[video N]` display
            placeholders, which are stripped before building the text block)
        images: Sequence of ImageData objects to include
        videos: Sequence of VideoData objects to include

    Returns:
        List of content blocks in LangChain multimodal format
    """
    content: list[dict[str, Any]] = []

    clean_text = strip_media_placeholders(text)
    if clean_text:
        content.append({"type": "text", "text": clean_text})

    content.extend(image.to_message_content() for image in images)
    content.extend(video.to_message_content() for video in videos)

    return content


def get_image_from_path(file_path: str | Path) -> ImageData | None:
    """Load an image from a file path and return as `ImageData`.

    Validates extension, file existence, size limit, and image integrity via PIL.
    """
    path = Path(file_path) if isinstance(file_path, str) else file_path

    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return None

    if not path.exists() or not path.is_file():
        return None

    try:
        file_size = path.stat().st_size
    except OSError:
        logger.debug("Failed to stat image file %s", path, exc_info=True)
        return None

    if file_size == 0:
        return None

    if file_size > MAX_MEDIA_BYTES:
        logger.warning(
            "Image file %s (%d bytes) exceeds maximum size (%d bytes)",
            path,
            file_size,
            MAX_MEDIA_BYTES,
        )
        return None

    try:
        image_bytes = path.read_bytes()
        img = Image.open(io.BytesIO(image_bytes))
        pil_format = img.format
        if not pil_format:
            return None

        format_lower = pil_format.lower()
        if format_lower in {"png", "jpeg", "gif", "webp"}:
            img.verify()
            base64_data = base64.b64encode(image_bytes).decode("utf-8")
            return ImageData(
                base64_data=base64_data,
                format=format_lower,
                placeholder="[image]",
                source=path.name,
                size_bytes=file_size,
            )

        img.load()
        output = io.BytesIO()
        img.save(output, format="PNG")
        png_bytes = output.getvalue()
        base64_data = base64.b64encode(png_bytes).decode("utf-8")
        return ImageData(
            base64_data=base64_data,
            format="png",
            placeholder="[image]",
            source=path.name,
            size_bytes=len(png_bytes),
        )
    except Exception:
        logger.debug("Failed to load image from path %s", path, exc_info=True)
        return None


def _validate_video_header(header: bytes, expected_suffix: str) -> str | None:
    """Validate video magic bytes and return the detected MIME type.

    Returns:
        MIME type string if valid, None if the header doesn't match any known
            video container format.
    """
    if len(header) < 12:
        return None

    if header[:4] == _WEBM_MAGIC:
        return "video/webm"

    if header[4:8] == _FTYP_MARKER:
        brand = header[8:12]
        if brand in _QUICKTIME_BRANDS:
            return "video/quicktime"
        return VIDEO_MIME_TYPES.get(expected_suffix, "video/mp4")

    return None


def get_video_from_path(file_path: str | Path) -> VideoData | None:
    """Load a video from a file path and return as `VideoData`.

    Validates extension, file existence, size limit, and magic byte header.
    """
    path = Path(file_path) if isinstance(file_path, str) else file_path

    suffix = path.suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        return None

    if not path.exists() or not path.is_file():
        return None

    try:
        file_size = path.stat().st_size
    except OSError:
        logger.debug("Failed to stat video file %s", path, exc_info=True)
        return None

    if file_size == 0:
        return None

    if file_size > MAX_MEDIA_BYTES:
        logger.warning(
            "Video file %s (%d bytes) exceeds maximum size (%d bytes)",
            path,
            file_size,
            MAX_MEDIA_BYTES,
        )
        return None

    try:
        video_bytes = path.read_bytes()
        mime_type = _validate_video_header(video_bytes[:12], suffix)
        if mime_type is None:
            logger.debug("File %s failed video header validation", path)
            return None

        base64_data = base64.b64encode(video_bytes).decode("utf-8")
        return VideoData(
            base64_data=base64_data,
            mime_type=mime_type,
            placeholder="[video]",
            source=path.name,
            size_bytes=file_size,
        )
    except Exception:
        logger.debug("Failed to load video from path %s", path, exc_info=True)
        return None


def get_media_from_path(file_path: str | Path) -> ImageData | VideoData | None:
    """Load an image or video from a file path."""
    path = Path(file_path) if isinstance(file_path, str) else file_path
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return get_image_from_path(path)
    if suffix in VIDEO_EXTENSIONS:
        return get_video_from_path(path)
    return None


def is_media_path(file_path: str | Path) -> bool:
    """Return True if *file_path* has a supported image or video file extension."""
    suffix = (Path(file_path) if isinstance(file_path, str) else file_path).suffix.lower()
    return suffix in IMAGE_EXTENSIONS or suffix in VIDEO_EXTENSIONS


__all__ = [
    "IMAGE_EXTENSIONS",
    "MAX_MEDIA_BYTES",
    "VIDEO_EXTENSIONS",
    "VIDEO_MIME_TYPES",
    "ImageData",
    "VideoData",
    "create_multimodal_content",
    "encode_image_to_base64",
    "format_media_size",
    "get_clipboard_image",
    "get_image_from_path",
    "get_media_from_path",
    "get_video_from_path",
    "is_media_path",
    "strip_media_placeholders",
]

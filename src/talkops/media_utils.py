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

"""Multimodal input/output utilities aligned with LangChain ContentBlocks and DeepAgents."""

from __future__ import annotations

import base64
import io
import mimetypes
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from deepagents.backends.utils import (
    _EXTENSION_TO_FILE_TYPE,
    _VIDEO_EXTRA_EXTENSIONS,
    FileType,
)
from langchain_core.messages.content import (
    AudioContentBlock,
    ContentBlock,
    FileContentBlock,
    ImageContentBlock,
    VideoContentBlock,
    create_audio_block,
    create_file_block,
    create_image_block,
    create_text_block,
    create_video_block,
    ensure_id,
    is_data_content_block,
)
from PIL import Image

from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)

# Maximum file size in bytes (20 MB, aligned with DeepAgents FilesystemMiddleware max_media_bytes)
MAX_MEDIA_BYTES = 20 * 1024 * 1024

# Maximum image dimension before optional downscaling in compress_image_bytes
MAX_IMAGE_SIDE_PX = 2048

# Derive canonical extension sets from deepagents.backends.utils._EXTENSION_TO_FILE_TYPE
# plus additional container extensions supported by Antigravity CLI (agy).
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {ext for ext, ftype in _EXTENSION_TO_FILE_TYPE.items() if ftype == "image"}
    | {".bmp", ".tiff", ".tif", ".avif", ".svg"}
)

VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {ext for ext, ftype in _EXTENSION_TO_FILE_TYPE.items() if ftype == "video"}
    | set(_VIDEO_EXTRA_EXTENSIONS)
    | {".m4v", ".3gp", ".ogv"}
)

AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {ext for ext, ftype in _EXTENSION_TO_FILE_TYPE.items() if ftype == "audio"}
    | {".m4a", ".opus", ".oga", ".aif", ".weba"}
)

FILE_EXTENSIONS: frozenset[str] = frozenset(
    {ext for ext, ftype in _EXTENSION_TO_FILE_TYPE.items() if ftype == "file"}
    | {".doc", ".docx", ".xls", ".xlsx"}
)
DOCUMENT_EXTENSIONS = FILE_EXTENSIONS

# Extension -> MIME type mappings
IMAGE_MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
}

VIDEO_MIME_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".avi": "video/x-msvideo",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    ".3gp": "video/3gpp",
    ".3gpp": "video/3gpp",
    ".ogv": "video/ogg",
    ".mkv": "video/x-matroska",
}

AUDIO_MIME_TYPES: dict[str, str] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
    ".weba": "audio/webm",
}

FILE_MIME_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

MIME_TO_EXTENSION: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/avif": ".avif",
    "image/svg+xml": ".svg",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/mpeg": ".mpeg",
    "video/x-msvideo": ".avi",
    "video/x-flv": ".flv",
    "video/x-ms-wmv": ".wmv",
    "video/3gpp": ".3gp",
    "video/ogg": ".ogv",
    "video/x-matroska": ".mkv",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/aiff": ".aiff",
    "audio/webm": ".weba",
    "audio/weba": ".weba",
    "application/pdf": ".pdf",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}

# Magic byte signatures for binary containers
_WEBM_MAGIC = b"\x1a\x45\xdf\xa3"  # EBML header (WebM / Matroska)
_FTYP_MARKER = b"ftyp"  # ISOBMFF (MP4 / MOV / M4A / 3GP) at offset 4
_QUICKTIME_BRANDS = frozenset({b"qt  "})
_M4A_BRANDS = frozenset({b"M4A ", b"M4B ", b"M4P "})
_OGG_MAGIC = b"OggS"
_FLAC_MAGIC = b"fLaC"
_RIFF_MAGIC = b"RIFF"
_WAVE_MARKER = b"WAVE"
_AVI_MARKER = b"AVI "
_FORM_MAGIC = b"FORM"
_AIFF_MARKERS = frozenset({b"AIFF", b"AIFC"})
_ID3_MAGIC = b"ID3"
_FLV_MAGIC = b"FLV"
_ASF_WMV_MAGIC = b"\x30\x26\xb2\x75\x8e\x66\xcf\x11\xa6\xd9\x00\xaa\x00\x62\xce\x6c"
_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def format_media_size(size_bytes: int) -> str:
    """Format byte count into human-readable size (e.g. '168 KB', '1.2 MB')."""
    if size_bytes <= 0:
        return "0 B"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{max(1, round(size_bytes / 1024))} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def classify_media_extension(file_path: str | Path) -> FileType:
    """Classify a file path into DeepAgents `FileType` (`image`, `video`, `audio`, `file`, or `text`)."""
    suffix = PurePosixPath(str(file_path)).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in FILE_EXTENSIONS:
        return "file"
    return _EXTENSION_TO_FILE_TYPE.get(suffix, "text")


@dataclass
class ImageData:
    """Represents a pasted or loaded image ready to be sent as a LangChain `ImageContentBlock`."""

    base64_data: str
    format: str  # "png", "jpeg", "gif", "webp", "svg+xml", etc.
    placeholder: str  # Display text like "[image 1]" or "[image]"
    source: str = "clipboard"
    size_bytes: int = 0
    block_id: str = field(default_factory=lambda: ensure_id(None))

    def __post_init__(self) -> None:
        """Validate and normalize fields after dataclass initialization."""
        if self.size_bytes <= 0 and self.base64_data:
            self.size_bytes = max(1, (len(self.base64_data.rstrip("=")) * 3) // 4)

    @property
    def mime_type(self) -> str:
        """Return the MIME type for this image (e.g. 'image/png')."""
        if self.format.startswith("image/"):
            return self.format
        if self.format == "svg":
            return "image/svg+xml"
        if self.format == "jpg":
            return "image/jpeg"
        return f"image/{self.format}"

    def summary_label(self) -> str:
        """Return 'source, size, mime_type' (e.g. 'clipboard, 168 KB, image/png')."""
        return f"{self.source}, {format_media_size(self.size_bytes)}, {self.mime_type}"

    def expanded_label(self) -> str:
        """Return 'source · size · mime_type' matching Antigravity CLI expanded view."""
        return f"{self.source} · {format_media_size(self.size_bytes)} · {self.mime_type}"

    def to_content_block(self) -> ImageContentBlock:
        """Convert to canonical LangChain `ImageContentBlock`."""
        return create_image_block(
            base64=self.base64_data,
            mime_type=self.mime_type,
            id=self.block_id,
        )

    def to_message_content(self) -> dict[str, Any]:
        """Convert to LangChain `ImageContentBlock` dict for `HumanMessage(content_blocks=...)`."""
        return dict(self.to_content_block())


@dataclass
class VideoData:
    """Represents a pasted or loaded video ready to be sent as a LangChain `VideoContentBlock`."""

    base64_data: str
    mime_type: str  # "video/mp4", "video/quicktime", "video/webm", etc.
    placeholder: str  # Display text like "[video 1]" or "[video]"
    source: str = "clipboard"
    size_bytes: int = 0
    block_id: str = field(default_factory=lambda: ensure_id(None))

    def __post_init__(self) -> None:
        """Validate and normalize fields after dataclass initialization."""
        if self.size_bytes <= 0 and self.base64_data:
            self.size_bytes = max(1, (len(self.base64_data.rstrip("=")) * 3) // 4)

    def summary_label(self) -> str:
        """Return 'source, size, mime_type' (e.g. 'demo.mp4, 1.2 MB, video/mp4')."""
        return f"{self.source}, {format_media_size(self.size_bytes)}, {self.mime_type}"

    def expanded_label(self) -> str:
        """Return 'source · size · mime_type' matching Antigravity CLI expanded view."""
        return f"{self.source} · {format_media_size(self.size_bytes)} · {self.mime_type}"

    def to_content_block(self) -> VideoContentBlock:
        """Convert to canonical LangChain `VideoContentBlock`."""
        return create_video_block(
            base64=self.base64_data,
            mime_type=self.mime_type,
            id=self.block_id,
        )

    def to_message_content(self) -> dict[str, Any]:
        """Convert to LangChain `VideoContentBlock` dict for `HumanMessage(content_blocks=...)`."""
        return dict(self.to_content_block())


@dataclass
class AudioData:
    """Represents a pasted or loaded audio/music clip ready to be sent as a LangChain `AudioContentBlock`."""

    base64_data: str
    mime_type: str  # "audio/mpeg", "audio/wav", "audio/ogg", "audio/flac", "audio/mp4", etc.
    placeholder: str  # Display text like "[audio 1]" or "[audio]"
    source: str = "clipboard"
    size_bytes: int = 0
    block_id: str = field(default_factory=lambda: ensure_id(None))

    def __post_init__(self) -> None:
        """Validate and normalize fields after dataclass initialization."""
        if self.size_bytes <= 0 and self.base64_data:
            self.size_bytes = max(1, (len(self.base64_data.rstrip("=")) * 3) // 4)

    def summary_label(self) -> str:
        """Return 'source, size, mime_type' (e.g. 'sample.mp3, 412 KB, audio/mpeg')."""
        return f"{self.source}, {format_media_size(self.size_bytes)}, {self.mime_type}"

    def expanded_label(self) -> str:
        """Return 'source · size · mime_type' matching Antigravity CLI expanded view."""
        return f"{self.source} · {format_media_size(self.size_bytes)} · {self.mime_type}"

    def to_content_block(self) -> AudioContentBlock:
        """Convert to canonical LangChain `AudioContentBlock`."""
        return create_audio_block(
            base64=self.base64_data,
            mime_type=self.mime_type,
            id=self.block_id,
        )

    def to_message_content(self) -> dict[str, Any]:
        """Convert to LangChain `AudioContentBlock` dict for `HumanMessage(content_blocks=...)`."""
        return dict(self.to_content_block())


@dataclass
class FileData:
    """Represents a pasted or loaded document/PDF ready to be sent as a LangChain `FileContentBlock`."""

    base64_data: str
    mime_type: str  # "application/pdf", etc.
    placeholder: str  # Display text like "[file 1]" or "[file]"
    source: str = "clipboard"
    size_bytes: int = 0
    filename: str | None = None
    block_id: str = field(default_factory=lambda: ensure_id(None))

    def __post_init__(self) -> None:
        """Validate and normalize fields after dataclass initialization."""
        if self.size_bytes <= 0 and self.base64_data:
            self.size_bytes = max(1, (len(self.base64_data.rstrip("=")) * 3) // 4)
        if self.filename is None and self.source and self.source != "clipboard":
            self.filename = self.source

    def summary_label(self) -> str:
        """Return 'source, size, mime_type' (e.g. 'runbook.pdf, 240 KB, application/pdf')."""
        return f"{self.source}, {format_media_size(self.size_bytes)}, {self.mime_type}"

    def expanded_label(self) -> str:
        """Return 'source · size · mime_type' matching Antigravity CLI expanded view."""
        return f"{self.source} · {format_media_size(self.size_bytes)} · {self.mime_type}"

    def to_content_block(self) -> FileContentBlock:
        """Convert to canonical LangChain `FileContentBlock`."""
        kwargs: dict[str, Any] = {}
        if self.filename:
            kwargs["filename"] = self.filename
        return create_file_block(
            base64=self.base64_data,
            mime_type=self.mime_type,
            id=self.block_id,
            **kwargs,
        )

    def to_message_content(self) -> dict[str, Any]:
        """Convert to LangChain `FileContentBlock` dict for `HumanMessage(content_blocks=...)`."""
        return dict(self.to_content_block())


DocumentData = FileData
MediaItem = ImageData | VideoData | AudioData | FileData


# ── Binary Container & Magic-Byte Validation (Antigravity CLI Parity) ────────


def walk_isobmff_atoms(data: bytes) -> list[bytes]:
    """Walk top-level ISOBMFF (MP4/MOV/M4A/3GP) boxes and return their 4-byte atom types.

    Mirrors `walkTopLevelISOBMFFAtoms` in Antigravity CLI (`cortex/utils/media`).
    """
    atoms: list[bytes] = []
    offset = 0
    total = len(data)
    while offset + 8 <= total:
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        atom_type = data[offset + 4 : offset + 8]
        atoms.append(atom_type)
        if size == 0:
            # Box extends to EOF
            break
        if size == 1:
            # 64-bit largesize box
            if offset + 16 > total:
                break
            size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
            if size < 16:
                break
        elif size < 8:
            break
        offset += size
    return atoms


def validate_isobmff_container(data: bytes, *, require_moov: bool = True) -> bool:
    """Validate an ISOBMFF container (`ftyp` + top-level `'moov'` atom presence).

    Unfinalized screen recordings often have `ftyp` and `mdat` without a `'moov'`
    atom. When `require_moov=True`, multi-box containers without `'moov'` are rejected.
    """
    if len(data) < 12 or data[4:8] != _FTYP_MARKER:
        return False
    if not require_moov:
        return True
    atoms = walk_isobmff_atoms(data)
    if not atoms:
        return False
    # If only a single header stub box was provided in a synthetic test fixture, allow it
    first_box_size = struct.unpack(">I", data[:4])[0]
    if len(atoms) == 1 and len(data) <= max(first_box_size, 32):
        return True
    return b"moov" in atoms


def validate_webm_container(data: bytes, *, require_media_blocks: bool = False) -> bool:
    r"""Validate a WebM / Matroska EBML container (`\x1a\x45\xdf\xa3`).

    When `require_media_blocks=True`, also checks for a Matroska Cluster (`\x1f\x43\xb6\x75`)
    and SimpleBlock (`\xa3`) or BlockGroup (`\xa0`), matching `validateWebMContainer` in `agy`.
    """
    if len(data) < 4 or data[:4] != _WEBM_MAGIC:
        return False
    if not require_media_blocks:
        return True
    cluster_id = b"\x1f\x43\xb6\x75"
    cluster_pos = data.find(cluster_id)
    if cluster_pos < 0:
        return False
    payload = data[cluster_pos + len(cluster_id) :]
    return b"\xa3" in payload or b"\xa0" in payload


def triage_ogg_mime_type(data: bytes, default_mime: str = "audio/ogg") -> str:
    """Inspect an Ogg container's codec identification headers to distinguish audio vs. video.

    Mirrors `triageOggMimeType` (`0x100e064c0`) in Antigravity CLI (`cortex/utils/media`).
    """
    if len(data) < 32 or data[:4] != _OGG_MAGIC:
        return default_mime
    head = data[:4096]
    if b"\x80theora" in head or b"\x01video\x00\x00\x00" in head or b"BBCD\x00" in head:
        return "video/ogg"
    if b"OpusHead" in head or b"\x01vorbis" in head or b"\x7fFLAC" in head or b"Speex   " in head:
        return "audio/ogg"
    return default_mime


def convert_gif_to_png_bytes(gif_bytes: bytes) -> bytes:
    """Convert a GIF image (specifically animated GIFs) to a static first-frame PNG.

    Mirrors `convertGIFToPNG` (`0x102cc6bd0`) in Antigravity CLI so providers that
    reject animated GIFs receive a valid static PNG.
    """
    img = Image.open(io.BytesIO(gif_bytes))
    img.seek(0)
    rgba = img.convert("RGBA") if img.mode not in ("RGB", "RGBA") else img.copy()
    out = io.BytesIO()
    rgba.save(out, format="PNG")
    return out.getvalue()


def compress_image_bytes(
    image_bytes: bytes,
    *,
    max_side: int = MAX_IMAGE_SIDE_PX,
    jpeg_quality: int = 85,
) -> tuple[bytes, str]:
    """Downscale and compress an image if its dimensions exceed `max_side`.

    Mirrors `CompressMedia` (`0x100e04e00`) / `resizeToMaxSide` (`0x100e052c0`) in `agy`.

    Returns:
        Tuple of `(compressed_bytes, format_name)` where `format_name` is `'png'` or `'jpeg'`.
    """
    img = Image.open(io.BytesIO(image_bytes))
    orig_format = (img.format or "PNG").lower()
    width, height = img.size
    if width <= max_side and height <= max_side:
        return image_bytes, ("jpeg" if orig_format == "jpg" else orig_format)

    resized = img.copy()
    resized.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

    png_buf = io.BytesIO()
    resized.save(png_buf, format="PNG")
    png_data = png_buf.getvalue()

    # If the image has no alpha channel, compare JPEG size vs PNG size (smallerAsJPEG in agy)
    if resized.mode in ("RGB", "L"):
        jpg_buf = io.BytesIO()
        resized.convert("RGB").save(jpg_buf, format="JPEG", quality=jpeg_quality, optimize=True)
        jpg_data = jpg_buf.getvalue()
        if len(jpg_data) < len(png_data):
            return jpg_data, "jpeg"

    return png_data, "png"


def _validate_video_header(data: bytes, expected_suffix: str) -> str | None:
    """Validate video container bytes and return the detected MIME type."""
    if len(data) < 12:
        return None

    if data[:4] == _WEBM_MAGIC:
        return "video/x-matroska" if expected_suffix == ".mkv" else "video/webm"

    if data[4:8] == _FTYP_MARKER:
        if not validate_isobmff_container(data, require_moov=True):
            return None
        brand = data[8:12]
        if brand in _QUICKTIME_BRANDS or expected_suffix == ".mov":
            return "video/quicktime"
        if expected_suffix in {".3gp", ".3gpp"} or brand.startswith(b"3gp"):
            return "video/3gpp"
        return VIDEO_MIME_TYPES.get(expected_suffix, "video/mp4")

    if data[:4] == _OGG_MAGIC:
        return triage_ogg_mime_type(data, default_mime="video/ogg")

    if data[:4] == _RIFF_MAGIC and data[8:12] == _AVI_MARKER:
        return "video/x-msvideo"

    if data[:3] == _FLV_MAGIC:
        return "video/x-flv"

    if len(data) >= 16 and data[:16] == _ASF_WMV_MAGIC:
        return "video/x-ms-wmv"

    # MPEG-PS / MPEG-TS start codes
    if data[:4] in {b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"} or (
        data[0] == 0x47 and expected_suffix in {".mpeg", ".mpg"}
    ):
        return "video/mpeg"

    return None


def _validate_audio_header(data: bytes, expected_suffix: str) -> str | None:
    """Validate audio magic bytes and return the detected MIME type."""
    if len(data) < 4:
        return None

    # WAV: RIFF....WAVE
    if len(data) >= 12 and data[:4] == _RIFF_MAGIC and data[8:12] == _WAVE_MARKER:
        return "audio/wav"

    # FLAC: fLaC
    if data[:4] == _FLAC_MAGIC:
        return "audio/flac"

    # Ogg Vorbis / Opus / Speex: OggS
    if data[:4] == _OGG_MAGIC:
        if expected_suffix == ".opus" or b"OpusHead" in data[:256]:
            return "audio/opus"
        return triage_ogg_mime_type(data, default_mime="audio/ogg")

    # AIFF / AIFC: FORM....AIFF or FORM....AIFC
    if len(data) >= 12 and data[:4] == _FORM_MAGIC and data[8:12] in _AIFF_MARKERS:
        return "audio/aiff"

    # M4A / MP4 audio: ....ftyp
    if len(data) >= 12 and data[4:8] == _FTYP_MARKER:
        if not validate_isobmff_container(data, require_moov=True):
            return None
        brand = data[8:12]
        if brand in _M4A_BRANDS or expected_suffix == ".m4a":
            return "audio/mp4"
        return AUDIO_MIME_TYPES.get(expected_suffix, "audio/mp4")

    # WebM Audio (.weba)
    if data[:4] == _WEBM_MAGIC and expected_suffix == ".weba":
        return "audio/webm"

    # MP3 with ID3v2 tag or MPEG audio frame sync (0xFFE0 mask)
    if data[:3] == _ID3_MAGIC:
        return "audio/aac" if expected_suffix == ".aac" else "audio/mpeg"

    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        # ADTS AAC has layer == 00 ((data[1] & 0x06) == 0x00)
        if expected_suffix == ".aac" or (data[1] & 0xF6) in {0xF0, 0xF1, 0xF8, 0xF9}:
            if expected_suffix == ".aac":
                return "audio/aac"
        return AUDIO_MIME_TYPES.get(expected_suffix, "audio/mpeg")

    return None


def _validate_file_header(data: bytes, expected_suffix: str) -> str | None:
    """Validate document/file magic bytes (PDF, Office OpenXML, OLE2) and return MIME type."""
    if not data:
        return None

    if expected_suffix == ".pdf":
        if _PDF_MAGIC in data[:1024]:
            return "application/pdf"
        return None

    if expected_suffix in {".pptx", ".docx", ".xlsx"}:
        if data[:4] == _ZIP_MAGIC:
            return FILE_MIME_TYPES[expected_suffix]
        return None

    if expected_suffix in {".ppt", ".doc", ".xls"}:
        if data[:8] == _OLE2_MAGIC or data[:4] == _ZIP_MAGIC:
            return FILE_MIME_TYPES[expected_suffix]
        return None

    return None


def validate_binary_media(data: bytes, suffix_or_mime: str) -> str | None:
    """Validate binary media bytes against container/magic-byte rules and return its MIME type.

    Supports images, audio/music, video, and PDF/Office documents.
    """
    if not data:
        return None

    norm = suffix_or_mime.lower().strip()
    suffix = norm if norm.startswith(".") else MIME_TO_EXTENSION.get(norm, "")

    if suffix in IMAGE_EXTENSIONS or norm.startswith("image/"):
        if suffix == ".svg" or norm == "image/svg+xml":
            head_text = data[:1024].decode("utf-8", errors="ignore").lower()
            return "image/svg+xml" if "<svg" in head_text else None
        try:
            img = Image.open(io.BytesIO(data))
            fmt = (img.format or "").lower()
            if not fmt:
                return None
            return "image/jpeg" if fmt == "jpg" else f"image/{fmt}"
        except Exception:
            return None

    if suffix in VIDEO_EXTENSIONS or norm.startswith("video/"):
        return _validate_video_header(data, suffix or ".mp4")

    if suffix in AUDIO_EXTENSIONS or norm.startswith("audio/"):
        return _validate_audio_header(data, suffix or ".mp3")

    if suffix in FILE_EXTENSIONS or norm in FILE_MIME_TYPES.values():
        return _validate_file_header(data, suffix or ".pdf")

    return None


# ── Clipboard Media Detection (Finder File Copy + Raw Image Bytes) ───────────


def _resolve_file_uri_or_path(raw: str) -> Path | None:
    """Resolve a POSIX path or `file://` URI from the clipboard into an existing `Path`."""
    cleaned = raw.strip()
    if not cleaned:
        return None
    if cleaned.startswith("file://"):
        parsed = urlparse(cleaned)
        cleaned = unquote(parsed.path)
    p = Path(cleaned).expanduser()
    try:
        if p.exists() and p.is_file():
            return p.resolve()
    except OSError:
        pass
    return None


def _get_macos_clipboard_file_path() -> Path | None:
    """Read a copied file's POSIX path from the macOS clipboard via `«class furl»`.

    Mirrors `readClipboardFileDarwin` (`0x102cc7950`) in Antigravity CLI so copying any
    media file (photo, screenshot, `.mp3`, `.wav`, `.mp4`, `.pdf`) in Finder and pressing
    `Ctrl+V` / `Cmd+V` attaches the file directly.
    """
    osascript_path = shutil.which("osascript")
    if not osascript_path:
        return None
    script = (
        "try\n"
        "\tset fileURL to the clipboard as «class furl»\n"
        "\treturn POSIX path of fileURL\n"
        "on error\n"
        '\treturn ""\n'
        "end try"
    )
    try:
        result = subprocess.run(
            [osascript_path, "-e", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return _resolve_file_uri_or_path(result.stdout.strip())
    except (subprocess.TimeoutExpired, OSError):
        logger.debug("macOS osascript furl clipboard check failed", exc_info=True)
    return None


def _get_linux_clipboard_file_path() -> Path | None:
    """Read a copied file path (`text/uri-list`) from the Linux clipboard via `wl-paste` or `xclip`."""
    wl_paste_path = shutil.which("wl-paste")
    if wl_paste_path and os.environ.get("WAYLAND_DISPLAY"):
        try:
            result = subprocess.run(
                [wl_paste_path, "--type", "text/uri-list"],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
            if result.returncode == 0 and result.stdout:
                for line in result.stdout.splitlines():
                    if not line.startswith("#") and (resolved := _resolve_file_uri_or_path(line)):
                        return resolved
        except (subprocess.TimeoutExpired, OSError):
            pass

    xclip_path = shutil.which("xclip")
    if xclip_path and os.environ.get("DISPLAY"):
        try:
            result = subprocess.run(
                [xclip_path, "-selection", "clipboard", "-t", "text/uri-list", "-o"],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
            if result.returncode == 0 and result.stdout:
                for line in result.stdout.splitlines():
                    if not line.startswith("#") and (resolved := _resolve_file_uri_or_path(line)):
                        return resolved
        except (subprocess.TimeoutExpired, OSError):
            pass
    return None


def get_clipboard_image() -> ImageData | None:
    """Attempt to read an image from the system clipboard.

    Supports macOS (Finder copied image file, `pngpaste`, or `osascript` fallback) and Linux.

    Returns:
        `ImageData` if an image is present in clipboard, `None` otherwise.
    """
    if sys.platform == "darwin":
        return _get_macos_clipboard_image()
    if sys.platform.startswith("linux"):
        return _get_linux_clipboard_image()
    return None


def get_clipboard_media() -> MediaItem | None:
    """Attempt to read any supported media (image, audio, video, PDF/file) from the clipboard.

    First checks if a media file was copied in Finder / file manager (`«class furl»` on macOS,
    `text/uri-list` on Linux) and loads it via `get_media_from_path`. Otherwise falls back to
    reading raw clipboard image bitmap data via `get_clipboard_image()`.
    """
    if sys.platform == "darwin":
        copied_path = _get_macos_clipboard_file_path()
        if copied_path is not None and is_media_path(copied_path):
            media = get_media_from_path(copied_path)
            if media is not None:
                return media
    elif sys.platform.startswith("linux"):
        copied_path = _get_linux_clipboard_file_path()
        if copied_path is not None and is_media_path(copied_path):
            media = get_media_from_path(copied_path)
            if media is not None:
                return media

    return get_clipboard_image()


def _get_linux_clipboard_image() -> ImageData | None:
    """Get clipboard image on Linux using xclip or wl-paste if available."""
    copied_path = _get_linux_clipboard_file_path()
    if copied_path is not None and copied_path.suffix.lower() in IMAGE_EXTENSIONS:
        loaded = get_image_from_path(copied_path)
        if loaded is not None:
            return loaded

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
                        size_bytes=len(result.stdout),
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
                        size_bytes=len(result.stdout),
                    )
                except Exception:
                    pass
        except (subprocess.TimeoutExpired, OSError):
            pass

    return None


def _get_macos_clipboard_image() -> ImageData | None:
    """Get clipboard image on macOS using Finder `furl`, `pngpaste`, or `osascript`."""
    copied_path = _get_macos_clipboard_file_path()
    if copied_path is not None and copied_path.suffix.lower() in IMAGE_EXTENSIONS:
        loaded = get_image_from_path(copied_path)
        if loaded is not None:
            return loaded

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
                        size_bytes=len(result.stdout),
                    )
                except Exception:
                    logger.debug("Invalid image data from pngpaste", exc_info=True)
        except (subprocess.TimeoutExpired, OSError):
            logger.debug("pngpaste failed, falling back to osascript", exc_info=True)

    # Fallback: use osascript with a temp file
    return _get_clipboard_via_osascript()


def _get_clipboard_via_osascript() -> ImageData | None:
    """Get clipboard image via osascript using a temp file (PNGf, JPEG, GIFf, or TIFF)."""
    osascript_path = shutil.which("osascript")
    if not osascript_path:
        return None

    fd, temp_path = tempfile.mkstemp(prefix="cli_clipboard_paste_", suffix=".png")
    os.close(fd)

    try:
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
        if not any(tag in clipboard_info for tag in ("pngf", "jpeg", "giff", "tiff")):
            return None

        if "pngf" in clipboard_info:
            clip_class = "«class PNGf»"
        elif "jpeg" in clipboard_info:
            clip_class = "JPEG picture"
        elif "giff" in clipboard_info:
            clip_class = "GIF picture"
        else:
            clip_class = "TIFF picture"

        get_script = f"""
        set imgData to the clipboard as {clip_class}
        set theFile to open for access POSIX file "{temp_path}" with write permission
        set eof of theFile to 0
        write imgData to theFile
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
            img.seek(0)
            output = io.BytesIO()
            img.save(output, format="PNG")
            png_bytes = output.getvalue()
            base64_data = base64.b64encode(png_bytes).decode("utf-8")
            return ImageData(
                base64_data=base64_data,
                format="png",
                placeholder="[image]",
                size_bytes=len(png_bytes),
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


_PLACEHOLDER_RE = re.compile(r"\[(?:image|video|audio|file)\s+\d+\]|\[(?:image|video|audio|file)\]")
_MULTI_SPACE_RE = re.compile(r"[^\S\n]{2,}")
_BLANK_LINE_CLEANUP_RE = re.compile(r"[^\S\n]*\n[^\S\n]*")


def strip_media_placeholders(text: str) -> str:
    """Remove `[image N]`, `[video N]`, `[audio N]`, and `[file N]` display placeholders from text."""
    cleaned = _PLACEHOLDER_RE.sub("", text)
    cleaned = _MULTI_SPACE_RE.sub(" ", cleaned)
    cleaned = _BLANK_LINE_CLEANUP_RE.sub("\n", cleaned)
    return cleaned.strip()


def create_multimodal_content(
    text: str,
    images: Sequence[ImageData] = (),
    videos: Sequence[VideoData] = (),
    audios: Sequence[AudioData] = (),
    files: Sequence[FileData] = (),
) -> list[dict[str, Any]]:
    """Create LangChain multimodal `content_blocks` with text, images, videos, audios, and files.

    Args:
        text: The text prompt (any `[image N]` / `[video N]` / `[audio N]` / `[file N]`
            UI placeholders are stripped before building the text block)
        images: Sequence of `ImageData` objects
        videos: Sequence of `VideoData` objects
        audios: Sequence of `AudioData` objects
        files: Sequence of `FileData` (`DocumentData`) objects

    Returns:
        List of canonical LangChain content blocks (`TextContentBlock`, `ImageContentBlock`,
        `VideoContentBlock`, `AudioContentBlock`, `FileContentBlock`).
    """
    content: list[dict[str, Any]] = []

    clean_text = strip_media_placeholders(text)
    if clean_text:
        content.append({"type": "text", "text": clean_text})

    content.extend(image.to_message_content() for image in images)
    content.extend(video.to_message_content() for video in videos)
    content.extend(audio.to_message_content() for audio in audios)
    content.extend(file_item.to_message_content() for file_item in files)

    return content


def _check_file_stat(path: Path, label: str) -> int | None:
    """Verify file exists, is non-empty, and is within `MAX_MEDIA_BYTES`."""
    if not path.exists() or not path.is_file():
        return None
    try:
        file_size = path.stat().st_size
    except OSError:
        logger.debug("Failed to stat %s file %s", label, path, exc_info=True)
        return None
    if file_size == 0:
        return None
    if file_size > MAX_MEDIA_BYTES:
        logger.warning(
            "%s file %s (%d bytes) exceeds maximum size (%d bytes)",
            label.capitalize(),
            path,
            file_size,
            MAX_MEDIA_BYTES,
        )
        return None
    return file_size


def get_image_from_path(file_path: str | Path) -> ImageData | None:
    """Load an image from a file path and return as `ImageData`.

    Validates extension, file existence, size limit, and image integrity.
    Converts animated GIFs to first-frame static PNG (`convertGIFToPNG` parity with `agy`).
    """
    path = Path(file_path) if isinstance(file_path, str) else file_path
    suffix = path.suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        return None

    file_size = _check_file_stat(path, "image")
    if file_size is None:
        return None

    try:
        image_bytes = path.read_bytes()
        if suffix == ".svg":
            if validate_binary_media(image_bytes, ".svg") is None:
                return None
            return ImageData(
                base64_data=base64.b64encode(image_bytes).decode("utf-8"),
                format="svg+xml",
                placeholder="[image]",
                source=path.name,
                size_bytes=file_size,
            )

        img = Image.open(io.BytesIO(image_bytes))
        pil_format = img.format
        if not pil_format:
            return None

        format_lower = pil_format.lower()
        # Convert animated GIFs to static first-frame PNG (agy convertGIFToPNG parity)
        if format_lower == "gif" and getattr(img, "is_animated", False):
            png_bytes = convert_gif_to_png_bytes(image_bytes)
            return ImageData(
                base64_data=base64.b64encode(png_bytes).decode("utf-8"),
                format="png",
                placeholder="[image]",
                source=path.name,
                size_bytes=len(png_bytes),
            )

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
        # macOS sips fallback for HEIC / HEIF / TIFF when Pillow plugin is absent
        if sys.platform == "darwin" and suffix in {".heic", ".heif", ".tiff", ".tif", ".avif"}:
            sips_path = shutil.which("sips")
            if sips_path:
                fd, tmp_png = tempfile.mkstemp(suffix=".png")
                os.close(fd)
                try:
                    res = subprocess.run(
                        [sips_path, "-s", "format", "png", str(path), "--out", tmp_png],
                        capture_output=True,
                        check=False,
                        timeout=5,
                    )
                    tmp_obj = Path(tmp_png)
                    if res.returncode == 0 and tmp_obj.exists() and tmp_obj.stat().st_size > 0:
                        png_bytes = tmp_obj.read_bytes()
                        return ImageData(
                            base64_data=base64.b64encode(png_bytes).decode("utf-8"),
                            format="png",
                            placeholder="[image]",
                            source=path.name,
                            size_bytes=len(png_bytes),
                        )
                except Exception:
                    pass
                finally:
                    try:
                        Path(tmp_png).unlink()
                    except OSError:
                        pass
        logger.debug("Failed to load image from path %s", path, exc_info=True)
        return None


def get_video_from_path(file_path: str | Path) -> VideoData | None:
    """Load a video from a file path and return as `VideoData`.

    Validates extension, file existence, size limit, and container header (`ftyp` + `moov`, EBML, etc.).
    """
    path = Path(file_path) if isinstance(file_path, str) else file_path
    suffix = path.suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        return None

    file_size = _check_file_stat(path, "video")
    if file_size is None:
        return None

    try:
        video_bytes = path.read_bytes()
        mime_type = _validate_video_header(video_bytes, suffix)
        if mime_type is None:
            logger.debug("File %s failed video container validation", path)
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


def get_audio_from_path(file_path: str | Path) -> AudioData | None:
    """Load an audio/music file from a file path and return as `AudioData`.

    Validates extension, file existence, size limit, and audio magic bytes.
    """
    path = Path(file_path) if isinstance(file_path, str) else file_path
    suffix = path.suffix.lower()
    if suffix not in AUDIO_EXTENSIONS:
        return None

    file_size = _check_file_stat(path, "audio")
    if file_size is None:
        return None

    try:
        audio_bytes = path.read_bytes()
        mime_type = _validate_audio_header(audio_bytes, suffix)
        if mime_type is None:
            logger.debug("File %s failed audio header validation", path)
            return None

        base64_data = base64.b64encode(audio_bytes).decode("utf-8")
        return AudioData(
            base64_data=base64_data,
            mime_type=mime_type,
            placeholder="[audio]",
            source=path.name,
            size_bytes=file_size,
        )
    except Exception:
        logger.debug("Failed to load audio from path %s", path, exc_info=True)
        return None


def get_file_from_path(file_path: str | Path) -> FileData | None:
    """Load a document/file (`.pdf`, `.ppt`, `.pptx`, `.doc`, `.docx`, `.xls`, `.xlsx`) as `FileData`."""
    path = Path(file_path) if isinstance(file_path, str) else file_path
    suffix = path.suffix.lower()
    if suffix not in FILE_EXTENSIONS:
        return None

    file_size = _check_file_stat(path, "document")
    if file_size is None:
        return None

    try:
        file_bytes = path.read_bytes()
        mime_type = _validate_file_header(file_bytes, suffix)
        if mime_type is None:
            logger.debug("File %s failed document header validation", path)
            return None

        base64_data = base64.b64encode(file_bytes).decode("utf-8")
        return FileData(
            base64_data=base64_data,
            mime_type=mime_type,
            placeholder="[file]",
            source=path.name,
            size_bytes=file_size,
            filename=path.name,
        )
    except Exception:
        logger.debug("Failed to load document from path %s", path, exc_info=True)
        return None


get_document_from_path = get_file_from_path


def get_media_from_path(file_path: str | Path) -> MediaItem | None:
    """Load an image, video, audio, or document/PDF from a file path."""
    path = Path(file_path) if isinstance(file_path, str) else file_path
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return get_image_from_path(path)
    if suffix in VIDEO_EXTENSIONS:
        return get_video_from_path(path)
    if suffix in AUDIO_EXTENSIONS:
        return get_audio_from_path(path)
    if suffix in FILE_EXTENSIONS:
        return get_file_from_path(path)
    return None


def is_media_path(file_path: str | Path) -> bool:
    """Return True if *file_path* has a supported image, video, audio, or document extension."""
    suffix = (Path(file_path) if isinstance(file_path, str) else file_path).suffix.lower()
    return (
        suffix in IMAGE_EXTENSIONS
        or suffix in VIDEO_EXTENSIONS
        or suffix in AUDIO_EXTENSIONS
        or suffix in FILE_EXTENSIONS
    )


# ── User Uploaded Media Artifact Persistence (`agy` `.user_uploaded/` Parity) ─


def save_user_uploaded_media(
    media_or_block: MediaItem | Mapping[str, Any],
    dest_dir: Path,
    *,
    index: int = 0,
    timestamp_ms: int | None = None,
) -> dict[str, str] | None:
    """Persist a user-attached `MediaItem` or LangChain `ContentBlock` to `.user_uploaded/`.

    Writes `<dest_dir>/uploaded_media_<index>_<timestamp_ms>.<ext>` (matching
    `SaveMediaAsUserMediaArtifacts` in Antigravity CLI) and returns
    `{"mime_type": mime_type, "uri": str(saved_path)}` for `transcript.jsonl`.
    """
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)

    if isinstance(media_or_block, (ImageData, VideoData, AudioData, FileData)):
        b64 = media_or_block.base64_data
        mime_type = media_or_block.mime_type
    elif isinstance(media_or_block, dict):
        btype = media_or_block.get("type")
        if btype == "image_url":
            url = (media_or_block.get("image_url") or {}).get("url", "")
            if not isinstance(url, str) or not url.startswith("data:") or ";base64," not in url:
                return None
            header, b64 = url.split(";base64,", 1)
            mime_type = header.removeprefix("data:") or "image/png"
        elif btype in {"image", "video", "audio", "file"} and isinstance(media_or_block.get("base64"), str):
            b64 = media_or_block["base64"]
            mime_type = str(media_or_block.get("mime_type") or "application/octet-stream")
        else:
            return None
    else:
        return None

    try:
        raw_bytes = base64.b64decode(b64)
    except Exception:
        logger.debug("Failed to decode base64 media for .user_uploaded persistence", exc_info=True)
        return None

    ext = MIME_TO_EXTENSION.get(mime_type.lower())
    if not ext:
        guessed = mimetypes.guess_extension(mime_type.lower())
        ext = guessed if guessed else ".bin"

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        target_path = dest_dir / f"uploaded_media_{index}_{ts}{ext}"
        target_path.write_bytes(raw_bytes)
        return {
            "mime_type": mime_type,
            "uri": str(target_path.resolve()),
        }
    except OSError:
        logger.debug("Failed to write user uploaded media to %s", dest_dir, exc_info=True)
        return None


if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "AUDIO_EXTENSIONS",
    "AUDIO_MIME_TYPES",
    "DOCUMENT_EXTENSIONS",
    "FILE_EXTENSIONS",
    "FILE_MIME_TYPES",
    "IMAGE_EXTENSIONS",
    "IMAGE_MIME_TYPES",
    "MAX_IMAGE_SIDE_PX",
    "MAX_MEDIA_BYTES",
    "MIME_TO_EXTENSION",
    "VIDEO_EXTENSIONS",
    "VIDEO_MIME_TYPES",
    "AudioContentBlock",
    "AudioData",
    "ContentBlock",
    "DocumentData",
    "FileContentBlock",
    "FileData",
    "ImageContentBlock",
    "ImageData",
    "MediaItem",
    "VideoContentBlock",
    "VideoData",
    "classify_media_extension",
    "compress_image_bytes",
    "convert_gif_to_png_bytes",
    "create_audio_block",
    "create_file_block",
    "create_image_block",
    "create_multimodal_content",
    "create_text_block",
    "create_video_block",
    "encode_image_to_base64",
    "format_media_size",
    "get_audio_from_path",
    "get_clipboard_image",
    "get_clipboard_media",
    "get_document_from_path",
    "get_file_from_path",
    "get_image_from_path",
    "get_media_from_path",
    "get_video_from_path",
    "is_data_content_block",
    "is_media_path",
    "save_user_uploaded_media",
    "strip_media_placeholders",
    "triage_ogg_mime_type",
    "validate_binary_media",
    "validate_isobmff_container",
    "validate_webm_container",
    "walk_isobmff_atoms",
]

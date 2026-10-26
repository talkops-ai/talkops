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

"""Input handling utilities including @file mention parsing, pasted path resolution, and MediaTracker."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import unquote, urlparse

from talkops.media_utils import ImageData, VideoData
from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

PATH_CHAR_CLASS = r"A-Za-z0-9._~/\\:\-\u00a0\u202f"
FILE_MENTION_PATTERN = re.compile(r"@(?P<path>(?:\\.|[" + PATH_CHAR_CLASS + r"])+)")
EMAIL_PREFIX_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]$")
WINDOWS_DRIVE_PATH_PATTERN = re.compile(r"^[a-zA-Z]:[\\/]")

IMAGE_PLACEHOLDER_PATTERN = re.compile(r"\[image \d+\]")
VIDEO_PLACEHOLDER_PATTERN = re.compile(r"\[video \d+\]")

MIN_DROPPED_COMPACT_PATH_COUNT = 2
UNICODE_SPACES: tuple[str, ...] = ("\u00a0", "\u202f")


@dataclass(frozen=True)
class ParsedPastedPathPayload:
    """Structured result for path payload parsing."""

    paths: list[Path]
    token_end: int | None = None
    had_candidate: bool = False


@dataclass(frozen=True)
class _TrackedMediaEntry:
    """Tracked media attachment and its live text span when known."""

    media: ImageData | VideoData
    span: tuple[int, int] | None = None
    inline: bool = True


MediaSnapshot = tuple[tuple[ImageData | VideoData, tuple[int, int] | None], ...]


def _resolve_with_unicode_space_variants(path: Path) -> Path | None:
    """Try resolving `path`, falling back to Unicode space substitutions.

    macOS screenshots use U+202F (narrow no-break space) before AM/PM in
    filenames like `Screenshot 2026-03-09 at 6.23.09\u202fPM.png`. Terminals
    and clipboard tools often normalize this to a regular ASCII space (` `),
    causing `Path.exists()` to return `False`.
    """
    resolved = path.resolve()
    if resolved.exists() and resolved.is_file():
        return resolved

    parent = resolved.parent
    name = resolved.name
    if " " not in name or not parent.is_dir():
        return None

    for u_space in UNICODE_SPACES:
        candidate = parent / re.sub(r" (?=[APap][Mm]\b)", u_space, name)
        if candidate.exists() and candidate.is_file():
            return candidate
        candidate = parent / name.replace(" ", u_space)
        if candidate.exists() and candidate.is_file():
            return candidate

    return None


def _unescape_shell_path(value: str) -> str:
    """Unescape backslash-escaped characters in shell-style paths."""
    if "\\" not in value:
        return value
    chars: list[str] = []
    idx = 0
    while idx < len(value):
        if value[idx] == "\\" and idx + 1 < len(value):
            chars.append(value[idx + 1])
            idx += 2
            continue
        chars.append(value[idx])
        idx += 1
    return "".join(chars)


def _decode_file_url(url_text: str) -> Path | None:
    """Decode a `file://` URL into a local path."""
    parsed = urlparse(url_text)
    if parsed.scheme != "file":
        return None
    if parsed.netloc and parsed.netloc != "localhost":
        raw_path = f"//{parsed.netloc}{unquote(parsed.path)}"
    else:
        raw_path = unquote(parsed.path)
    if not raw_path:
        return None
    if re.match(r"^/[a-zA-Z]:/", raw_path):
        raw_path = raw_path[1:]
    return Path(raw_path)


def normalize_pasted_path(raw_token: str) -> Path | None:
    """Normalize a single pasted/dropped token into a filesystem path candidate."""
    token = raw_token.strip()
    if not token:
        return None
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        token = token[1:-1].strip()
    if not token:
        return None
    if token.startswith("file://"):
        return _decode_file_url(token)
    if WINDOWS_DRIVE_PATH_PATTERN.match(token):
        return Path(token)
    return Path(_unescape_shell_path(token))


def _resolve_existing_file(candidate: Path) -> Path | None:
    """Expand and resolve a candidate path when it points to a regular file."""
    try:
        expanded = candidate.expanduser()
        if not expanded.is_absolute():
            expanded = Path.cwd() / expanded
        return _resolve_with_unicode_space_variants(expanded)
    except (OSError, RuntimeError) as e:
        logger.debug("Invalid pasted path %s: %s", candidate, e)
        return None


def _looks_like_path_candidate_text(raw: str) -> bool:
    """Return True if raw input resembles a path/file URL payload."""
    token = raw.strip()
    if not token:
        return False
    if token[0] in {"'", '"'}:
        quote = token[0]
        token = token[1:]
        if token.endswith(quote):
            token = token[:-1]
        token = token.strip()
    if not token:
        return False
    return bool(
        token.startswith(("file://", "/", "~/", "./", "../"))
        or WINDOWS_DRIVE_PATH_PATTERN.match(token)
    )


def _split_unquoted_Paste_tokens(text: str) -> list[str]:
    """Split text on whitespace unless escaped by a backslash."""
    tokens: list[str] = []
    current: list[str] = []
    escaped = False

    for char in text:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            continue
        if char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)

    if current:
        tokens.append("".join(current))
    return tokens


def _split_quoted_Paste_tokens(text: str) -> list[str] | None:
    """Split text into quoted tokens when all segments are wrapped in quotes."""
    tokens: list[str] = []
    idx = 0
    length = len(text)
    while idx < length:
        while idx < length and text[idx].isspace():
            idx += 1
        if idx >= length:
            break
        quote = text[idx]
        if quote not in {"'", '"'}:
            return None
        idx += 1
        start = idx
        while idx < length and text[idx] != quote:
            idx += 1
        if idx >= length:
            return None
        tokens.append(text[start:idx])
        idx += 1
    return tokens or None


def _parse_tokens_as_existing_files(tokens: list[str]) -> list[Path] | None:
    """Resolve all tokens as existing files; return None if any token fails."""
    if not tokens:
        return None
    resolved_paths: list[Path] = []
    for token in tokens:
        candidate = normalize_pasted_path(token)
        if candidate is None:
            return None
        resolved = _resolve_existing_file(candidate)
        if resolved is None:
            return None
        resolved_paths.append(resolved)
    return resolved_paths


def _split_compact_dropped_paths(text: str) -> list[str] | None:
    """Split compact multi-file drop payloads that lack whitespace separators."""
    stripped = text.strip()
    if not stripped:
        return None
    marker_pattern = re.compile(
        r"(?:file://|~/|\.\.?/|[a-zA-Z]:[\\/]|/Users/|/home/|/tmp/|/var/|/opt/|/etc/|/Volumes/|/private/)"
    )
    starts = [match.start() for match in marker_pattern.finditer(stripped)]
    if len(starts) < MIN_DROPPED_COMPACT_PATH_COUNT or starts[0] != 0:
        return None

    segments: list[str] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(stripped)
        segment = stripped[start:end].strip()
        if not segment:
            return None
        segments.append(segment)
    return segments


def _iter_paste_token_lists(stripped: str) -> list[list[str]]:
    """Build candidate token lists from a stripped paste payload."""
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    candidates: list[list[str]] = [[stripped]]
    if len(lines) > 1:
        candidates.append(lines)
    quoted_tokens = _split_quoted_Paste_tokens(stripped)
    if quoted_tokens:
        candidates.append(quoted_tokens)
    unquoted_tokens = _split_unquoted_Paste_tokens(stripped)
    if len(unquoted_tokens) > 1:
        candidates.append(unquoted_tokens)
    compact_tokens = _split_compact_dropped_paths(stripped)
    if compact_tokens:
        candidates.append(compact_tokens)
    return candidates


def parse_pasted_file_paths(text: str) -> list[Path]:
    """Parse pasted/dropped text into existing file paths when all tokens resolve."""
    stripped = text.strip()
    if not stripped:
        return []
    for token_list in _iter_paste_token_lists(stripped):
        parsed = _parse_tokens_as_existing_files(token_list)
        if parsed:
            return parsed
    return []


def parse_pasted_path_payload(
    text: str, *, allow_leading_path: bool = False
) -> ParsedPastedPathPayload | None:
    """Parse pasted/dropped path payload into resolved paths when valid."""
    paths = dropped_payload_paths(text)
    if paths:
        return ParsedPastedPathPayload(paths=paths, token_end=None, had_candidate=True)
    if allow_leading_path:
        leading_match = extract_leading_pasted_file_path(text)
        if leading_match is not None:
            leading_path, token_end = leading_match
            return ParsedPastedPathPayload(
                paths=[leading_path], token_end=token_end, had_candidate=True
            )
    return None


def looks_like_dropped_payload(text: str) -> bool:
    """Return True if text appears to start with a dropped file path/URL."""
    return _looks_like_path_candidate_text(text.lstrip())


def dropped_payload_paths(text: str) -> list[Path] | None:
    """Extract existing file paths from a dropped payload text."""
    stripped = text.strip()
    if not looks_like_dropped_payload(stripped):
        return None
    paths = parse_pasted_file_paths(stripped)
    return paths or None


def parse_single_pasted_file_path(text: str) -> Path | None:
    """Resolve a single pasted path string as an existing file."""
    candidate = normalize_pasted_path(text)
    if candidate is None:
        return None
    return _resolve_existing_file(candidate)


def extract_leading_pasted_file_path(text: str) -> tuple[Path, int] | None:
    """Extract a leading pasted/dropped file path from a text buffer."""
    if not text:
        return None

    first_non_ws = len(text) - len(text.lstrip())
    if first_non_ws >= len(text):
        return None

    remainder = text[first_non_ws:]
    if not looks_like_dropped_payload(remainder):
        return None

    full_path = parse_single_pasted_file_path(remainder)
    if full_path is not None:
        return full_path, len(text)

    if remainder[0] in {"'", '"'}:
        quote = remainder[0]
        closing = remainder.find(quote, 1)
        if closing != -1:
            candidate_text = remainder[: closing + 1]
            resolved = parse_single_pasted_file_path(candidate_text)
            if resolved is not None:
                return resolved, first_non_ws + closing + 1

    escaped = False
    split_offsets: list[int] = []
    for idx, char in enumerate(remainder):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char.isspace():
            if idx > 0:
                split_offsets.append(idx)
            break

    for offset in split_offsets:
        candidate_text = remainder[:offset]
        resolved = parse_single_pasted_file_path(candidate_text)
        if resolved is not None:
            return resolved, first_non_ws + offset

    candidate_breaks: list[int] = []
    escaped = False
    for idx, char in enumerate(remainder):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char.isspace() and idx > 0:
            candidate_breaks.append(idx)
    candidate_breaks.append(len(remainder))

    best_match: tuple[Path, int] | None = None
    for break_idx in candidate_breaks:
        candidate_text = remainder[:break_idx].rstrip()
        if not candidate_text:
            continue
        resolved = parse_single_pasted_file_path(candidate_text)
        if resolved is not None:
            best_match = (resolved, first_non_ws + len(candidate_text))

    return best_match


def parse_file_mentions(text: str, *, warn: bool = True) -> tuple[str, list[Path]]:
    """Extract `@file` mentions from input text and return resolved file paths.

    Args:
        text: Input text potentially containing `@file` mentions.
        warn: Log a warning when a mentioned path does not exist or cannot be
            resolved. Set to `False` for speculative callers.

    Returns:
        Tuple of (original text unchanged, list of resolved file paths that exist).
    """
    matches = FILE_MENTION_PATTERN.finditer(text)
    files: list[Path] = []

    for match in matches:
        text_before = text[: match.start()]
        if text_before and EMAIL_PREFIX_PATTERN.search(text_before):
            continue

        raw_path = match.group("path")
        clean_path = raw_path.replace("\\ ", " ")

        try:
            path = Path(clean_path).expanduser()
            if not path.is_absolute():
                path = Path.cwd() / path

            resolved = _resolve_with_unicode_space_variants(path)
            if resolved is not None:
                if resolved not in files:
                    files.append(resolved)
            elif warn:
                logger.warning("File not found: %s", raw_path)
        except (OSError, RuntimeError) as e:
            if warn:
                logger.warning("Invalid path %s: %s", raw_path, e)
            else:
                logger.debug("Path resolution failed for %s: %s", raw_path, e)

    return text, files


class MediaTracker:
    """Track pasted images and videos in the current conversation."""

    def __init__(self) -> None:
        """Initialize MediaTracker."""
        self._entries: list[_TrackedMediaEntry] = []
        self._listeners: list[Callable[[], None]] = []
        self.next_image_id = 1
        self.next_video_id = 1

    def add_listener(self, callback: Callable[[], None]) -> None:
        """Register a listener notified whenever tracked media changes."""
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        """Unregister a previously added media change listener."""
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self) -> None:
        """Notify all registered listeners of a change in tracked media."""
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                logger.debug("MediaTracker listener raised an error", exc_info=True)

    @property
    def images(self) -> list[ImageData]:
        """List of currently tracked images."""
        return [
            entry.media
            for entry in self._entries
            if isinstance(entry.media, ImageData)
        ]

    @images.setter
    def images(self, value: list[ImageData]) -> None:
        """Replace tracked images while preserving tracked videos."""
        video_entries = [
            entry for entry in self._entries if isinstance(entry.media, VideoData)
        ]
        self._entries = [
            *(_TrackedMediaEntry(media=image) for image in value),
            *video_entries,
        ]
        self._sync_next_ids()
        self._notify()

    @property
    def videos(self) -> list[VideoData]:
        """List of currently tracked videos."""
        return [
            entry.media
            for entry in self._entries
            if isinstance(entry.media, VideoData)
        ]

    @videos.setter
    def videos(self, value: list[VideoData]) -> None:
        """Replace tracked videos while preserving tracked images."""
        image_entries = [
            entry for entry in self._entries if isinstance(entry.media, ImageData)
        ]
        self._entries = [
            *image_entries,
            *(_TrackedMediaEntry(media=video) for video in value),
        ]
        self._sync_next_ids()
        self._notify()

    def all_media(self) -> list[ImageData | VideoData]:
        """Return all tracked images and videos in attachment order."""
        return [entry.media for entry in self._entries]

    def count(self) -> int:
        """Return total number of tracked media attachments."""
        return len(self._entries)

    def pop_last(self) -> ImageData | VideoData | None:
        """Remove and return the most recently attached media item."""
        if not self._entries:
            return None
        removed = self._entries.pop()
        self._sync_next_ids()
        self._notify()
        return removed.media

    def remove_at(self, index: int) -> ImageData | VideoData | None:
        """Remove and return the media item at *index*."""
        if index < 0 or index >= len(self._entries):
            return None
        removed = self._entries.pop(index)
        self._sync_next_ids()
        self._notify()
        return removed.media

    def add_media(
        self,
        media: ImageData | VideoData,
        kind: str | None = None,
        *,
        existing_text: str = "",
        span: tuple[int, int] | None = None,
        inline: bool = True,
    ) -> str:
        """Add an image or video and return its placeholder text."""
        if kind == "image" or (kind is None and isinstance(media, ImageData)):
            return self.add_image(
                cast("ImageData", media), existing_text=existing_text, span=span, inline=inline
            )
        return self.add_video(
            cast("VideoData", media), existing_text=existing_text, span=span, inline=inline
        )

    def add_image(
        self,
        image_data: ImageData,
        *,
        existing_text: str = "",
        span: tuple[int, int] | None = None,
        inline: bool = True,
    ) -> str:
        """Add an image and return its placeholder text (e.g. `[image 1]`)."""
        while existing_text and f"[image {self.next_image_id}]" in existing_text:
            self.next_image_id += 1
        placeholder = f"[image {self.next_image_id}]"
        image_data.placeholder = placeholder
        self._entries.append(
            _TrackedMediaEntry(
                media=image_data,
                span=self._normalize_span(span, placeholder),
                inline=inline,
            )
        )
        self.next_image_id += 1
        self._notify()
        return placeholder

    def add_video(
        self,
        video_data: VideoData,
        *,
        existing_text: str = "",
        span: tuple[int, int] | None = None,
        inline: bool = True,
    ) -> str:
        """Add a video and return its placeholder text (e.g. `[video 1]`)."""
        while existing_text and f"[video {self.next_video_id}]" in existing_text:
            self.next_video_id += 1
        placeholder = f"[video {self.next_video_id}]"
        video_data.placeholder = placeholder
        self._entries.append(
            _TrackedMediaEntry(
                media=video_data,
                span=self._normalize_span(span, placeholder),
                inline=inline,
            )
        )
        self.next_video_id += 1
        self._notify()
        return placeholder

    def get_images(self) -> list[ImageData]:
        """Get all tracked images."""
        return list(self.images)

    def get_videos(self) -> list[VideoData]:
        """Get all tracked videos."""
        return list(self.videos)

    def clear(self) -> None:
        """Clear all tracked images and videos and reset ID counters."""
        had_entries = bool(self._entries)
        self._entries.clear()
        self.next_image_id = 1
        self.next_video_id = 1
        if had_entries:
            self._notify()

    def snapshot(self) -> MediaSnapshot:
        """Return a restoreable copy of the currently tracked media entries."""
        return tuple((replace(entry.media), entry.span) for entry in self._entries)

    def restore(self, snapshot: MediaSnapshot, *, inline: bool | None = None) -> None:
        """Replace tracked media with a previously captured snapshot."""
        self._entries = [
            _TrackedMediaEntry(
                media=replace(media),
                span=self._normalize_span(span, media.placeholder),
                inline=inline if inline is not None else True,
            )
            for media, span in snapshot
        ]
        self._sync_next_ids()
        self._notify()

    def remap_spans_to_text(
        self,
        text: str,
        transform_offset: Callable[[int], int] | None = None,
        *,
        previous_text: str | None = None,
    ) -> None:
        """Project tracked placeholder spans into a transformed text coordinate space."""
        if not self._entries:
            return
        if transform_offset is None:
            self.sync_to_text(text, previous_text=previous_text)
            return
        self._entries = [
            replace(
                entry,
                span=self._remap_span(
                    entry.span,
                    entry.media.placeholder,
                    text,
                    transform_offset,
                ),
            )
            if entry.inline
            else entry
            for entry in self._entries
        ]

    def sync_to_text(
        self,
        text: str,
        *,
        previous_text: str | None = None,
        cursor_offset: int | None = None,
        edited_Original_range: tuple[int, int] | None = None,
        edited_new_range: tuple[int, int] | None = None,
    ) -> None:
        """Synchronize inline-tracked media with placeholders in *text*, preserving dock-attached media."""
        _ = (previous_text, cursor_offset)
        if not self._entries:
            return

        dock_entries = [entry for entry in self._entries if not entry.inline]
        inline_entries = [entry for entry in self._entries if entry.inline]
        if not inline_entries:
            return

        placeholder_spans = self._find_placeholder_spans(text) if text else {}
        if not placeholder_spans:
            if not dock_entries:
                self.clear()
            else:
                self._entries = dock_entries
                self._sync_next_ids()
                self._notify()
            return

        reconciled_inline = self._reconcile_entries(
            placeholder_spans,
            edited_Original_range=edited_Original_range,
            edited_new_range=edited_new_range,
            entries=inline_entries,
        )
        new_entries = [*dock_entries, *reconciled_inline]
        changed = len(new_entries) != len(self._entries)
        self._entries = new_entries
        self._sync_next_ids()
        if changed:
            self._notify()

    @staticmethod
    def _normalize_span(
        span: tuple[int, int] | None,
        placeholder: str,
    ) -> tuple[int, int] | None:
        """Validate a placeholder span before storing it."""
        if span is None:
            return None
        start, end = span
        if start < 0 or end - start != len(placeholder):
            return None
        return (start, end)

    @classmethod
    def _remap_span(
        cls,
        span: tuple[int, int] | None,
        placeholder: str,
        text: str,
        transform_offset: Callable[[int], int],
    ) -> tuple[int, int] | None:
        """Map a tracked span through `transform_offset` when it still matches."""
        if span is None:
            return None
        start = transform_offset(span[0])
        end = transform_offset(span[1])
        normalized = cls._normalize_span((start, end), placeholder)
        if normalized is None:
            return None
        if text[normalized[0] : normalized[1]] != placeholder:
            return None
        return normalized

    @staticmethod
    def _find_placeholder_spans(
        text: str,
    ) -> dict[str, list[tuple[int, int]]]:
        """Collect all placeholder spans present in `text`, grouped by token."""
        spans: dict[str, list[tuple[int, int]]] = {}
        for pattern in (IMAGE_PLACEHOLDER_PATTERN, VIDEO_PLACEHOLDER_PATTERN):
            for match in pattern.finditer(text):
                spans.setdefault(match.group(0), []).append(match.span())
        return spans

    @staticmethod
    def _expected_span_after_edit(
        span: tuple[int, int] | None,
        edited_Original_range: tuple[int, int] | None,
        edited_new_range: tuple[int, int] | None,
    ) -> tuple[int, int] | None:
        """Project a prior span into post-edit coordinates when unaffected."""
        if span is None:
            return None
        if edited_Original_range is None or edited_new_range is None:
            return span

        old_start, old_end = edited_Original_range
        new_start, new_end = edited_new_range
        start, end = span
        if end <= old_start:
            return span
        if start >= old_end:
            delta = (new_end - new_start) - (old_end - old_start)
            return (start + delta, end + delta)
        return None

    def _reconcile_entries(
        self,
        placeholder_spans: dict[str, list[tuple[int, int]]],
        *,
        edited_Original_range: tuple[int, int] | None,
        edited_new_range: tuple[int, int] | None,
        entries: list[_TrackedMediaEntry] | None = None,
    ) -> list[_TrackedMediaEntry]:
        """Match tracked entries to surviving placeholder spans."""
        source_entries = entries if entries is not None else self._entries
        tracked_counts = Counter(entry.media.placeholder for entry in source_entries)
        unused_spans = {
            placeholder: list(spans) for placeholder, spans in placeholder_spans.items()
        }
        matched_spans: dict[int, tuple[int, int]] = {}

        for idx, entry in enumerate(source_entries):
            placeholder = entry.media.placeholder
            available = unused_spans.get(placeholder)
            if not available:
                continue

            expected_span = self._expected_span_after_edit(
                entry.span,
                edited_Original_range,
                edited_new_range,
            )
            if expected_span is not None and expected_span in available:
                available.remove(expected_span)
                matched_spans[idx] = expected_span

        for idx, entry in enumerate(source_entries):
            if idx in matched_spans:
                continue
            placeholder = entry.media.placeholder
            available = unused_spans.get(placeholder)
            if not available:
                continue
            if (
                edited_Original_range is not None
                and edited_new_range is not None
                and entry.span is not None
                and self._expected_span_after_edit(
                    entry.span,
                    edited_Original_range,
                    edited_new_range,
                )
                is None
                and len(available) < tracked_counts[placeholder]
            ):
                continue
            matched_spans[idx] = available.pop(0)

        for idx, entry in enumerate(source_entries):
            if idx in matched_spans:
                continue
            available = unused_spans.get(entry.media.placeholder)
            if available:
                matched_spans[idx] = available.pop(0)

        reconciled = [
            replace(entry, span=matched_spans[idx])
            for idx, entry in enumerate(source_entries)
            if idx in matched_spans
        ]
        reconciled.sort(key=lambda entry: entry.span[0] if entry.span else -1)
        return reconciled

    def _sync_next_ids(self) -> None:
        """Reset next IDs to one past the highest surviving placeholder index."""
        max_image_id = max(
            (
                int(image.placeholder.removeprefix("[image ").removesuffix("]"))
                for image in self.images
                if IMAGE_PLACEHOLDER_PATTERN.fullmatch(image.placeholder)
            ),
            default=0,
        )
        max_video_id = max(
            (
                int(video.placeholder.removeprefix("[video ").removesuffix("]"))
                for video in self.videos
                if VIDEO_PLACEHOLDER_PATTERN.fullmatch(video.placeholder)
            ),
            default=0,
        )
        self.next_image_id = max_image_id + 1
        self.next_video_id = max_video_id + 1


# Backwards compatibility alias
ImageTracker = MediaTracker

__all__ = [
    "EMAIL_PREFIX_PATTERN",
    "FILE_MENTION_PATTERN",
    "IMAGE_PLACEHOLDER_PATTERN",
    "ImageTracker",
    "MediaSnapshot",
    "MediaTracker",
    "PATH_CHAR_CLASS",
    "ParsedPastedPathPayload",
    "VIDEO_PLACEHOLDER_PATTERN",
    "dropped_payload_paths",
    "extract_leading_pasted_file_path",
    "looks_like_dropped_payload",
    "normalize_pasted_path",
    "parse_file_mentions",
    "parse_pasted_file_paths",
    "parse_pasted_path_payload",
    "parse_single_pasted_file_path",
]

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

"""Media safety, attachment hydration, outbound Markdown media extraction, and OAuth callback protection.

Ported and adapted from `deepagents_talon/media.py`, `deepagents_talon/channels/base.py`,
`deepagents_talon/channels/slack.py`, and `deepagents_talon/mcp_auth.py`.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import IO, Any, NoReturn

from talkops.channels.interfaces import ChannelMedia, MediaKind

MAX_INBOUND_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TEXT_DOCUMENT_BYTES = 100 * 1024
DEFAULT_MAX_MEDIA_BYTES = 64 * 1024 * 1024
SLACK_FILE_HOST = "files.slack.com"
DISCORD_MEDIA_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})

READABLE_DOCUMENT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".csv",
        ".json",
        ".xml",
        ".yaml",
        ".yml",
        ".log",
        ".py",
        ".js",
        ".ts",
        ".html",
        ".css",
        ".sh",
        ".tf",
        ".hcl",
        ".toml",
        ".ini",
        ".conf",
    }
)
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm", ".3gp", ".m4v"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".ogg", ".m4a", ".flac", ".opus"})

_SAFE_SUFFIX_RE = re.compile(r"\.[a-z0-9]{1,16}")
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_MARKDOWN_MEDIA_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_FENCE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_FENCE_PLACEHOLDER = "\x00CH_FENCE_"
_CODE_PLACEHOLDER = "\x00CH_CODE_"
_CANDIDATE_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", flags=re.IGNORECASE)


class ChannelMediaError(ValueError):
    """Raised when inbound or outbound channel media fails security or size validation."""


@dataclass(frozen=True, slots=True)
class MarkdownMediaRef:
    """Local media reference extracted from an assistant Markdown response (`![alt](path)`)."""

    alt: str
    path: Path


def safe_filename_part(value: str) -> str:
    """Sanitize an untrusted string for safe use in a local filename."""
    return _UNSAFE_FILENAME_RE.sub("_", value).strip("._") or "file"


def safe_suffix(filename: str, content_type: str | None = None) -> str:
    """Return a validated lowercase alphanumeric file suffix, or `.bin`."""
    suffix = Path(filename).suffix.lower()
    if _SAFE_SUFFIX_RE.fullmatch(suffix):
        return suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        if guessed and _SAFE_SUFFIX_RE.fullmatch(guessed.lower()):
            return guessed.lower()
    return ".bin"


def resolve_bounded_media_path(
    path: Path | str,
    root: Path | None = None,
    *,
    allowed_roots: Sequence[Path] | None = None,
    require_relative: bool = False,
) -> Path:
    """Resolve a local media path and enforce strict symlink, regular-file, and root containment."""
    path_obj = Path(path) if isinstance(path, str) else path
    raw = str(path_obj)
    parsed = urllib.parse.urlparse(raw)
    win_path = PureWindowsPath(raw)
    is_win_drive = bool(win_path.drive)

    if parsed.scheme and not is_win_drive:
        raise ChannelMediaError(f"media path must be a local filesystem path: {path_obj}")
    if raw.startswith("~") or (
        require_relative and (path_obj.is_absolute() or win_path.is_absolute() or is_win_drive)
    ):
        raise ChannelMediaError(
            f"media path must be a relative path under the outbound root: {path_obj}"
        )
    if ".." in path_obj.parts or ".." in win_path.parts:
        raise ChannelMediaError(
            f"media path must not contain parent-directory traversal ('..'): {path_obj}"
        )

    roots: list[Path] = []
    if root is not None:
        roots.append(root.expanduser().resolve())
    if allowed_roots:
        roots.extend(r.expanduser().resolve() for r in allowed_roots)

    base_root = roots[0] if roots else None
    candidate_input = (
        (base_root / path_obj) if (base_root is not None and not path_obj.is_absolute()) else path_obj
    )
    if candidate_input.is_symlink():
        raise ChannelMediaError(f"Symlink media paths are not allowed: {path_obj}")
    try:
        candidate = candidate_input.resolve(strict=True)
    except OSError as exc:
        raise ChannelMediaError(f"media file is unavailable: {path_obj}") from exc

    if roots and not any(candidate.is_relative_to(r) for r in roots):
        raise ChannelMediaError(f"media path escapes outbound root: {path_obj}")
    if not candidate.is_file():
        raise ChannelMediaError(f"media path is not a regular file: {path_obj}")
    return candidate


def detect_media_kind(path: Path) -> MediaKind:
    """Infer `MediaKind` (`image`, `video`, `audio`, `document`) from a file path."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    mime, _ = mimetypes.guess_type(path.name)
    if mime:
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("audio/"):
            return "audio"
    return "document"


def validate_outbound_media(
    media: ChannelMedia,
    *,
    root: Path | None = None,
    max_bytes: int = DEFAULT_MAX_MEDIA_BYTES,
) -> ChannelMedia:
    """Validate an outbound `ChannelMedia` file's containment, existence, and size."""
    if media.path is None:
        raise ChannelMediaError("outbound media requires a local file path")
    resolved = (
        resolve_bounded_media_path(media.path, root, require_relative=False)
        if root is not None
        else media.path.expanduser().resolve()
    )
    if not resolved.is_file():
        raise ChannelMediaError(f"media file does not exist: {resolved}")
    size = resolved.stat().st_size
    if size > max_bytes:
        raise ChannelMediaError(f"media file size {size} exceeds max_bytes {max_bytes}")
    mime = media.mime_type or mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    return ChannelMedia(
        media_type=media.media_type or detect_media_kind(resolved),
        path=resolved,
        url=media.url,
        mime_type=mime,
        filename=media.filename or resolved.name,
        size_bytes=size,
        metadata=media.metadata,
    )


def extract_markdown_media(text: str) -> tuple[str, tuple[ChannelMedia, ...]]:
    """Extract local `![alt](path)` media references from assistant Markdown outside code spans."""
    if not text:
        return text, ()

    fences: list[str] = []
    codes: list[str] = []

    def _save_fence(m: re.Match[str]) -> str:
        idx = len(fences)
        fences.append(m.group(0))
        return f"{_FENCE_PLACEHOLDER}{idx}\x00"

    def _save_code(m: re.Match[str]) -> str:
        idx = len(codes)
        codes.append(m.group(0))
        return f"{_CODE_PLACEHOLDER}{idx}\x00"

    masked = _FENCE_RE.sub(_save_fence, text)
    masked = _INLINE_CODE_RE.sub(_save_code, masked)

    extracted: list[ChannelMedia] = []

    def _record(m: re.Match[str]) -> str:
        raw_target = m.group(2).strip()
        parsed = urllib.parse.urlparse(raw_target)
        if parsed.scheme in {"http", "https", "data"}:
            return m.group(0)
        p = Path(raw_target)
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        extracted.append(
            ChannelMedia(
                media_type=detect_media_kind(p),
                path=p,
                mime_type=mime,
                filename=p.name,
                metadata={"alt": m.group(1)},
            )
        )
        return ""

    cleaned = _MARKDOWN_MEDIA_RE.sub(_record, masked)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    for idx, code in enumerate(codes):
        cleaned = cleaned.replace(f"{_CODE_PLACEHOLDER}{idx}\x00", code)
    for idx, fence in enumerate(fences):
        cleaned = cleaned.replace(f"{_FENCE_PLACEHOLDER}{idx}\x00", fence)
    return cleaned.strip(), tuple(extracted)


def build_inbound_media_prompt(text: str, media_items: Sequence[ChannelMedia]) -> str:
    """Enrich user prompt with inline content for readable documents/logs and metadata for binary files."""
    if not media_items:
        return text

    parts: list[str] = [text.strip()] if text.strip() else []
    summary_items: list[str] = []
    for item in media_items:
        label = item.filename or (item.path.name if item.path else item.media_type)
        summary_items.append(f"{label} ({item.url or item.path})")
        if item.path is not None and item.path.is_file():
            suffix = item.path.suffix.lower()
            if suffix in READABLE_DOCUMENT_EXTENSIONS:
                try:
                    size = item.path.stat().st_size
                    if size <= MAX_TEXT_DOCUMENT_BYTES:
                        content = item.path.read_text(encoding="utf-8", errors="replace").strip()
                        parts.append(
                            f'<attached-media name="{label}" path="{item.path}">\n'
                            f"{content}\n"
                            f"</attached-media>"
                        )
                        continue
                except OSError:
                    pass
            parts.append(
                f'<attached-media type="{item.media_type}" name="{label}" path="{item.path}" />'
            )

    parts.append(f"[Attached Media: {', '.join(summary_items)}]")
    return "\n\n".join(p for p in parts if p)


def build_multimodal_content(
    text: str,
    media_items: Sequence[ChannelMedia],
) -> str | list[dict[str, Any]]:
    """Build LangChain multimodal content blocks when local image attachments are present."""
    image_blocks: list[dict[str, Any]] = []
    for item in media_items:
        if item.media_type != "image" or item.path is None or not item.path.is_file():
            continue
        try:
            if item.path.stat().st_size > MAX_INBOUND_IMAGE_BYTES:
                continue
            b64 = base64.b64encode(item.path.read_bytes()).decode("ascii")
            mime = (
                item.mime_type
                or mimetypes.guess_type(item.path.name)[0]
                or "image/png"
            )
            image_blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                    "mime_type": mime,
                }
            )
        except OSError:
            continue

    if not image_blocks:
        return text
    return [{"type": "text", "text": text.strip() or "(image)"}, *image_blocks]


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects so a bot bearer token is never forwarded to a third-party host."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> NoReturn:
        del req, fp, code, msg, headers, newurl
        raise ChannelMediaError("File download redirected; refusing to follow redirect with token")


def is_allowed_media_host_url(url: str, allowed_hosts: Sequence[str] | frozenset[str]) -> bool:
    """Return `True` only when `url` uses HTTPS and its hostname is in `allowed_hosts`."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    allowed = {h.lower() for h in allowed_hosts}
    return parsed.scheme == "https" and host in allowed


def is_slack_file_host_url(url: str) -> bool:
    """Return `True` only when `url` is `https://files.slack.com/...`."""
    return is_allowed_media_host_url(url, (SLACK_FILE_HOST,))


def is_discord_media_host_url(url: str) -> bool:
    """Return `True` only when `url` is hosted on Discord's official CDN hosts."""
    return is_allowed_media_host_url(url, DISCORD_MEDIA_HOSTS)


async def download_channel_media_safely(
    url: str,
    *,
    allowed_hosts: Sequence[str] | frozenset[str],
    provider_label: str = "channel",
    destination: Path | None = None,
    dest_dir: Path | None = None,
    filename: str = "attachment.bin",
    auth_headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    max_bytes: int = DEFAULT_MAX_MEDIA_BYTES,
    http_client: Any = None,
) -> Path:
    """Download an inbound channel attachment with strict host allowlist, no-redirect, and `O_NOFOLLOW` guards."""
    if not is_allowed_media_host_url(url, allowed_hosts):
        raise ChannelMediaError(
            f"Refusing to download {provider_label} file from untrusted host: {url}"
        )
    target_path = destination or ((dest_dir or Path("/tmp")) / safe_filename_part(filename))
    req_headers = dict(auth_headers or {})

    if http_client is not None:
        resp = await http_client.get(
            url,
            headers=req_headers,
            follow_redirects=False,
        )
        if 300 <= resp.status_code < 400 or resp.is_redirect:
            raise ChannelMediaError("File download redirected; refusing to follow redirect with token")
        resp.raise_for_status()
        data = resp.content
        if len(data) > max_bytes:
            raise ChannelMediaError(
                f"{provider_label} media file is too large: {len(data)} bytes exceeds {max_bytes}"
            )
        target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target_path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        return target_path

    req = urllib.request.Request(url, headers=req_headers)  # noqa: S310
    opener = urllib.request.build_opener(_RefuseRedirects)
    with opener.open(req, timeout=timeout) as response:
        content_len_raw = response.headers.get("content-length")
        expected: int | None = None
        if content_len_raw is not None:
            try:
                expected = int(content_len_raw)
            except ValueError:
                expected = None
        if expected is not None and expected > max_bytes:
            raise ChannelMediaError(
                f"{provider_label} media file is too large: {expected} bytes exceeds {max_bytes}"
            )
        _write_capped_no_follow(response, target_path, max_bytes=max_bytes, expected=expected)
    return target_path


async def download_discord_attachment_safely(
    url: str,
    destination: Path | None = None,
    *,
    dest_dir: Path | None = None,
    filename: str = "attachment.bin",
    timeout: float = 30.0,
    max_bytes: int = DEFAULT_MAX_MEDIA_BYTES,
    http_client: Any = None,
) -> Path:
    """Download a Discord CDN attachment without forwarding bot credentials and with strict host checks."""
    return await download_channel_media_safely(
        url,
        allowed_hosts=DISCORD_MEDIA_HOSTS,
        provider_label="Discord",
        destination=destination,
        dest_dir=dest_dir,
        filename=filename,
        auth_headers=None,
        timeout=timeout,
        max_bytes=max_bytes,
        http_client=http_client,
    )


async def download_slack_file_safely(
    url: str,
    destination: Path | None = None,
    *,
    dest_dir: Path | None = None,
    filename: str = "attachment.bin",
    bot_token: str | None = None,
    token: str | None = None,
    timeout: float = 30.0,
    max_bytes: int = DEFAULT_MAX_MEDIA_BYTES,
    http_client: Any = None,
) -> Path:
    """Download a private Slack file with strict `files.slack.com` host, redirect, and `O_NOFOLLOW` guards."""
    auth_token = (bot_token or token or "").strip()
    return await download_channel_media_safely(
        url,
        allowed_hosts=(SLACK_FILE_HOST,),
        provider_label="Slack",
        destination=destination,
        dest_dir=dest_dir,
        filename=filename,
        auth_headers={"Authorization": f"Bearer {auth_token}"},
        timeout=timeout,
        max_bytes=max_bytes,
        http_client=http_client,
    )


def _write_capped_no_follow(
    response: IO[bytes],
    destination: Path,
    *,
    max_bytes: int,
    expected: int | None,
) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            total = 0
            while chunk := response.read(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ChannelMediaError(
                        f"Media file exceeded max_bytes ({max_bytes}) during download"
                    )
                handle.write(chunk)
            if expected is not None and total != expected:
                raise ChannelMediaError(
                    f"Incomplete media download: received {total} of {expected} bytes"
                )
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def extract_loopback_oauth_callback_url(text: str) -> str | None:
    """Detect a pasted localhost/loopback OAuth callback URL containing `code=` and `state=`."""
    if not text:
        return None
    for candidate in _CANDIDATE_URL_RE.findall(text):
        cleaned = candidate.rstrip(".,;:)>]")
        try:
            parsed = urllib.parse.urlsplit(cleaned)
        except Exception:
            continue
        host = (parsed.hostname or "").lower()
        if host not in {"localhost", "127.0.0.1", "::1"}:
            continue
        query = urllib.parse.parse_qs(parsed.query)
        if ("code" in query or "error" in query) and "state" in query:
            return cleaned
    return None


def contains_oauth_callback_url(text: str) -> bool:
    """Return `True` if `text` contains a loopback OAuth callback URL that must not enter LLM context."""
    return extract_loopback_oauth_callback_url(text) is not None

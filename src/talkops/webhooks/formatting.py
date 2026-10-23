"""Shared channel exposure policies, text chunking, and Markdown formatting utilities.

Adapts `deepagents_talon/channels/base.py` for Slack (`mrkdwn`), Microsoft Teams
(Markdown + Adaptive Cards), and Discord (2000-char limit + fenced code preservation).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
import fnmatch
import re
from typing import Any, Literal

DEFAULT_MAX_TEXT_CHARS = 4000
SLACK_MAX_TEXT_CHARS = 4000
TEAMS_MAX_TEXT_CHARS = 4000
DISCORD_MAX_TEXT_CHARS = 2000
SLACK_MAX_BLOCKS = 50
SLACK_SECTION_MAX_CHARS = 3000
SLACK_HEADER_MAX_CHARS = 150

_BLOCK_FENCE_RE = re.compile(r"(```[\s\S]*?```)")
_FENCE_RE = re.compile(r"(```[\s\S]*?```|`[^`\n]+`)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", flags=re.MULTILINE)
_SINGLE_LINE_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_HR_LINE_RE = re.compile(r"^\s*(?:-\s*-\s*-[-\s]*|\*\s*\*\s*\*[\*\s]*|_\s*_\s*_[_\s]*)\s*$")
_UNORDERED_LIST_RE = re.compile(r"^([ \t]*)[-*+][ \t]+(.+)$", flags=re.MULTILINE)
_CALLOUT_HEADER_RE = re.compile(
    r"^>\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*(.*)$",
    flags=re.IGNORECASE,
)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_INBOUND_SLACK_LINK_RE = re.compile(r"<((?:https?|mailto):[^<>|]+)(?:\|([^<>]*))?>")
_SLACK_BROADCAST_RE = re.compile(r"<!(?:here|channel|everyone)(?:\|[^>]+)?>", flags=re.IGNORECASE)
_SLACK_USER_MENTION_RE = re.compile(r"<@([UW][A-Z0-9_]+)(?:\|[^>]+)?>")
_DISCORD_BROADCAST_RE = re.compile(r"@(everyone|here)\b")
_DISCORD_USER_MENTION_RE = re.compile(r"<@!?(\d+)>")

_CALLOUT_ICONS: dict[str, tuple[str, str]] = {
    "note": ("ℹ️", "Note"),
    "tip": ("💡", "Tip"),
    "important": ("📌", "Important"),
    "warning": ("⚠️", "Warning"),
    "caution": ("🚨", "Caution"),
}

APPROVAL_REACTION_MAP: dict[str, str] = {
    "\U0001f44d": "approve",
    "👍": "approve",
    "+1": "approve",
    "thumbsup": "approve",
    "thumbsup_all": "approve",
    "like": "approve",
    "heart": "approve",
    "white_check_mark": "approve",
    "\U0001f44e": "reject",
    "👎": "reject",
    "-1": "reject",
    "thumbsdown": "reject",
    "x": "reject",
}


class ExposureMode(StrEnum):
    """Inbound channel exposure policy, modeled after `deepagents-talon`."""

    SELF = "self"
    ALLOWLIST = "allowlist"
    OPEN = "open"


@dataclass(frozen=True, slots=True)
class ChannelExposure:
    """Exposure policy controlling which senders and chats can trigger agent turns.

    Args:
        mode: `SELF` (operator only), `ALLOWLIST` (operator + allowlisted users/chats
            + paired senders), or `OPEN` (any sender in reachable chats; requires explicit ack).
        operator_id: Primary operator user ID on this channel.
        allowed_users: Additional user IDs permitted in `ALLOWLIST` mode.
        allowed_chats: Channel/conversation IDs permitted in `ALLOWLIST` mode.
        open_ack: Whether the operator explicitly acknowledged `OPEN` exposure risk.
        require_mention_in_channels: Whether non-DM channel messages require `@bot` mention.
    """

    mode: ExposureMode = ExposureMode.ALLOWLIST
    operator_id: str | None = None
    allowed_users: frozenset[str] = field(default_factory=frozenset)
    allowed_chats: frozenset[str] = field(default_factory=frozenset)
    mention_allowlist_user_ids: frozenset[str] = field(default_factory=frozenset)
    mention_patterns: tuple[str, ...] = ()
    open_ack: bool = False
    require_mention_in_channels: bool = True

    def validate(self, provider: str) -> None:
        """Raise `ValueError` if `OPEN` mode is selected without `open_ack=True`."""
        if self.mode == ExposureMode.OPEN and not self.open_ack:
            raise ValueError(
                f"{provider} exposure mode 'open' requires explicit acknowledgment "
                f"(set TALKOPS_CHANNELS_{provider.upper()}_OPEN_ACK=1 or open_ack=true)."
            )
        if self.mode == ExposureMode.SELF and not self.operator_id:
            raise ValueError(
                f"{provider} exposure mode 'self' requires operator_id "
                f"(set TALKOPS_CHANNELS_{provider.upper()}_OPERATOR_ID)."
            )

    def is_operator(self, sender_id: str | None) -> bool:
        """Return whether `sender_id` is the configured channel operator."""
        return bool(sender_id and self.operator_id and sender_id == self.operator_id)

    def matches_mention_pattern(self, text: str) -> bool:
        """Return whether `text` matches any configured regex or glob `mention_patterns`."""
        if not text or not self.mention_patterns:
            return False
        for pat in self.mention_patterns:
            try:
                if re.search(pat, text, flags=re.IGNORECASE):
                    return True
            except re.error:
                pass
            if fnmatch.fnmatchcase(text.lower(), pat.lower()):
                return True
        return False

    @staticmethod
    def _matches_set_or_glob(value: str, patterns: frozenset[str]) -> bool:
        if value in patterns:
            return True
        return any(
            ("*" in p or "?" in p) and fnmatch.fnmatchcase(value, p) for p in patterns
        )

    def allows(
        self,
        *,
        sender_id: str | None,
        conversation_id: str,
        parent_channel_id: str | None = None,
        is_dm: bool = False,
        is_paired: bool = False,
        text: str = "",
    ) -> bool:
        """Evaluate whether a message from `sender_id` in `conversation_id` is admitted."""
        if not sender_id:
            return False
        if self.is_operator(sender_id) or is_paired:
            return True
        if self.mode == ExposureMode.SELF:
            return False
        if self.mode == ExposureMode.OPEN:
            return self.open_ack
        # ALLOWLIST mode:
        # If no operator_id, allowed_users, allowed_chats, or mention_patterns are configured yet
        # (default zero-config setup), admit senders so the channel works out-of-the-box.
        if (
            not self.operator_id
            and not self.allowed_users
            and not self.allowed_chats
            and not self.mention_patterns
        ):
            return True
        if self._matches_set_or_glob(sender_id, self.allowed_users):
            return True
        if not is_dm:
            if self._matches_set_or_glob(conversation_id, self.allowed_chats):
                return True
            if parent_channel_id and self._matches_set_or_glob(parent_channel_id, self.allowed_chats):
                return True
            # Also check `<channel_id>:<thread_ts>` prefix match against allowed_chats
            if ":" in conversation_id and self._matches_set_or_glob(
                conversation_id.split(":", 1)[0], self.allowed_chats
            ):
                return True
        if text and self.matches_mention_pattern(text):
            return True
        return False


def split_csv(value: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Split a comma-separated string or sequence into stripped non-empty tokens."""
    if not value:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        items: list[str] = []
        for item in value:
            items.extend(part.strip() for part in item.split(",") if part.strip())
        return tuple(items)
    return tuple(part.strip() for part in value.split(",") if part.strip())


def normalize_reaction_decision(emoji: str) -> str | None:
    """Map a platform reaction emoji/name to `'approve'` or `'reject'`, or `None`."""
    cleaned = emoji.strip().strip(":").lower()
    # Strip Slack skin-tone suffixes like `thumbsup::skin-tone-3`
    if "::skin-tone-" in cleaned:
        cleaned = cleaned.split("::skin-tone-", 1)[0]
    return APPROVAL_REACTION_MAP.get(cleaned) or APPROVAL_REACTION_MAP.get(emoji.strip())


def chunk_text(text: str, max_chars: int = DEFAULT_MAX_TEXT_CHARS) -> list[str]:
    """Split `text` into chunks of at most `max_chars`, preserving fenced code blocks.

    When a chunk boundary falls inside a ```` ``` ```` code block, the fence is closed at
    the end of the chunk and reopened at the start of the next chunk so Slack, Teams,
    and Discord never render broken code blocks.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    stripped = text.strip()
    if not stripped:
        return []
    if len(stripped) <= max_chars:
        return [stripped]

    chunks: list[str] = []
    remaining = stripped
    reserve_fence = 12  # Room for closing/reopening ```

    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break

        limit = max(1, max_chars - reserve_fence)
        candidate = remaining[:limit]

        # Prefer splitting at paragraph, line, or word boundaries
        split_at = candidate.rfind("\n\n")
        if split_at < limit // 2:
            split_at = candidate.rfind("\n")
        if split_at < limit // 2:
            split_at = candidate.rfind(" ")
        if split_at <= 0:
            split_at = limit

        head = remaining[:split_at].rstrip()
        tail = remaining[split_at:].lstrip("\n ")

        # Check if we are inside an unclosed triple-backtick fence
        fence_count = head.count("```")
        if fence_count % 2 == 1:
            # Find language tag of the last opened fence
            last_fence_idx = head.rfind("```")
            after_fence = head[last_fence_idx + 3 :]
            first_line = after_fence.split("\n", 1)[0].strip()
            lang = first_line if first_line.isalnum() else ""
            head = f"{head}\n```"
            tail = f"```{lang}\n{tail}"

        chunks.append(head)
        remaining = tail

    return chunks


@dataclass(frozen=True, slots=True)
class MarkdownTable:
    """Parsed Markdown pipe table with headers, column alignments, and body rows."""

    headers: tuple[str, ...]
    alignments: tuple[Literal["left", "center", "right"], ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class MarkdownBlock:
    """Platform-agnostic AST node produced by `parse_markdown_blocks`."""

    kind: Literal["heading", "paragraph", "code", "table", "divider", "callout"]
    text: str = ""
    level: int = 1
    language: str = ""
    table: MarkdownTable | None = None
    callout_type: str = ""


@dataclass(frozen=True, slots=True)
class ParsedChannelOutput:
    """Compiled channel response containing both fallback text and native rich UI blocks/cards."""

    channel: str
    text: str
    blocks_or_cards: Any = None
    ast: tuple[MarkdownBlock, ...] = ()


def _split_pipe_row(line: str) -> list[str]:
    """Split a Markdown table row on unescaped `|` characters outside inline `` `code` `` spans."""
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|") and not inner.endswith(r"\|"):
        inner = inner[:-1]

    cells: list[str] = []
    buf: list[str] = []
    in_code = False
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == "\\" and i + 1 < len(inner) and inner[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if ch == "`":
            in_code = not in_code
            buf.append(ch)
            i += 1
            continue
        if ch == "|" and not in_code:
            cells.append("".join(buf).strip())
            buf.clear()
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf).strip())
    return cells


def _is_table_divider_row(cells: list[str]) -> bool:
    """Return True if all cells in a Markdown table row are divider dashes (`---`, `:---:`, etc.)."""
    if not cells:
        return False
    return all(bool(c) and set(c) <= {"-", ":", " "} and "-" in c for c in cells)


def _parse_divider_alignments(
    cells: list[str],
    num_cols: int,
) -> tuple[Literal["left", "center", "right"], ...]:
    alignments: list[Literal["left", "center", "right"]] = []
    for idx in range(num_cols):
        cell = cells[idx].strip() if idx < len(cells) else "---"
        if cell.startswith(":") and cell.endswith(":"):
            alignments.append("center")
        elif cell.endswith(":"):
            alignments.append("right")
        else:
            alignments.append("left")
    return tuple(alignments)


def _parse_markdown_table(lines: list[str]) -> MarkdownTable | None:
    """Parse consecutive pipe-table lines into a `MarkdownTable` AST node."""
    if len(lines) < 2:
        return None
    raw_rows = [_split_pipe_row(line) for line in lines]
    if not raw_rows or len(raw_rows[0]) < 1:
        return None

    # Find the divider row (usually index 1)
    divider_idx: int | None = None
    for idx, r in enumerate(raw_rows):
        if _is_table_divider_row(r):
            divider_idx = idx
            break

    if divider_idx == 1:
        headers = tuple(raw_rows[0])
        num_cols = max(len(r) for r in raw_rows)
        if len(headers) < num_cols:
            headers = headers + ("",) * (num_cols - len(headers))
        alignments = _parse_divider_alignments(raw_rows[1], num_cols)
        body_rows: list[tuple[str, ...]] = []
        for r in raw_rows[2:]:
            if _is_table_divider_row(r):
                continue
            padded = tuple(r[c] if c < len(r) else "" for c in range(num_cols))
            body_rows.append(padded)
        return MarkdownTable(headers=headers, alignments=alignments, rows=tuple(body_rows))

    # Fallback if table has >= 2 rows with pipes even without a standard separator row
    num_cols = max(len(r) for r in raw_rows)
    if num_cols < 2:
        return None
    headers = tuple(raw_rows[0][c] if c < len(raw_rows[0]) else "" for c in range(num_cols))
    fallback_alignments: tuple[Literal["left", "center", "right"], ...] = tuple(
        "left" for _ in range(num_cols)
    )
    body_rows = [
        tuple(r[c] if c < len(r) else "" for c in range(num_cols))
        for idx, r in enumerate(raw_rows[1:], start=1)
        if idx != divider_idx
    ]
    return MarkdownTable(headers=headers, alignments=fallback_alignments, rows=tuple(body_rows))


def _clean_table_cell(cell: str) -> str:
    """Strip inline Markdown code backticks and bold markers inside a monospace table cell."""
    cleaned = cell.strip()
    cleaned = re.sub(r"`([^`\n]+)`", r"\1", cleaned)
    cleaned = _BOLD_RE.sub(lambda m: m.group(1) or m.group(2) or "", cleaned)
    cleaned = _STRIKE_RE.sub(r"\1", cleaned)
    return cleaned.strip()


def _format_aligned_table(lines: list[str]) -> list[str]:
    """Parse Markdown pipe-table lines and format them into an aligned monospace ASCII table."""
    parsed_rows: list[list[str]] = []
    divider_indices: set[int] = set()

    for idx, raw_line in enumerate(lines):
        raw_cells = _split_pipe_row(raw_line)
        if _is_table_divider_row(raw_cells):
            divider_indices.add(idx)
            parsed_rows.append(raw_cells)
        else:
            parsed_rows.append([_clean_table_cell(c) for c in raw_cells])

    if not parsed_rows:
        return lines

    num_cols = max(len(r) for r in parsed_rows)
    col_widths = [0] * num_cols
    for idx, row in enumerate(parsed_rows):
        if idx in divider_indices:
            continue
        for col_idx in range(num_cols):
            val = row[col_idx] if col_idx < len(row) else ""
            if len(val) > col_widths[col_idx]:
                col_widths[col_idx] = len(val)

    col_widths = [max(w, 3) for w in col_widths]

    formatted_lines: list[str] = []
    for idx, row in enumerate(parsed_rows):
        if idx in divider_indices:
            sep_cells = ["-" * col_widths[c] for c in range(num_cols)]
            formatted_lines.append("| " + " | ".join(sep_cells) + " |")
        else:
            padded_cells = [
                (row[c] if c < len(row) else "").ljust(col_widths[c])
                for c in range(num_cols)
            ]
            formatted_lines.append("| " + " | ".join(padded_cells) + " |")
    return formatted_lines


def _convert_markdown_tables_to_code(text: str) -> str:
    """Wrap consecutive Markdown pipe-table lines in aligned triple-backtick code blocks for Slack."""
    lines = text.splitlines()
    output: list[str] = []
    table_buf: list[str] = []

    def _flush_table() -> None:
        if not table_buf:
            return
        if len(table_buf) >= 2:
            output.append("```")
            output.extend(_format_aligned_table(table_buf))
            output.append("```")
        else:
            output.extend(table_buf)
        table_buf.clear()

    for line in lines:
        if _TABLE_LINE_RE.match(line):
            table_buf.append(line)
        else:
            _flush_table()
            output.append(line)
    _flush_table()
    return "\n".join(output)


def decode_slack_mrkdwn(text: str) -> str:
    """Decode Slack's inbound message encoding back into plain user text.

    Matches `deepagents-talon` `_decode_mrkdwn`:
    - Unwraps `<https://...|label>` into `label (https://...)` or `https://...` when label matches URL.
    - Unescapes `&lt;`, `&gt;`, and `&amp;` (`&amp;` last so literal `&lt;` is preserved).
    """
    if not text:
        return ""

    def _unwrap_link(match: re.Match[str]) -> str:
        url, label = match.group(1), match.group(2)
        if not label or url in {label, f"mailto:{label}"}:
            return url
        return f"{label} ({url})"

    unwrapped = _INBOUND_SLACK_LINK_RE.sub(_unwrap_link, text)
    return unwrapped.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _convert_markdown_lists_and_callouts(segment: str) -> str:
    """Convert Markdown unordered lists (`- ` / `* `) and GitHub callouts (`> [!NOTE]`) for Slack."""
    lines = segment.split("\n")
    out: list[str] = []
    for line in lines:
        callout_m = _CALLOUT_HEADER_RE.match(line)
        if callout_m:
            kind = callout_m.group(1).lower()
            rest = callout_m.group(2).strip()
            icon, label = _CALLOUT_ICONS.get(kind, ("ℹ️", kind.title()))
            out.append(f"> {icon} *{label}*" + (f": {rest}" if rest else ""))
            continue
        list_m = _UNORDERED_LIST_RE.match(line)
        if list_m:
            indent, item_body = list_m.group(1), list_m.group(2)
            depth = len(indent.expandtabs(2)) // 2
            bullet = "◦" if depth >= 1 else "•"
            prefix = ("    " * depth) + f"{bullet} "
            out.append(f"{prefix}{item_body}")
            continue
        out.append(line)
    return "\n".join(out)


def _format_inline_slack_mrkdwn(
    text: str,
    *,
    mention_allowlist: frozenset[str] | None = None,
) -> str:
    """Convert inline Markdown outside code spans into Slack `mrkdwn`."""
    if not text:
        return ""
    parts = _FENCE_RE.split(text)
    transformed: list[str] = []
    for idx, segment in enumerate(parts):
        if idx % 2 == 1:
            transformed.append(segment)
            continue

        seg = _convert_markdown_lists_and_callouts(segment)
        seg = _MD_LINK_RE.sub(r"<\2|\1>", seg)
        seg = _HEADING_RE.sub(r"*\2*", seg)
        seg = _BOLD_RE.sub(lambda m: f"*{m.group(1) or m.group(2)}*", seg)
        seg = _STRIKE_RE.sub(r"~\1~", seg)
        seg = _SLACK_BROADCAST_RE.sub(
            lambda m: m.group(0).replace("<", "&lt;").replace(">", "&gt;"),
            seg,
        )
        if mention_allowlist is not None:
            seg = _SLACK_USER_MENTION_RE.sub(
                lambda m: (
                    f"<@{m.group(1)}>"
                    if m.group(1) in mention_allowlist
                    else f"&lt;@{m.group(1)}&gt;"
                ),
                seg,
            )
        transformed.append(seg)
    return "".join(transformed)


def format_markdown_for_slack(
    text: str,
    *,
    mention_allowlist: frozenset[str] | None = None,
) -> str:
    """Convert standard GitHub-flavored Markdown into Slack `mrkdwn` format.

    Preserves inline code and fenced code blocks while converting:
    - Markdown tables -> fenced monospace blocks
    - `- item` / `* item` -> `• item` (preventing `* **Key:**` bold collisions)
    - `> [!NOTE]` / `> [!WARNING]` -> `> ℹ️ *Note*` / `> ⚠️ *Warning*`
    - `[label](https://...)` -> `<https://...|label>`
    - `**bold**` / `__bold__` -> `*bold*`
    - `~~strike~~` -> `~strike~`
    - `# Heading` -> `*Heading*`
    - Neutralizes `<!here>`, `<!channel>`, `<!everyone>` broadcast pings and filters
      `<@U...>` user mentions against `mention_allowlist` when provided.
    """
    if not text:
        return ""

    # Step 1: Convert Markdown pipe-tables outside fenced ``` code blocks first
    # so inline `code` cells inside table rows do not split the table in half.
    block_parts = _BLOCK_FENCE_RE.split(text)
    table_converted_parts: list[str] = []
    for b_idx, b_seg in enumerate(block_parts):
        if b_idx % 2 == 1:
            table_converted_parts.append(b_seg)
        else:
            table_converted_parts.append(_convert_markdown_tables_to_code(b_seg))
    text_with_tables = "".join(table_converted_parts)

    # Step 2: Convert inline Markdown (lists, callouts, bold, links, headings, strikethrough)
    return _format_inline_slack_mrkdwn(text_with_tables, mention_allowlist=mention_allowlist)


def parse_markdown_blocks(text: str) -> list[MarkdownBlock]:
    """Generic output parser that converts arbitrary LLM Markdown into structured `MarkdownBlock` AST nodes.

    Handles:
    - Fenced code blocks (```` ```lang ... ``` ````)
    - ATX headings (`#` .. `######`)
    - Horizontal dividers (`---`, `***`, `___`)
    - GitHub-flavored Markdown pipe tables (`| Col1 | Col2 |`)
    - Callout / blockquote sections (`> [!NOTE]`, `> ...`)
    - Prose paragraphs & bullet/numbered lists
    """
    if not text or not text.strip():
        return []

    normalized = text.replace("\r\n", "\n").strip()
    blocks: list[MarkdownBlock] = []

    # Split by fenced code blocks first so content inside ``` is never parsed as tables/headings
    fence_parts = _BLOCK_FENCE_RE.split(normalized)
    for part_idx, part in enumerate(fence_parts):
        if not part:
            continue
        if part_idx % 2 == 1:
            # Fenced code block: ```lang\n...\n```
            inner = part[3:-3] if part.startswith("```") and part.endswith("```") else part
            first_nl = inner.find("\n")
            if first_nl != -1:
                first_line = inner[:first_nl].strip()
                if first_line and " " not in first_line and not first_line.startswith("|"):
                    lang = first_line
                    code_body = inner[first_nl + 1 :].strip("\n")
                else:
                    lang = ""
                    code_body = inner.strip("\n")
            else:
                lang = ""
                code_body = inner.strip()
            blocks.append(MarkdownBlock(kind="code", text=code_body, language=lang))
            continue

        lines = part.split("\n")
        para_buf: list[str] = []
        table_buf: list[str] = []
        callout_buf: list[str] = []

        def _flush_para() -> None:
            if not para_buf:
                return
            joined = "\n".join(para_buf).strip()
            if joined:
                blocks.append(MarkdownBlock(kind="paragraph", text=joined))
            para_buf.clear()

        def _flush_table() -> None:
            if not table_buf:
                return
            parsed_tbl = _parse_markdown_table(table_buf)
            if parsed_tbl is not None:
                blocks.append(
                    MarkdownBlock(
                        kind="table",
                        text="\n".join(table_buf),
                        table=parsed_tbl,
                    )
                )
            else:
                para_buf.extend(table_buf)
            table_buf.clear()

        def _flush_callout() -> None:
            if not callout_buf:
                return
            first = callout_buf[0]
            m = _CALLOUT_HEADER_RE.match(first)
            if m:
                c_type = m.group(1).lower()
                rest_first = m.group(2).strip()
                body_lines = ([rest_first] if rest_first else []) + [
                    re.sub(r"^>\s?", "", ln) for ln in callout_buf[1:]
                ]
                blocks.append(
                    MarkdownBlock(
                        kind="callout",
                        callout_type=c_type,
                        text="\n".join(body_lines).strip(),
                    )
                )
            else:
                body_lines = [re.sub(r"^>\s?", "", ln) for ln in callout_buf]
                blocks.append(
                    MarkdownBlock(
                        kind="callout",
                        callout_type="quote",
                        text="\n".join(body_lines).strip(),
                    )
                )
            callout_buf.clear()

        for line in lines:
            stripped = line.strip()

            # 1. Pipe table row
            if _TABLE_LINE_RE.match(line):
                _flush_para()
                _flush_callout()
                table_buf.append(line)
                continue
            else:
                _flush_table()

            # 2. Blockquote / Callout line
            if stripped.startswith(">"):
                _flush_para()
                callout_buf.append(stripped)
                continue
            else:
                _flush_callout()

            # 3. Horizontal rule (`---`, `***`, `___`)
            if _HR_LINE_RE.match(line):
                _flush_para()
                blocks.append(MarkdownBlock(kind="divider"))
                continue

            # 4. Heading (`# Heading`)
            heading_m = _SINGLE_LINE_HEADING_RE.match(line)
            if heading_m:
                _flush_para()
                level = len(heading_m.group(1))
                heading_text = heading_m.group(2).strip()
                blocks.append(MarkdownBlock(kind="heading", level=level, text=heading_text))
                continue

            # 5. Blank line separates paragraphs
            if not stripped:
                _flush_para()
                continue

            para_buf.append(line)

        _flush_table()
        _flush_callout()
        _flush_para()

    return blocks


_INLINE_TOKEN_RE = re.compile(
    r"`(?P<code>[^`\n]+)`"
    r"|\[(?P<md_label>[^\]\n]+)\]\((?P<md_url>https?://[^)\s]+)\)"
    r"|<(?:(?P<slack_url>https?://[^>|]+)\|(?P<slack_label>[^>]+))>"
    r"|\*\*(?P<bold1>.+?)\*\*"
    r"|__(?P<bold2>.+?)__"
    r"|~~(?P<strike>.+?)~~"
)


def _parse_cell_to_slack_rich_text(
    cell_text: str,
    *,
    is_header: bool = False,
) -> dict[str, Any]:
    """Convert a Markdown table cell into a Slack Block Kit `table` `rich_text` cell."""
    cleaned = cell_text.strip()
    if not cleaned:
        return {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [
                        {
                            "type": "text",
                            "text": "—",
                            **({"style": {"bold": True}} if is_header else {}),
                        }
                    ],
                }
            ],
        }

    elements: list[dict[str, Any]] = []
    pos = 0
    for match in _INLINE_TOKEN_RE.finditer(cleaned):
        if match.start() > pos:
            plain = cleaned[pos : match.start()]
            if plain:
                el: dict[str, Any] = {"type": "text", "text": plain}
                if is_header:
                    el["style"] = {"bold": True}
                elements.append(el)

        if match.group("code") is not None:
            style: dict[str, bool] = {"code": True}
            if is_header:
                style["bold"] = True
            elements.append(
                {
                    "type": "text",
                    "text": match.group("code") or " ",
                    "style": style,
                }
            )
        elif match.group("md_url") is not None:
            link_el: dict[str, Any] = {
                "type": "link",
                "url": match.group("md_url"),
                "text": match.group("md_label") or match.group("md_url"),
            }
            if is_header:
                link_el["style"] = {"bold": True}
            elements.append(link_el)
        elif match.group("slack_url") is not None:
            link_el = {
                "type": "link",
                "url": match.group("slack_url"),
                "text": match.group("slack_label") or match.group("slack_url"),
            }
            if is_header:
                link_el["style"] = {"bold": True}
            elements.append(link_el)
        elif match.group("bold1") is not None or match.group("bold2") is not None:
            b_txt = match.group("bold1") or match.group("bold2") or " "
            elements.append(
                {
                    "type": "text",
                    "text": b_txt,
                    "style": {"bold": True},
                }
            )
        elif match.group("strike") is not None:
            style = {"strike": True}
            if is_header:
                style["bold"] = True
            elements.append(
                {
                    "type": "text",
                    "text": match.group("strike") or " ",
                    "style": style,
                }
            )
        pos = match.end()

    if pos < len(cleaned):
        tail = cleaned[pos:]
        if tail:
            el = {"type": "text", "text": tail}
            if is_header:
                el["style"] = {"bold": True}
            elements.append(el)

    if not elements:
        elements.append(
            {
                "type": "text",
                "text": cleaned or "—",
                **({"style": {"bold": True}} if is_header else {}),
            }
        )

    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": elements,
            }
        ],
    }


def _build_slack_native_table_block(table: MarkdownTable) -> dict[str, Any]:
    """Build a native Slack Block Kit `table` block (`{"type": "table", "rows": [...]}`)."""
    num_cols = max(1, min(20, len(table.headers)))
    header_row = [
        _parse_cell_to_slack_rich_text(
            table.headers[c] if c < len(table.headers) else f"Col {c + 1}",
            is_header=True,
        )
        for c in range(num_cols)
    ]
    rows: list[list[dict[str, Any]]] = [header_row]
    for row in table.rows[:99]:  # Slack max 100 rows total including header
        rows.append(
            [
                _parse_cell_to_slack_rich_text(
                    row[c] if c < len(row) else "",
                    is_header=False,
                )
                for c in range(num_cols)
            ]
        )
    col_settings = [
        {
            "align": table.alignments[c] if c < len(table.alignments) else "left",
            "is_wrapped": True,
        }
        for c in range(num_cols)
    ]
    return {
        "type": "table",
        "column_settings": col_settings,
        "rows": rows,
    }


def _build_slack_structured_table_sections(
    table: MarkdownTable,
    *,
    mention_allowlist: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Render a `MarkdownTable` into clean Slack `section` blocks (`fields` or bullet cards) as fallback."""
    num_cols = len(table.headers)
    headers = [ _clean_table_cell(h) or f"Column {i + 1}" for i, h in enumerate(table.headers) ]

    # For 2-column tables with up to 5 rows, Slack's native 2-column `fields` grid is ultra clean
    if num_cols == 2 and 1 <= len(table.rows) <= 5:
        fields: list[dict[str, str]] = []
        for row in table.rows:
            for c_idx in range(2):
                val = _format_inline_slack_mrkdwn(
                    row[c_idx] if c_idx < len(row) else "—",
                    mention_allowlist=mention_allowlist,
                )
                fields.append(
                    {
                        "type": "mrkdwn",
                        "text": f"*{headers[c_idx]}*\n{val or '—'}"[:2000],
                    }
                )
        return [{"type": "section", "fields": fields[:10]}]

    # For <= 4 columns and <= 20 rows, render structured bullet cards preserving inline `code` and links
    if num_cols <= 4 and len(table.rows) <= 20:
        bullet_lines: list[str] = []
        for row in table.rows:
            parts: list[str] = []
            for c_idx in range(num_cols):
                h_label = headers[c_idx]
                cell_val = _format_inline_slack_mrkdwn(
                    row[c_idx] if c_idx < len(row) else "—",
                    mention_allowlist=mention_allowlist,
                )
                parts.append(f"*{h_label}:* {cell_val or '—'}")
            bullet_lines.append("• " + "  ·  ".join(parts))
        joined = "\n".join(bullet_lines)
        return [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": chunk[:SLACK_SECTION_MAX_CHARS]},
            }
            for chunk in chunk_text(joined, max_chars=SLACK_SECTION_MAX_CHARS)
        ]

    # Wide or long tables: aligned monospace table in a section block
    raw_lines = [
        "| " + " | ".join(table.headers) + " |",
        "| " + " | ".join("---" for _ in table.headers) + " |",
        *("| " + " | ".join(r) + " |" for r in table.rows),
    ]
    aligned = "```\n" + "\n".join(_format_aligned_table(raw_lines)) + "\n```"
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": chunk[:SLACK_SECTION_MAX_CHARS]},
        }
        for chunk in chunk_text(aligned, max_chars=SLACK_SECTION_MAX_CHARS)
    ]


def build_slack_response_blocks(
    text: str,
    *,
    mention_allowlist: frozenset[str] | None = None,
    use_native_table: bool = True,
    tool_names: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Compile arbitrary LLM Markdown output into industry-standard Slack Block Kit blocks.

    Maps:
    - `#` / `##` / `###` headings -> `header` block (plain_text, emoji=True)
    - `---` horizontal rules -> `divider` block
    - Pipe tables -> Native Slack `table` block (with rich_text inline code/bold/links) for the
      first table, or structured `section` `fields` / bullet cards when `use_native_table=False`
    - Callouts (`> [!NOTE]`, `> ...`) -> Styled blockquote `section` block
    - Fenced code blocks -> Monospace `section` blocks
    - Prose & bullet lists -> `section` (`mrkdwn`) blocks with proper `•` / `◦` bullets
    - Executed tool names -> Compact footer `context` block
    """
    ast_blocks = parse_markdown_blocks(text)
    if not ast_blocks:
        return []

    slack_blocks: list[dict[str, Any]] = []
    emitted_native_table = False
    max_body_blocks = SLACK_MAX_BLOCKS - (1 if tool_names else 0)

    for node in ast_blocks:
        if len(slack_blocks) >= max_body_blocks:
            break

        if node.kind == "divider":
            slack_blocks.append({"type": "divider"})

        elif node.kind == "heading":
            clean_heading = _clean_table_cell(node.text)
            if node.level <= 3 and clean_heading:
                if (
                    slack_blocks
                    and slack_blocks[-1].get("type") not in {"divider", "header"}
                    and len(slack_blocks) + 1 < max_body_blocks
                ):
                    slack_blocks.append({"type": "divider"})
                slack_blocks.append(
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": clean_heading[:SLACK_HEADER_MAX_CHARS],
                            "emoji": True,
                        },
                    }
                )
            else:
                formatted_h = _format_inline_slack_mrkdwn(
                    node.text, mention_allowlist=mention_allowlist
                )
                slack_blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{formatted_h}*"[:SLACK_SECTION_MAX_CHARS],
                        },
                    }
                )

        elif node.kind == "table" and node.table is not None:
            if use_native_table and not emitted_native_table and len(node.table.rows) >= 1:
                slack_blocks.append(_build_slack_native_table_block(node.table))
                emitted_native_table = True
            else:
                sections = _build_slack_structured_table_sections(
                    node.table, mention_allowlist=mention_allowlist
                )
                for sec in sections:
                    if len(slack_blocks) < max_body_blocks:
                        slack_blocks.append(sec)

        elif node.kind == "callout":
            formatted_body = _format_inline_slack_mrkdwn(
                node.text, mention_allowlist=mention_allowlist
            )
            if node.callout_type and node.callout_type != "quote":
                icon, label = _CALLOUT_ICONS.get(
                    node.callout_type, ("ℹ️", node.callout_type.title())
                )
                quoted_lines = [f"> {icon} *{label}*"] + [
                    f"> {ln}" for ln in formatted_body.split("\n") if ln.strip()
                ]
            else:
                quoted_lines = [f"> {ln}" for ln in formatted_body.split("\n")]
            quoted_text = "\n".join(quoted_lines)
            slack_blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": quoted_text[:SLACK_SECTION_MAX_CHARS]},
                }
            )

        elif node.kind == "code":
            fenced = f"```\n{node.text}\n```"
            for chunk in chunk_text(fenced, max_chars=SLACK_SECTION_MAX_CHARS - 20):
                if len(slack_blocks) >= max_body_blocks:
                    break
                slack_blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": chunk[:SLACK_SECTION_MAX_CHARS]},
                    }
                )

        elif node.kind == "paragraph":
            formatted_p = _format_inline_slack_mrkdwn(
                node.text, mention_allowlist=mention_allowlist
            )
            for chunk in chunk_text(formatted_p, max_chars=SLACK_SECTION_MAX_CHARS - 20):
                if len(slack_blocks) >= max_body_blocks:
                    break
                slack_blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": chunk[:SLACK_SECTION_MAX_CHARS]},
                    }
                )

    if tool_names and len(slack_blocks) < SLACK_MAX_BLOCKS:
        dedup_tools = list(dict.fromkeys(t for t in tool_names if t))
        if dedup_tools:
            tools_str = ", ".join(f"`{t}`" for t in dedup_tools[:8])
            slack_blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"🔧 *Tools used:* {tools_str}",
                        }
                    ],
                }
            )

    return slack_blocks


_PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "alertmanager": "Alertmanager",
    "prometheus": "Prometheus",
    "grafana": "Grafana",
    "pagerduty": "PagerDuty",
    "datadog": "Datadog",
    "cloudwatch": "AWS CloudWatch",
    "sns": "AWS SNS",
    "github": "GitHub",
    "servicenow": "ServiceNow",
    "sentry": "Sentry",
    "opsgenie": "Opsgenie",
    "generic": "Webhook",
}

_SEVERITY_BADGES: dict[str, tuple[str, str]] = {
    "critical": ("🔴", "Critical"),
    "p1": ("🔴", "Critical (P1)"),
    "sev1": ("🔴", "Critical (SEV-1)"),
    "high": ("🟠", "High"),
    "error": ("🟠", "High"),
    "p2": ("🟠", "High (P2)"),
    "sev2": ("🟠", "High (SEV-2)"),
    "warning": ("🟡", "Warning"),
    "warn": ("🟡", "Warning"),
    "medium": ("🟡", "Medium"),
    "p3": ("🟡", "Medium (P3)"),
    "info": ("🔵", "Info"),
    "low": ("🔵", "Low"),
    "ok": ("🟢", "Resolved"),
    "resolved": ("🟢", "Resolved"),
}

_LEADING_BRACKET_TAG_RE = re.compile(r"^\s*(?:\[[^\]\n]{1,32}\]\s*)+")
_TRAILING_SEVERITY_PAREN_RE = re.compile(
    r"\s*\((?:CRITICAL|HIGH|MEDIUM|LOW|WARNING|WARN|INFO|UNKNOWN|FIRING|RESOLVED|TRIGGERED)\)\s*$",
    flags=re.IGNORECASE,
)
_ALERT_COUNT_PLURAL_RE = re.compile(r"\((\d+)\s+alert\(s\)\)", flags=re.IGNORECASE)
_SIGNAL_BULLET_RE = re.compile(
    r"^\s*[-•*]\s*(?:\[(?P<state>[A-Z_]+)\]\s*)?(?P<target>[A-Za-z0-9_.:/-]{2,96}):\s*(?P<desc>.+)$"
)
_POD_IN_NS_RE = re.compile(
    r"\b(?:Pod|Deployment|StatefulSet|DaemonSet|Service|Job|Node)\s+`?([a-z0-9][-a-z0-9.]+)`?(?:\s+in\s+(?:namespace\s+)?`?([a-z0-9][-a-z0-9.]+)`?)?",
    flags=re.IGNORECASE,
)
_TECH_TOKEN_RE = re.compile(
    r"(?<!`)\b("
    r"CrashLoopBackOff|OOMKilled|ImagePullBackOff|ErrImagePull|CreateContainerConfigError|"
    r"NodeNotReady|BackOff|Evicted|Pending|ContainerCreating|"
    r"[a-z0-9]+(?:-[a-z0-9]+){2,}|"
    r"v\d+\.\d+(?:\.\d+)?|"
    r"\d+(?:Mi|Gi|Ki|Ti|m)\b"
    r")(?!`)"
)


def _highlight_technical_tokens(text: str) -> str:
    """Wrap unquoted Kubernetes/cloud identifiers, status codes, and resource units in inline backticks."""
    if not text:
        return ""
    parts = _FENCE_RE.split(text)
    out: list[str] = []
    for idx, seg in enumerate(parts):
        if idx % 2 == 1:
            out.append(seg)
        else:
            # Avoid modifying URLs
            url_parts = re.split(r"(https?://\S+|<https?://[^>]+>)", seg)
            seg_out: list[str] = []
            for u_idx, u_seg in enumerate(url_parts):
                if u_idx % 2 == 1:
                    seg_out.append(u_seg)
                else:
                    seg_out.append(_TECH_TOKEN_RE.sub(r"`\1`", u_seg))
            out.append("".join(seg_out))
    return "".join(out)


def _clean_incident_title(raw_title: str, provider: str = "") -> tuple[str, str]:
    """Return `(cleaned_title, extracted_provider)` with redundant `[PROVIDER]`/`[FIRING]`/`(CRITICAL)` tags stripped."""
    cleaned = raw_title.strip()
    extracted_prov = provider.strip()
    if not extracted_prov:
        m_prov = re.match(r"^\s*\[([A-Za-z0-9_-]+)\]", cleaned)
        if m_prov and m_prov.group(1).lower() not in {"firing", "resolved", "triggered", "ok", "warning", "critical"}:
            extracted_prov = m_prov.group(1)

    cleaned = _LEADING_BRACKET_TAG_RE.sub("", cleaned).strip()
    cleaned = _TRAILING_SEVERITY_PAREN_RE.sub("", cleaned).strip()
    cleaned = _ALERT_COUNT_PLURAL_RE.sub(
        lambda m: f"({m.group(1)} alert)" if m.group(1) == "1" else f"({m.group(1)} alerts)",
        cleaned,
    )
    return (cleaned or raw_title.strip() or "Incident Alert"), extracted_prov


def _infer_scope_from_summary(summary: str, explicit_service: str = "") -> str:
    """Return a formatted Service / Scope string from `explicit_service` or inferred from `summary`."""
    svc = explicit_service.strip()
    m_pod = _POD_IN_NS_RE.search(summary)
    if m_pod:
        resource_name = m_pod.group(1)
        ns_name = m_pod.group(2)
        if svc and ns_name and svc != ns_name:
            return f"`{svc}` (`{ns_name}`)"
        if ns_name:
            # Derive base workload name if resource_name is a replicaset pod hash
            workload = re.sub(r"-[a-f0-9]{6,10}-[a-z0-9]{5}$", "", resource_name)
            if workload and workload != resource_name:
                return f"`{workload}` (`{ns_name}`)"
            return f"`{ns_name}` · `{resource_name}`"
        if not svc:
            return f"`{resource_name}`"
    if svc:
        return f"`{svc}`"
    for line in summary.splitlines():
        m_sig = _SIGNAL_BULLET_RE.match(line)
        if m_sig:
            target = m_sig.group("target").strip("`")
            workload = re.sub(r"-[a-f0-9]{6,10}-[a-z0-9]{5}$", "", target)
            return f"`{workload or target}`"
    return ""


def extract_rca_takeaway(analysis: str, max_chars: int = 280) -> str:
    """Extract a concise 1-2 sentence executive takeaway from an RCA Markdown report."""
    if not analysis or not analysis.strip():
        return ""
    lines = [ln.strip() for ln in analysis.strip().splitlines()]
    # Look for Executive Summary heading first
    in_exec_summary = False
    collected: list[str] = []
    for ln in lines:
        if _SINGLE_LINE_HEADING_RE.match(ln):
            if any(k in ln.lower() for k in ("executive summary", "summary", "tl;dr", "root cause")):
                in_exec_summary = True
                continue
            if in_exec_summary and collected:
                break
            continue
        if ln.startswith("---") or _TABLE_LINE_RE.match(ln) or ln.startswith("```"):
            if in_exec_summary and collected:
                break
            continue
        if not ln:
            if in_exec_summary and collected:
                break
            continue
        cleaned_ln = re.sub(r"^[-•*]\s+", "", ln)
        cleaned_ln = re.sub(r"^>\s*", "", cleaned_ln)
        cleaned_ln = re.sub(
            r"^(?:\*\*TL;DR\*\*|\*TL;DR\*|TL;DR|\*\*Summary\*\*|Summary)\s*:\s*",
            "",
            cleaned_ln,
            flags=re.IGNORECASE,
        ).strip()
        if cleaned_ln:
            collected.append(cleaned_ln)
            if len(" ".join(collected)) >= max_chars:
                break

    if not collected:
        for ln in lines:
            if not ln or _SINGLE_LINE_HEADING_RE.match(ln) or ln.startswith("---") or ln.startswith("```"):
                continue
            cleaned_ln = re.sub(r"^[-•*]\s+", "", ln)
            cleaned_ln = re.sub(
                r"^(?:\*\*TL;DR\*\*|\*TL;DR\*|TL;DR|\*\*Summary\*\*|Summary)\s*:\s*",
                "",
                cleaned_ln,
                flags=re.IGNORECASE,
            ).strip()
            if cleaned_ln:
                collected.append(cleaned_ln)
                if len(" ".join(collected)) >= max_chars:
                    break

    takeaway = _highlight_technical_tokens(" ".join(collected).strip())
    if len(takeaway) > max_chars:
        takeaway = takeaway[: max_chars - 1].rstrip() + "…"
    return takeaway


def build_slack_incident_blocks(
    *,
    title: str,
    summary: str = "",
    provider: str = "",
    severity: str = "",
    service_name: str = "",
    source_url: str = "",
    lg_thread_id: str = "",
    investigation_state: Literal["investigating", "completed", "error"] = "investigating",
    rca_preview: str = "",
    mention_allowlist: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Build structured Slack Block Kit blocks for an incident thread root card.

    Uses clean, minimalist typography (single severity indicator dot, inline code
    pills, compact bullets, and zero decorative emoji clutter).
    """
    view = _normalize_incident_view(
        title=title,
        summary=summary,
        provider=provider,
        severity=severity,
        service_name=service_name,
        source_url=source_url,
        lg_thread_id=lg_thread_id,
        investigation_state=investigation_state,
        rca_preview=rca_preview,
    )

    if investigation_state == "completed":
        status_field_text = "*RCA Complete* _(in thread)_"
    elif investigation_state == "error":
        status_field_text = "*Investigation Failed*"
    else:
        status_field_text = "*Investigating...* _(in thread)_"

    source_field_val = f"*{view.prov_display}*"
    if view.extracted_source_url.startswith(("http://", "https://")):
        source_field_val += f" · <{view.extracted_source_url}|View Alert>"

    fields: list[dict[str, str]] = [
        {"type": "mrkdwn", "text": f"*Severity*\n{view.sev_icon} *{view.sev_label}*"},
        {"type": "mrkdwn", "text": f"*Source*\n{source_field_val}"},
    ]
    if view.scope_display:
        fields.append({"type": "mrkdwn", "text": f"*Service / Scope*\n{view.scope_display}"})
    fields.append({"type": "mrkdwn", "text": f"*Status*\n{status_field_text}"})

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": view.clean_title[:SLACK_HEADER_MAX_CHARS],
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": fields[:10],
        },
        {"type": "divider"},
    ]

    if view.overview_lines:
        overview_raw = "\n".join(view.overview_lines)
        overview_highlighted = _highlight_technical_tokens(overview_raw)
        overview_mrkdwn = _format_inline_slack_mrkdwn(
            overview_highlighted,
            mention_allowlist=mention_allowlist,
        )
        quoted_overview = "\n".join(f"> {line}" for line in overview_mrkdwn.splitlines())
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Alert Summary*\n{quoted_overview}"[:SLACK_SECTION_MAX_CHARS],
                },
            }
        )

    if view.signal_bullets or view.other_bullets:
        formatted_signals: list[str] = []
        for _st, tgt, desc in view.signal_bullets:
            desc_highlighted = _highlight_technical_tokens(desc)
            desc_mrkdwn = _format_inline_slack_mrkdwn(
                desc_highlighted,
                mention_allowlist=mention_allowlist,
            )
            formatted_signals.append(f"• *`{tgt}`* — {desc_mrkdwn}")

        for item in view.other_bullets:
            item_highlighted = _highlight_technical_tokens(item)
            item_mrkdwn = _format_inline_slack_mrkdwn(
                item_highlighted,
                mention_allowlist=mention_allowlist,
            )
            formatted_signals.append(f"• {item_mrkdwn}")

        signals_header = "*Active Signals*" if view.signal_bullets else "*Details*"
        signals_body = f"{signals_header}\n" + "\n".join(formatted_signals)
        for chunk in chunk_text(signals_body, max_chars=SLACK_SECTION_MAX_CHARS - 20):
            if len(blocks) >= SLACK_MAX_BLOCKS - 3:
                break
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": chunk[:SLACK_SECTION_MAX_CHARS],
                    },
                }
            )

    if view.takeaway:
        takeaway_mrkdwn = _format_inline_slack_mrkdwn(
            view.takeaway,
            mention_allowlist=mention_allowlist,
        )
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Root Cause Takeaway*\n> {takeaway_mrkdwn}"[:SLACK_SECTION_MAX_CHARS],
                },
            }
        )

    blocks.append({"type": "divider"})

    footer_parts: list[str] = ["*TalkOps SRE*"]
    if view.extracted_thread_id:
        footer_parts.append(f"Thread: `{view.extracted_thread_id}`")
    if view.extracted_source_url.startswith(("http://", "https://")):
        footer_parts.append(f"<{view.extracted_source_url}|Alert Source>")
    if investigation_state == "completed":
        footer_parts.append("_Full RCA & remediation steps in thread_")
    elif investigation_state == "error":
        footer_parts.append("_Reply in thread to retry_")
    else:
        footer_parts.append("_Investigating in thread_")

    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "  ·  ".join(footer_parts)}],
        }
    )
    return blocks


def build_slack_rca_blocks(
    analysis: str,
    *,
    provider: str = "",
    title: str = "",
    severity: str = "",
    status: str = "completed",
    source_url: str = "",
    lg_thread_id: str = "",
    tool_names: Sequence[str] = (),
    mention_allowlist: frozenset[str] | None = None,
    use_native_table: bool = True,
) -> list[dict[str, Any]]:
    """Compile an automated webhook RCA report into structured Slack Block Kit blocks."""
    body_blocks = build_slack_response_blocks(
        analysis,
        mention_allowlist=mention_allowlist,
        use_native_table=use_native_table,
        tool_names=tool_names,
    )
    is_err = status.lower() in {"error", "failed"}
    banner_title = "TalkOps Investigation Report" if is_err else "TalkOps Root Cause Analysis"

    blocks: list[dict[str, Any]] = []
    if not body_blocks or body_blocks[0].get("type") != "header":
        blocks.append(
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": banner_title[:SLACK_HEADER_MAX_CHARS],
                    "emoji": True,
                },
            }
        )
    elif body_blocks and body_blocks[0].get("type") == "header":
        first_header_text = str(body_blocks[0].get("text", {}).get("text", ""))
        if "root cause analysis" not in first_header_text.lower():
            blocks.append(
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": banner_title[:SLACK_HEADER_MAX_CHARS],
                        "emoji": True,
                    },
                }
            )
            blocks.append({"type": "divider"})

    max_before_footer = SLACK_MAX_BLOCKS - 2
    for b in body_blocks:
        if len(blocks) >= max_before_footer:
            break
        blocks.append(b)

    footer_parts: list[str] = []
    if provider:
        eff_prov = _PROVIDER_DISPLAY_NAMES.get(provider.strip().lower(), provider.strip().upper())
        footer_parts.append(f"Source: *{eff_prov}*")
    if (source_url or "").strip().startswith(("http://", "https://")):
        footer_parts.append(f"<{(source_url or '').strip()}|View Alert>")
    if (lg_thread_id or "").strip():
        footer_parts.append(f"Thread: `{(lg_thread_id or '').strip()}`")
    footer_parts.append("_Reply in this thread to continue investigating_")

    if blocks and blocks[-1].get("type") != "divider" and len(blocks) + 2 <= SLACK_MAX_BLOCKS:
        blocks.append({"type": "divider"})
    if len(blocks) < SLACK_MAX_BLOCKS:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "  ·  ".join(footer_parts),
                    }
                ],
            }
        )
    return blocks


def build_teams_response_card(
    text: str,
    *,
    tool_names: Sequence[str] = (),
) -> list[dict[str, Any]] | None:
    """Compile structured LLM Markdown into a Microsoft Teams Adaptive Card v1.4 attachment.

    Returns `None` for simple single-paragraph messages so plain Teams Markdown is used,
    or a list containing an Adaptive Card attachment when tables, headings, callouts,
    or tool footers are present.
    """
    ast_blocks = parse_markdown_blocks(text)
    if not ast_blocks:
        return None
    has_rich_structure = any(
        b.kind in {"table", "heading", "callout", "divider"} for b in ast_blocks
    ) or bool(tool_names)
    if not has_rich_structure:
        return None

    body_elements: list[dict[str, Any]] = []
    for node in ast_blocks:
        if node.kind == "heading":
            size = "Large" if node.level == 1 else ("Medium" if node.level == 2 else "Default")
            body_elements.append(
                {
                    "type": "TextBlock",
                    "text": _clean_table_cell(node.text),
                    "size": size,
                    "weight": "Bolder",
                    "wrap": True,
                }
            )
        elif node.kind == "divider":
            body_elements.append(
                {
                    "type": "Container",
                    "separator": True,
                    "items": [],
                }
            )
        elif node.kind == "table" and node.table is not None:
            if len(node.table.headers) == 2 and len(node.table.rows) <= 15:
                # 2-column table: render each row as FactSet or ColumnSet
                header_cols = [
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": h,
                                "weight": "Bolder",
                                "wrap": True,
                            }
                        ],
                    }
                    for h in node.table.headers
                ]
                body_elements.append({"type": "ColumnSet", "columns": header_cols})
                for r in node.table.rows:
                    row_cols = [
                        {
                            "type": "Column",
                            "width": "stretch",
                            "items": [
                                {
                                    "type": "TextBlock",
                                    "text": r[c] if c < len(r) else "—",
                                    "wrap": True,
                                }
                            ],
                        }
                        for c in range(len(node.table.headers))
                    ]
                    body_elements.append(
                        {"type": "ColumnSet", "separator": True, "columns": row_cols}
                    )
            else:
                header_cols = [
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": h,
                                "weight": "Bolder",
                                "wrap": True,
                            }
                        ],
                    }
                    for h in node.table.headers
                ]
                body_elements.append({"type": "ColumnSet", "columns": header_cols})
                for r in node.table.rows[:25]:
                    row_cols = [
                        {
                            "type": "Column",
                            "width": "stretch",
                            "items": [
                                {
                                    "type": "TextBlock",
                                    "text": r[c] if c < len(r) else "—",
                                    "wrap": True,
                                }
                            ],
                        }
                        for c in range(len(node.table.headers))
                    ]
                    body_elements.append(
                        {"type": "ColumnSet", "separator": True, "columns": row_cols}
                    )
        elif node.kind == "code":
            body_elements.append(
                {
                    "type": "TextBlock",
                    "text": node.text,
                    "fontType": "Monospace",
                    "wrap": True,
                }
            )
        elif node.kind == "callout":
            icon, label = _CALLOUT_ICONS.get(node.callout_type, ("ℹ️", "Note"))
            prefix = f"**{icon} {label}:** " if node.callout_type != "quote" else "> "
            body_elements.append(
                {
                    "type": "TextBlock",
                    "text": f"{prefix}{node.text}",
                    "wrap": True,
                    "isSubtle": True,
                }
            )
        elif node.kind == "paragraph":
            body_elements.append(
                {
                    "type": "TextBlock",
                    "text": node.text,
                    "wrap": True,
                }
            )

    if tool_names:
        dedup_tools = list(dict.fromkeys(t for t in tool_names if t))
        if dedup_tools:
            body_elements.append(
                {
                    "type": "TextBlock",
                    "text": "🔧 **Tools used:** " + ", ".join(f"`{t}`" for t in dedup_tools[:8]),
                    "size": "Small",
                    "isSubtle": True,
                    "separator": True,
                    "wrap": True,
                }
            )

    return [
        {
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": body_elements,
            },
        }
    ]


def format_markdown_for_teams(text: str) -> str:
    """Normalize Markdown for Microsoft Teams messages and Adaptive Card TextBlocks."""
    if not text:
        return ""
    return text.replace("\r\n", "\n").strip()


def format_markdown_for_discord(
    text: str,
    *,
    mention_allowlist: frozenset[str] | None = None,
) -> str:
    """Normalize Markdown for Discord messages, converting pipe tables and neutralizing `@everyone`/`@here`."""
    if not text:
        return ""
    cleaned = text.replace("\r\n", "\n").strip()
    block_parts = _BLOCK_FENCE_RE.split(cleaned)
    table_converted: list[str] = []
    for b_idx, b_seg in enumerate(block_parts):
        if b_idx % 2 == 1:
            table_converted.append(b_seg)
        else:
            table_converted.append(_convert_markdown_tables_to_code(b_seg))
    with_tables = "".join(table_converted)

    parts = _FENCE_RE.split(with_tables)
    out: list[str] = []
    for idx, seg in enumerate(parts):
        if idx % 2 == 1:
            out.append(seg)
        else:
            s = _DISCORD_BROADCAST_RE.sub(lambda m: f"@\u200b{m.group(1)}", seg)
            if mention_allowlist is not None:
                s = _DISCORD_USER_MENTION_RE.sub(
                    lambda m: f"<@{m.group(1)}>" if m.group(1) in mention_allowlist else f"`@{m.group(1)}`",
                    s,
                )
            out.append(s)
    return "".join(out)


def format_for_channel(channel: str, text: str) -> str:
    """Format outbound assistant Markdown for the target `channel` provider."""
    norm = channel.strip().lower()
    if norm == "slack":
        return format_markdown_for_slack(text)
    if norm == "teams":
        return format_markdown_for_teams(text)
    if norm == "discord":
        return format_markdown_for_discord(text)
    return text


def parse_channel_output(
    channel: str,
    text: str,
    *,
    mention_allowlist: frozenset[str] | None = None,
    tool_names: Sequence[str] = (),
    use_native_table: bool = True,
) -> ParsedChannelOutput:
    """Generic output parser entry point compiling LLM Markdown into channel text + native blocks/cards."""
    norm = channel.strip().lower()
    ast = tuple(parse_markdown_blocks(text))
    if norm == "slack":
        formatted_text = format_markdown_for_slack(text, mention_allowlist=mention_allowlist)
        blocks = build_slack_response_blocks(
            text,
            mention_allowlist=mention_allowlist,
            use_native_table=use_native_table,
            tool_names=tool_names,
        )
        return ParsedChannelOutput(
            channel="slack",
            text=formatted_text,
            blocks_or_cards=blocks or None,
            ast=ast,
        )
    if norm == "teams":
        formatted_text = format_markdown_for_teams(text)
        cards = build_teams_response_card(text, tool_names=tool_names)
        return ParsedChannelOutput(
            channel="teams",
            text=formatted_text,
            blocks_or_cards=cards,
            ast=ast,
        )
    if norm == "discord":
        formatted_text = format_markdown_for_discord(text, mention_allowlist=mention_allowlist)
        if tool_names:
            dedup_tools = list(dict.fromkeys(t for t in tool_names if t))
            if dedup_tools:
                formatted_text += "\n\n_🔧 Tools used: " + ", ".join(f"`{t}`" for t in dedup_tools[:8]) + "_"
        return ParsedChannelOutput(
            channel="discord",
            text=formatted_text,
            blocks_or_cards=None,
            ast=ast,
        )
    return ParsedChannelOutput(channel=norm, text=text, blocks_or_cards=None, ast=ast)


@dataclass(frozen=True, slots=True)
class _NormalizedIncidentView:
    """Channel-neutral representation of a normalized incident card."""

    clean_title: str
    prov_display: str
    sev_raw: str
    sev_icon: str
    sev_label: str
    header_icon: str
    status_label_plain: str
    scope_display: str
    overview_lines: tuple[str, ...]
    signal_bullets: tuple[tuple[str, str, str], ...]
    other_bullets: tuple[str, ...]
    extracted_source_url: str
    extracted_thread_id: str
    takeaway: str
    investigation_state: Literal["investigating", "completed", "error"]


def _normalize_incident_view(
    *,
    title: str,
    summary: str | None = "",
    provider: str | None = "",
    severity: str | None = "",
    service_name: str | None = "",
    source_url: str | None = "",
    lg_thread_id: str | None = "",
    investigation_state: Literal["investigating", "completed", "error"] = "investigating",
    rca_preview: str | None = "",
) -> _NormalizedIncidentView:
    summary = summary or ""
    provider = provider or ""
    severity = severity or ""
    service_name = service_name or ""
    source_url = source_url or ""
    lg_thread_id = lg_thread_id or ""
    rca_preview = rca_preview or ""
    clean_title, inferred_prov = _clean_incident_title(title, provider)
    eff_prov_raw = (provider or inferred_prov or "webhook").strip().lower()
    prov_display = _PROVIDER_DISPLAY_NAMES.get(
        eff_prov_raw,
        eff_prov_raw.replace("_", " ").replace("-", " ").upper()
        if len(eff_prov_raw) <= 4
        else eff_prov_raw.replace("_", " ").replace("-", " ").title(),
    )

    sev_raw = severity.strip().lower()
    if not sev_raw:
        m_sev = _TRAILING_SEVERITY_PAREN_RE.search(title)
        if m_sev:
            sev_raw = m_sev.group(0).strip(" ()").lower()
    sev_icon, sev_label = _SEVERITY_BADGES.get(
        sev_raw,
        ("⚪", sev_raw.capitalize() if sev_raw else "Unknown"),
    )

    if investigation_state == "completed":
        header_icon = ""
        status_label_plain = "RCA Complete (in thread)"
    elif investigation_state == "error":
        header_icon = ""
        status_label_plain = "Investigation Failed"
    else:
        header_icon = ""
        status_label_plain = "Investigating... (in thread)"

    overview_lines: list[str] = []
    signal_bullets: list[tuple[str, str, str]] = []
    other_bullets: list[str] = []
    extracted_source_url = source_url.strip()
    extracted_thread_id = lg_thread_id.strip()

    for raw_line in summary.splitlines():
        ln = raw_line.strip()
        if not ln:
            continue
        if "TalkOps Autonomous Investigation Started" in ln:
            continue
        if ln.lower().startswith("source:"):
            candidate_url = ln.split(":", 1)[1].strip()
            if not extracted_source_url and candidate_url.startswith(("http://", "https://")):
                extracted_source_url = candidate_url
            continue
        if ln.lower().startswith("thread id:"):
            candidate_tid = ln.split(":", 1)[1].strip().strip("`")
            if not extracted_thread_id and candidate_tid:
                extracted_thread_id = candidate_tid
            continue

        m_sig = _SIGNAL_BULLET_RE.match(ln)
        if m_sig:
            st = (m_sig.group("state") or "FIRING").upper()
            tgt = m_sig.group("target").strip("`")
            desc = m_sig.group("desc").strip()
            signal_bullets.append((st, tgt, desc))
            continue

        if ln.startswith(("- ", "* ", "• ")):
            other_bullets.append(re.sub(r"^[-*•]\s+", "", ln))
            continue

        overview_lines.append(ln)

    scope_display = _infer_scope_from_summary(summary, service_name)
    takeaway = extract_rca_takeaway(rca_preview) if rca_preview.strip() else ""

    return _NormalizedIncidentView(
        clean_title=clean_title,
        prov_display=prov_display,
        sev_raw=sev_raw,
        sev_icon=sev_icon,
        sev_label=sev_label,
        header_icon=header_icon,
        status_label_plain=status_label_plain,
        scope_display=scope_display,
        overview_lines=tuple(overview_lines),
        signal_bullets=tuple(signal_bullets),
        other_bullets=tuple(other_bullets),
        extracted_source_url=extracted_source_url,
        extracted_thread_id=extracted_thread_id,
        takeaway=takeaway,
        investigation_state=investigation_state,
    )


def build_teams_incident_card(
    *,
    title: str,
    summary: str = "",
    provider: str = "",
    severity: str = "",
    service_name: str = "",
    source_url: str = "",
    lg_thread_id: str = "",
    investigation_state: Literal["investigating", "completed", "error"] = "investigating",
    rca_preview: str = "",
) -> list[dict[str, Any]]:
    """Build a Microsoft Teams Adaptive Card v1.4 attachment for an incident root message."""
    view = _normalize_incident_view(
        title=title,
        summary=summary,
        provider=provider,
        severity=severity,
        service_name=service_name,
        source_url=source_url,
        lg_thread_id=lg_thread_id,
        investigation_state=investigation_state,
        rca_preview=rca_preview,
    )
    facts: list[dict[str, str]] = [
        {"title": "Severity", "value": f"{view.sev_icon} {view.sev_label}"},
        {"title": "Source", "value": view.prov_display},
    ]
    if view.scope_display:
        facts.append({"title": "Service / Scope", "value": view.scope_display})
    facts.append({"title": "Status", "value": view.status_label_plain})

    body_elements: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": view.clean_title,
            "size": "Large",
            "weight": "Bolder",
            "wrap": True,
        },
        {"type": "FactSet", "facts": facts},
    ]

    if view.overview_lines:
        overview_text = _highlight_technical_tokens("\n".join(view.overview_lines))
        body_elements.append(
            {
                "type": "Container",
                "separator": True,
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "**Alert Summary**",
                        "weight": "Bolder",
                        "wrap": True,
                    },
                    {
                        "type": "TextBlock",
                        "text": overview_text,
                        "wrap": True,
                        "isSubtle": True,
                    },
                ],
            }
        )

    if view.signal_bullets or view.other_bullets:
        sig_items: list[dict[str, Any]] = [
            {
                "type": "TextBlock",
                "text": "**Active Signals**" if view.signal_bullets else "**Details**",
                "weight": "Bolder",
                "wrap": True,
            }
        ]
        for _st, tgt, desc in view.signal_bullets:
            sig_items.append(
                {
                    "type": "TextBlock",
                    "text": f"- **`{tgt}`** — {_highlight_technical_tokens(desc)}",
                    "wrap": True,
                }
            )
        for item in view.other_bullets:
            sig_items.append(
                {
                    "type": "TextBlock",
                    "text": f"- {_highlight_technical_tokens(item)}",
                    "wrap": True,
                }
            )
        body_elements.append({"type": "Container", "separator": True, "items": sig_items})

    if view.takeaway:
        body_elements.append(
            {
                "type": "Container",
                "separator": True,
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "**Root Cause Takeaway**",
                        "weight": "Bolder",
                        "wrap": True,
                    },
                    {
                        "type": "TextBlock",
                        "text": f"> {view.takeaway}",
                        "wrap": True,
                    },
                ],
            }
        )

    footer_parts = ["TalkOps SRE"]
    if view.extracted_thread_id:
        footer_parts.append(f"Thread: `{view.extracted_thread_id}`")
    body_elements.append(
        {
            "type": "TextBlock",
            "text": " · ".join(footer_parts),
            "size": "Small",
            "isSubtle": True,
            "separator": True,
            "wrap": True,
        }
    )

    card_content: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body_elements,
    }
    if view.extracted_source_url.startswith(("http://", "https://")):
        card_content["actions"] = [
            {
                "type": "Action.OpenUrl",
                "title": "View Alert",
                "url": view.extracted_source_url,
            }
        ]
    return [
        {
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": card_content,
        }
    ]


def build_teams_rca_card(
    analysis: str,
    *,
    provider: str = "",
    title: str = "",
    severity: str = "",
    status: str = "completed",
    source_url: str = "",
    lg_thread_id: str = "",
    tool_names: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Build a Microsoft Teams Adaptive Card v1.4 attachment for an automated RCA report."""
    is_err = status.lower() in {"error", "failed"}
    banner_title = "TalkOps Investigation Report" if is_err else "TalkOps Root Cause Analysis"

    base_cards = build_teams_response_card(analysis, tool_names=tool_names)
    if base_cards and isinstance(base_cards[0].get("content"), Mapping):
        existing_body = list(base_cards[0]["content"].get("body", []))
    else:
        existing_body = [
            {
                "type": "TextBlock",
                "text": format_markdown_for_teams(analysis),
                "wrap": True,
            }
        ]

    body_elements: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": banner_title,
            "size": "Large",
            "weight": "Bolder",
            "wrap": True,
        },
        *existing_body,
    ]
    footer_parts: list[str] = []
    if provider:
        eff_prov = _PROVIDER_DISPLAY_NAMES.get(provider.strip().lower(), provider.strip().upper())
        footer_parts.append(f"Source: {eff_prov}")
    if (lg_thread_id or "").strip():
        footer_parts.append(f"Thread: `{(lg_thread_id or '').strip()}`")
    footer_parts.append("Reply in this thread to continue investigating")
    body_elements.append(
        {
            "type": "TextBlock",
            "text": "  ·  ".join(footer_parts),
            "size": "Small",
            "isSubtle": True,
            "separator": True,
            "wrap": True,
        }
    )

    card_content: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body_elements,
    }
    if (source_url or "").strip().startswith(("http://", "https://")):
        card_content["actions"] = [
            {
                "type": "Action.OpenUrl",
                "title": "View Alert",
                "url": (source_url or "").strip(),
            }
        ]
    return [
        {
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": card_content,
        }
    ]


def build_discord_incident_embed(
    *,
    title: str,
    summary: str = "",
    provider: str = "",
    severity: str = "",
    service_name: str = "",
    source_url: str = "",
    lg_thread_id: str = "",
    investigation_state: Literal["investigating", "completed", "error"] = "investigating",
    rca_preview: str = "",
    mention_allowlist: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Build a Discord Rich Embed + Link Button component payload for an incident root message."""
    view = _normalize_incident_view(
        title=title,
        summary=summary,
        provider=provider,
        severity=severity,
        service_name=service_name,
        source_url=source_url,
        lg_thread_id=lg_thread_id,
        investigation_state=investigation_state,
        rca_preview=rca_preview,
    )
    if investigation_state == "completed":
        color = 0x2EB67D
    elif investigation_state == "error":
        color = 0xECB22E
    elif view.sev_raw in {"critical", "high", "p1", "sev1", "p2", "sev2"}:
        color = 0xE01E5A
    elif view.sev_raw in {"warning", "warn", "medium", "p3", "sev3"}:
        color = 0xECB22E
    else:
        color = 0x36C5F0

    desc_sections: list[str] = []
    if view.overview_lines:
        overview_highlighted = _highlight_technical_tokens("\n".join(view.overview_lines))
        overview_fmt = format_markdown_for_discord(
            overview_highlighted, mention_allowlist=mention_allowlist
        )
        quoted = "\n".join(f"> {ln}" for ln in overview_fmt.splitlines())
        desc_sections.append(f"**Alert Summary**\n{quoted}")

    if view.signal_bullets or view.other_bullets:
        sig_lines: list[str] = []
        for _st, tgt, desc in view.signal_bullets:
            desc_fmt = format_markdown_for_discord(
                _highlight_technical_tokens(desc), mention_allowlist=mention_allowlist
            )
            sig_lines.append(f"• **`{tgt}`** — {desc_fmt}")
        for item in view.other_bullets:
            item_fmt = format_markdown_for_discord(
                _highlight_technical_tokens(item), mention_allowlist=mention_allowlist
            )
            sig_lines.append(f"• {item_fmt}")
        sig_header = "**Active Signals**" if view.signal_bullets else "**Details**"
        desc_sections.append(f"{sig_header}\n" + "\n".join(sig_lines))

    if view.takeaway:
        desc_sections.append(f"**Root Cause Takeaway**\n> {view.takeaway}")

    fields: list[dict[str, Any]] = [
        {"name": "Severity", "value": f"{view.sev_icon} **{view.sev_label}**", "inline": True},
        {"name": "Source", "value": f"**{view.prov_display}**", "inline": True},
    ]
    if view.scope_display:
        fields.append({"name": "Service / Scope", "value": view.scope_display, "inline": True})
    fields.append({"name": "Status", "value": view.status_label_plain, "inline": True})

    embed: dict[str, Any] = {
        "title": view.clean_title[:256],
        "description": "\n\n".join(desc_sections)[:4000],
        "color": color,
        "fields": fields,
        "footer": {
            "text": (
                f"TalkOps SRE · Thread: {view.extracted_thread_id}"
                if view.extracted_thread_id
                else "TalkOps SRE"
            )[:2048]
        },
    }
    if view.extracted_source_url.startswith(("http://", "https://")):
        embed["url"] = view.extracted_source_url

    result: dict[str, Any] = {"embeds": [embed]}
    if view.extracted_source_url.startswith(("http://", "https://")):
        result["components"] = [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": 5,
                        "label": "View Alert",
                        "url": view.extracted_source_url,
                    }
                ],
            }
        ]
    return result


def build_discord_rca_embed(
    analysis: str,
    *,
    provider: str = "",
    title: str = "",
    severity: str = "",
    status: str = "completed",
    source_url: str = "",
    lg_thread_id: str = "",
    tool_names: Sequence[str] = (),
) -> dict[str, Any]:
    """Build a Discord summary embed + Link Button metadata footer for an automated RCA report."""
    is_err = status.lower() in {"error", "failed"}
    banner_title = "TalkOps Investigation Report" if is_err else "TalkOps Root Cause Analysis"
    takeaway = extract_rca_takeaway(analysis)

    footer_parts: list[str] = []
    if provider:
        eff_prov = _PROVIDER_DISPLAY_NAMES.get(provider.strip().lower(), provider.strip().upper())
        footer_parts.append(f"Source: {eff_prov}")
    if tool_names:
        dedup_tools = list(dict.fromkeys(t for t in tool_names if t))
        if dedup_tools:
            footer_parts.append("Tools: " + ", ".join(dedup_tools[:6]))
    if (lg_thread_id or "").strip():
        footer_parts.append(f"Thread: {(lg_thread_id or '').strip()}")

    embed: dict[str, Any] = {
        "title": banner_title[:256],
        "description": (f"**Takeaway:** {takeaway}" if takeaway else "Investigation complete.")[:2000],
        "color": 0xECB22E if is_err else 0x2EB67D,
        "footer": {
            "text": (" · ".join(footer_parts) or "TalkOps SRE")[:2048],
        },
    }
    if (source_url or "").strip().startswith(("http://", "https://")):
        embed["url"] = (source_url or "").strip()

    result: dict[str, Any] = {"embeds": [embed]}
    if (source_url or "").strip().startswith(("http://", "https://")):
        result["components"] = [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": 5,
                        "label": "View Alert",
                        "url": (source_url or "").strip(),
                    }
                ],
            }
        ]
    return result


def parse_incident_output(
    channel: str,
    *,
    title: str,
    summary: str | None = "",
    provider: str | None = "",
    severity: str | None = "",
    service_name: str | None = "",
    source_url: str | None = "",
    lg_thread_id: str | None = "",
    investigation_state: Literal["investigating", "completed", "error"] = "investigating",
    rca_preview: str | None = "",
    mention_allowlist: frozenset[str] | None = None,
) -> ParsedChannelOutput:
    """Channel-agnostic incident compiler producing both formatted text and native blocks/cards/embeds."""
    summary = summary or ""
    provider = provider or ""
    severity = severity or ""
    service_name = service_name or ""
    source_url = source_url or ""
    lg_thread_id = lg_thread_id or ""
    rca_preview = rca_preview or ""
    norm = channel.strip().lower()
    view = _normalize_incident_view(
        title=title,
        summary=summary,
        provider=provider,
        severity=severity,
        service_name=service_name,
        source_url=source_url,
        lg_thread_id=lg_thread_id,
        investigation_state=investigation_state,
        rca_preview=rca_preview,
    )

    # Construct structured universal Markdown text (used as fallback or primary body)
    md_lines: list[str] = [
        f"**[{view.prov_display.upper()}] {view.clean_title}** (`{view.sev_label.upper()}`)",
        f"**Severity:** {view.sev_icon} {view.sev_label}  ·  **Source:** {view.prov_display}"
        + (f"  ·  **Scope:** {view.scope_display}" if view.scope_display else "")
        + f"  ·  **Status:** {view.status_label_plain}",
    ]
    if view.overview_lines:
        overview_highlighted = _highlight_technical_tokens("\n".join(view.overview_lines))
        quoted = "\n".join(f"> {ln}" for ln in overview_highlighted.splitlines())
        md_lines.append(f"\n**Alert Summary**\n{quoted}")
    if view.signal_bullets or view.other_bullets:
        sig_lines: list[str] = ["\n**Active Signals**" if view.signal_bullets else "\n**Details**"]
        for _st, tgt, desc in view.signal_bullets:
            sig_lines.append(f"- **`{tgt}`** — {_highlight_technical_tokens(desc)}")
        for item in view.other_bullets:
            sig_lines.append(f"- {_highlight_technical_tokens(item)}")
        md_lines.append("\n".join(sig_lines))
    if view.takeaway:
        md_lines.append(f"\n**Root Cause Takeaway**\n> {view.takeaway}")
    if view.extracted_source_url:
        md_lines.append(f"\nSource: {view.extracted_source_url}")
    if view.extracted_thread_id:
        md_lines.append(f"Thread ID: `{view.extracted_thread_id}`")

    universal_md = "\n".join(md_lines).strip()

    if norm == "slack":
        blocks = build_slack_incident_blocks(
            title=title,
            summary=summary,
            provider=provider,
            severity=severity,
            service_name=service_name,
            source_url=source_url,
            lg_thread_id=lg_thread_id,
            investigation_state=investigation_state,
            rca_preview=rca_preview,
            mention_allowlist=mention_allowlist,
        )
        return ParsedChannelOutput(
            channel="slack",
            text=format_markdown_for_slack(universal_md, mention_allowlist=mention_allowlist),
            blocks_or_cards=blocks,
        )
    if norm == "teams":
        cards = build_teams_incident_card(
            title=title,
            summary=summary,
            provider=provider,
            severity=severity,
            service_name=service_name,
            source_url=source_url,
            lg_thread_id=lg_thread_id,
            investigation_state=investigation_state,
            rca_preview=rca_preview,
        )
        return ParsedChannelOutput(
            channel="teams",
            text=format_markdown_for_teams(universal_md),
            blocks_or_cards=cards,
        )
    if norm == "discord":
        embed_payload = build_discord_incident_embed(
            title=title,
            summary=summary,
            provider=provider,
            severity=severity,
            service_name=service_name,
            source_url=source_url,
            lg_thread_id=lg_thread_id,
            investigation_state=investigation_state,
            rca_preview=rca_preview,
            mention_allowlist=mention_allowlist,
        )
        return ParsedChannelOutput(
            channel="discord",
            text=format_markdown_for_discord(universal_md, mention_allowlist=mention_allowlist),
            blocks_or_cards=embed_payload,
        )
    return ParsedChannelOutput(
        channel=norm,
        text=format_for_channel(norm, universal_md),
        blocks_or_cards=None,
    )


def parse_rca_output(
    channel: str,
    analysis: str,
    *,
    provider: str | None = "",
    title: str | None = "",
    severity: str | None = "",
    status: str = "completed",
    source_url: str | None = "",
    lg_thread_id: str | None = "",
    tool_names: Sequence[str] = (),
    mention_allowlist: frozenset[str] | None = None,
    use_native_table: bool = True,
) -> ParsedChannelOutput:
    """Channel-agnostic RCA report compiler producing formatted text and native blocks/cards/embeds."""
    provider = provider or ""
    title = title or ""
    severity = severity or ""
    source_url = source_url or ""
    lg_thread_id = lg_thread_id or ""
    norm = channel.strip().lower()
    is_err = status.lower() in {"error", "failed"}
    banner_title = "TalkOps Investigation Report" if is_err else "TalkOps Automated RCA"

    md_lines = [f"**{banner_title}**\n\n{analysis.strip()}"]
    if source_url.strip():
        md_lines.append(f"\nSource: {source_url.strip()}")
    if lg_thread_id.strip():
        md_lines.append(f"_Reply in this thread to continue investigating (`{lg_thread_id.strip()}`)._")
    universal_md = "\n".join(md_lines).strip()
    ast = tuple(parse_markdown_blocks(analysis))

    if norm == "slack":
        blocks = build_slack_rca_blocks(
            analysis,
            provider=provider,
            title=title,
            severity=severity,
            status=status,
            source_url=source_url,
            lg_thread_id=lg_thread_id,
            tool_names=tool_names,
            mention_allowlist=mention_allowlist,
            use_native_table=use_native_table,
        )
        return ParsedChannelOutput(
            channel="slack",
            text=format_markdown_for_slack(universal_md, mention_allowlist=mention_allowlist),
            blocks_or_cards=blocks,
            ast=ast,
        )
    if norm == "teams":
        cards = build_teams_rca_card(
            analysis,
            provider=provider,
            title=title,
            severity=severity,
            status=status,
            source_url=source_url,
            lg_thread_id=lg_thread_id,
            tool_names=tool_names,
        )
        return ParsedChannelOutput(
            channel="teams",
            text=format_markdown_for_teams(universal_md),
            blocks_or_cards=cards,
            ast=ast,
        )
    if norm == "discord":
        embed_payload = build_discord_rca_embed(
            analysis,
            provider=provider,
            title=title,
            severity=severity,
            status=status,
            source_url=source_url,
            lg_thread_id=lg_thread_id,
            tool_names=tool_names,
        )
        return ParsedChannelOutput(
            channel="discord",
            text=format_markdown_for_discord(universal_md, mention_allowlist=mention_allowlist),
            blocks_or_cards=embed_payload,
            ast=ast,
        )
    return ParsedChannelOutput(
        channel=norm,
        text=format_for_channel(norm, universal_md),
        blocks_or_cards=None,
        ast=ast,
    )


def exposure_from_mapping(
    provider: str,
    cfg: Mapping[str, object],
    env: Mapping[str, str],
) -> ChannelExposure:
    """Construct a `ChannelExposure` from addon config dictionary and environment variables."""
    prefix = f"TALKOPS_CHANNELS_{provider.upper()}"
    raw_mode = str(
        cfg.get("exposure_mode")
        or env.get(f"{prefix}_EXPOSURE_MODE")
        or env.get("TALKOPS_CHANNELS_EXPOSURE_MODE")
        or "allowlist"
    ).strip().lower()
    try:
        mode = ExposureMode(raw_mode)
    except ValueError as exc:
        raise ValueError(
            f"Invalid exposure_mode '{raw_mode}' for {provider}; expected 'self', 'allowlist', or 'open'."
        ) from exc

    operator_id = (
        str(cfg.get("operator_id") or env.get(f"{prefix}_OPERATOR_ID") or "").strip() or None
    )
    allowed_users = frozenset(
        split_csv(cfg.get("allowlist_users") or env.get(f"{prefix}_ALLOWLIST_USERS"))  # type: ignore[arg-type]
    )
    allowed_chats = frozenset(
        split_csv(cfg.get("allowlist_chats") or env.get(f"{prefix}_ALLOWLIST_CHATS"))  # type: ignore[arg-type]
    )
    raw_ack = cfg.get("open_ack")
    if raw_ack is None:
        raw_ack = env.get(f"{prefix}_OPEN_ACK", "0")
    open_ack = str(raw_ack).strip().lower() in {"1", "true", "yes", "on"}

    raw_mention = cfg.get("require_mention_in_channels")
    if raw_mention is None:
        raw_mention = env.get(f"{prefix}_REQUIRE_MENTION", "1")
    require_mention = str(raw_mention).strip().lower() not in {"0", "false", "no", "off"}

    exposure = ChannelExposure(
        mode=mode,
        operator_id=operator_id,
        allowed_users=allowed_users,
        allowed_chats=allowed_chats,
        open_ack=open_ack,
        require_mention_in_channels=require_mention,
    )
    exposure.validate(provider)
    return exposure

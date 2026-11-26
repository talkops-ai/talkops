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

"""Two-Phase Tool-Payload Shaping & Native LangChain Context Editing for TalkOps v2.

Prevents 50k-token Prometheus/CloudWatch/kubectl/log tool outputs from flooding the
LLM context window:
- Phase 1: ``ToolPayloadShaperMiddleware`` intercepts oversized ``ToolMessage``
  results (> ``char_threshold``, default 12,000 chars / ~3,000 tokens), preserves
  the full raw payload in ``ToolMessage.artifact`` + an indexed bounded cache,
  and replaces ``ToolMessage.content`` with a <300-token Structural Shape Digest.
- Phase 2: Exposes the ``artifact_query`` LangChain tool so the agent can surgically
  filter rows/lines by substring, regex, or JSON field on demand.
- Companion: ``build_sre_context_editing_middleware()`` wraps native LangChain
  ``ContextEditingMiddleware`` + ``ClearToolUsesEdit``.
"""

from __future__ import annotations

import collections
import json
import math
import os
import re
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Annotated, Any

from langchain.agents.middleware import ClearToolUsesEdit, ContextEditingMiddleware
from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import InjectedState
from pydantic import Field

from talkops.middleware.registry import register_middleware

DEFAULT_SHAPER_CHAR_THRESHOLD = 12_000
_MAX_CACHED_ARTIFACTS = 256

_ISO_TS_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)

_STATUS_KEYWORDS: tuple[str, ...] = (
    "ERROR",
    "FATAL",
    "CRITICAL",
    "WARN",
    "WARNING",
    "OOMKilled",
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "Timeout",
    "ConnectionRefused",
    "500",
    "502",
    "503",
    "504",
)

_ERROR_LINE_RE = re.compile(
    r"(?i)\b(?:error|exception|fatal|panic|oomkilled|crashloopbackoff|timeout|failed|refused|deadlock)\b"
)


class ArtifactShapeStore:
    """Thread-safe bounded LRU store mapping ``tool_call_id`` -> raw uncompacted string."""

    def __init__(self, max_entries: int = _MAX_CACHED_ARTIFACTS) -> None:
        """Initialize ArtifactShapeStore."""
        self._max_entries = max(16, max_entries)
        self._items: collections.OrderedDict[str, tuple[str, str]] = collections.OrderedDict()
        self._lock = threading.Lock()

    def put(self, tool_call_id: str, tool_name: str, raw_payload: str) -> None:
        """Execute `put`."""
        if not tool_call_id:
            return
        with self._lock:
            self._items.pop(tool_call_id, None)
            self._items[tool_call_id] = (tool_name, raw_payload)
            while len(self._items) > self._max_entries:
                self._items.popitem(last=False)

    def get(self, tool_call_id: str) -> tuple[str, str] | None:
        """Execute `get`."""
        with self._lock:
            entry = self._items.get(tool_call_id)
            if entry is not None:
                self._items.move_to_end(tool_call_id)
            return entry

    def clear(self) -> None:
        """Execute `clear`."""
        with self._lock:
            self._items.clear()


_GLOBAL_ARTIFACT_STORE = ArtifactShapeStore()


def get_artifact_shape_store() -> ArtifactShapeStore:
    """Return the singleton ``ArtifactShapeStore``."""
    return _GLOBAL_ARTIFACT_STORE


def _percentile(sorted_vals: Sequence[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, math.ceil((pct / 100.0) * len(sorted_vals)) - 1))
    return round(sorted_vals[idx], 3)


def compute_payload_shape_digest(
    raw_text: str,
    *,
    tool_name: str,
    tool_call_id: str,
) -> dict[str, Any]:
    """Compute a <300-token structural shape summary of a large JSON, JSONL, or log payload."""
    approx_tokens = max(1, len(raw_text) // 4)
    timestamps = _ISO_TS_RE.findall(raw_text[:200_000])
    time_range = (
        {"min": min(timestamps), "max": max(timestamps)}
        if timestamps
        else None
    )

    status_counts: dict[str, int] = {}
    sample_window = raw_text[:250_000]
    for kw in _STATUS_KEYWORDS:
        cnt = len(re.findall(rf"\b{re.escape(kw)}\b", sample_window))
        if cnt > 0:
            status_counts[kw] = cnt

    fmt = "log_lines"
    row_count = 0
    keys: list[str] = []
    numeric_stats: dict[str, dict[str, float]] = {}
    records: list[Mapping[str, Any]] = []

    stripped = raw_text.strip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                fmt = "json_array"
                row_count = len(parsed)
                records = [r for r in parsed[:500] if isinstance(r, Mapping)]
            elif isinstance(parsed, Mapping):
                fmt = "json_object"
                keys = [str(k) for k in list(parsed.keys())[:20]]
                # Detect nested list containers (`items`, `results`, `data`, `events`, `rows`)
                for list_key in ("items", "results", "data", "events", "rows", "logs", "series", "alerts"):
                    candidate = parsed.get(list_key)
                    if isinstance(candidate, list):
                        row_count = len(candidate)
                        records = [r for r in candidate[:500] if isinstance(r, Mapping)]
                        break
                if row_count == 0:
                    row_count = len(parsed)
        except Exception:
            pass

    if fmt == "log_lines":
        lines = raw_text.splitlines()
        row_count = len(lines)
        # Check if JSONL
        if lines and lines[0].lstrip().startswith("{"):
            jsonl_recs: list[Mapping[str, Any]] = []
            for ln in lines[:200]:
                try:
                    obj = json.loads(ln)
                    if isinstance(obj, Mapping):
                        jsonl_recs.append(obj)
                except Exception:
                    break
            if len(jsonl_recs) >= min(3, len(lines)):
                fmt = "jsonl"
                records = jsonl_recs

    if records:
        key_set: list[str] = []
        seen_k: set[str] = set()
        numeric_buckets: dict[str, list[float]] = {}
        for rec in records:
            for k, v in rec.items():
                ks = str(k)
                if ks not in seen_k and len(key_set) < 20:
                    seen_k.add(ks)
                    key_set.append(ks)
                if isinstance(v, (int, float)) and not isinstance(v, bool) and len(numeric_buckets) <= 8:
                    numeric_buckets.setdefault(ks, []).append(float(v))
        if not keys:
            keys = key_set
        for nk, vals in list(numeric_buckets.items())[:5]:
            if len(vals) >= 2:
                s_vals = sorted(vals)
                numeric_stats[nk] = {
                    "p50": _percentile(s_vals, 50.0),
                    "p99": _percentile(s_vals, 99.0),
                }

    top_errors: list[str] = []
    seen_err: set[str] = set()
    for line in raw_text.splitlines()[:2000]:
        s_line = line.strip()
        if not s_line or not _ERROR_LINE_RE.search(s_line):
            continue
        clipped = s_line[:140]
        if clipped not in seen_err:
            seen_err.add(clipped)
            top_errors.append(clipped)
            if len(top_errors) >= 4:
                break

    digest: dict[str, Any] = {
        "shaped": True,
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "raw_chars": len(raw_text),
        "approx_tokens": approx_tokens,
        "format": fmt,
        "row_count": row_count,
    }
    if keys:
        digest["keys"] = keys[:15]
    if time_range:
        digest["time_range"] = time_range
    if status_counts:
        digest["status_counts"] = dict(list(status_counts.items())[:8])
    if numeric_stats:
        digest["numeric_p50_p99"] = numeric_stats
    if top_errors:
        digest["top_errors"] = top_errors
    digest["drilldown_hint"] = (
        f'Call artifact_query(tool_call_id="{tool_call_id}", filter_expr="<regex_or_keyword>") '
        "to inspect specific rows or log lines."
    )
    return digest


def query_shaped_artifact(
    tool_call_id: str,
    filter_expr: str = "",
    *,
    limit: int = 25,
    offset: int = 0,
    state: Mapping[str, Any] | None = None,
    store: ArtifactShapeStore | None = None,
) -> str:
    """Retrieve filtered slices from a shaped tool artifact by ``tool_call_id``."""
    active_store = store or get_artifact_shape_store()
    entry = active_store.get(tool_call_id)
    raw_text: str | None = entry[1] if entry is not None else None

    # Fallback: recover from state['messages'] ToolMessage.artifact if available
    if raw_text is None and isinstance(state, Mapping):
        for msg in reversed(list(state.get("messages", []) or [])):
            if getattr(msg, "tool_call_id", None) == tool_call_id:
                art = getattr(msg, "artifact", None)
                if isinstance(art, Mapping) and isinstance(art.get("raw_payload"), str):
                    raw_text = art["raw_payload"]
                    break
                if isinstance(art, str):
                    raw_text = art
                    break

    if raw_text is None:
        return f"No shaped artifact found for tool_call_id={tool_call_id!r}."

    bounded_limit = max(1, min(100, int(limit)))
    bounded_offset = max(0, int(offset))
    expr = (filter_expr or "").strip()

    # Try structured JSON array / nested list filtering first
    stripped = raw_text.strip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(stripped)
            items: list[Any] | None = None
            if isinstance(parsed, list):
                items = parsed
            elif isinstance(parsed, Mapping):
                if expr and expr in parsed:
                    val = parsed[expr]
                    rendered = json.dumps(val, indent=2) if not isinstance(val, str) else val
                    return rendered[:6000]
                for lk in ("items", "results", "data", "events", "rows", "logs", "series", "alerts"):
                    if isinstance(parsed.get(lk), list):
                        items = parsed[lk]
                        break
            if items is not None:
                if expr:
                    try:
                        rx = re.compile(expr, re.IGNORECASE)
                        matched = [it for it in items if rx.search(json.dumps(it, default=str))]
                    except re.error:
                        low = expr.lower()
                        matched = [it for it in items if low in json.dumps(it, default=str).lower()]
                else:
                    matched = items
                sliced = matched[bounded_offset : bounded_offset + bounded_limit]
                return json.dumps(
                    {
                        "tool_call_id": tool_call_id,
                        "total_matches": len(matched),
                        "offset": bounded_offset,
                        "returned": len(sliced),
                        "matches": sliced,
                    },
                    indent=2,
                    default=str,
                )[:6000]
        except Exception:
            pass

    lines = raw_text.splitlines()
    if expr:
        try:
            rx = re.compile(expr, re.IGNORECASE)
            matched_lines = [f"L{idx + 1}: {ln}" for idx, ln in enumerate(lines) if rx.search(ln)]
        except re.error:
            low = expr.lower()
            matched_lines = [f"L{idx + 1}: {ln}" for idx, ln in enumerate(lines) if low in ln.lower()]
    else:
        matched_lines = [f"L{idx + 1}: {ln}" for idx, ln in enumerate(lines)]

    sliced_lines = matched_lines[bounded_offset : bounded_offset + bounded_limit]
    header = (
        f"[artifact_query tool_call_id={tool_call_id} total_matches={len(matched_lines)} "
        f"showing={bounded_offset}..{bounded_offset + len(sliced_lines)}]"
    )
    return (header + "\n" + "\n".join(sliced_lines))[:6000]


def build_artifact_query_tool(store: ArtifactShapeStore | None = None) -> BaseTool:
    """Create the LangChain ``artifact_query`` drill-down tool."""

    @tool
    def artifact_query(
        tool_call_id: Annotated[
            str,
            Field(description="Exact tool_call_id from a shaped tool result digest."),
        ],
        filter_expr: Annotated[
            str,
            Field(
                default="",
                description="Regex, keyword, HTTP status, pod/service name, or top-level JSON key to filter on.",
            ),
        ] = "",
        limit: Annotated[
            int,
            Field(default=25, ge=1, le=100, description="Max matching records or lines to return."),
        ] = 25,
        offset: Annotated[
            int,
            Field(default=0, ge=0, description="Pagination offset into matching records/lines."),
        ] = 0,
        state: Annotated[dict[str, Any] | None, InjectedState] = None,
    ) -> str:
        """Query the full uncompacted output of a shaped tool call without flooding context."""
        return query_shaped_artifact(
            tool_call_id=tool_call_id,
            filter_expr=filter_expr,
            limit=limit,
            offset=offset,
            state=state,
            store=store,
        )

    return artifact_query


@register_middleware("tool_payload_shaper")
class ToolPayloadShaperMiddleware(AgentMiddleware):
    """Shapes oversized tool payloads (>3k tokens) into <300-token structural digests + ``artifact_query``."""

    # Tools whose output should remain verbatim (e.g., direct file reads or artifact drill-downs)
    DEFAULT_EXCLUDED_TOOLS: frozenset[str] = frozenset(
        {
            "artifact_query",
            "read_file",
            "write_file",
            "edit_file",
            "ask_user",
            "update_goal",
        }
    )

    def __init__(
        self,
        *,
        char_threshold: int | None = None,
        excluded_tools: Sequence[str] = (),
        artifact_store: ArtifactShapeStore | None = None,
        expose_query_tool: bool = True,
    ) -> None:
        """Initialize ToolPayloadShaperMiddleware."""
        super().__init__()
        env_thresh = os.environ.get("TALKOPS_PAYLOAD_SHAPER_CHAR_THRESHOLD", "").strip()
        self.char_threshold = (
            char_threshold
            if char_threshold is not None
            else (int(env_thresh) if env_thresh.isdigit() else DEFAULT_SHAPER_CHAR_THRESHOLD)
        )
        self.excluded_tools = self.DEFAULT_EXCLUDED_TOOLS | frozenset(excluded_tools)
        self._store = artifact_store or get_artifact_shape_store()
        if expose_query_tool:
            self.tools = [build_artifact_query_tool(self._store)]

    def _shape_result(self, request: ToolCallRequest, result: Any) -> Any:
        tool_name = str(request.tool_call.get("name", ""))
        if tool_name in self.excluded_tools:
            return result
        if not isinstance(result, ToolMessage):
            return result

        content = result.content
        if isinstance(content, str):
            raw_text = content
        elif isinstance(content, list):
            parts = [
                blk.get("text", "") if isinstance(blk, Mapping) else str(blk)
                for blk in content
            ]
            raw_text = "\n".join(p for p in parts if p)
        else:
            return result

        if len(raw_text) <= self.char_threshold:
            return result

        tool_call_id = str(result.tool_call_id or request.tool_call.get("id", ""))
        self._store.put(tool_call_id, tool_name, raw_text)
        digest = compute_payload_shape_digest(
            raw_text,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )
        existing_artifact = getattr(result, "artifact", None)
        shaped_artifact: dict[str, Any] = {
            "raw_payload": raw_text,
            "shape_digest": digest,
        }
        if existing_artifact is not None:
            shaped_artifact["original_artifact"] = existing_artifact

        return result.model_copy(
            update={
                "content": json.dumps(digest, indent=2),
                "artifact": shaped_artifact,
            }
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """Execute `wrap_tool_call`."""
        res = handler(request)
        return self._shape_result(request, res)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """Execute `awrap_tool_call`."""
        res = await handler(request)
        return self._shape_result(request, res)


def build_sre_context_editing_middleware(
    *,
    trigger_tokens: int = 60_000,
    keep_recent_tool_uses: int = 5,
) -> ContextEditingMiddleware:
    """Create native LangChain ``ContextEditingMiddleware`` configured for multi-step SRE runs."""
    return ContextEditingMiddleware(
        edits=[
            ClearToolUsesEdit(
                trigger=trigger_tokens,
                keep=keep_recent_tool_uses,
                exclude_tools=("artifact_query", "read_file", "update_goal"),
                placeholder="[older tool output cleared by ContextEditingMiddleware; re-query via artifact_query if needed]",
            )
        ],
        token_count_method="approximate",
    )

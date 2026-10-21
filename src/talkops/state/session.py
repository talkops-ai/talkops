"""Thread and session management using LangGraph SQLite checkpoint persistence.

Provides high-performance thread listing, batch checkpoint decoding with
JsonPlusSerializer, delta-channel-aware message counting, multi-tiered LRU caching,
and cloud environment metadata extraction.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import sys
import time
from collections.abc import AsyncGenerator, AsyncIterator, Generator, Iterable, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime
import os
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    NamedTuple,
    NotRequired,
    Self,
    TypedDict,
    cast,
)

from talkops.middleware.goal_state_notice import is_internal_message
from talkops.state.cloud_context import CloudEnvironmentContext, format_cloud_badge
from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    import aiosqlite
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import CheckpointTuple
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

logger = get_logger(__name__)

_current_user_id: ContextVar[str] = ContextVar("talkops_current_user_id", default="")


def get_current_user_id(explicit_user_id: str | None = None) -> str:
    """Resolve the active user identity from explicit argument, contextvar, or environment."""
    if explicit_user_id and explicit_user_id.strip():
        return explicit_user_id.strip()
    ctx_val = _current_user_id.get()
    if ctx_val and ctx_val.strip():
        return ctx_val.strip()
    env_val = os.environ.get("TALKOPS_USER_ID", "").strip()
    if env_val:
        return env_val
    return "default"


def set_current_user_id(user_id: str | None) -> None:
    """Set the active user identity in the current async context."""
    _current_user_id.set((user_id or "").strip())


@contextmanager
def bind_user_context(user_id: str | None) -> Generator[str, None, None]:
    """Temporarily bind `user_id` for the duration of a block."""
    resolved = get_current_user_id(user_id)
    token = _current_user_id.set(resolved)
    try:
        yield resolved
    finally:
        _current_user_id.reset(token)


_aiosqlite_patched = False
_jsonplus_serializer: JsonPlusSerializer | None = None
_message_count_cache: dict[str, tuple[str | None, int]] = {}
_MAX_MESSAGE_COUNT_CACHE = 4096
_initial_prompt_cache: dict[str, tuple[str | None, str | None]] = {}
_MAX_INITIAL_PROMPT_CACHE = 4096
_cloud_badge_cache: dict[str, tuple[str | None, str | None]] = {}
_thread_title_cache: dict[str, str] = {}
_recent_threads_cache: dict[tuple[str | None, int], list[ThreadInfo]] = {}
_MAX_RECENT_THREADS_CACHE_KEYS = 16
_DEFAULT_SQLITE_TIMEOUT = 5.0
_SQLITE_MAX_VARIABLE_NUMBER = 500


def _patch_aiosqlite() -> None:
    """Patch aiosqlite.Connection with is_alive() if missing."""
    global _aiosqlite_patched
    if _aiosqlite_patched:
        return

    import aiosqlite as _aiosqlite

    if not hasattr(_aiosqlite.Connection, "is_alive"):
        def _is_alive(self: _aiosqlite.Connection) -> bool:
            return self._running and self._connection is not None

        _aiosqlite.Connection.is_alive = _is_alive  # type: ignore[attr-defined]

    _aiosqlite_patched = True


async def _drain_aiosqlite_worker(conn: Any) -> None:
    """Join the aiosqlite worker thread after its connection is closed."""
    worker = getattr(conn, "_thread", None)
    if worker is None or not worker.is_alive():
        return
    with contextlib.suppress(RuntimeError):
        await asyncio.to_thread(worker.join, 5.0)


async def _guard_sqlite_handle(conn: Any) -> None:
    """Apply connection safety and WAL performance settings (IA-11)."""
    with contextlib.suppress(Exception):
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = NORMAL")
        await conn.execute("PRAGMA busy_timeout = 15000")
        await conn.execute("PRAGMA temp_store = MEMORY")
        await conn.execute("PRAGMA cache_size = -16000")
        await conn.execute("PRAGMA foreign_keys = ON")


_db_path: Path | None = None
_last_active_db_path: Path | None = None


def _clear_in_memory_caches() -> None:
    """Clear all in-memory thread metadata and summary caches."""
    _message_count_cache.clear()
    _initial_prompt_cache.clear()
    _cloud_badge_cache.clear()
    _thread_title_cache.clear()
    _recent_threads_cache.clear()


def get_db_path() -> Path:
    """Return the canonical path to the SQLite sessions database."""
    global _db_path
    if _db_path is not None:
        return _db_path
    from talkops.config.paths import SESSIONS_DB_PATH

    SESSIONS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _db_path = SESSIONS_DB_PATH
    return _db_path


def set_db_path(path: Path) -> None:
    """Override database path (used by tests)."""
    global _db_path, _last_active_db_path
    _db_path = path
    _last_active_db_path = path
    _clear_in_memory_caches()


@asynccontextmanager
async def _connect() -> AsyncGenerator[Any, None]:
    """Provide an asynchronous context manager connected to the sessions database."""
    global _last_active_db_path
    import aiosqlite as _aiosqlite

    _patch_aiosqlite()
    active_path = get_db_path()
    if _last_active_db_path != active_path:
        _last_active_db_path = active_path
        _clear_in_memory_caches()

    conn: _aiosqlite.Connection | None = None
    try:
        async with _aiosqlite.connect(str(active_path), timeout=30.0) as opened:
            conn = opened
            await _guard_sqlite_handle(opened)
            yield opened
    finally:
        if conn is not None:
            await _drain_aiosqlite_worker(conn)


class ThreadInfo(TypedDict):
    """Metadata for an TalkOps execution thread."""

    thread_id: str
    agent_name: str | None
    updated_at: str | None
    created_at: NotRequired[str | None]
    git_branch: NotRequired[str | None]
    title: NotRequired[str | None]
    initial_prompt: NotRequired[str | None]
    message_count: NotRequired[int]
    latest_checkpoint_id: NotRequired[str | None]
    cwd: NotRequired[str | None]
    cloud_provider: NotRequired[str | None]
    cloud_badge: NotRequired[str | None]
    owner: NotRequired[str | None]
    parent_thread_id: NotRequired[str | None]
    status: NotRequired[str | None]


class _CheckpointSummary(NamedTuple):
    """Structured operational facts extracted from a checkpoint payload."""

    message_count: int | None
    initial_prompt: str | None
    cloud_provider: str | None
    cloud_badge: str | None


class _ThreadSummaryRow(NamedTuple):
    """Indexed summary row from SQLite ``thread_summaries`` (agy summarystore pattern)."""

    thread_id: str
    user_id: str
    title: str
    preview: str
    agent_name: str | None
    cwd: str | None
    git_branch: str | None
    created_at: str | None
    updated_at: str | None
    latest_checkpoint_id: str | None
    message_count: int | None
    cloud_provider: str | None
    cloud_badge: str | None
    parent_thread_id: str | None
    status: str | None


def generate_thread_id() -> str:
    """Generate a chronological UUID7 string for thread identity."""
    try:
        from uuid_utils import uuid7
        return str(uuid7())
    except ImportError:
        import uuid
        return str(uuid.uuid4())


def format_timestamp(iso_timestamp: str | None) -> str:
    """Format an ISO timestamp to human readable date."""
    if not iso_timestamp:
        return ""
    try:
        dt = datetime.fromisoformat(iso_timestamp).astimezone()
        return dt.strftime("%b %d, %-I:%M%p").lower()
    except Exception:
        return ""


def format_relative_timestamp(iso_timestamp: str | None) -> str:
    """Format an ISO timestamp as a relative time string (e.g. 5m ago)."""
    if not iso_timestamp:
        return ""
    try:
        dt = datetime.fromisoformat(iso_timestamp).astimezone()
    except Exception:
        return ""

    delta = datetime.now(tz=dt.tzinfo) - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{max(0, seconds)}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


async def _table_exists(conn: Any, table: str) -> bool:
    query = "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?"
    async with conn.execute(query, (table,)) as cursor:
        return await cursor.fetchone() is not None


_THREADS_LIST_INDEX = "idx_talkops_threads_list"
_THREAD_SUMMARIES_TABLE = "thread_summaries"

_THREAD_SUMMARIES_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("user_id", "TEXT NOT NULL DEFAULT 'default'"),
    ("agent_name", "TEXT"),
    ("cwd", "TEXT"),
    ("git_branch", "TEXT"),
    ("created_at", "TEXT"),
    ("latest_checkpoint_id", "TEXT"),
    ("message_count", "INTEGER"),
    ("cloud_provider", "TEXT"),
    ("cloud_badge", "TEXT"),
    ("parent_thread_id", "TEXT"),
    ("status", "TEXT NOT NULL DEFAULT 'idle'"),
)


async def _ensure_thread_summaries_table(conn: Any) -> None:
    """Ensure the SQLite ``thread_summaries`` table and indexes exist (agy summarystore pattern).

    Automatically migrates pre-existing ``thread_summaries`` tables by adding any missing
    columns and creating lookup indexes on ``updated_at``, ``(user_id, updated_at)``, and
    ``(cwd, updated_at)``.
    """
    await conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_THREAD_SUMMARIES_TABLE} (
            thread_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'default',
            title TEXT NOT NULL DEFAULT '',
            preview TEXT NOT NULL DEFAULT '',
            agent_name TEXT,
            cwd TEXT,
            git_branch TEXT,
            created_at TEXT,
            updated_at TEXT NOT NULL DEFAULT '',
            latest_checkpoint_id TEXT,
            message_count INTEGER,
            cloud_provider TEXT,
            cloud_badge TEXT,
            parent_thread_id TEXT,
            status TEXT NOT NULL DEFAULT 'idle'
        )
        """
    )
    if not getattr(conn, "_talkops_summaries_migrated", False):
        existing_cols: set[str] = set()
        with contextlib.suppress(Exception):
            async with conn.execute(f"PRAGMA table_info({_THREAD_SUMMARIES_TABLE})") as cursor:
                for row in await cursor.fetchall():
                    if len(row) > 1 and isinstance(row[1], str):
                        existing_cols.add(row[1])
        for col_name, col_def in _THREAD_SUMMARIES_EXTRA_COLUMNS:
            if col_name not in existing_cols:
                with contextlib.suppress(Exception):
                    await conn.execute(
                        f"ALTER TABLE {_THREAD_SUMMARIES_TABLE} ADD COLUMN {col_name} {col_def}"
                    )
        with contextlib.suppress(Exception):
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_thread_summaries_updated_at "
                f"ON {_THREAD_SUMMARIES_TABLE}(updated_at DESC)"
            )
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_thread_summaries_user_updated_at "
                f"ON {_THREAD_SUMMARIES_TABLE}(user_id, updated_at DESC)"
            )
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_thread_summaries_cwd_updated_at "
                f"ON {_THREAD_SUMMARIES_TABLE}(cwd, updated_at DESC)"
            )
        with contextlib.suppress(Exception):
            setattr(conn, "_talkops_summaries_migrated", True)


async def _load_thread_summaries_batch(
    conn: Any,
    thread_ids: list[str],
) -> dict[str, _ThreadSummaryRow]:
    """Batch-load full ``_ThreadSummaryRow`` records from ``thread_summaries``."""
    if not thread_ids or not await _table_exists(conn, _THREAD_SUMMARIES_TABLE):
        return {}
    await _ensure_thread_summaries_table(conn)
    results: dict[str, _ThreadSummaryRow] = {}
    for start in range(0, len(thread_ids), _SQLITE_MAX_VARIABLE_NUMBER):
        chunk = thread_ids[start : start + _SQLITE_MAX_VARIABLE_NUMBER]
        placeholders = ",".join("?" * len(chunk))
        query = (
            f"SELECT thread_id, user_id, title, preview, agent_name, cwd, git_branch, "
            f"created_at, updated_at, latest_checkpoint_id, message_count, "
            f"cloud_provider, cloud_badge, parent_thread_id, status "
            f"FROM {_THREAD_SUMMARIES_TABLE} "
            f"WHERE thread_id IN ({placeholders})"
        )
        async with conn.execute(query, chunk) as cursor:
            rows = await cursor.fetchall()
        for r in rows:
            tid = str(r[0])
            t_str = r[2].strip() if isinstance(r[2], str) else ""
            p_str = r[3].strip() if isinstance(r[3], str) else ""
            if t_str:
                _thread_title_cache[tid] = t_str
            results[tid] = _ThreadSummaryRow(
                thread_id=tid,
                user_id=str(r[1]) if r[1] else "default",
                title=t_str,
                preview=p_str,
                agent_name=str(r[4]) if r[4] else None,
                cwd=str(r[5]) if r[5] else None,
                git_branch=str(r[6]) if r[6] else None,
                created_at=str(r[7]) if r[7] else None,
                updated_at=str(r[8]) if r[8] else None,
                latest_checkpoint_id=str(r[9]) if r[9] else None,
                message_count=int(r[10]) if r[10] is not None else None,
                cloud_provider=str(r[11]) if r[11] else None,
                cloud_badge=str(r[12]) if r[12] else None,
                parent_thread_id=str(r[13]) if r[13] else None,
                status=str(r[14]) if r[14] else "idle",
            )
    return results


async def _load_thread_titles_batch(
    conn: Any,
    thread_ids: list[str],
) -> dict[str, tuple[str, str]]:
    """Batch-load ``(title, preview)`` tuples from ``thread_summaries`` for given thread IDs."""
    summaries = await _load_thread_summaries_batch(conn, thread_ids)
    return {tid: (row.title, row.preview) for tid, row in summaries.items()}


async def _upsert_thread_summary_on_conn(
    conn: Any,
    thread_id: str,
    *,
    user_id: str | None = None,
    title: str | None = None,
    preview: str | None = None,
    agent_name: str | None = None,
    cwd: str | None = None,
    git_branch: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    latest_checkpoint_id: str | None = None,
    message_count: int | None = None,
    cloud_provider: str | None = None,
    cloud_badge: str | None = None,
    parent_thread_id: str | None = None,
    status: str | None = None,
    overwrite_title: bool = False,
) -> None:
    """Upsert a thread summary record on an open SQLite connection."""
    if not thread_id or not isinstance(thread_id, str):
        return

    from talkops.state.conversation_title import (
        postprocess_conversation_title,
        truncate_summary_preview,
    )

    await _ensure_thread_summaries_table(conn)
    eff_user = (user_id or "").strip() or get_current_user_id()
    cleaned_title = postprocess_conversation_title(title) if title else ""
    cleaned_preview = truncate_summary_preview(preview) if preview else ""
    now_iso = updated_at or datetime.now().astimezone().isoformat()
    created_iso = created_at or now_iso
    status_val = status or "idle"

    title_update_expr = (
        "CASE WHEN excluded.title != '' THEN excluded.title ELSE thread_summaries.title END"
        if overwrite_title
        else "CASE WHEN thread_summaries.title != '' THEN thread_summaries.title ELSE excluded.title END"
    )

    await conn.execute(
        f"""
        INSERT INTO {_THREAD_SUMMARIES_TABLE} (
            thread_id, user_id, title, preview, agent_name, cwd, git_branch,
            created_at, updated_at, latest_checkpoint_id, message_count,
            cloud_provider, cloud_badge, parent_thread_id, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(thread_id) DO UPDATE SET
            user_id = CASE
                WHEN {_THREAD_SUMMARIES_TABLE}.user_id IN ('', 'default') AND excluded.user_id NOT IN ('', 'default')
                THEN excluded.user_id
                ELSE {_THREAD_SUMMARIES_TABLE}.user_id
            END,
            title = {title_update_expr},
            preview = CASE
                WHEN {_THREAD_SUMMARIES_TABLE}.preview != '' THEN {_THREAD_SUMMARIES_TABLE}.preview
                ELSE excluded.preview
            END,
            agent_name = COALESCE(excluded.agent_name, {_THREAD_SUMMARIES_TABLE}.agent_name),
            cwd = COALESCE(excluded.cwd, {_THREAD_SUMMARIES_TABLE}.cwd),
            git_branch = COALESCE(excluded.git_branch, {_THREAD_SUMMARIES_TABLE}.git_branch),
            created_at = COALESCE(NULLIF({_THREAD_SUMMARIES_TABLE}.created_at, ''), excluded.created_at),
            updated_at = CASE
                WHEN excluded.updated_at != '' THEN excluded.updated_at
                ELSE {_THREAD_SUMMARIES_TABLE}.updated_at
            END,
            latest_checkpoint_id = COALESCE(excluded.latest_checkpoint_id, {_THREAD_SUMMARIES_TABLE}.latest_checkpoint_id),
            message_count = COALESCE(excluded.message_count, {_THREAD_SUMMARIES_TABLE}.message_count),
            cloud_provider = COALESCE(excluded.cloud_provider, {_THREAD_SUMMARIES_TABLE}.cloud_provider),
            cloud_badge = COALESCE(excluded.cloud_badge, {_THREAD_SUMMARIES_TABLE}.cloud_badge),
            parent_thread_id = COALESCE(excluded.parent_thread_id, {_THREAD_SUMMARIES_TABLE}.parent_thread_id),
            status = COALESCE(excluded.status, {_THREAD_SUMMARIES_TABLE}.status)
        """,
        (
            thread_id,
            eff_user,
            cleaned_title,
            cleaned_preview,
            agent_name,
            cwd,
            git_branch,
            created_iso,
            now_iso,
            latest_checkpoint_id,
            message_count,
            cloud_provider,
            cloud_badge,
            parent_thread_id,
            status_val,
        ),
    )
    if cleaned_title and overwrite_title:
        _thread_title_cache[thread_id] = cleaned_title
    elif cleaned_title and thread_id not in _thread_title_cache:
        _thread_title_cache[thread_id] = cleaned_title


async def upsert_thread_summary(
    thread_id: str,
    *,
    user_id: str | None = None,
    title: str | None = None,
    preview: str | None = None,
    agent_name: str | None = None,
    cwd: str | None = None,
    git_branch: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    latest_checkpoint_id: str | None = None,
    message_count: int | None = None,
    cloud_provider: str | None = None,
    cloud_badge: str | None = None,
    parent_thread_id: str | None = None,
    status: str | None = None,
    overwrite_title: bool = False,
) -> None:
    """Persist or update a thread summary record in SQLite ``thread_summaries``."""
    if not thread_id or not isinstance(thread_id, str) or not thread_id.strip():
        return
    async with _connect() as conn:
        await _upsert_thread_summary_on_conn(
            conn,
            thread_id.strip(),
            user_id=user_id,
            title=title,
            preview=preview,
            agent_name=agent_name,
            cwd=cwd,
            git_branch=git_branch,
            created_at=created_at,
            updated_at=updated_at,
            latest_checkpoint_id=latest_checkpoint_id,
            message_count=message_count,
            cloud_provider=cloud_provider,
            cloud_badge=cloud_badge,
            parent_thread_id=parent_thread_id,
            status=status,
            overwrite_title=overwrite_title,
        )
        await conn.commit()
    _recent_threads_cache.clear()


async def get_thread_title(
    thread_id: str,
    *,
    user_id: str | None = None,
) -> str | None:
    """Retrieve the persisted LLM-generated title for ``thread_id``, if any."""
    if not thread_id or not isinstance(thread_id, str):
        return None
    cached = _thread_title_cache.get(thread_id)
    if cached:
        return cached

    from talkops.state.postgres_session import PostgresSessionBackend, get_postgres_session_uri
    from talkops.state.remote_session import RemoteSessionBackend, is_remote_client_mode

    if is_remote_client_mode():
        title_val = await RemoteSessionBackend(user_id=user_id).get_thread_title(thread_id)
        if title_val:
            _thread_title_cache[thread_id] = title_val
        return title_val

    pg_uri = get_postgres_session_uri()
    if pg_uri:
        eff_user = get_current_user_id(user_id)
        title_val = await PostgresSessionBackend(pg_uri).get_thread_title(
            thread_id, user_id=eff_user
        )
        if title_val:
            _thread_title_cache[thread_id] = title_val
        return title_val

    async with _connect() as conn:
        if not await _table_exists(conn, _THREAD_SUMMARIES_TABLE):
            return None
        async with conn.execute(
            f"SELECT title FROM {_THREAD_SUMMARIES_TABLE} WHERE thread_id = ? LIMIT 1",
            (thread_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row and isinstance(row[0], str) and row[0].strip():
                title_val = row[0].strip()
                _thread_title_cache[thread_id] = title_val
                return title_val
    return None


async def set_thread_title(
    thread_id: str,
    title: str,
    *,
    preview: str | None = None,
    only_if_empty: bool = True,
    user_id: str | None = None,
) -> bool:
    """Persist an LLM-generated conversation title to ``thread_summaries``.

    When ``only_if_empty=True`` (the default, matching agy ``maybeGenerateConversationTitle.func1``
    at ``0x101d8dd08``), an existing non-empty title is never overwritten.
    """
    if not thread_id or not isinstance(thread_id, str) or not thread_id.strip():
        return False

    from talkops.state.conversation_title import (
        postprocess_conversation_title,
        truncate_summary_preview,
    )
    from talkops.state.postgres_session import PostgresSessionBackend, get_postgres_session_uri
    from talkops.state.remote_session import RemoteSessionBackend, is_remote_client_mode

    cleaned_title = postprocess_conversation_title(title)
    if not cleaned_title:
        return False

    if is_remote_client_mode():
        updated = await RemoteSessionBackend(user_id=user_id).set_thread_title(
            thread_id,
            cleaned_title,
            preview=preview,
            only_if_empty=only_if_empty,
        )
        if updated:
            _thread_title_cache[thread_id] = cleaned_title
            _recent_threads_cache.clear()
        return updated

    eff_user = get_current_user_id(user_id)
    pg_uri = get_postgres_session_uri()
    if pg_uri:
        updated = await PostgresSessionBackend(pg_uri).set_thread_title(
            thread_id,
            cleaned_title,
            preview=preview,
            only_if_empty=only_if_empty,
            user_id=eff_user,
        )
        if updated:
            _thread_title_cache[thread_id] = cleaned_title
            _recent_threads_cache.clear()
        return updated

    cleaned_preview = truncate_summary_preview(preview) if preview else ""
    now_iso = datetime.now().astimezone().isoformat()

    async with _connect() as conn:
        await _ensure_thread_summaries_table(conn)
        if only_if_empty:
            async with conn.execute(
                f"SELECT title FROM {_THREAD_SUMMARIES_TABLE} WHERE thread_id = ? LIMIT 1",
                (thread_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row and isinstance(row[0], str) and row[0].strip():
                    existing = row[0].strip()
                    _thread_title_cache[thread_id] = existing
                    return False

        await conn.execute(
            f"""
            INSERT INTO {_THREAD_SUMMARIES_TABLE} (thread_id, user_id, title, preview, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                user_id = CASE
                    WHEN {_THREAD_SUMMARIES_TABLE}.user_id IN ('', 'default') THEN excluded.user_id
                    ELSE {_THREAD_SUMMARIES_TABLE}.user_id
                END,
                title = excluded.title,
                preview = CASE
                    WHEN excluded.preview != '' THEN excluded.preview
                    ELSE {_THREAD_SUMMARIES_TABLE}.preview
                END,
                updated_at = excluded.updated_at
            """,
            (thread_id, eff_user, cleaned_title, cleaned_preview, now_iso),
        )
        await conn.commit()

    _thread_title_cache[thread_id] = cleaned_title
    _recent_threads_cache.clear()
    return True


async def _ensure_threads_list_index(conn: Any) -> None:
    try:
        await conn.execute(
            f"CREATE INDEX IF NOT EXISTS {_THREADS_LIST_INDEX} ON checkpoints("
            "thread_id, "
            "json_extract(metadata, '$.updated_at'), "
            "checkpoint_id, "
            "json_extract(metadata, '$.agent_name'), "
            "json_extract(metadata, '$.git_branch'), "
            "json_extract(metadata, '$.cwd'))"
        )
        await conn.commit()
    except Exception:
        pass


def _get_jsonplus_serializer() -> JsonPlusSerializer:
    global _jsonplus_serializer
    if _jsonplus_serializer is not None:
        return _jsonplus_serializer
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    _jsonplus_serializer = JsonPlusSerializer()
    return _jsonplus_serializer


def _thread_freshness(thread: ThreadInfo) -> str | None:
    return thread.get("latest_checkpoint_id") or thread.get("updated_at")


def _cache_message_count(thread_id: str, freshness: str | None, count: int) -> None:
    if len(_message_count_cache) >= _MAX_MESSAGE_COUNT_CACHE:
        _message_count_cache.clear()
    _message_count_cache[thread_id] = (freshness, count)


def _cache_initial_prompt(thread_id: str, freshness: str | None, prompt: str | None) -> None:
    if len(_initial_prompt_cache) >= _MAX_INITIAL_PROMPT_CACHE:
        _initial_prompt_cache.clear()
    _initial_prompt_cache[thread_id] = (freshness, prompt)


def _cache_cloud_badge(thread_id: str, freshness: str | None, badge: str | None) -> None:
    if len(_cloud_badge_cache) >= _MAX_INITIAL_PROMPT_CACHE:
        _cloud_badge_cache.clear()
    _cloud_badge_cache[thread_id] = (freshness, badge)


def _visible_message_count(messages: list[object]) -> int:
    return sum(not is_internal_message(m) for m in messages)


def _checkpoint_messages(data: object) -> list[object] | None:
    if not isinstance(data, dict):
        return None
    channel_values = data.get("channel_values")
    if not isinstance(channel_values, dict):
        return None
    messages = channel_values.get("messages")
    if not isinstance(messages, list):
        return None
    return list(messages)


def _coerce_prompt_text(content: object) -> str | None:
    if isinstance(content, str):
        stripped = content.strip()
        return stripped if stripped else None
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        joined = " ".join(parts).strip()
        return joined if joined else None
    return None


def _initial_prompt_from_messages(messages: Sequence[object]) -> str | None:
    for msg in messages:
        if is_internal_message(msg):
            continue
        if getattr(msg, "type", None) == "human":
            prompt = _coerce_prompt_text(getattr(msg, "content", None))
            if prompt:
                return prompt
        elif isinstance(msg, dict):
            role = msg.get("role")
            type_ = msg.get("type")
            if role in {"user", "human"} or type_ == "human":
                prompt = _coerce_prompt_text(msg.get("content"))
                if prompt:
                    return prompt
    return None


def _summarize_checkpoint(data: object) -> _CheckpointSummary:
    messages = _checkpoint_messages(data)
    count = _visible_message_count(messages) if messages is not None else None
    prompt = _initial_prompt_from_messages(messages or [])

    cloud_provider: str | None = None
    cloud_badge: str | None = None
    if isinstance(data, dict):
        cv = data.get("channel_values")
        if isinstance(cv, dict):
            provider_val = cv.get("_active_cloud_provider")
            if isinstance(provider_val, str):
                cloud_provider = provider_val
            ctx_val = cv.get("_cloud_context")
            if isinstance(ctx_val, dict):
                cloud_badge = format_cloud_badge(cast(CloudEnvironmentContext, ctx_val))
            elif cloud_provider:
                cloud_badge = f"[{cloud_provider.upper()}]"

    return _CheckpointSummary(
        message_count=count,
        initial_prompt=prompt,
        cloud_provider=cloud_provider,
        cloud_badge=cloud_badge,
    )


def _count_messages_from_deltas(deltas: list[Any]) -> int:
    from langchain_core.messages import RemoveMessage
    from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages
    from langgraph.types import Overwrite

    buffer: list[Any] = []
    needs_exact_fold = False
    for delta in deltas:
        if isinstance(delta, Overwrite):
            val = delta.value
            buffer = list(val) if isinstance(val, list) else []
            continue
        items = delta if isinstance(delta, list) else [delta]
        for item in items:
            if isinstance(item, RemoveMessage):
                if item.id == REMOVE_ALL_MESSAGES:
                    buffer = []
                else:
                    needs_exact_fold = True
                    break
            else:
                buffer.append(item)
        if needs_exact_fold:
            break

    if not needs_exact_fold:
        try:
            reduced = cast(list[Any], add_messages([], buffer))
            return _visible_message_count(reduced)
        except Exception:
            pass

    # Sequential fold fallback
    reduced_seq: list[Any] = []
    for delta in deltas:
        if isinstance(delta, Overwrite):
            val = delta.value
            reduced_seq = list(val) if isinstance(val, list) else []
            continue
        try:
            reduced_seq = cast(list[Any], add_messages(reduced_seq, delta))
        except Exception:
            pass
    return _visible_message_count(reduced_seq)


def _reduce_message_write_rows(
    rows: list[tuple[str, str | None, bytes | None]],
    serde: JsonPlusSerializer,
) -> dict[str, int]:
    deltas_by_thread: dict[str, list[Any]] = {}
    for tid, type_str, value_blob in rows:
        if not type_str or not value_blob:
            continue
        try:
            delta = serde.loads_typed((type_str, value_blob))
            deltas_by_thread.setdefault(tid, []).append(delta)
        except Exception:
            continue

    counts: dict[str, int] = {}
    for tid, deltas in deltas_by_thread.items():
        try:
            counts[tid] = _count_messages_from_deltas(deltas)
        except Exception:
            pass
    return counts


async def _load_latest_checkpoint_summaries_batch(
    conn: Any,
    thread_ids: list[str],
    serde: JsonPlusSerializer,
) -> dict[str, _CheckpointSummary]:
    if not thread_ids:
        return {}
    results: dict[str, _CheckpointSummary] = {}
    loop = asyncio.get_running_loop()

    for start in range(0, len(thread_ids), _SQLITE_MAX_VARIABLE_NUMBER):
        chunk = thread_ids[start : start + _SQLITE_MAX_VARIABLE_NUMBER]
        placeholders = ",".join("?" * len(chunk))
        query = f"""
            SELECT c.thread_id, c.type, c.checkpoint
            FROM checkpoints AS c
            JOIN (
                SELECT rowid AS rid,
                       ROW_NUMBER() OVER (
                           PARTITION BY thread_id ORDER BY checkpoint_id DESC
                       ) AS rn
                FROM checkpoints
                WHERE thread_id IN ({placeholders})
            ) AS ranked ON c.rowid = ranked.rid
            WHERE ranked.rn = 1
        """
        async with conn.execute(query, chunk) as cursor:
            rows = await cursor.fetchall()

        for tid, type_str, checkpoint_blob in rows:
            if not type_str or not checkpoint_blob:
                results[tid] = _CheckpointSummary(None, None, None, None)
                continue
            try:
                data = await loop.run_in_executor(
                    None, serde.loads_typed, (type_str, checkpoint_blob)
                )
                results[tid] = _summarize_checkpoint(data)
            except Exception:
                results[tid] = _CheckpointSummary(None, None, None, None)

    return results


async def _load_initial_prompts_from_writes_batch(
    conn: Any,
    thread_ids: list[str],
    serde: JsonPlusSerializer,
) -> dict[str, str | None]:
    if not thread_ids:
        return {}
    results: dict[str, str | None] = {}
    loop = asyncio.get_running_loop()

    for start in range(0, len(thread_ids), _SQLITE_MAX_VARIABLE_NUMBER):
        chunk = thread_ids[start : start + _SQLITE_MAX_VARIABLE_NUMBER]
        placeholders = ",".join("?" * len(chunk))
        query = f"""
            SELECT w.thread_id, w.type, w.value
            FROM writes AS w
            JOIN (
                SELECT rowid AS rid,
                       ROW_NUMBER() OVER (
                           PARTITION BY thread_id
                           ORDER BY checkpoint_id ASC, idx ASC
                       ) AS rn
                FROM writes
                WHERE thread_id IN ({placeholders}) AND channel = 'messages'
            ) AS ranked ON w.rowid = ranked.rid
            WHERE ranked.rn = 1
        """
        async with conn.execute(query, chunk) as cursor:
            rows = await cursor.fetchall()

        for tid, type_str, value_blob in rows:
            if not type_str or not value_blob:
                continue
            try:
                messages = await loop.run_in_executor(
                    None, serde.loads_typed, (type_str, value_blob)
                )
                if isinstance(messages, list):
                    prompt = _initial_prompt_from_messages(messages)
                    if prompt:
                        results[tid] = prompt
            except Exception:
                pass
    return results


async def _load_message_counts_from_writes_batch(
    conn: Any,
    thread_ids: list[str],
    serde: JsonPlusSerializer,
) -> dict[str, int]:
    if not thread_ids:
        return {}
    loop = asyncio.get_running_loop()
    results: dict[str, int] = {}

    for start in range(0, len(thread_ids), _SQLITE_MAX_VARIABLE_NUMBER):
        chunk = thread_ids[start : start + _SQLITE_MAX_VARIABLE_NUMBER]
        placeholders = ",".join("?" * len(chunk))
        query = f"""
            SELECT thread_id, type, value
            FROM writes
            WHERE thread_id IN ({placeholders})
              AND checkpoint_ns = ''
              AND channel = 'messages'
            ORDER BY thread_id, checkpoint_id ASC, task_id ASC, idx ASC
        """
        async with conn.execute(query, chunk) as cursor:
            rows = await cursor.fetchall()

        chunk_counts = await loop.run_in_executor(
            None, _reduce_message_write_rows, list(rows), serde
        )
        results.update(chunk_counts)

    return results


async def _backfill_thread_summaries_batch(
    conn: Any,
    threads: Sequence[ThreadInfo],
) -> None:
    """Persist computed checkpoint summary fields back to ``thread_summaries`` (self-healing index)."""
    if not threads:
        return
    with contextlib.suppress(Exception):
        await _ensure_thread_summaries_table(conn)
        for t in threads:
            tid = t.get("thread_id")
            if not tid:
                continue
            await _upsert_thread_summary_on_conn(
                conn,
                tid,
                user_id=t.get("owner") or get_current_user_id(),
                title=t.get("title"),
                preview=t.get("initial_prompt"),
                agent_name=t.get("agent_name"),
                cwd=t.get("cwd"),
                git_branch=t.get("git_branch"),
                created_at=t.get("created_at") or t.get("updated_at"),
                updated_at=t.get("updated_at"),
                latest_checkpoint_id=t.get("latest_checkpoint_id"),
                message_count=t.get("message_count"),
                cloud_provider=t.get("cloud_provider"),
                cloud_badge=t.get("cloud_badge"),
                parent_thread_id=t.get("parent_thread_id"),
                status=t.get("status") or "idle",
            )
        await conn.commit()


async def _populate_checkpoint_fields(
    conn: Any,
    threads: list[ThreadInfo],
    *,
    include_message_count: bool = True,
    include_initial_prompt: bool = True,
) -> None:
    """Populate message count, initial prompt, and cloud context in threads."""
    serde = _get_jsonplus_serializer()
    uncached: list[ThreadInfo] = []

    for thread in threads:
        thread_id = thread["thread_id"]
        freshness = _thread_freshness(thread)
        needs_count = False
        needs_prompt = False
        needs_badge = False

        if include_message_count and "message_count" not in thread:
            cached_cnt = _message_count_cache.get(thread_id)
            if cached_cnt is not None and cached_cnt[0] == freshness:
                thread["message_count"] = cached_cnt[1]
            else:
                needs_count = True

        if include_initial_prompt and "initial_prompt" not in thread:
            cached_p = _initial_prompt_cache.get(thread_id)
            if cached_p is not None and cached_p[0] == freshness:
                thread["initial_prompt"] = cached_p[1]
            else:
                needs_prompt = True

        if "cloud_badge" not in thread:
            cached_badge = _cloud_badge_cache.get(thread_id)
            if cached_badge is not None and cached_badge[0] == freshness:
                if cached_badge[1]:
                    thread["cloud_badge"] = cached_badge[1]
            else:
                needs_badge = True

        if needs_count or needs_prompt or needs_badge:
            uncached.append(thread)

    if not uncached:
        return

    uncached_ids = [t["thread_id"] for t in uncached]
    batch_results = await _load_latest_checkpoint_summaries_batch(conn, uncached_ids, serde)
    prompt_results = await _load_initial_prompts_from_writes_batch(conn, uncached_ids, serde)

    needs_writes_count: list[str] = []
    for thread in uncached:
        tid = thread["thread_id"]
        freshness = _thread_freshness(thread)
        summary = batch_results.get(tid)

        if summary:
            if summary.cloud_badge:
                thread["cloud_badge"] = summary.cloud_badge
            if summary.cloud_provider:
                thread["cloud_provider"] = summary.cloud_provider
            _cache_cloud_badge(tid, freshness, thread.get("cloud_badge"))
        else:
            _cache_cloud_badge(tid, freshness, thread.get("cloud_badge"))

        if include_message_count and "message_count" not in thread:
            if summary and summary.message_count is not None:
                thread["message_count"] = summary.message_count
                _cache_message_count(tid, freshness, summary.message_count)
            else:
                needs_writes_count.append(tid)

        if include_initial_prompt and "initial_prompt" not in thread:
            prompt = prompt_results.get(tid) or (summary.initial_prompt if summary else None)
            thread["initial_prompt"] = prompt
            _cache_initial_prompt(tid, freshness, prompt)

    if needs_writes_count:
        writes_counts = await _load_message_counts_from_writes_batch(conn, needs_writes_count, serde)
        uncached_by_id = {t["thread_id"]: t for t in uncached}
        for tid in needs_writes_count:
            cnt = writes_counts.get(tid, 0)
            if tid in uncached_by_id:
                t = uncached_by_id[tid]
                t["message_count"] = cnt
                _cache_message_count(tid, _thread_freshness(t), cnt)

    await _backfill_thread_summaries_batch(conn, uncached)


async def list_threads(
    agent_name: str | None = None,
    limit: int = 20,
    include_message_count: bool = True,
    sort_by: str = "updated",
    branch: str | None = None,
    cwd: str | None = None,
    *,
    user_id: str | None = None,
) -> list[ThreadInfo]:
    """List execution threads with parsed metadata, message counts, and initial prompts."""
    from talkops.state.postgres_session import PostgresSessionBackend, get_postgres_session_uri
    from talkops.state.remote_session import RemoteSessionBackend, is_remote_client_mode

    if is_remote_client_mode():
        return await RemoteSessionBackend(user_id=user_id).list_threads(
            agent_name=agent_name,
            limit=limit,
            include_message_count=include_message_count,
            sort_by=sort_by,
            branch=branch,
            cwd=cwd,
        )

    eff_user = get_current_user_id(user_id)
    pg_uri = get_postgres_session_uri()
    if pg_uri:
        return await PostgresSessionBackend(pg_uri).list_threads(
            agent_name=agent_name,
            limit=limit,
            include_message_count=include_message_count,
            sort_by=sort_by,
            branch=branch,
            cwd=cwd,
            user_id=eff_user,
        )

    async with _connect() as conn:
        if not await _table_exists(conn, "checkpoints"):
            return []

        await _ensure_threads_list_index(conn)

        order_col = "created_at" if sort_by == "created" else "updated_at"
        where_clauses: list[str] = []
        params_list: list[str | int] = []

        if agent_name:
            where_clauses.append("json_extract(metadata, '$.agent_name') = ?")
            params_list.append(agent_name)
        if branch:
            where_clauses.append("json_extract(metadata, '$.git_branch') = ?")
            params_list.append(branch)
        if cwd:
            where_clauses.append("json_extract(metadata, '$.cwd') = ?")
            params_list.append(cwd)

        has_summaries = await _table_exists(conn, _THREAD_SUMMARIES_TABLE)
        if eff_user and eff_user not in ("*", "default", "local"):
            if has_summaries:
                await _ensure_thread_summaries_table(conn)
                where_clauses.append(
                    "("
                    "COALESCE("
                    "json_extract(CAST(metadata AS TEXT), '$.owner'), "
                    f"(SELECT ts.user_id FROM {_THREAD_SUMMARIES_TABLE} ts WHERE ts.thread_id = checkpoints.thread_id), "
                    "'default'"
                    ") IN (?, 'webhook', 'default')"
                    ")"
                )
            else:
                where_clauses.append(
                    "COALESCE(json_extract(CAST(metadata AS TEXT), '$.owner'), 'default') IN (?, 'webhook', 'default')"
                )
            params_list.append(eff_user)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        query = f"""
            SELECT thread_id,
                   json_extract(metadata, '$.agent_name') as agent_name,
                   MAX(json_extract(metadata, '$.updated_at')) as updated_at,
                   MAX(checkpoint_id) as latest_checkpoint_id,
                   MIN(json_extract(metadata, '$.updated_at')) as created_at,
                   MAX(json_extract(metadata, '$.git_branch')) as git_branch,
                   MAX(json_extract(metadata, '$.cwd')) as cwd,
                   MAX(json_extract(CAST(metadata AS TEXT), '$.owner')) as owner,
                   MAX(json_extract(CAST(metadata AS TEXT), '$.title')) as meta_title
            FROM checkpoints
            {where_sql}
            GROUP BY thread_id
            ORDER BY {order_col} DESC
            LIMIT ?
        """
        params: tuple = (*params_list, limit)

        async with conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            threads: list[ThreadInfo] = []
            for r in rows:
                t_item = ThreadInfo(
                    thread_id=r[0],
                    agent_name=r[1],
                    updated_at=r[2],
                    latest_checkpoint_id=r[3],
                    created_at=r[4],
                    git_branch=r[5],
                    cwd=r[6],
                )
                if len(r) > 7 and r[7]:
                    t_item["owner"] = str(r[7])
                    if r[7] == "webhook" and not t_item.get("cloud_badge"):
                        t_item["cloud_badge"] = "[WEBHOOK]"
                if len(r) > 8 and r[8] and not t_item.get("title"):
                    t_item["title"] = str(r[8])
                threads.append(t_item)

        if threads:
            summary_rows = await _load_thread_summaries_batch(
                conn,
                [t["thread_id"] for t in threads],
            )
            for t in threads:
                tid = t["thread_id"]
                freshness = _thread_freshness(t)
                s_row = summary_rows.get(tid)
                if s_row is not None:
                    if s_row.title:
                        t["title"] = s_row.title
                    elif not t.get("title"):
                        cached_t = _thread_title_cache.get(tid)
                        t["title"] = cached_t if cached_t else None
                    if s_row.preview and not t.get("initial_prompt"):
                        t["initial_prompt"] = s_row.preview
                        _cache_initial_prompt(tid, freshness, s_row.preview)
                    if not t.get("agent_name") and s_row.agent_name:
                        t["agent_name"] = s_row.agent_name
                    if not t.get("cwd") and s_row.cwd:
                        t["cwd"] = s_row.cwd
                    if not t.get("git_branch") and s_row.git_branch:
                        t["git_branch"] = s_row.git_branch
                    if not t.get("created_at") and s_row.created_at:
                        t["created_at"] = s_row.created_at
                    if not t.get("owner") and s_row.user_id and s_row.user_id != "default":
                        t["owner"] = s_row.user_id
                    if s_row.parent_thread_id:
                        t["parent_thread_id"] = s_row.parent_thread_id
                    if s_row.status:
                        t["status"] = s_row.status

                    # Fast Path (agy summarystore pattern): when thread_summaries is already
                    # synchronized with the latest checkpoint_id, use precomputed counts & badges
                    # directly without deserializing heavy checkpoint/write blobs.
                    if (
                        s_row.latest_checkpoint_id
                        and s_row.latest_checkpoint_id == t.get("latest_checkpoint_id")
                    ):
                        if s_row.message_count is not None and include_message_count:
                            t["message_count"] = s_row.message_count
                            _cache_message_count(tid, freshness, s_row.message_count)
                        if s_row.cloud_provider and not t.get("cloud_provider"):
                            t["cloud_provider"] = s_row.cloud_provider
                        if s_row.cloud_badge and not t.get("cloud_badge"):
                            t["cloud_badge"] = s_row.cloud_badge
                        _cache_cloud_badge(tid, freshness, t.get("cloud_badge"))
                else:
                    if not t.get("title"):
                        cached_t = _thread_title_cache.get(tid)
                        t["title"] = cached_t if cached_t else None

        if include_message_count and threads:
            await _populate_checkpoint_fields(
                conn,
                threads,
                include_message_count=True,
                include_initial_prompt=True,
            )

        return threads


async def reconcile_thread_summaries(limit: int = 200) -> int:
    """Reconcile SQLite ``thread_summaries`` with ``checkpoints`` (agy startup reconcile pattern).

    - Cleans up orphaned summary rows that have no checkpoints and no custom title.
    - Backfills missing or stale summary rows (message_count, preview, metadata) from ``checkpoints``.
    - Returns the number of active threads reconciled/verified.
    """
    from talkops.state.postgres_session import get_postgres_session_uri
    from talkops.state.remote_session import is_remote_client_mode

    if is_remote_client_mode() or get_postgres_session_uri():
        return 0

    try:
        async with _connect() as conn:
            if not await _table_exists(conn, "checkpoints"):
                return 0
            await _ensure_thread_summaries_table(conn)
            await conn.execute(
                f"""
                DELETE FROM {_THREAD_SUMMARIES_TABLE}
                WHERE COALESCE(TRIM(title), '') = ''
                  AND NOT EXISTS (
                      SELECT 1 FROM checkpoints c
                      WHERE c.thread_id = {_THREAD_SUMMARIES_TABLE}.thread_id
                  )
                """
            )
            await conn.commit()

        threads = await list_threads(limit=limit, include_message_count=True, user_id="*")
        return len(threads)
    except Exception as exc:
        logger.debug("Failed reconciling thread summaries: %s", exc)
        return 0


async def prewarm_thread_message_counts(limit: int = 50) -> None:
    """Pre-warm caches and reconcile SQLite thread_summaries in background for snappy TUI /resume."""
    try:
        await reconcile_thread_summaries(limit=limit)
    except Exception as exc:
        logger.debug("Failed prewarming thread message counts: %s", exc)


async def delete_thread(
    thread_id: str,
    *,
    user_id: str | None = None,
) -> bool:
    """Delete a thread and all associated checkpoints, writes, and summaries."""
    from talkops.state.postgres_session import PostgresSessionBackend, get_postgres_session_uri
    from talkops.state.remote_session import RemoteSessionBackend, is_remote_client_mode

    if is_remote_client_mode():
        deleted = await RemoteSessionBackend(user_id=user_id).delete_thread(thread_id)
        if deleted:
            _message_count_cache.pop(thread_id, None)
            _initial_prompt_cache.pop(thread_id, None)
            _cloud_badge_cache.pop(thread_id, None)
            _thread_title_cache.pop(thread_id, None)
        return deleted

    eff_user = get_current_user_id(user_id)
    pg_uri = get_postgres_session_uri()
    if pg_uri:
        deleted = await PostgresSessionBackend(pg_uri).delete_thread(
            thread_id, user_id=eff_user
        )
        if deleted:
            _message_count_cache.pop(thread_id, None)
            _initial_prompt_cache.pop(thread_id, None)
            _cloud_badge_cache.pop(thread_id, None)
            _thread_title_cache.pop(thread_id, None)
        return deleted

    deleted = False
    async with _connect() as conn:
        if eff_user and eff_user not in ("*", "default", "local"):
            owner: str | None = None
            if await _table_exists(conn, "checkpoints"):
                async with conn.execute(
                    "SELECT json_extract(CAST(metadata AS TEXT), '$.owner') FROM checkpoints WHERE thread_id = ? LIMIT 1",
                    (thread_id,),
                ) as cur:
                    row = await cur.fetchone()
                    if row and row[0]:
                        owner = str(row[0])
            if not owner and await _table_exists(conn, _THREAD_SUMMARIES_TABLE):
                await _ensure_thread_summaries_table(conn)
                async with conn.execute(
                    f"SELECT user_id FROM {_THREAD_SUMMARIES_TABLE} WHERE thread_id = ? LIMIT 1",
                    (thread_id,),
                ) as cur:
                    row = await cur.fetchone()
                    if row and row[0]:
                        owner = str(row[0])
            if owner and owner not in (eff_user, "default", "webhook"):
                return False

        if await _table_exists(conn, "checkpoints"):
            cursor = await conn.execute(
                "DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,)
            )
            deleted = cursor.rowcount > 0
            if await _table_exists(conn, "writes"):
                await conn.execute(
                    "DELETE FROM writes WHERE thread_id = ?", (thread_id,)
                )
        if await _table_exists(conn, _THREAD_SUMMARIES_TABLE):
            summary_cursor = await conn.execute(
                f"DELETE FROM {_THREAD_SUMMARIES_TABLE} WHERE thread_id = ?",
                (thread_id,),
            )
            deleted = deleted or (summary_cursor.rowcount > 0)
        await conn.commit()
        if deleted:
            _message_count_cache.pop(thread_id, None)
            _initial_prompt_cache.pop(thread_id, None)
            _cloud_badge_cache.pop(thread_id, None)
            _thread_title_cache.pop(thread_id, None)
    return deleted


async def thread_exists(
    thread_id: str,
    *,
    user_id: str | None = None,
) -> bool:
    """Check if thread exists in checkpoints."""
    from talkops.state.postgres_session import PostgresSessionBackend, get_postgres_session_uri
    from talkops.state.remote_session import RemoteSessionBackend, is_remote_client_mode

    if is_remote_client_mode():
        return await RemoteSessionBackend(user_id=user_id).thread_exists(thread_id)

    eff_user = get_current_user_id(user_id)
    pg_uri = get_postgres_session_uri()
    if pg_uri:
        return await PostgresSessionBackend(pg_uri).thread_exists(
            thread_id, user_id=eff_user
        )

    async with _connect() as conn:
        if not await _table_exists(conn, "checkpoints"):
            return False
        async with conn.execute(
            "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1", (thread_id,)
        ) as cursor:
            return await cursor.fetchone() is not None


async def find_similar_threads(
    thread_id_prefix: str,
    limit: int = 3,
    *,
    user_id: str | None = None,
) -> list[str]:
    """Find threads starting with or matching prefix."""
    from talkops.state.postgres_session import PostgresSessionBackend, get_postgres_session_uri
    from talkops.state.remote_session import RemoteSessionBackend, is_remote_client_mode

    if is_remote_client_mode():
        return await RemoteSessionBackend(user_id=user_id).find_similar_threads(
            thread_id_prefix, limit=limit
        )

    eff_user = get_current_user_id(user_id)
    pg_uri = get_postgres_session_uri()
    if pg_uri:
        return await PostgresSessionBackend(pg_uri).find_similar_threads(
            thread_id_prefix, limit=limit, user_id=eff_user
        )

    async with _connect() as conn:
        if not await _table_exists(conn, "checkpoints"):
            return []
        query = """
            SELECT DISTINCT thread_id FROM checkpoints
            WHERE thread_id LIKE ?
            ORDER BY checkpoint_id DESC
            LIMIT ?
        """
        async with conn.execute(query, (f"{thread_id_prefix}%", limit)) as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]


async def get_most_recent(
    agent_name: str | None = None,
    branch: str | None = None,
    cwd: str | None = None,
    *,
    user_id: str | None = None,
) -> ThreadInfo | None:
    """Return the most recently updated thread."""
    threads = await list_threads(
        agent_name=agent_name,
        limit=1,
        include_message_count=True,
        branch=branch,
        cwd=cwd,
        user_id=user_id,
    )
    return threads[0] if threads else None


from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


class TalkOpsCheckpointer(AsyncSqliteSaver):
    """Custom SQLite checkpointer implementing LangGraph API extended capabilities.

    Adheres to the official LangChain custom checkpointer specification by implementing:
    - `adelete_for_runs`: deletes checkpoints and writes associated with specific run IDs,
      enabling the multitask_strategy='rollback' capability.
    - `acopy_thread`: duplicates thread state, checkpoints, and writes for thread forking.
    - `aprune`: prunes thread checkpoint history ('keep_latest' or 'delete_all') to keep
      storage bounded for long-lived threads.
    - Automatic write-through summary indexing to ``thread_summaries`` on ``aput`` / ``aput_writes``
      following the Antigravity CLI (``agy``) ``summarystore`` architecture.
    """

    @classmethod
    @asynccontextmanager
    async def from_conn_string(
        cls: type[Self], conn_string: str
    ) -> AsyncIterator[Self]:
        """Create a new TalkOpsCheckpointer instance from a connection string."""
        import aiosqlite

        async with aiosqlite.connect(conn_string, timeout=30.0) as conn:
            await _guard_sqlite_handle(conn)
            yield cls(conn)

    async def setup(self) -> None:
        """Set up the checkpoint database and TalkOps summary store tables/indexes."""
        await super().setup()
        with contextlib.suppress(Exception):
            await _ensure_thread_summaries_table(self.conn)
            await _ensure_threads_list_index(self.conn)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any,
    ) -> RunnableConfig:
        """Save a checkpoint and synchronously update the indexed ``thread_summaries`` row."""
        saved_config = await super().aput(config, checkpoint, metadata, new_versions)
        configurable = config.get("configurable") if isinstance(config, dict) else None
        if isinstance(configurable, dict):
            checkpoint_ns = str(configurable.get("checkpoint_ns") or "")
            thread_id = str(configurable.get("thread_id") or "")
            if thread_id and checkpoint_ns == "":
                with contextlib.suppress(Exception):
                    saved_cfg_inner = (
                        saved_config.get("configurable")
                        if isinstance(saved_config, dict)
                        else None
                    )
                    cp_id = (
                        str(saved_cfg_inner.get("checkpoint_id"))
                        if isinstance(saved_cfg_inner, dict) and saved_cfg_inner.get("checkpoint_id")
                        else (
                            str(checkpoint.get("id"))
                            if isinstance(checkpoint, dict) and checkpoint.get("id")
                            else None
                        )
                    )
                    meta_dict = metadata if isinstance(metadata, dict) else {}
                    updated_at = str(
                        meta_dict.get("updated_at")
                        or (checkpoint.get("ts") if isinstance(checkpoint, dict) else "")
                        or datetime.now().astimezone().isoformat()
                    )
                    agent_name = str(meta_dict["agent_name"]) if meta_dict.get("agent_name") else None
                    cwd = str(meta_dict["cwd"]) if meta_dict.get("cwd") else None
                    git_branch = str(meta_dict["git_branch"]) if meta_dict.get("git_branch") else None
                    owner = str(
                        meta_dict.get("owner")
                        or meta_dict.get("user_id")
                        or get_current_user_id()
                    )
                    meta_title = str(meta_dict["title"]) if meta_dict.get("title") else None

                    summary = _summarize_checkpoint(checkpoint)
                    msg_cnt = (
                        summary.message_count
                        if (
                            summary.message_count is not None
                            and (summary.message_count > 0 or summary.initial_prompt)
                        )
                        else None
                    )
                    channel_tag = (
                        f"[{str(meta_dict['channel']).upper()}]"
                        if meta_dict.get("channel")
                        else ("[WEBHOOK]" if owner == "webhook" else None)
                    )
                    badge = summary.cloud_badge or channel_tag

                    async with self.lock:
                        await _upsert_thread_summary_on_conn(
                            self.conn,
                            thread_id,
                            user_id=owner,
                            title=meta_title,
                            preview=summary.initial_prompt,
                            agent_name=agent_name,
                            cwd=cwd,
                            git_branch=git_branch,
                            created_at=updated_at,
                            updated_at=updated_at,
                            latest_checkpoint_id=cp_id,
                            message_count=msg_cnt,
                            cloud_provider=summary.cloud_provider,
                            cloud_badge=badge,
                            status="idle",
                        )
                        await self.conn.commit()

                    freshness = cp_id or updated_at
                    if msg_cnt is not None:
                        _cache_message_count(thread_id, freshness, msg_cnt)
                    if summary.initial_prompt:
                        _cache_initial_prompt(thread_id, freshness, summary.initial_prompt)
                    _cache_cloud_badge(thread_id, freshness, badge)
                    _recent_threads_cache.clear()
        return saved_config

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Store intermediate writes and keep ``thread_summaries`` status/counts consistent."""
        await super().aput_writes(config, writes, task_id, task_path)
        configurable = config.get("configurable") if isinstance(config, dict) else None
        if isinstance(configurable, dict):
            checkpoint_ns = str(configurable.get("checkpoint_ns") or "")
            thread_id = str(configurable.get("thread_id") or "")
            if thread_id and checkpoint_ns == "":
                has_messages = any(ch == "messages" for ch, _ in writes)
                has_interrupt = any(ch == "__interrupt__" for ch, _ in writes)
                if has_messages or has_interrupt:
                    with contextlib.suppress(Exception):
                        if has_messages:
                            _message_count_cache.pop(thread_id, None)
                        async with self.lock:
                            if await _table_exists(self.conn, _THREAD_SUMMARIES_TABLE):
                                await _ensure_thread_summaries_table(self.conn)
                                if has_messages and has_interrupt:
                                    await self.conn.execute(
                                        f"UPDATE {_THREAD_SUMMARIES_TABLE} "
                                        f"SET message_count = NULL, status = 'interrupted' "
                                        f"WHERE thread_id = ?",
                                        (thread_id,),
                                    )
                                elif has_messages:
                                    await self.conn.execute(
                                        f"UPDATE {_THREAD_SUMMARIES_TABLE} "
                                        f"SET message_count = NULL "
                                        f"WHERE thread_id = ?",
                                        (thread_id,),
                                    )
                                elif has_interrupt:
                                    await self.conn.execute(
                                        f"UPDATE {_THREAD_SUMMARIES_TABLE} "
                                        f"SET status = 'interrupted' "
                                        f"WHERE thread_id = ?",
                                        (thread_id,),
                                    )
                                await self.conn.commit()

    async def adelete_thread(self, thread_id: str) -> None:
        """Delete all checkpoints, writes, and summary records for ``thread_id``."""
        await super().adelete_thread(thread_id)
        with contextlib.suppress(Exception):
            async with self.lock:
                if await _table_exists(self.conn, _THREAD_SUMMARIES_TABLE):
                    await self.conn.execute(
                        f"DELETE FROM {_THREAD_SUMMARIES_TABLE} WHERE thread_id = ?",
                        (thread_id,),
                    )
                    await self.conn.commit()
        _message_count_cache.pop(thread_id, None)
        _initial_prompt_cache.pop(thread_id, None)
        _cloud_badge_cache.pop(thread_id, None)
        _thread_title_cache.pop(thread_id, None)
        _recent_threads_cache.clear()

    async def aget_iter(self, config: RunnableConfig) -> AsyncIterator[CheckpointTuple]:
        """Asynchronously iterate checkpoints matching config."""
        async def gen() -> AsyncIterator[CheckpointTuple]:
            tup = await self.aget_tuple(config)
            if tup is not None:
                yield tup

        return gen()

    async def adelete_for_runs(self, run_ids: Sequence[str] | Iterable[str]) -> None:
        """Asynchronously delete all checkpoints and writes associated with the given run IDs.

        Enables the LangGraph API multitask_strategy='rollback' to clean up
        checkpoints and intermediate writes when a run is cancelled.
        """
        run_id_list = [r for r in run_ids if r]
        if not run_id_list:
            return
        await self.setup()
        affected_threads: set[str] = set()
        async with self.lock, self.conn.cursor() as cur:
            for r_id in run_id_list:
                await cur.execute(
                    "SELECT thread_id, checkpoint_ns, checkpoint_id FROM checkpoints "
                    "WHERE json_extract(CAST(metadata AS TEXT), '$.run_id') = ?",
                    (r_id,),
                )
                cps = await cur.fetchall()
                for tid, c_ns, cid in cps:
                    affected_threads.add(str(tid))
                    await cur.execute(
                        "DELETE FROM writes WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?",
                        (tid, c_ns, cid),
                    )
                    await cur.execute(
                        "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?",
                        (tid, c_ns, cid),
                    )
            if affected_threads and await _table_exists(self.conn, _THREAD_SUMMARIES_TABLE):
                for tid in affected_threads:
                    _message_count_cache.pop(tid, None)
                    await cur.execute(
                        f"UPDATE {_THREAD_SUMMARIES_TABLE} "
                        f"SET latest_checkpoint_id = NULL, message_count = NULL "
                        f"WHERE thread_id = ?",
                        (tid,),
                    )
            await self.conn.commit()

    async def acopy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        """Asynchronously copy all checkpoints and writes from one thread to another.

        Supports LangGraph API POST /threads/<id>/copy with fast SQLite bulk duplication.
        """
        source_id = source_thread_id
        target_id = target_thread_id
        await self.setup()
        async with self.lock, self.conn.cursor() as cur:
            await cur.execute(
                "SELECT checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata "
                "FROM checkpoints WHERE thread_id = ?",
                (source_id,),
            )
            rows = await cur.fetchall()
            for checkpoint_ns, checkpoint_id, parent_checkpoint_id, cp_type, checkpoint_blob, metadata_blob in rows:
                updated_metadata_blob = metadata_blob
                if metadata_blob:
                    try:
                        meta_dict = json.loads(metadata_blob.decode("utf-8", "ignore"))
                        if "thread_id" in meta_dict:
                            meta_dict["thread_id"] = target_id
                        updated_metadata_blob = json.dumps(meta_dict, ensure_ascii=False).encode("utf-8", "ignore")
                    except Exception:
                        pass
                await cur.execute(
                    "INSERT OR REPLACE INTO checkpoints "
                    "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (target_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, cp_type, checkpoint_blob, updated_metadata_blob),
                )
            await cur.execute(
                "SELECT checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value "
                "FROM writes WHERE thread_id = ?",
                (source_id,),
            )
            write_rows = await cur.fetchall()
            for checkpoint_ns, checkpoint_id, task_id, idx, channel, w_type, value_blob in write_rows:
                await cur.execute(
                    "INSERT OR REPLACE INTO writes "
                    "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (target_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, w_type, value_blob),
                )
            if await _table_exists(self.conn, _THREAD_SUMMARIES_TABLE):
                await _ensure_thread_summaries_table(self.conn)
                await cur.execute(
                    f"""
                    SELECT user_id, title, preview, agent_name, cwd, git_branch,
                           created_at, updated_at, latest_checkpoint_id, message_count,
                           cloud_provider, cloud_badge, status
                    FROM {_THREAD_SUMMARIES_TABLE}
                    WHERE thread_id = ?
                    """,
                    (source_id,),
                )
                summary_row = await cur.fetchone()
                if summary_row:
                    await cur.execute(
                        f"""
                        INSERT OR REPLACE INTO {_THREAD_SUMMARIES_TABLE} (
                            thread_id, user_id, title, preview, agent_name, cwd, git_branch,
                            created_at, updated_at, latest_checkpoint_id, message_count,
                            cloud_provider, cloud_badge, parent_thread_id, status
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            target_id,
                            summary_row[0] or "default",
                            summary_row[1] or "",
                            summary_row[2] or "",
                            summary_row[3],
                            summary_row[4],
                            summary_row[5],
                            summary_row[6],
                            summary_row[7] or "",
                            summary_row[8],
                            summary_row[9],
                            summary_row[10],
                            summary_row[11],
                            source_id,
                            summary_row[12] or "idle",
                        ),
                    )
                    if isinstance(summary_row[1], str) and summary_row[1].strip():
                        _thread_title_cache[target_id] = summary_row[1].strip()
            await self.conn.commit()

    async def aprune(
        self,
        thread_ids: Sequence[str] | None = None,
        *,
        strategy: str = "keep_latest",
        prune_subagents: bool = True,
        turn_completed: bool = False,
    ) -> None:
        """Asynchronously compact thread checkpoint history (destructive compaction).

        WARNING (IA-04): 'keep_latest' is a destructive history compaction strategy designed
        to bound database storage. Older checkpoints and intermediate writes are deleted.
        Non-terminal checkpoints with pending human-in-the-loop interrupts (__interrupt__)
        are explicitly preserved to safeguard thread resumability and approvals.
        """
        if strategy in ("delete_all", "delete"):
            if thread_ids is not None:
                for tid in thread_ids:
                    await self.adelete_thread(tid)
                    if await _table_exists(self.conn, _THREAD_SUMMARIES_TABLE):
                        async with self.lock, self.conn.cursor() as cur:
                            await cur.execute(
                                f"DELETE FROM {_THREAD_SUMMARIES_TABLE} WHERE thread_id = ?",
                                (tid,),
                            )
                            await self.conn.commit()
                    _thread_title_cache.pop(tid, None)
            else:
                await self.setup()
                async with self.lock, self.conn.cursor() as cur:
                    await cur.execute("DELETE FROM writes")
                    await cur.execute("DELETE FROM checkpoints")
                    if await _table_exists(self.conn, _THREAD_SUMMARIES_TABLE):
                        await cur.execute(f"DELETE FROM {_THREAD_SUMMARIES_TABLE}")
                    await self.conn.commit()
                _thread_title_cache.clear()
            return
        if strategy != "keep_latest":
            raise ValueError(f"Unknown prune strategy: {strategy}")

        await self.setup()
        async with self.lock, self.conn.cursor() as cur:
            tids = list(thread_ids) if thread_ids is not None else None
            if tids is None:
                await cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
                tids = [row[0] for row in await cur.fetchall()]

            preserved_interrupts_by_thread: dict[str, set[tuple[str, str]]] = {tid: set() for tid in tids}
            if not turn_completed:
                for tid in tids:
                    await cur.execute(
                        """
                        SELECT DISTINCT w1.checkpoint_ns, w1.checkpoint_id
                        FROM writes w1
                        WHERE w1.thread_id = ?
                          AND w1.channel = '__interrupt__'
                          AND NOT EXISTS (
                              SELECT 1 FROM writes w2
                              WHERE w2.thread_id = w1.thread_id
                                AND w2.checkpoint_ns = w1.checkpoint_ns
                                AND w2.checkpoint_id = w1.checkpoint_id
                                AND (
                                    w2.channel = '__resume__'
                                    OR w2.channel LIKE 'branch:to:%'
                                    OR w2.channel = 'jump_to'
                                )
                          )
                        """,
                        (tid,),
                    )
                    for ns, cid in await cur.fetchall():
                        if cid:
                            preserved_interrupts_by_thread[tid].add((ns or "", cid))

            if prune_subagents:
                for tid in tids:
                    await cur.execute(
                        "SELECT DISTINCT checkpoint_ns, checkpoint_id FROM checkpoints "
                        "WHERE thread_id = ? AND checkpoint_ns != ''",
                        (tid,),
                    )
                    sub_rows = await cur.fetchall()
                    for sub_ns, sub_cid in sub_rows:
                        if (sub_ns or "", sub_cid) in preserved_interrupts_by_thread[tid]:
                            continue
                        await cur.execute(
                            "DELETE FROM writes WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?",
                            (tid, sub_ns, sub_cid),
                        )
                        await cur.execute(
                            "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?",
                            (tid, sub_ns, sub_cid),
                        )

            for tid in tids:
                await cur.execute(
                    "SELECT checkpoint_ns, MAX(checkpoint_id) FROM checkpoints "
                    "WHERE thread_id = ? GROUP BY checkpoint_ns",
                    (tid,),
                )
                latest_rows = await cur.fetchall()
                for checkpoint_ns, latest_cid in latest_rows:
                    if latest_cid is None:
                        continue
                    ns_key = checkpoint_ns or ""
                    cids_to_keep = {latest_cid}
                    for p_ns, p_cid in preserved_interrupts_by_thread[tid]:
                        if p_ns == ns_key:
                            cids_to_keep.add(p_cid)

                    placeholders = ",".join("?" for _ in cids_to_keep)
                    await cur.execute(
                        f"DELETE FROM writes WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id NOT IN ({placeholders})",
                        (tid, checkpoint_ns, *cids_to_keep),
                    )
                    await cur.execute(
                        f"DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id NOT IN ({placeholders})",
                        (tid, checkpoint_ns, *cids_to_keep),
                    )

                await cur.execute(
                    """
                    DELETE FROM writes
                    WHERE thread_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM checkpoints c
                          WHERE c.thread_id = writes.thread_id
                            AND c.checkpoint_ns = writes.checkpoint_ns
                            AND c.checkpoint_id = writes.checkpoint_id
                      )
                    """,
                    (tid,),
                )
            await self.conn.commit()

    async def avacuum(self) -> None:
        """Run VACUUM to reclaim deleted page space."""
        await self.setup()
        async with self.lock:
            await self.conn.commit()
            with contextlib.suppress(Exception):
                await self.conn.execute("VACUUM")


@asynccontextmanager
async def get_checkpointer() -> AsyncGenerator[Any, None]:
    """Provide a checkpointer connected to PostgreSQL (in Server Mode) or SQLite (in Local Mode)."""
    from talkops.state.postgres_session import (
        TalkOpsPostgresCheckpointer,
        get_postgres_session_uri,
    )

    pg_uri = get_postgres_session_uri()
    if pg_uri:
        async with TalkOpsPostgresCheckpointer.from_conn_string(
            pg_uri, use_pool=False
        ) as pg_saver:
            yield pg_saver
        return

    _patch_aiosqlite()
    db_path = get_db_path()
    async with TalkOpsCheckpointer.from_conn_string(str(db_path)) as saver:
        await _guard_sqlite_handle(saver.conn)
        yield saver


@asynccontextmanager
async def create_store() -> AsyncGenerator[Any, None]:
    """Provide a LangGraph BaseStore connected to PostgreSQL (Server Mode) or InMemoryStore (Local Mode)."""
    from talkops.state.postgres_session import (
        create_postgres_store,
        get_postgres_session_uri,
    )

    pg_uri = get_postgres_session_uri()
    if pg_uri:
        async with create_postgres_store(pg_uri) as pg_store:
            yield pg_store
        return

    from langgraph.store.memory import InMemoryStore

    yield InMemoryStore()


class SessionManager:
    """High-level facade for thread session lifecycle in TalkOps."""

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is not None:
            set_db_path(db_path)
        self.db_path = get_db_path()

    @staticmethod
    def get_db_path() -> Path:
        return get_db_path()

    @staticmethod
    async def list_threads(
        limit: int = 50,
        agent_name: str | None = None,
        include_message_count: bool = True,
        *,
        user_id: str | None = None,
    ) -> list[ThreadInfo]:
        return await list_threads(
            limit=limit,
            agent_name=agent_name,
            include_message_count=include_message_count,
            user_id=user_id,
        )

    @staticmethod
    async def get_thread_title(
        thread_id: str,
        *,
        user_id: str | None = None,
    ) -> str | None:
        return await get_thread_title(thread_id, user_id=user_id)

    @staticmethod
    async def set_thread_title(
        thread_id: str,
        title: str,
        *,
        preview: str | None = None,
        only_if_empty: bool = True,
        user_id: str | None = None,
    ) -> bool:
        return await set_thread_title(
            thread_id,
            title,
            preview=preview,
            only_if_empty=only_if_empty,
            user_id=user_id,
        )

    @staticmethod
    async def delete_thread(
        thread_id: str,
        *,
        user_id: str | None = None,
    ) -> bool:
        return await delete_thread(thread_id, user_id=user_id)

    @staticmethod
    async def get_most_recent(
        agent_name: str | None = None,
        *,
        user_id: str | None = None,
    ) -> ThreadInfo | None:
        return await get_most_recent(agent_name=agent_name, user_id=user_id)

    @staticmethod
    async def thread_exists(
        thread_id: str,
        *,
        user_id: str | None = None,
    ) -> bool:
        return await thread_exists(thread_id, user_id=user_id)

    @staticmethod
    async def find_similar_threads(
        prefix: str,
        limit: int = 3,
        *,
        user_id: str | None = None,
    ) -> list[str]:
        return await find_similar_threads(prefix, limit=limit, user_id=user_id)

    @staticmethod
    def get_checkpointer() -> Any:
        return get_checkpointer()

    @staticmethod
    async def prune_thread(
        thread_id: str,
        *,
        strategy: str = "keep_latest",
        turn_completed: bool = False,
    ) -> None:
        """Prune intermediate history and subagent checkpoints for a thread."""
        from talkops.state.remote_session import RemoteSessionBackend, is_remote_client_mode

        if is_remote_client_mode():
            await RemoteSessionBackend().prune_thread(
                thread_id, strategy=strategy, turn_completed=turn_completed
            )
            return

        try:
            async with get_checkpointer() as saver:
                await saver.aprune(
                    [thread_id],
                    strategy=strategy,
                    prune_subagents=True,
                    turn_completed=turn_completed,
                )
        except Exception:
            logger.debug("Failed to prune thread %s", thread_id, exc_info=True)

    @staticmethod
    async def reconcile_summaries(limit: int = 200) -> int:
        """Reconcile SQLite ``thread_summaries`` with ``checkpoints`` (agy startup reconcile)."""
        return await reconcile_thread_summaries(limit=limit)

    @staticmethod
    async def maintain_database(db_path: Path | None = None, *, size_threshold_mb: float = 20.0) -> dict[str, Any]:
        """Compact obsolete checkpoints, flush WAL pages, and vacuum database if size exceeds threshold (IA-12/DV-08)."""
        from talkops.state.postgres_session import get_postgres_session_uri
        from talkops.state.remote_session import is_remote_client_mode

        if is_remote_client_mode() or get_postgres_session_uri():
            return {"performed": False, "reason": "remote_or_postgres_mode"}

        try:
            target_path = db_path or get_db_path()
            if not target_path.exists():
                return {"performed": False, "reason": "db_not_found"}

            total_bytes = target_path.stat().st_size
            wal_path = target_path.with_name(target_path.name + "-wal")
            shm_path = target_path.with_name(target_path.name + "-shm")
            if wal_path.exists():
                total_bytes += wal_path.stat().st_size
            if shm_path.exists():
                total_bytes += shm_path.stat().st_size

            size_mb = total_bytes / (1024 * 1024)
            if size_mb < size_threshold_mb:
                return {"performed": False, "reason": "below_threshold", "initial_mb": size_mb}

            lock_file = target_path.with_name(target_path.name + ".maint_lock")
            lock_fp = None
            if sys.platform != "win32":
                try:
                    import fcntl

                    lock_fp = open(lock_file, "w")
                    fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError):
                    logger.debug("Database maintenance lease held by another process; skipping")
                    if lock_fp:
                        lock_fp.close()
                    return {"performed": False, "reason": "lease_busy", "initial_mb": size_mb}

            try:
                logger.info("Maintaining sessions database (current size: %.1f MB)", size_mb)
                async with get_checkpointer() as saver:
                    with contextlib.suppress(Exception):
                        await saver.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    await saver.aprune(None, strategy="keep_latest", prune_subagents=True)
                    await saver.avacuum()
                    with contextlib.suppress(Exception):
                        await saver.conn.execute("PRAGMA optimize")
                        await saver.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                with contextlib.suppress(Exception):
                    await reconcile_thread_summaries(limit=200)

                new_total = target_path.stat().st_size
                if wal_path.exists():
                    new_total += wal_path.stat().st_size
                if shm_path.exists():
                    new_total += shm_path.stat().st_size
                new_size_mb = new_total / (1024 * 1024)
                logger.info("Sessions database maintenance complete: %.1f MB -> %.1f MB", size_mb, new_size_mb)
                return {
                    "performed": True,
                    "reason": "completed",
                    "initial_mb": size_mb,
                    "final_mb": new_size_mb,
                }
            finally:
                if lock_fp:
                    with contextlib.suppress(Exception):
                        import fcntl

                        fcntl.flock(lock_fp, fcntl.LOCK_UN)
                        lock_fp.close()
        except Exception as exc:
            logger.warning("Database maintenance encountered an error", exc_info=True)
            return {"performed": False, "reason": "error", "error": str(exc)}

    @staticmethod
    async def fork_thread(
        source_thread_id: str,
        target_thread_id: str | None = None,
        *,
        user_id: str | None = None,
    ) -> str:
        """Fork a conversation thread by copying all checkpoints and writes into a new thread ID."""
        return await fork_thread(
            source_thread_id,
            target_thread_id=target_thread_id,
            user_id=user_id,
        )


async def fork_thread(
    source_thread_id: str,
    target_thread_id: str | None = None,
    *,
    user_id: str | None = None,
) -> str:
    """Copy all checkpoints and delta writes from ``source_thread_id`` into a new thread ID."""
    from talkops.state.remote_session import RemoteSessionBackend, is_remote_client_mode

    if is_remote_client_mode():
        return await RemoteSessionBackend(user_id=user_id).fork_thread(
            source_thread_id, target_thread_id=target_thread_id
        )

    target_id = target_thread_id or generate_thread_id()
    try:
        async with get_checkpointer() as saver:
            await saver.acopy_thread(source_thread_id, target_id)
        _recent_threads_cache.clear()
    except Exception:
        logger.debug(
            "Checkpointer copy skipped or failed for %s -> %s",
            source_thread_id,
            target_id,
            exc_info=True,
        )
    return target_id


__all__ = [
    "TalkOpsCheckpointer",
    "SessionManager",
    "ThreadInfo",
    "bind_user_context",
    "create_store",
    "delete_thread",
    "find_similar_threads",
    "fork_thread",
    "format_relative_timestamp",
    "format_timestamp",
    "generate_thread_id",
    "get_checkpointer",
    "get_current_user_id",
    "get_db_path",
    "get_most_recent",
    "get_thread_title",
    "list_threads",
    "prewarm_thread_message_counts",
    "reconcile_thread_summaries",
    "set_current_user_id",
    "set_db_path",
    "set_thread_title",
    "thread_exists",
    "upsert_thread_summary",
]

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

"""PostgreSQL checkpointer, store, and session backend for TalkOps Central Server Mode.

Implements:
- `TalkOpsPostgresCheckpointer(AsyncPostgresSaver)`: Extends official LangGraph PostgreSQL
  checkpointer with `acopy_thread`, `adelete_for_runs`, extended `aprune`, and the
  `thread_summaries` table for multi-user thread title/preview persistence.
- `create_postgres_store`: Async context manager yielding an initialized `AsyncPostgresStore`.
- `PostgresSessionBackend`: Multi-user aware session query backend (`list_threads`,
  `get_thread_title`, `set_thread_title`, `delete_thread`, `fork_thread`,
  `find_similar_threads`, `thread_exists`, `get_most_recent`).
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncGenerator, AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any, Self, cast
from urllib.parse import quote_plus

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from talkops.utils.logger import get_logger

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import CheckpointTuple

    from talkops.state.session import ThreadInfo

logger = get_logger(__name__)

_THREAD_SUMMARIES_TABLE = "thread_summaries"
_WEBHOOK_THREAD_MAPPINGS_TABLE = "webhook_thread_mappings"


def get_postgres_session_uri() -> str | None:
    """Resolve the active PostgreSQL connection URI from environment variables.

    Checks full URI variables first (`TALKOPS_POSTGRES_URI`, `TALKOPS_SERVER_POSTGRES_URI`,
    `POSTGRES_URI`, `DATABASE_URL`), and falls back to discrete variables
    (`TALKOPS_POSTGRES_HOST` / `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`,
    `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_SSLMODE`).
    """
    for env_key in (
        "TALKOPS_POSTGRES_URI",
        "TALKOPS_SERVER_POSTGRES_URI",
        "POSTGRES_URI",
        "DATABASE_URL",
    ):
        val = os.environ.get(env_key, "").strip()
        if val and val.startswith(("postgres://", "postgresql://")):
            return val

    host = (
        os.environ.get("TALKOPS_POSTGRES_HOST")
        or os.environ.get("POSTGRES_HOST")
        or ""
    ).strip()
    if host:
        port = (
            os.environ.get("TALKOPS_POSTGRES_PORT")
            or os.environ.get("POSTGRES_PORT")
            or "5432"
        ).strip()
        user = (
            os.environ.get("TALKOPS_POSTGRES_USER")
            or os.environ.get("POSTGRES_USER")
            or "postgres"
        ).strip()
        password = (
            os.environ.get("TALKOPS_POSTGRES_PASSWORD")
            or os.environ.get("POSTGRES_PASSWORD")
            or ""
        )
        dbname = (
            os.environ.get("TALKOPS_POSTGRES_DB")
            or os.environ.get("POSTGRES_DB")
            or "talkops"
        ).strip()
        sslmode = (
            os.environ.get("TALKOPS_POSTGRES_SSLMODE")
            or os.environ.get("POSTGRES_SSLMODE")
            or ""
        ).strip()

        auth_part = f"{quote_plus(user)}:{quote_plus(password)}@" if password else f"{quote_plus(user)}@"
        query_part = f"?sslmode={quote_plus(sslmode)}" if sslmode else ""
        return f"postgresql://{auth_part}{host}:{port}/{quote_plus(dbname)}{query_part}"

    return None


async def _ensure_pg_talkops_tables(cur: Any) -> None:
    """Ensure TalkOps auxiliary tables (`thread_summaries`, `webhook_thread_mappings`) exist in Postgres."""
    await cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_THREAD_SUMMARIES_TABLE} (
            thread_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'default',
            title TEXT NOT NULL DEFAULT '',
            preview TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    with contextlib.suppress(Exception):
        await cur.execute(
            f"ALTER TABLE {_THREAD_SUMMARIES_TABLE} ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT 'default'"
        )
    await cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_talkops_pg_thread_summaries_user
        ON {_THREAD_SUMMARIES_TABLE} (user_id, updated_at DESC)
        """
    )
    await cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_WEBHOOK_THREAD_MAPPINGS_TABLE} (
            mapping_key TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            external_id TEXT NOT NULL,
            lg_thread_id TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )


class TalkOpsPostgresCheckpointer(AsyncPostgresSaver):
    """PostgreSQL checkpointer implementing LangGraph extended server lifecycle methods.

    Extends `AsyncPostgresSaver` with:
    - `adelete_for_runs`: Deletes checkpoints, blobs, and writes associated with run IDs.
    - `acopy_thread`: Duplicates checkpoints, blobs, writes, and `thread_summaries` for `/fork`.
    - `aprune`: Compacts checkpoint history while accepting `prune_subagents` and `turn_completed`.
    """

    _talkops_tables_ready: bool = False

    @classmethod
    @asynccontextmanager
    async def from_conn_string(
        cls: type[Self],
        conn_string: str,
        *,
        pipeline: bool = False,
        serde: Any | None = None,
        use_pool: bool = True,
        min_size: int = 1,
        max_size: int = 10,
    ) -> AsyncIterator[Self]:
        """Create an `TalkOpsPostgresCheckpointer` from a PostgreSQL connection string."""
        if use_pool and not pipeline:
            async with AsyncConnectionPool(
                conn_string,
                min_size=min_size,
                max_size=max_size,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                },
            ) as pool:
                saver = cls(conn=cast("Any", pool), serde=serde)
                await saver.setup()
                yield saver
        else:
            async with await AsyncConnection.connect(
                conn_string,
                autocommit=True,
                prepare_threshold=0,
                row_factory=cast("Any", dict_row),
            ) as conn:
                if pipeline:
                    async with conn.pipeline() as pipe:
                        saver = cls(conn=cast("Any", conn), pipe=pipe, serde=serde)
                        await saver.setup()
                        yield saver
                else:
                    saver = cls(conn=cast("Any", conn), serde=serde)
                    await saver.setup()
                    yield saver

    async def setup(self) -> None:
        """Set up core LangGraph Postgres checkpoint tables plus TalkOps auxiliary tables."""
        await super().setup()
        if not self._talkops_tables_ready:
            async with self._cursor() as cur:
                await _ensure_pg_talkops_tables(cur)
            self._talkops_tables_ready = True

    async def aget_iter(self, config: RunnableConfig) -> AsyncIterator[CheckpointTuple]:
        """Asynchronously iterate checkpoints matching config."""
        async def gen() -> AsyncIterator[CheckpointTuple]:
            """Execute gen."""
            tup = await self.aget_tuple(config)
            if tup is not None:
                yield tup

        return gen()

    async def adelete_for_runs(self, run_ids: Sequence[str] | Iterable[str]) -> None:
        """Delete checkpoints and writes associated with the specified `run_ids`."""
        run_id_list = [str(r) for r in run_ids if r]
        if not run_id_list:
            return
        await self.setup()
        async with self._cursor() as cur:
            await cur.execute(
                """
                SELECT thread_id, checkpoint_ns, checkpoint_id
                FROM checkpoints
                WHERE metadata ->> 'run_id' = ANY(%s)
                """,
                (run_id_list,),
            )
            rows = await cur.fetchall()
            for row in rows:
                tid = row["thread_id"]
                c_ns = row["checkpoint_ns"]
                cid = row["checkpoint_id"]
                await cur.execute(
                    "DELETE FROM checkpoint_writes WHERE thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s",
                    (tid, c_ns, cid),
                )
                await cur.execute(
                    "DELETE FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s",
                    (tid, c_ns, cid),
                )

    async def acopy_thread(
        self,
        source_thread_id: str,
        target_thread_id: str,
        *,
        user_id: str | None = None,
    ) -> None:
        """Copy all checkpoints, blobs, writes, and summary metadata from `source_thread_id` to `target_thread_id`."""
        await self.setup()
        async with self._cursor() as cur:
            # 1. Copy checkpoints
            await cur.execute(
                """
                SELECT checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata
                FROM checkpoints
                WHERE thread_id = %s
                """,
                (source_thread_id,),
            )
            cp_rows = await cur.fetchall()
            for row in cp_rows:
                meta = dict(row["metadata"]) if isinstance(row["metadata"], dict) else {}
                if "thread_id" in meta:
                    meta["thread_id"] = target_thread_id
                if user_id and user_id != "default":
                    meta["owner"] = user_id
                cp_json = (
                    json.dumps(row["checkpoint"])
                    if isinstance(row["checkpoint"], dict)
                    else str(row["checkpoint"])
                )
                meta_json = json.dumps(meta)
                await cur.execute(
                    """
                    INSERT INTO checkpoints
                        (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                    ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id) DO UPDATE SET
                        parent_checkpoint_id = EXCLUDED.parent_checkpoint_id,
                        type = EXCLUDED.type,
                        checkpoint = EXCLUDED.checkpoint,
                        metadata = EXCLUDED.metadata
                    """,
                    (
                        target_thread_id,
                        row["checkpoint_ns"],
                        row["checkpoint_id"],
                        row["parent_checkpoint_id"],
                        row["type"],
                        cp_json,
                        meta_json,
                    ),
                )

            # 2. Copy checkpoint_blobs
            await cur.execute(
                """
                INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob)
                SELECT %s, checkpoint_ns, channel, version, type, blob
                FROM checkpoint_blobs
                WHERE thread_id = %s
                ON CONFLICT (thread_id, checkpoint_ns, channel, version) DO UPDATE SET
                    type = EXCLUDED.type,
                    blob = EXCLUDED.blob
                """,
                (target_thread_id, source_thread_id),
            )

            # 3. Copy checkpoint_writes
            await cur.execute(
                """
                INSERT INTO checkpoint_writes
                    (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob, task_path)
                SELECT %s, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob, task_path
                FROM checkpoint_writes
                WHERE thread_id = %s
                ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id, task_id, idx) DO UPDATE SET
                    channel = EXCLUDED.channel,
                    type = EXCLUDED.type,
                    blob = EXCLUDED.blob,
                    task_path = EXCLUDED.task_path
                """,
                (target_thread_id, source_thread_id),
            )

            # 4. Copy thread_summaries
            await cur.execute(
                f"SELECT user_id, title, preview, updated_at FROM {_THREAD_SUMMARIES_TABLE} WHERE thread_id = %s",
                (source_thread_id,),
            )
            summary_row = await cur.fetchone()
            if summary_row:
                effective_user = user_id or summary_row.get("user_id") or "default"
                await cur.execute(
                    f"""
                    INSERT INTO {_THREAD_SUMMARIES_TABLE} (thread_id, user_id, title, preview, updated_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (thread_id) DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        title = EXCLUDED.title,
                        preview = EXCLUDED.preview,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        target_thread_id,
                        effective_user,
                        summary_row.get("title") or "",
                        summary_row.get("preview") or "",
                        summary_row.get("updated_at") or datetime.now().astimezone().isoformat(),
                    ),
                )

    async def aprune(
        self,
        thread_ids: Sequence[str] | None = None,
        *,
        strategy: str = "keep_latest",
        prune_subagents: bool = True,
        turn_completed: bool = False,
    ) -> None:
        """Compact checkpoint history in PostgreSQL while preserving pending HITL interrupts."""
        await self.setup()
        if strategy in ("delete_all", "delete"):
            if thread_ids is not None:
                tids = [str(t) for t in thread_ids if t]
                if not tids:
                    return
                for tid in tids:
                    await self.adelete_thread(tid)
                async with self._cursor() as cur:
                    await cur.execute(
                        f"DELETE FROM {_THREAD_SUMMARIES_TABLE} WHERE thread_id = ANY(%s)",
                        (tids,),
                    )
            else:
                async with self._cursor() as cur:
                    await cur.execute("DELETE FROM checkpoint_writes")
                    await cur.execute("DELETE FROM checkpoint_blobs")
                    await cur.execute("DELETE FROM checkpoints")
                    await cur.execute(f"DELETE FROM {_THREAD_SUMMARIES_TABLE}")
            return

        if strategy != "keep_latest":
            raise ValueError(f"Unknown prune strategy: {strategy!r}")

        async with self._cursor() as cur:
            if thread_ids is None:
                await cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
                rows = await cur.fetchall()
                tids = [str(r["thread_id"]) for r in rows]
            else:
                tids = [str(t) for t in thread_ids if t]

            if not tids:
                return

            for tid in tids:
                preserved_cids: dict[str, set[str]] = {}
                if not turn_completed:
                    await cur.execute(
                        """
                        SELECT DISTINCT w1.checkpoint_ns, w1.checkpoint_id
                        FROM checkpoint_writes w1
                        WHERE w1.thread_id = %s
                          AND w1.channel = '__interrupt__'
                          AND NOT EXISTS (
                              SELECT 1 FROM checkpoint_writes w2
                              WHERE w2.thread_id = w1.thread_id
                                AND w2.checkpoint_ns = w1.checkpoint_ns
                                AND w2.checkpoint_id = w1.checkpoint_id
                                AND (
                                    w2.channel = '__resume__'
                                    OR w2.channel LIKE 'branch:to:%%'
                                    OR w2.channel = 'jump_to'
                                )
                          )
                        """,
                        (tid,),
                    )
                    for row in await cur.fetchall():
                        ns = row["checkpoint_ns"] or ""
                        cid = row["checkpoint_id"]
                        if cid:
                            preserved_cids.setdefault(ns, set()).add(cid)

                if prune_subagents:
                    await cur.execute(
                        """
                        SELECT DISTINCT checkpoint_ns, checkpoint_id
                        FROM checkpoints
                        WHERE thread_id = %s AND checkpoint_ns != ''
                        """,
                        (tid,),
                    )
                    for row in await cur.fetchall():
                        sub_ns = row["checkpoint_ns"] or ""
                        sub_cid = row["checkpoint_id"]
                        if sub_cid in preserved_cids.get(sub_ns, set()):
                            continue
                        await cur.execute(
                            "DELETE FROM checkpoint_writes WHERE thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s",
                            (tid, sub_ns, sub_cid),
                        )
                        await cur.execute(
                            "DELETE FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s",
                            (tid, sub_ns, sub_cid),
                        )

                await cur.execute(
                    """
                    SELECT checkpoint_ns, MAX(checkpoint_id) AS latest_cid
                    FROM checkpoints
                    WHERE thread_id = %s
                    GROUP BY checkpoint_ns
                    """,
                    (tid,),
                )
                for row in await cur.fetchall():
                    ns = row["checkpoint_ns"] or ""
                    latest_cid = row["latest_cid"]
                    if not latest_cid:
                        continue
                    keep = list({latest_cid, *preserved_cids.get(ns, set())})
                    await cur.execute(
                        """
                        DELETE FROM checkpoint_writes
                        WHERE thread_id = %s AND checkpoint_ns = %s AND NOT (checkpoint_id = ANY(%s))
                        """,
                        (tid, ns, keep),
                    )
                    await cur.execute(
                        """
                        DELETE FROM checkpoints
                        WHERE thread_id = %s AND checkpoint_ns = %s AND NOT (checkpoint_id = ANY(%s))
                        """,
                        (tid, ns, keep),
                    )


@asynccontextmanager
async def create_postgres_store(
    conn_string: str | None = None,
) -> AsyncGenerator[AsyncPostgresStore, None]:
    """Create and initialize an `AsyncPostgresStore` connected to PostgreSQL."""
    uri = conn_string or get_postgres_session_uri()
    if not uri:
        raise ValueError("PostgreSQL connection URI is required for create_postgres_store")
    async with AsyncPostgresStore.from_conn_string(uri) as store:
        await store.setup()
        yield store


class PostgresSessionBackend:
    """Multi-user PostgreSQL session query backend for Central Server Mode."""

    def __init__(self, conn_string: str | None = None) -> None:
        """Initialize PostgresSessionBackend."""
        self.conn_string = conn_string or get_postgres_session_uri()
        if not self.conn_string:
            raise ValueError("PostgreSQL URI not configured for PostgresSessionBackend")

    @asynccontextmanager
    async def _connect(self) -> AsyncGenerator[AsyncConnection[DictRow], None]:
        assert self.conn_string is not None
        async with await AsyncConnection.connect(
            self.conn_string,
            autocommit=True,
            prepare_threshold=0,
            row_factory=cast("Any", dict_row),
        ) as conn:
            yield cast("AsyncConnection[DictRow]", conn)

    async def ensure_initialized(self) -> None:
        """Ensure PostgreSQL checkpoint and auxiliary TalkOps tables exist."""
        assert self.conn_string is not None
        async with TalkOpsPostgresCheckpointer.from_conn_string(
            self.conn_string, use_pool=False
        ):
            pass

    async def get_thread_title(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
    ) -> str | None:
        """Retrieve thread title."""
        if not thread_id:
            return None
        async with self._connect() as conn, conn.cursor() as cur:
            await _ensure_pg_talkops_tables(cur)
            if user_id and user_id not in ("*", "default", "local"):
                await cur.execute(
                    f"""
                    SELECT title FROM {_THREAD_SUMMARIES_TABLE}
                    WHERE thread_id = %s AND user_id IN (%s, 'webhook', 'default')
                    LIMIT 1
                    """,
                    (thread_id, user_id),
                )
            else:
                await cur.execute(
                    f"SELECT title FROM {_THREAD_SUMMARIES_TABLE} WHERE thread_id = %s LIMIT 1",
                    (thread_id,),
                )
            row = await cur.fetchone()
            if row and isinstance(row.get("title"), str) and row["title"].strip():
                return row["title"].strip()
        return None

    async def set_thread_title(
        self,
        thread_id: str,
        title: str,
        *,
        preview: str | None = None,
        only_if_empty: bool = True,
        user_id: str | None = None,
    ) -> bool:
        """Set thread title."""
        if not thread_id or not isinstance(thread_id, str) or not thread_id.strip():
            return False

        from talkops.state.conversation_title import (
            postprocess_conversation_title,
            truncate_summary_preview,
        )

        cleaned_title = postprocess_conversation_title(title)
        if not cleaned_title:
            return False
        cleaned_preview = truncate_summary_preview(preview) if preview else ""
        now_iso = datetime.now().astimezone().isoformat()
        effective_user = (user_id or "default").strip() or "default"

        async with self._connect() as conn, conn.cursor() as cur:
            await _ensure_pg_talkops_tables(cur)
            if only_if_empty:
                await cur.execute(
                    f"SELECT title FROM {_THREAD_SUMMARIES_TABLE} WHERE thread_id = %s LIMIT 1",
                    (thread_id,),
                )
                row = await cur.fetchone()
                if row and isinstance(row.get("title"), str) and row["title"].strip():
                    return False

            await cur.execute(
                f"""
                INSERT INTO {_THREAD_SUMMARIES_TABLE} (thread_id, user_id, title, preview, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (thread_id) DO UPDATE SET
                    user_id = CASE
                        WHEN {_THREAD_SUMMARIES_TABLE}.user_id IN ('', 'default') THEN EXCLUDED.user_id
                        ELSE {_THREAD_SUMMARIES_TABLE}.user_id
                    END,
                    title = EXCLUDED.title,
                    preview = CASE
                        WHEN EXCLUDED.preview != '' THEN EXCLUDED.preview
                        ELSE {_THREAD_SUMMARIES_TABLE}.preview
                    END,
                    updated_at = EXCLUDED.updated_at
                """,
                (thread_id, effective_user, cleaned_title, cleaned_preview, now_iso),
            )
        return True

    async def list_threads(
        self,
        *,
        agent_name: str | None = None,
        limit: int = 20,
        include_message_count: bool = True,
        sort_by: str = "updated",
        branch: str | None = None,
        cwd: str | None = None,
        user_id: str | None = None,
    ) -> list[ThreadInfo]:
        """List threads."""
        from talkops.state.session import (
            ThreadInfo,
            _summarize_checkpoint,
        )

        assert self.conn_string is not None
        async with TalkOpsPostgresCheckpointer.from_conn_string(
            self.conn_string, use_pool=False
        ) as saver:
            async with saver._cursor() as cur:
                where_clauses: list[str] = ["c.checkpoint_ns = ''"]
                params: list[Any] = []

                if agent_name:
                    where_clauses.append("c.metadata ->> 'agent_name' = %s")
                    params.append(agent_name)
                if branch:
                    where_clauses.append("c.metadata ->> 'git_branch' = %s")
                    params.append(branch)
                if cwd:
                    where_clauses.append("c.metadata ->> 'cwd' = %s")
                    params.append(cwd)
                if user_id and user_id not in ("*", "default", "local"):
                    where_clauses.append(
                        "(COALESCE(c.metadata ->> 'owner', ts.user_id, 'default') IN (%s, 'webhook', 'default'))"
                    )
                    params.append(user_id)

                order_col = "created_at" if sort_by == "created" else "updated_at"
                where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

                query = f"""
                    SELECT c.thread_id,
                           MAX(c.metadata ->> 'agent_name') AS agent_name,
                           MAX(COALESCE(c.metadata ->> 'updated_at', ts.updated_at, c.checkpoint ->> 'ts')) AS updated_at,
                           MAX(c.checkpoint_id) AS latest_checkpoint_id,
                           MIN(COALESCE(c.metadata ->> 'updated_at', ts.updated_at, c.checkpoint ->> 'ts')) AS created_at,
                           MAX(c.metadata ->> 'git_branch') AS git_branch,
                           MAX(c.metadata ->> 'cwd') AS cwd,
                           MAX(COALESCE(ts.title, c.metadata ->> 'title')) AS title,
                           MAX(ts.preview) AS preview,
                           MAX(COALESCE(c.metadata ->> 'owner', ts.user_id, 'default')) AS owner
                    FROM checkpoints c
                    LEFT JOIN {_THREAD_SUMMARIES_TABLE} ts ON ts.thread_id = c.thread_id
                    {where_sql}
                    GROUP BY c.thread_id
                    ORDER BY {order_col} DESC NULLS LAST, latest_checkpoint_id DESC
                    LIMIT %s
                """
                params.append(limit)
                await cur.execute(cast("Any", query), tuple(params))
                rows = await cur.fetchall()

            threads: list[ThreadInfo] = []
            for r in rows:
                t_info = ThreadInfo(
                    thread_id=str(r["thread_id"]),
                    agent_name=r.get("agent_name"),
                    updated_at=r.get("updated_at"),
                    latest_checkpoint_id=r.get("latest_checkpoint_id"),
                    created_at=r.get("created_at"),
                    git_branch=r.get("git_branch"),
                    cwd=r.get("cwd"),
                    title=r.get("title") or None,
                )
                if r.get("preview"):
                    t_info["initial_prompt"] = str(r["preview"])
                if r.get("owner") == "webhook" and not t_info.get("cloud_badge"):
                    t_info["cloud_badge"] = "[WEBHOOK]"
                threads.append(t_info)

            if include_message_count and threads:
                for t in threads:
                    tid = t["thread_id"]
                    try:
                        tup = await saver.aget_tuple({"configurable": {"thread_id": tid}})
                        if tup and tup.checkpoint:
                            summary = _summarize_checkpoint(tup.checkpoint)
                            t["message_count"] = summary.message_count or 0
                            if not t.get("initial_prompt") and summary.initial_prompt:
                                t["initial_prompt"] = summary.initial_prompt
                            if summary.cloud_badge:
                                t["cloud_badge"] = summary.cloud_badge
                            if summary.cloud_provider:
                                t["cloud_provider"] = summary.cloud_provider
                        else:
                            t["message_count"] = 0
                    except Exception:
                        t.setdefault("message_count", 0)

            return threads

    async def delete_thread(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
    ) -> bool:
        """Delete thread."""
        assert self.conn_string is not None
        async with TalkOpsPostgresCheckpointer.from_conn_string(
            self.conn_string, use_pool=False
        ) as saver:
            async with saver._cursor() as cur:
                if user_id and user_id not in ("*", "default", "local"):
                    await cur.execute(
                        f"""
                        SELECT COALESCE(c.metadata ->> 'owner', ts.user_id, 'default') AS owner
                        FROM checkpoints c
                        LEFT JOIN {_THREAD_SUMMARIES_TABLE} ts ON ts.thread_id = c.thread_id
                        WHERE c.thread_id = %s
                        LIMIT 1
                        """,
                        (thread_id,),
                    )
                    owner_row = await cur.fetchone()
                    if owner_row and owner_row.get("owner") not in (user_id, "default", "webhook"):
                        return False

                await cur.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s", (thread_id,))
                await cur.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s", (thread_id,))
                await cur.execute("DELETE FROM checkpoints WHERE thread_id = %s", (thread_id,))
                cp_deleted = cur.rowcount > 0
                await cur.execute(
                    f"DELETE FROM {_THREAD_SUMMARIES_TABLE} WHERE thread_id = %s",
                    (thread_id,),
                )
                sum_deleted = cur.rowcount > 0
                return cp_deleted or sum_deleted

    async def thread_exists(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
    ) -> bool:
        """Execute thread exists on PostgresSessionBackend."""
        async with self._connect() as conn, conn.cursor() as cur:
            if user_id and user_id not in ("*", "default", "local"):
                await cur.execute(
                    f"""
                    SELECT 1 FROM checkpoints c
                    LEFT JOIN {_THREAD_SUMMARIES_TABLE} ts ON ts.thread_id = c.thread_id
                    WHERE c.thread_id = %s
                      AND COALESCE(c.metadata ->> 'owner', ts.user_id, 'default') IN (%s, 'webhook', 'default')
                    LIMIT 1
                    """,
                    (thread_id, user_id),
                )
            else:
                await cur.execute(
                    "SELECT 1 FROM checkpoints WHERE thread_id = %s LIMIT 1",
                    (thread_id,),
                )
            return await cur.fetchone() is not None

    async def find_similar_threads(
        self,
        thread_id_prefix: str,
        limit: int = 3,
        *,
        user_id: str | None = None,
    ) -> list[str]:
        """Find and return similar threads."""
        async with self._connect() as conn, conn.cursor() as cur:
            if user_id and user_id not in ("*", "default", "local"):
                await cur.execute(
                    f"""
                    SELECT DISTINCT c.thread_id, MAX(c.checkpoint_id) AS max_cid
                    FROM checkpoints c
                    LEFT JOIN {_THREAD_SUMMARIES_TABLE} ts ON ts.thread_id = c.thread_id
                    WHERE c.thread_id LIKE %s
                      AND COALESCE(c.metadata ->> 'owner', ts.user_id, 'default') IN (%s, 'webhook', 'default')
                    GROUP BY c.thread_id
                    ORDER BY max_cid DESC
                    LIMIT %s
                    """,
                    (f"{thread_id_prefix}%", user_id, limit),
                )
            else:
                await cur.execute(
                    """
                    SELECT DISTINCT thread_id, MAX(checkpoint_id) AS max_cid
                    FROM checkpoints
                    WHERE thread_id LIKE %s
                    GROUP BY thread_id
                    ORDER BY max_cid DESC
                    LIMIT %s
                    """,
                    (f"{thread_id_prefix}%", limit),
                )
            rows = await cur.fetchall()
            return [str(r["thread_id"]) for r in rows]


__all__ = [
    "TalkOpsPostgresCheckpointer",
    "PostgresSessionBackend",
    "create_postgres_store",
    "get_postgres_session_uri",
]

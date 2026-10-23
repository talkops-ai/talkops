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

"""Bidirectional mapping between platform thread IDs and LangGraph thread IDs.

LangGraph requires a ``thread_id`` in the ``configurable`` dict for
checkpointing and interrupt / resume.  Each messaging platform has its
own thread identifier format (Slack ``thread_ts``, Teams
``conversationId``, etc.).

This store creates stable UUID-based LangGraph thread IDs and
maintains the mapping in both directions so that:

1. An incoming platform event can be mapped to its LangGraph thread.
2. An outgoing notification (e.g. from a cron-triggered run) can be
   mapped back to the correct platform thread for delivery.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from talkops.utils.logger import get_logger

logger = get_logger(__name__)

_WEBHOOK_THREAD_MAPPINGS_TABLE = "webhook_thread_mappings"


class ThreadStore:
    """Maps ``platform:thread_id`` ↔ ``langgraph_thread_id``.

    Uses an in-memory cache backed by PostgreSQL (`webhook_thread_mappings`)
    when `TALKOPS_POSTGRES_URI` / `POSTGRES_URI` / `DATABASE_URL` is configured,
    or SQLite (`~/.talkops/.state/sessions.db`) in local/SQLite server mode
    so forward and reverse lookups survive server restarts.
    """

    def __init__(
        self,
        backend: str = "auto",
        postgres_uri: str | None = None,
        sqlite_path: Path | str | None = None,
    ) -> None:
        """Initialize ThreadStore mapping platform and LangGraph threads.

        Args:
            backend: Storage backend identifier ("auto", "memory", "sqlite", or "postgres").
            postgres_uri: Optional explicit PostgreSQL URI override.
            sqlite_path: Optional explicit SQLite database path override.
        """
        self._platform_to_lg: dict[str, str] = {}
        self._lg_to_platform: dict[str, dict[str, str]] = {}
        self._backend = backend
        self._postgres_uri = postgres_uri
        self._sqlite_path = Path(sqlite_path) if sqlite_path else None
        self._pg_table_ready = False
        self._sqlite_table_ready = False

    def _effective_postgres_uri(self) -> str | None:
        if self._backend in ("memory", "sqlite"):
            return None
        if self._postgres_uri:
            return self._postgres_uri
        from talkops.state.postgres_session import get_postgres_session_uri

        return get_postgres_session_uri()

    def _effective_sqlite_path(self) -> Path | None:
        if self._backend in ("memory", "postgres"):
            return None
        if self._effective_postgres_uri():
            return None
        if self._sqlite_path is not None:
            return self._sqlite_path

        if self._backend == "auto" and os.environ.get("PYTEST_CURRENT_TEST"):
            return None
        try:
            from talkops.state.session import get_db_path

            return get_db_path()
        except Exception:
            return None

    def _ensure_sqlite_table(self, conn: sqlite3.Connection) -> None:
        if self._sqlite_table_ready:
            return
        conn.execute(
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
        self._sqlite_table_ready = True

    def _persist_to_sqlite(self, key: str, platform: str, platform_thread_id: str, lg_id: str) -> None:
        db_path = self._effective_sqlite_path()
        if db_path is None:
            return
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            now_iso = datetime.now().astimezone().isoformat()
            with sqlite3.connect(str(db_path), timeout=10.0) as conn:
                self._ensure_sqlite_table(conn)
                conn.execute(
                    f"""
                    INSERT INTO {_WEBHOOK_THREAD_MAPPINGS_TABLE}
                        (mapping_key, provider, external_id, lg_thread_id, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(mapping_key) DO UPDATE SET
                        lg_thread_id = excluded.lg_thread_id,
                        updated_at = excluded.updated_at
                    """,
                    (key, platform, platform_thread_id, lg_id, now_iso),
                )
                conn.commit()
        except Exception as exc:
            logger.debug("ThreadStore SQLite persistence skipped for %s: %s", key, exc)

    def _ensure_pg_table(self, conn: object) -> None:
        if self._pg_table_ready:
            return
        cur = getattr(conn, "cursor", lambda: None)()
        if cur is None:
            return
        with cur:
            cur.execute(
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
        self._pg_table_ready = True

    def _persist_to_pg(self, key: str, platform: str, platform_thread_id: str, lg_id: str) -> None:
        pg_uri = self._effective_postgres_uri()
        if not pg_uri:
            return
        try:
            import psycopg

            now_iso = datetime.now().astimezone().isoformat()
            with psycopg.connect(pg_uri, autocommit=True) as conn:
                self._ensure_pg_table(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        INSERT INTO {_WEBHOOK_THREAD_MAPPINGS_TABLE}
                            (mapping_key, provider, external_id, lg_thread_id, updated_at)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (mapping_key) DO UPDATE SET
                            lg_thread_id = EXCLUDED.lg_thread_id,
                            updated_at = EXCLUDED.updated_at
                        """,
                        (key, platform, platform_thread_id, lg_id, now_iso),
                    )
        except Exception as exc:
            logger.debug("ThreadStore Postgres persistence skipped for %s: %s", key, exc)

    # ── Forward lookup ────────────────────────────────────────────────

    def bind(self, platform: str, platform_thread_id: str, lg_thread_id: str) -> str:
        """Explicitly bind a platform thread (`platform:platform_thread_id`) to an existing `lg_thread_id`."""
        key = f"{platform}:{platform_thread_id}"
        self._platform_to_lg[key] = lg_thread_id
        self._lg_to_platform[lg_thread_id] = {
            "platform": platform,
            "thread_id": platform_thread_id,
        }
        self._persist_to_pg(key, platform, platform_thread_id, lg_thread_id)
        self._persist_to_sqlite(key, platform, platform_thread_id, lg_thread_id)
        return lg_thread_id

    def resolve(self, platform: str, platform_thread_id: str) -> str:
        """Get or create a LangGraph thread_id for a platform thread."""
        key = f"{platform}:{platform_thread_id}"
        if key not in self._platform_to_lg:
            if not self.has_thread(platform, platform_thread_id):
                lg_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, key))
                self._platform_to_lg[key] = lg_id
                self._lg_to_platform[lg_id] = {
                    "platform": platform,
                    "thread_id": platform_thread_id,
                }
                self._persist_to_pg(key, platform, platform_thread_id, lg_id)
                self._persist_to_sqlite(key, platform, platform_thread_id, lg_id)
        return self._platform_to_lg[key]

    # ── Reverse lookup ────────────────────────────────────────────────

    def reverse_lookup(self, lg_thread_id: str) -> dict[str, str] | None:
        """Look up platform info from a LangGraph thread_id."""
        cached = self._lg_to_platform.get(lg_thread_id)
        if cached is not None:
            return cached

        pg_uri = self._effective_postgres_uri()
        if pg_uri:
            try:
                import psycopg

                with psycopg.connect(pg_uri, autocommit=True) as conn:
                    self._ensure_pg_table(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            f"""
                            SELECT provider, external_id, mapping_key
                            FROM {_WEBHOOK_THREAD_MAPPINGS_TABLE}
                            WHERE lg_thread_id = %s
                            LIMIT 1
                            """,
                            (lg_thread_id,),
                        )
                        row = cur.fetchone()
                        if row:
                            provider, external_id, mapping_key = str(row[0]), str(row[1]), str(row[2])
                            info = {"platform": provider, "thread_id": external_id}
                            self._lg_to_platform[lg_thread_id] = info
                            self._platform_to_lg[mapping_key] = lg_thread_id
                            return info
            except Exception as exc:
                logger.debug("ThreadStore reverse_lookup Postgres query failed: %s", exc)

        db_path = self._effective_sqlite_path()
        if db_path is not None and db_path.exists():
            try:
                with sqlite3.connect(str(db_path), timeout=5.0) as conn:
                    self._ensure_sqlite_table(conn)
                    cur = conn.execute(
                        f"""
                        SELECT provider, external_id, mapping_key
                        FROM {_WEBHOOK_THREAD_MAPPINGS_TABLE}
                        WHERE lg_thread_id = ?
                        LIMIT 1
                        """,
                        (lg_thread_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        provider, external_id, mapping_key = str(row[0]), str(row[1]), str(row[2])
                        info = {"platform": provider, "thread_id": external_id}
                        self._lg_to_platform[lg_thread_id] = info
                        self._platform_to_lg[mapping_key] = lg_thread_id
                        return info
            except Exception as exc:
                logger.debug("ThreadStore reverse_lookup SQLite query failed: %s", exc)
        return None

    # ── Existence check ───────────────────────────────────────────────

    def has_thread(self, platform: str, platform_thread_id: str) -> bool:
        """Check if a platform thread already has a LangGraph mapping."""
        key = f"{platform}:{platform_thread_id}"
        if key in self._platform_to_lg:
            return True
        pg_uri = self._effective_postgres_uri()
        if pg_uri:
            try:
                import psycopg

                with psycopg.connect(pg_uri, autocommit=True) as conn:
                    self._ensure_pg_table(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT lg_thread_id FROM {_WEBHOOK_THREAD_MAPPINGS_TABLE} WHERE mapping_key = %s LIMIT 1",
                            (key,),
                        )
                        row = cur.fetchone()
                        if row and row[0]:
                            lg_id = str(row[0])
                            self._platform_to_lg[key] = lg_id
                            self._lg_to_platform[lg_id] = {
                                "platform": platform,
                                "thread_id": platform_thread_id,
                            }
                            return True
            except Exception:
                pass

        db_path = self._effective_sqlite_path()
        if db_path is not None and db_path.exists():
            try:
                with sqlite3.connect(str(db_path), timeout=5.0) as conn:
                    self._ensure_sqlite_table(conn)
                    cur = conn.execute(
                        f"SELECT lg_thread_id FROM {_WEBHOOK_THREAD_MAPPINGS_TABLE} WHERE mapping_key = ? LIMIT 1",
                        (key,),
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        lg_id = str(row[0])
                        self._platform_to_lg[key] = lg_id
                        self._lg_to_platform[lg_id] = {
                            "platform": platform,
                            "thread_id": platform_thread_id,
                        }
                        return True
            except Exception:
                pass
        return False

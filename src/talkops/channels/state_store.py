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

"""Durable state store abstraction for `talkops.channels`.

Persists cross-channel state that must survive container restarts and horizontal
scaling across multiple API replicas (e.g. LangGraph Agent Server / Kubernetes):
1. Pending HITL tool approvals (`channel_pending_approvals`)
2. Webhook-to-channel incident root-message & thread bindings (`channel_incident_refs`)
3. Distributed leader leases for persistent WebSocket / polling transports (`channel_transport_leases`)

Uses the same dual-backend strategy as `ThreadStore`:
- In-memory (`InMemoryChannelStateStore`) for unit tests and ephemeral runs
- PostgreSQL when `TALKOPS_POSTGRES_URI` / `POSTGRES_URI` / `DATABASE_URL` is configured
- Zero-config WAL-mode SQLite otherwise
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from talkops.utils.logger import get_logger

logger = get_logger(__name__)

_APPROVALS_TABLE = "talkops_channel_approvals"
_INCIDENT_REFS_TABLE = "talkops_channel_incident_refs"
_CONVERSATION_REFS_TABLE = "talkops_channel_conversation_refs"
_LEASES_TABLE = "talkops_channel_transport_leases"


@runtime_checkable
class ChannelStateStore(Protocol):
    """Pluggable durable store shared by `ChannelHub`, `ChannelsAddon`, and persistent transports."""

    # ── Pending HITL Approvals ────────────────────────────────────────
    def put_approval(self, approval_id: str, record: Mapping[str, Any]) -> None:
        """Persist a pending HITL approval record."""
        ...

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        """Fetch a pending HITL approval record by `approval_id`."""
        ...

    def get_approval_by_conversation(self, channel: str, conversation_id: str) -> dict[str, Any] | None:
        """Fetch the active pending approval record for `(channel, conversation_id)`."""
        ...

    def get_approval_by_message(self, channel: str, message_id: str) -> dict[str, Any] | None:
        """Fetch the active pending approval record bound to `(channel, message_id)`."""
        ...

    def delete_approval(self, approval_id: str) -> None:
        """Remove a pending HITL approval record."""
        ...

    # ── Webhook Incident Thread / Root-Message Refs ───────────────────
    def put_incident_ref(
        self,
        lg_thread_id: str,
        channel: str,
        conversation_id: str,
        ref: Mapping[str, Any],
    ) -> None:
        """Persist webhook incident root-message and thread metadata."""
        ...

    def get_incident_ref(
        self,
        lg_thread_id: str,
        channel: str,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        """Fetch stored incident metadata for `(lg_thread_id, channel, conversation_id)`."""
        ...

    # ── Provider Conversation Routing Refs (e.g. Teams serviceUrl/bot) ─
    def put_conversation_ref(
        self,
        channel: str,
        conversation_id: str,
        ref: Mapping[str, Any],
    ) -> None:
        """Persist provider-specific conversation routing metadata."""
        ...

    def get_conversation_ref(
        self,
        channel: str,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        """Fetch stored routing metadata for `(channel, conversation_id)`."""
        ...

    # ── Distributed Transport Leader Leases ───────────────────────────
    def try_acquire_lease(
        self,
        lease_key: str,
        owner_id: str,
        *,
        ttl_seconds: float = 45.0,
        now: float | None = None,
    ) -> bool:
        """Atomically acquire or renew `lease_key` for `owner_id`."""
        ...

    def renew_lease(
        self,
        lease_key: str,
        owner_id: str,
        *,
        ttl_seconds: float = 45.0,
        now: float | None = None,
    ) -> bool:
        """Extend an active lease held by `owner_id`."""
        ...

    def release_lease(self, lease_key: str, owner_id: str) -> None:
        """Release `lease_key` if currently owned by `owner_id`."""
        ...

    def get_lease_owner(self, lease_key: str, *, now: float | None = None) -> str | None:
        """Return the active owner ID of `lease_key`, or `None` if unowned/expired."""
        ...


class InMemoryChannelStateStore:
    """Thread-safe in-memory `ChannelStateStore` implementation (default for unit tests)."""

    def __init__(self) -> None:
        """Initialize empty in-memory indices."""
        self._lock = threading.Lock()
        self._approvals: dict[str, dict[str, Any]] = {}
        self._incident_refs: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._conversation_refs: dict[tuple[str, str], dict[str, Any]] = {}
        self._leases: dict[str, tuple[str, float]] = {}

    def put_approval(self, approval_id: str, record: Mapping[str, Any]) -> None:
        """Store or overwrite a serialized pending approval record."""
        with self._lock:
            rec = dict(record)
            ch = str(rec.get("channel") or "")
            conv = str(rec.get("conversation_id") or "")
            # Evict stale approval for same conversation
            for existing_id, existing in list(self._approvals.items()):
                if existing_id != approval_id and existing.get("channel") == ch and existing.get("conversation_id") == conv:
                    self._approvals.pop(existing_id, None)
            self._approvals[approval_id] = rec

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        """Return serialized pending approval matching `approval_id`, if present."""
        with self._lock:
            rec = self._approvals.get(approval_id)
            return dict(rec) if rec is not None else None

    def get_approval_by_conversation(self, channel: str, conversation_id: str) -> dict[str, Any] | None:
        """Return serialized pending approval for `(channel, conversation_id)`."""
        with self._lock:
            for rec in self._approvals.values():
                if rec.get("channel") == channel and rec.get("conversation_id") == conversation_id:
                    return dict(rec)
            return None

    def get_approval_by_message(self, channel: str, message_id: str) -> dict[str, Any] | None:
        """Return serialized pending approval bound to `(channel, message_id)`."""
        with self._lock:
            for rec in self._approvals.values():
                if rec.get("channel") == channel and rec.get("prompt_message_id") == message_id:
                    return dict(rec)
            return None

    def delete_approval(self, approval_id: str) -> None:
        """Delete a pending approval record by ID."""
        with self._lock:
            self._approvals.pop(approval_id, None)

    def put_incident_ref(
        self,
        lg_thread_id: str,
        channel: str,
        conversation_id: str,
        ref: Mapping[str, Any],
    ) -> None:
        """Store incident root-message and thread metadata."""
        with self._lock:
            self._incident_refs[(lg_thread_id, channel, conversation_id)] = dict(ref)

    def get_incident_ref(
        self,
        lg_thread_id: str,
        channel: str,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        """Fetch stored incident metadata for `(lg_thread_id, channel, conversation_id)`."""
        with self._lock:
            rec = self._incident_refs.get((lg_thread_id, channel, conversation_id))
            return dict(rec) if rec is not None else None

    def put_conversation_ref(
        self,
        channel: str,
        conversation_id: str,
        ref: Mapping[str, Any],
    ) -> None:
        """Store provider-specific conversation routing metadata."""
        with self._lock:
            self._conversation_refs[(channel, conversation_id)] = dict(ref)

    def get_conversation_ref(
        self,
        channel: str,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        """Fetch stored conversation routing metadata for `(channel, conversation_id)`."""
        with self._lock:
            rec = self._conversation_refs.get((channel, conversation_id))
            return dict(rec) if rec is not None else None

    def try_acquire_lease(
        self,
        lease_key: str,
        owner_id: str,
        *,
        ttl_seconds: float = 45.0,
        now: float | None = None,
    ) -> bool:
        """Acquire or renew `lease_key` if unowned, expired, or already owned by `owner_id`."""
        ts = time.time() if now is None else now
        with self._lock:
            cur = self._leases.get(lease_key)
            if cur is None or cur[1] <= ts or cur[0] == owner_id:
                self._leases[lease_key] = (owner_id, ts + ttl_seconds)
                return True
            return False

    def renew_lease(
        self,
        lease_key: str,
        owner_id: str,
        *,
        ttl_seconds: float = 45.0,
        now: float | None = None,
    ) -> bool:
        """Extend lease expiry if `owner_id` currently owns `lease_key`."""
        ts = time.time() if now is None else now
        with self._lock:
            cur = self._leases.get(lease_key)
            if cur is not None and cur[0] == owner_id and cur[1] > ts:
                self._leases[lease_key] = (owner_id, ts + ttl_seconds)
                return True
            return False

    def release_lease(self, lease_key: str, owner_id: str) -> None:
        """Release `lease_key` if currently owned by `owner_id`."""
        with self._lock:
            cur = self._leases.get(lease_key)
            if cur is not None and cur[0] == owner_id:
                self._leases.pop(lease_key, None)

    def get_lease_owner(self, lease_key: str, *, now: float | None = None) -> str | None:
        """Return active owner ID of `lease_key`, or `None` if unowned/expired."""
        ts = time.time() if now is None else now
        with self._lock:
            cur = self._leases.get(lease_key)
            if cur is not None and cur[1] > ts:
                return cur[0]
            return None


class PersistentChannelStateStore:
    """Durable SQLite (WAL) / PostgreSQL `ChannelStateStore` with in-process cache fallback.

    Mirrors `ThreadStore`'s auto-detection of PostgreSQL (`TALKOPS_POSTGRES_URI`) vs
    SQLite (`~/.talkops/.state/sessions.db` or explicit `sqlite_path`) and falls back
    gracefully to memory under `PYTEST_CURRENT_TEST` unless an explicit path/URI is supplied.
    """

    def __init__(
        self,
        *,
        backend: str = "auto",
        postgres_uri: str | None = None,
        sqlite_path: Path | str | None = None,
    ) -> None:
        """Initialize `PersistentChannelStateStore`."""
        self._backend = backend
        self._postgres_uri = postgres_uri
        self._sqlite_path = Path(sqlite_path) if sqlite_path is not None else None
        self._memory = InMemoryChannelStateStore()
        self._sqlite_ready = False
        self._pg_ready = False
        self._init_lock = threading.Lock()

    def _effective_postgres_uri(self) -> str | None:
        if self._backend in ("memory", "sqlite"):
            return None
        if self._postgres_uri:
            return self._postgres_uri
        try:
            from talkops.state.postgres_session import get_postgres_session_uri

            return get_postgres_session_uri()
        except Exception:
            return None

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

    def _ensure_sqlite_schema(self, conn: sqlite3.Connection) -> None:
        if self._sqlite_ready:
            return
        with self._init_lock:
            if self._sqlite_ready:
                return
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_APPROVALS_TABLE} (
                    approval_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    prompt_message_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_ch_appr_conv
                    ON {_APPROVALS_TABLE} (channel, conversation_id)
                """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_ch_appr_msg
                    ON {_APPROVALS_TABLE} (channel, prompt_message_id)
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_INCIDENT_REFS_TABLE} (
                    ref_key TEXT PRIMARY KEY,
                    lg_thread_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_CONVERSATION_REFS_TABLE} (
                    ref_key TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_LEASES_TABLE} (
                    lease_key TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._sqlite_ready = True

    def _ensure_pg_schema(self, conn: Any) -> None:
        if self._pg_ready:
            return
        with self._init_lock:
            if self._pg_ready:
                return
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_APPROVALS_TABLE} (
                        approval_id TEXT PRIMARY KEY,
                        channel TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        prompt_message_id TEXT NOT NULL DEFAULT '',
                        payload_json TEXT NOT NULL,
                        updated_at DOUBLE PRECISION NOT NULL
                    )
                    """
                )
                cur.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_ch_appr_conv
                        ON {_APPROVALS_TABLE} (channel, conversation_id)
                    """
                )
                cur.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_ch_appr_msg
                        ON {_APPROVALS_TABLE} (channel, prompt_message_id)
                    """
                )
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_INCIDENT_REFS_TABLE} (
                        ref_key TEXT PRIMARY KEY,
                        lg_thread_id TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        updated_at DOUBLE PRECISION NOT NULL
                    )
                    """
                )
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_CONVERSATION_REFS_TABLE} (
                        ref_key TEXT PRIMARY KEY,
                        channel TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        updated_at DOUBLE PRECISION NOT NULL
                    )
                    """
                )
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_LEASES_TABLE} (
                        lease_key TEXT PRIMARY KEY,
                        owner_id TEXT NOT NULL,
                        lease_expires_at DOUBLE PRECISION NOT NULL,
                        updated_at DOUBLE PRECISION NOT NULL
                    )
                    """
                )
            self._pg_ready = True

    # ── Approvals ─────────────────────────────────────────────────────

    def put_approval(self, approval_id: str, record: Mapping[str, Any]) -> None:
        """Persist a pending approval record."""
        self._memory.put_approval(approval_id, record)
        channel = str(record.get("channel") or "")
        conversation_id = str(record.get("conversation_id") or "")
        prompt_message_id = str(record.get("prompt_message_id") or "")
        payload_json = json.dumps(dict(record), default=str)
        now = time.time()

        pg_uri = self._effective_postgres_uri()
        if pg_uri:
            try:
                import psycopg

                with psycopg.connect(pg_uri, autocommit=True) as conn:
                    self._ensure_pg_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            f"DELETE FROM {_APPROVALS_TABLE} WHERE channel = %s AND conversation_id = %s AND approval_id <> %s",
                            (channel, conversation_id, approval_id),
                        )
                        cur.execute(
                            f"""
                            INSERT INTO {_APPROVALS_TABLE}
                                (approval_id, channel, conversation_id, prompt_message_id, payload_json, updated_at)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (approval_id) DO UPDATE SET
                                prompt_message_id = EXCLUDED.prompt_message_id,
                                payload_json = EXCLUDED.payload_json,
                                updated_at = EXCLUDED.updated_at
                            """,
                            (approval_id, channel, conversation_id, prompt_message_id, payload_json, now),
                        )
                return
            except Exception as exc:
                logger.debug("ChannelStateStore Postgres put_approval failed: %s", exc)

        db_path = self._effective_sqlite_path()
        if db_path is not None:
            try:
                db_path.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(str(db_path), timeout=10.0) as conn:
                    self._ensure_sqlite_schema(conn)
                    conn.execute(
                        f"DELETE FROM {_APPROVALS_TABLE} WHERE channel = ? AND conversation_id = ? AND approval_id <> ?",
                        (channel, conversation_id, approval_id),
                    )
                    conn.execute(
                        f"""
                        INSERT INTO {_APPROVALS_TABLE}
                            (approval_id, channel, conversation_id, prompt_message_id, payload_json, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(approval_id) DO UPDATE SET
                            prompt_message_id = excluded.prompt_message_id,
                            payload_json = excluded.payload_json,
                            updated_at = excluded.updated_at
                        """,
                        (approval_id, channel, conversation_id, prompt_message_id, payload_json, now),
                    )
                    conn.commit()
            except Exception as exc:
                logger.debug("ChannelStateStore SQLite put_approval failed: %s", exc)

    def _query_approval(self, where_sql_pg: str, where_sql_sq: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        pg_uri = self._effective_postgres_uri()
        if pg_uri:
            try:
                import psycopg

                with psycopg.connect(pg_uri, autocommit=True) as conn:
                    self._ensure_pg_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT payload_json FROM {_APPROVALS_TABLE} WHERE {where_sql_pg} ORDER BY updated_at DESC LIMIT 1",
                            params,
                        )
                        row = cur.fetchone()
                        if row and row[0]:
                            parsed = json.loads(str(row[0]))
                            return parsed if isinstance(parsed, dict) else None
                return None
            except Exception as exc:
                logger.debug("ChannelStateStore Postgres get_approval failed: %s", exc)

        db_path = self._effective_sqlite_path()
        if db_path is not None and db_path.exists():
            try:
                with sqlite3.connect(str(db_path), timeout=5.0) as conn:
                    self._ensure_sqlite_schema(conn)
                    cur = conn.execute(
                        f"SELECT payload_json FROM {_APPROVALS_TABLE} WHERE {where_sql_sq} ORDER BY updated_at DESC LIMIT 1",
                        params,
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        parsed = json.loads(str(row[0]))
                        return parsed if isinstance(parsed, dict) else None
                return None
            except Exception as exc:
                logger.debug("ChannelStateStore SQLite get_approval failed: %s", exc)
        return None

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        """Fetch a pending approval record by `approval_id`."""
        persisted = self._query_approval("approval_id = %s", "approval_id = ?", (approval_id,))
        if persisted is not None:
            return persisted
        return self._memory.get_approval(approval_id)

    def get_approval_by_conversation(self, channel: str, conversation_id: str) -> dict[str, Any] | None:
        """Fetch the active pending approval record for `(channel, conversation_id)`."""
        persisted = self._query_approval(
            "channel = %s AND conversation_id = %s",
            "channel = ? AND conversation_id = ?",
            (channel, conversation_id),
        )
        if persisted is not None:
            return persisted
        return self._memory.get_approval_by_conversation(channel, conversation_id)

    def get_approval_by_message(self, channel: str, message_id: str) -> dict[str, Any] | None:
        """Fetch the active pending approval record bound to `(channel, message_id)`."""
        if not message_id:
            return None
        persisted = self._query_approval(
            "channel = %s AND prompt_message_id = %s",
            "channel = ? AND prompt_message_id = ?",
            (channel, message_id),
        )
        if persisted is not None:
            return persisted
        return self._memory.get_approval_by_message(channel, message_id)

    def delete_approval(self, approval_id: str) -> None:
        """Delete a pending approval record across memory and durable storage."""
        self._memory.delete_approval(approval_id)
        pg_uri = self._effective_postgres_uri()
        if pg_uri:
            try:
                import psycopg

                with psycopg.connect(pg_uri, autocommit=True) as conn:
                    self._ensure_pg_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            f"DELETE FROM {_APPROVALS_TABLE} WHERE approval_id = %s",
                            (approval_id,),
                        )
                return
            except Exception as exc:
                logger.debug("ChannelStateStore Postgres delete_approval failed: %s", exc)

        db_path = self._effective_sqlite_path()
        if db_path is not None and db_path.exists():
            try:
                with sqlite3.connect(str(db_path), timeout=5.0) as conn:
                    self._ensure_sqlite_schema(conn)
                    conn.execute(
                        f"DELETE FROM {_APPROVALS_TABLE} WHERE approval_id = ?",
                        (approval_id,),
                    )
                    conn.commit()
            except Exception as exc:
                logger.debug("ChannelStateStore SQLite delete_approval failed: %s", exc)

    # ── Incident Refs ─────────────────────────────────────────────────

    @staticmethod
    def _ref_key(lg_thread_id: str, channel: str, conversation_id: str) -> str:
        return f"{lg_thread_id}::{channel}::{conversation_id}"

    def put_incident_ref(
        self,
        lg_thread_id: str,
        channel: str,
        conversation_id: str,
        ref: Mapping[str, Any],
    ) -> None:
        """Persist webhook incident root-message & thread metadata."""
        self._memory.put_incident_ref(lg_thread_id, channel, conversation_id, ref)
        ref_key = self._ref_key(lg_thread_id, channel, conversation_id)
        payload_json = json.dumps(dict(ref), default=str)
        now = time.time()

        pg_uri = self._effective_postgres_uri()
        if pg_uri:
            try:
                import psycopg

                with psycopg.connect(pg_uri, autocommit=True) as conn:
                    self._ensure_pg_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            f"""
                            INSERT INTO {_INCIDENT_REFS_TABLE}
                                (ref_key, lg_thread_id, channel, conversation_id, payload_json, updated_at)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (ref_key) DO UPDATE SET
                                payload_json = EXCLUDED.payload_json,
                                updated_at = EXCLUDED.updated_at
                            """,
                            (ref_key, lg_thread_id, channel, conversation_id, payload_json, now),
                        )
                return
            except Exception as exc:
                logger.debug("ChannelStateStore Postgres put_incident_ref failed: %s", exc)

        db_path = self._effective_sqlite_path()
        if db_path is not None:
            try:
                db_path.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(str(db_path), timeout=10.0) as conn:
                    self._ensure_sqlite_schema(conn)
                    conn.execute(
                        f"""
                        INSERT INTO {_INCIDENT_REFS_TABLE}
                            (ref_key, lg_thread_id, channel, conversation_id, payload_json, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(ref_key) DO UPDATE SET
                            payload_json = excluded.payload_json,
                            updated_at = excluded.updated_at
                        """,
                        (ref_key, lg_thread_id, channel, conversation_id, payload_json, now),
                    )
                    conn.commit()
            except Exception as exc:
                logger.debug("ChannelStateStore SQLite put_incident_ref failed: %s", exc)

    def get_incident_ref(
        self,
        lg_thread_id: str,
        channel: str,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        """Fetch stored incident metadata across memory and durable storage."""
        cached = self._memory.get_incident_ref(lg_thread_id, channel, conversation_id)
        if cached is not None:
            return cached
        ref_key = self._ref_key(lg_thread_id, channel, conversation_id)

        pg_uri = self._effective_postgres_uri()
        if pg_uri:
            try:
                import psycopg

                with psycopg.connect(pg_uri, autocommit=True) as conn:
                    self._ensure_pg_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT payload_json FROM {_INCIDENT_REFS_TABLE} WHERE ref_key = %s LIMIT 1",
                            (ref_key,),
                        )
                        row = cur.fetchone()
                        if row and row[0]:
                            parsed = json.loads(str(row[0]))
                            if isinstance(parsed, dict):
                                self._memory.put_incident_ref(lg_thread_id, channel, conversation_id, parsed)
                                return parsed
                return None
            except Exception as exc:
                logger.debug("ChannelStateStore Postgres get_incident_ref failed: %s", exc)

        db_path = self._effective_sqlite_path()
        if db_path is not None and db_path.exists():
            try:
                with sqlite3.connect(str(db_path), timeout=5.0) as conn:
                    self._ensure_sqlite_schema(conn)
                    cur = conn.execute(
                        f"SELECT payload_json FROM {_INCIDENT_REFS_TABLE} WHERE ref_key = ? LIMIT 1",
                        (ref_key,),
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        parsed = json.loads(str(row[0]))
                        if isinstance(parsed, dict):
                            self._memory.put_incident_ref(lg_thread_id, channel, conversation_id, parsed)
                            return parsed
            except Exception as exc:
                logger.debug("ChannelStateStore SQLite get_incident_ref failed: %s", exc)
        return None

    # ── Provider Conversation Routing Refs ────────────────────────────

    @staticmethod
    def _conv_ref_key(channel: str, conversation_id: str) -> str:
        return f"{channel}::{conversation_id}"

    def put_conversation_ref(
        self,
        channel: str,
        conversation_id: str,
        ref: Mapping[str, Any],
    ) -> None:
        """Persist provider-specific conversation routing metadata."""
        self._memory.put_conversation_ref(channel, conversation_id, ref)
        ref_key = self._conv_ref_key(channel, conversation_id)
        payload_json = json.dumps(dict(ref), default=str)
        now = time.time()

        pg_uri = self._effective_postgres_uri()
        if pg_uri:
            try:
                import psycopg

                with psycopg.connect(pg_uri, autocommit=True) as conn:
                    self._ensure_pg_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            f"""
                            INSERT INTO {_CONVERSATION_REFS_TABLE}
                                (ref_key, channel, conversation_id, payload_json, updated_at)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (ref_key) DO UPDATE SET
                                payload_json = EXCLUDED.payload_json,
                                updated_at = EXCLUDED.updated_at
                            """,
                            (ref_key, channel, conversation_id, payload_json, now),
                        )
                return
            except Exception as exc:
                logger.debug("ChannelStateStore Postgres put_conversation_ref failed: %s", exc)

        db_path = self._effective_sqlite_path()
        if db_path is not None:
            try:
                db_path.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(str(db_path), timeout=5.0) as conn:
                    self._ensure_sqlite_schema(conn)
                    conn.execute(
                        f"""
                        INSERT INTO {_CONVERSATION_REFS_TABLE}
                            (ref_key, channel, conversation_id, payload_json, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(ref_key) DO UPDATE SET
                            payload_json = excluded.payload_json,
                            updated_at = excluded.updated_at
                        """,
                        (ref_key, channel, conversation_id, payload_json, now),
                    )
                    conn.commit()
            except Exception as exc:
                logger.debug("ChannelStateStore SQLite put_conversation_ref failed: %s", exc)

    def get_conversation_ref(
        self,
        channel: str,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        """Fetch persisted conversation routing metadata."""
        cached = self._memory.get_conversation_ref(channel, conversation_id)
        if cached is not None:
            return cached
        ref_key = self._conv_ref_key(channel, conversation_id)

        pg_uri = self._effective_postgres_uri()
        if pg_uri:
            try:
                import psycopg

                with psycopg.connect(pg_uri, autocommit=True) as conn:
                    self._ensure_pg_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT payload_json FROM {_CONVERSATION_REFS_TABLE} WHERE ref_key = %s LIMIT 1",
                            (ref_key,),
                        )
                        row = cur.fetchone()
                        if row and row[0]:
                            parsed = json.loads(str(row[0]))
                            if isinstance(parsed, dict):
                                self._memory.put_conversation_ref(channel, conversation_id, parsed)
                                return parsed
                return None
            except Exception as exc:
                logger.debug("ChannelStateStore Postgres get_conversation_ref failed: %s", exc)

        db_path = self._effective_sqlite_path()
        if db_path is not None and db_path.exists():
            try:
                with sqlite3.connect(str(db_path), timeout=5.0) as conn:
                    self._ensure_sqlite_schema(conn)
                    cur = conn.execute(
                        f"SELECT payload_json FROM {_CONVERSATION_REFS_TABLE} WHERE ref_key = ? LIMIT 1",
                        (ref_key,),
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        parsed = json.loads(str(row[0]))
                        if isinstance(parsed, dict):
                            self._memory.put_conversation_ref(channel, conversation_id, parsed)
                            return parsed
            except Exception as exc:
                logger.debug("ChannelStateStore SQLite get_conversation_ref failed: %s", exc)
        return None

    # ── Distributed Transport Leases ──────────────────────────────────

    def try_acquire_lease(
        self,
        lease_key: str,
        owner_id: str,
        *,
        ttl_seconds: float = 45.0,
        now: float | None = None,
    ) -> bool:
        """Atomically acquire or renew `lease_key` across replicas."""
        ts = time.time() if now is None else now
        expires_at = ts + ttl_seconds

        pg_uri = self._effective_postgres_uri()
        if pg_uri:
            try:
                import psycopg

                with psycopg.connect(pg_uri, autocommit=True) as conn:
                    self._ensure_pg_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            f"""
                            INSERT INTO {_LEASES_TABLE} (lease_key, owner_id, lease_expires_at, updated_at)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (lease_key) DO UPDATE SET
                                owner_id = EXCLUDED.owner_id,
                                lease_expires_at = EXCLUDED.lease_expires_at,
                                updated_at = EXCLUDED.updated_at
                            WHERE {_LEASES_TABLE}.owner_id = %s
                               OR {_LEASES_TABLE}.lease_expires_at <= %s
                            RETURNING owner_id
                            """,
                            (lease_key, owner_id, expires_at, ts, owner_id, ts),
                        )
                        row = cur.fetchone()
                        return bool(row and str(row[0]) == owner_id)
            except Exception as exc:
                logger.debug("ChannelStateStore Postgres try_acquire_lease failed: %s", exc)

        db_path = self._effective_sqlite_path()
        if db_path is not None:
            try:
                db_path.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(str(db_path), timeout=10.0) as conn:
                    self._ensure_sqlite_schema(conn)
                    conn.execute("BEGIN IMMEDIATE")
                    cur = conn.execute(
                        f"SELECT owner_id, lease_expires_at FROM {_LEASES_TABLE} WHERE lease_key = ?",
                        (lease_key,),
                    )
                    row = cur.fetchone()
                    if row is None or float(row[1]) <= ts or str(row[0]) == owner_id:
                        conn.execute(
                            f"""
                            INSERT INTO {_LEASES_TABLE} (lease_key, owner_id, lease_expires_at, updated_at)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(lease_key) DO UPDATE SET
                                owner_id = excluded.owner_id,
                                lease_expires_at = excluded.lease_expires_at,
                                updated_at = excluded.updated_at
                            """,
                            (lease_key, owner_id, expires_at, ts),
                        )
                        conn.commit()
                        return True
                    conn.rollback()
                    return False
            except Exception as exc:
                logger.debug("ChannelStateStore SQLite try_acquire_lease failed: %s", exc)

        return self._memory.try_acquire_lease(lease_key, owner_id, ttl_seconds=ttl_seconds, now=ts)

    def renew_lease(
        self,
        lease_key: str,
        owner_id: str,
        *,
        ttl_seconds: float = 45.0,
        now: float | None = None,
    ) -> bool:
        """Renew an active lease if still held by `owner_id`."""
        ts = time.time() if now is None else now
        expires_at = ts + ttl_seconds

        pg_uri = self._effective_postgres_uri()
        if pg_uri:
            try:
                import psycopg

                with psycopg.connect(pg_uri, autocommit=True) as conn:
                    self._ensure_pg_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            f"""
                            UPDATE {_LEASES_TABLE}
                            SET lease_expires_at = %s, updated_at = %s
                            WHERE lease_key = %s AND owner_id = %s AND lease_expires_at > %s
                            """,
                            (expires_at, ts, lease_key, owner_id, ts),
                        )
                        return cur.rowcount > 0
            except Exception as exc:
                logger.debug("ChannelStateStore Postgres renew_lease failed: %s", exc)

        db_path = self._effective_sqlite_path()
        if db_path is not None and db_path.exists():
            try:
                with sqlite3.connect(str(db_path), timeout=5.0) as conn:
                    self._ensure_sqlite_schema(conn)
                    cur = conn.execute(
                        f"""
                        UPDATE {_LEASES_TABLE}
                        SET lease_expires_at = ?, updated_at = ?
                        WHERE lease_key = ? AND owner_id = ? AND lease_expires_at > ?
                        """,
                        (expires_at, ts, lease_key, owner_id, ts),
                    )
                    conn.commit()
                    return cur.rowcount > 0
            except Exception as exc:
                logger.debug("ChannelStateStore SQLite renew_lease failed: %s", exc)

        return self._memory.renew_lease(lease_key, owner_id, ttl_seconds=ttl_seconds, now=ts)

    def release_lease(self, lease_key: str, owner_id: str) -> None:
        """Release `lease_key` if owned by `owner_id`."""
        self._memory.release_lease(lease_key, owner_id)
        pg_uri = self._effective_postgres_uri()
        if pg_uri:
            try:
                import psycopg

                with psycopg.connect(pg_uri, autocommit=True) as conn:
                    self._ensure_pg_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            f"DELETE FROM {_LEASES_TABLE} WHERE lease_key = %s AND owner_id = %s",
                            (lease_key, owner_id),
                        )
                return
            except Exception as exc:
                logger.debug("ChannelStateStore Postgres release_lease failed: %s", exc)

        db_path = self._effective_sqlite_path()
        if db_path is not None and db_path.exists():
            try:
                with sqlite3.connect(str(db_path), timeout=5.0) as conn:
                    self._ensure_sqlite_schema(conn)
                    conn.execute(
                        f"DELETE FROM {_LEASES_TABLE} WHERE lease_key = ? AND owner_id = ?",
                        (lease_key, owner_id),
                    )
                    conn.commit()
            except Exception as exc:
                logger.debug("ChannelStateStore SQLite release_lease failed: %s", exc)

    def get_lease_owner(self, lease_key: str, *, now: float | None = None) -> str | None:
        """Return active owner ID of `lease_key`, or `None`."""
        ts = time.time() if now is None else now
        pg_uri = self._effective_postgres_uri()
        if pg_uri:
            try:
                import psycopg

                with psycopg.connect(pg_uri, autocommit=True) as conn:
                    self._ensure_pg_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT owner_id FROM {_LEASES_TABLE} WHERE lease_key = %s AND lease_expires_at > %s LIMIT 1",
                            (lease_key, ts),
                        )
                        row = cur.fetchone()
                        return str(row[0]) if row and row[0] else None
            except Exception as exc:
                logger.debug("ChannelStateStore Postgres get_lease_owner failed: %s", exc)

        db_path = self._effective_sqlite_path()
        if db_path is not None and db_path.exists():
            try:
                with sqlite3.connect(str(db_path), timeout=5.0) as conn:
                    self._ensure_sqlite_schema(conn)
                    cur = conn.execute(
                        f"SELECT owner_id FROM {_LEASES_TABLE} WHERE lease_key = ? AND lease_expires_at > ? LIMIT 1",
                        (lease_key, ts),
                    )
                    row = cur.fetchone()
                    return str(row[0]) if row and row[0] else None
            except Exception as exc:
                logger.debug("ChannelStateStore SQLite get_lease_owner failed: %s", exc)

        return self._memory.get_lease_owner(lease_key, now=ts)

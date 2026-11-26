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

"""Factory and LangGraph BaseStore bridge for TalkOps OperationalStore."""

from __future__ import annotations

import asyncio
import atexit
import os
import threading
import time
from pathlib import Path
from typing import Any

from talkops.config.paths import DATA_DIR
from talkops.storage.postgres_store import PostgresOperationalStore
from talkops.storage.protocol import MemoryRuleRecord, OperationalStore
from talkops.storage.sqlite_store import SqliteOperationalStore

_singleton_lock = threading.Lock()
_default_store: OperationalStore | None = None


def create_operational_store(
    *,
    postgres_uri: str | None = None,
    sqlite_path: str | Path | None = None,
    project_root: Path | None = None,
) -> OperationalStore:
    """Create a PostgreSQL or local WAL-mode SQLite ``OperationalStore``."""
    uri = (
        postgres_uri
        or os.environ.get("TALKOPS_POSTGRES_URI")
        or os.environ.get("TALKOPS_DATABASE_URL")
        or ""
    ).strip()
    if uri:
        return PostgresOperationalStore(uri)

    env_sqlite = (os.environ.get("TALKOPS_SQLITE_PATH") or "").strip()
    if sqlite_path is not None:
        resolved_sqlite = Path(sqlite_path)
    elif env_sqlite:
        resolved_sqlite = Path(env_sqlite)
    elif project_root is not None:
        resolved_sqlite = Path(project_root) / ".talkops" / "state.db"
    else:
        resolved_sqlite = DATA_DIR / "state.db"

    return SqliteOperationalStore(resolved_sqlite)


def get_operational_store(*, project_root: Path | None = None) -> OperationalStore:
    """Return process-wide default ``OperationalStore`` singleton."""
    global _default_store
    if _default_store is None:
        with _singleton_lock:
            if _default_store is None:
                _default_store = create_operational_store(project_root=project_root)
    return _default_store


def set_operational_store(store: OperationalStore | None) -> None:
    """Override or reset the process-wide default ``OperationalStore`` (used in tests/server startup)."""
    global _default_store
    with _singleton_lock:
        _default_store = store


def reset_operational_store_cache() -> None:
    """Clear the cached process-wide default ``OperationalStore`` singleton."""
    close_operational_store()


async def aclose_operational_store() -> None:
    """Asynchronously close and clear the process-wide default ``OperationalStore`` singleton."""
    global _default_store
    with _singleton_lock:
        store = _default_store
        _default_store = None
    if store is not None:
        try:
            await store.aclose()
        except Exception:
            close_fn = getattr(store, "close", None)
            if callable(close_fn):
                close_fn()


def close_operational_store() -> None:
    """Synchronously close and clear the process-wide default ``OperationalStore`` singleton."""
    global _default_store
    with _singleton_lock:
        store = _default_store
        _default_store = None
    if store is None:
        return
    close_fn = getattr(store, "close", None)
    if callable(close_fn):
        try:
            close_fn()
            return
        except Exception:
            pass
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(store.aclose())
    except RuntimeError:
        try:
            asyncio.run(store.aclose())
        except Exception:
            pass


_register_thread_atexit = getattr(threading, "_register_atexit", None)
if callable(_register_thread_atexit):
    _register_thread_atexit(close_operational_store)
atexit.register(close_operational_store)


async def sync_rule_to_langgraph_store(
    langgraph_store: Any | None,
    rule: MemoryRuleRecord,
) -> None:
    """Mirror a calibrated ``MemoryRuleRecord`` into LangGraph ``BaseStore`` with native TTL.

    Enables tools and ``@dynamic_prompt`` / ``AgentMiddleware`` hooks accessing ``runtime.store``
    to query calibrated SRE episodic and procedural rules via standard LangGraph namespaces
    ``("sre_rules", tenant_id, scope_service)``.
    """
    if langgraph_store is None:
        return
    namespace = ("sre_rules", rule.tenant_id, rule.scope_service)
    ttl_minutes: float | None = None
    if rule.expires_at is not None:
        remaining_sec = max(60.0, rule.expires_at - time.time())
        ttl_minutes = remaining_sec / 60.0

    payload = {
        "rule_id": rule.rule_id,
        "memory_type": rule.memory_type,
        "rule_text": rule.rule_text,
        "source": rule.source,
        "confidence_weight": rule.confidence_weight,
        "active": rule.active,
    }
    try:
        if hasattr(langgraph_store, "aput"):
            if ttl_minutes is not None:
                try:
                    await langgraph_store.aput(namespace, rule.rule_id, payload, ttl=ttl_minutes)
                    return
                except TypeError:
                    await langgraph_store.aput(namespace, rule.rule_id, payload)
                    return
            await langgraph_store.aput(namespace, rule.rule_id, payload)
        elif hasattr(langgraph_store, "put"):
            langgraph_store.put(namespace, rule.rule_id, payload)
    except Exception:
        pass

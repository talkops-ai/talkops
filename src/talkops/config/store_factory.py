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

"""Config store factory — auto-detects postgres vs sqlite backend.

Uses the same connection-detection pattern as the checkpointer
(``state/checkpointer.py``): if ``TALKOPS_POSTGRES_URI`` or
``DATABASE_URL`` is set, use PostgreSQL; otherwise fall back to SQLite.
"""

from __future__ import annotations

import os

from talkops.config.store import ConfigStore
from talkops.utils.logger import get_logger

logger = get_logger(__name__)


def _has_postgres_config() -> bool:
    """Check if PostgreSQL connection is configured."""
    return bool(
        os.environ.get("POSTGRES_URI") or os.environ.get("TALKOPS_POSTGRES_URI") or os.environ.get("DATABASE_URL")
    )


async def create_config_store(backend: str = "auto") -> ConfigStore:
    """Create and initialize a ConfigStore.

    Args:
        backend: ``"auto"`` (detect from env), ``"postgres"``, or ``"sqlite"``.

    Returns:
        Initialized ``ConfigStore`` instance ready for reads/writes.
    """
    if backend == "auto":
        backend = "postgres" if _has_postgres_config() else "sqlite"

    if backend == "postgres":
        from talkops.config.adapters.postgres import PostgresConfigAdapter

        uri = (
            os.environ.get("POSTGRES_URI")
            or os.environ.get("TALKOPS_POSTGRES_URI")
            or os.environ.get("DATABASE_URL", "")
        )
        adapter = PostgresConfigAdapter(uri)

    elif backend == "sqlite":
        from talkops.config.adapters.sqlite import SqliteConfigAdapter
        from talkops.config.paths import STATE_DIR

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        db_path = STATE_DIR / "config.db"
        adapter = SqliteConfigAdapter(db_path)

    else:
        raise ValueError(f"Unknown config store backend: {backend!r}")

    store = ConfigStore(adapter)
    await store.initialize()
    logger.info("ConfigStore created with %s backend", backend)
    return store


def create_config_store_sync(backend: str = "auto") -> ConfigStore:
    """Create a ConfigStore synchronously with auto-detected backend.

    Args:
        backend: ``"auto"`` (detect from env), ``"postgres"``, or ``"sqlite"``.

    Returns:
        ``ConfigStore`` instance with auto-detected adapter.
    """
    if backend == "auto":
        backend = "postgres" if _has_postgres_config() else "sqlite"

    if backend == "postgres":
        from talkops.config.adapters.postgres import PostgresConfigAdapter

        uri = (
            os.environ.get("POSTGRES_URI")
            or os.environ.get("TALKOPS_POSTGRES_URI")
            or os.environ.get("DATABASE_URL", "")
        )
        adapter = PostgresConfigAdapter(uri)

    elif backend == "sqlite":
        from talkops.config.adapters.sqlite import SqliteConfigAdapter
        from talkops.config.paths import STATE_DIR

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        db_path = STATE_DIR / "config.db"
        adapter = SqliteConfigAdapter(db_path)

    else:
        raise ValueError(f"Unknown config store backend: {backend!r}")

    return ConfigStore(adapter)

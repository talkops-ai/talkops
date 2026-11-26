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

"""Unified OperationalStore package (PostgreSQL RRF + SQLite WAL/FTS5 + LangGraph BaseStore bridge)."""

from talkops.storage.factory import (
    aclose_operational_store,
    close_operational_store,
    create_operational_store,
    get_operational_store,
    set_operational_store,
    sync_rule_to_langgraph_store,
)
from talkops.storage.postgres_store import PostgresOperationalStore
from talkops.storage.protocol import (
    AutoQAScoreRecord,
    ChangeEventRecord,
    CorrelatedAlertItem,
    DurableWorkflowJob,
    DurableWorkflowPhase,
    DurableWorkflowStatus,
    EmbedderFn,
    IncidentRecord,
    MemoryRuleRecord,
    OperationalStore,
    TopologyEdge,
    TopologyNode,
    VerificationStatus,
    build_langchain_embedder,
    compute_rrf_scores,
    cosine_similarity,
    deterministic_local_embedding,
)
from talkops.storage.sqlite_store import SqliteOperationalStore

__all__ = [
    "AutoQAScoreRecord",
    "ChangeEventRecord",
    "CorrelatedAlertItem",
    "DurableWorkflowJob",
    "DurableWorkflowPhase",
    "DurableWorkflowStatus",
    "EmbedderFn",
    "IncidentRecord",
    "MemoryRuleRecord",
    "OperationalStore",
    "PostgresOperationalStore",
    "SqliteOperationalStore",
    "TopologyEdge",
    "TopologyNode",
    "VerificationStatus",
    "aclose_operational_store",
    "build_langchain_embedder",
    "close_operational_store",
    "compute_rrf_scores",
    "cosine_similarity",
    "create_operational_store",
    "deterministic_local_embedding",
    "get_operational_store",
    "set_operational_store",
    "sync_rule_to_langgraph_store",
]

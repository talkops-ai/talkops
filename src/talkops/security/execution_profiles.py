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

"""Open-source Execution Trust Profiles & Watson-style Metadata-Only Guard.

Implements TalkOps v2's 4 declarative execution trust profiles:
1. ``readonly-metadata`` (alias ``watson-readonly``): Zero writes across filesystem,
   shell, cloud/K8s CLIs, and MCP servers + strict datastore metadata-only SQL/NoSQL
   enforcement (system catalogs ``pg_stat_*``, ``information_schema``, ``EXPLAIN``,
   ``SHOW``, ``Redis INFO`` allowed; ``SELECT`` on user business tables blocked).
2. ``gitops-pr``: Local workspace edits, branch creation, and PR creation allowed;
   direct live cluster/cloud/database mutations blocked.
3. ``hitl-gated``: Read operations allowed; live mutating operations require interactive
   or webhook HITL approval.
4. ``autonomous-tnr``: Autonomous remediation permitted within ``scopeGuards`` and
   ``egressAllowlist`` boundaries, paired with TNR auto-undo & durable verification.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from deepagents import (
    FilesystemPermission,
    HarnessProfileConfig,
    register_harness_profile,
)

from talkops.plugins.models import ExecutionTrustProfile, normalize_execution_profile

# ---------------------------------------------------------------------------
# Shell / CLI / SQL / MCP Classification Patterns
# ---------------------------------------------------------------------------

_SHELL_MUTATING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "k8s_live_mutation",
        re.compile(
            r"(?i)\b(?:kubectl|oc)\s+(?:apply|create|delete|patch|replace|scale|autoscale|rollout\s+(?:restart|undo|pause|resume)|drain|cordon|uncordon|taint|label|annotate|edit|exec|cp|port-forward)\b"
        ),
    ),
    (
        "helm_live_mutation",
        re.compile(r"(?i)\bhelm\s+(?:install|upgrade|uninstall|delete|rollback)\b"),
    ),
    (
        "iac_live_mutation",
        re.compile(
            r"(?i)\b(?:terraform|tofu|terragrunt|pulumi|cdk)\s+(?:apply|destroy|import|state\s+(?:rm|mv|push)|deploy|up)\b"
        ),
    ),
    (
        "aws_live_mutation",
        re.compile(
            r"(?i)\baws\s+[a-z0-9-]+\s+(?:create-|delete-|update-|put-|terminate-|stop-|reboot-|modify-|attach-|detach-|revoke-|authorize-|run-instances|execute-)"
        ),
    ),
    (
        "gcloud_live_mutation",
        re.compile(
            r"(?i)\bgcloud\s+(?:[a-z0-9-]+\s+)*(?:create|delete|update|deploy|set-iam-policy|add-iam-policy-binding|remove-iam-policy-binding|reset|stop)\b"
        ),
    ),
    (
        "az_live_mutation",
        re.compile(r"(?i)\baz\s+(?:[a-z0-9-]+\s+)*(?:create|delete|update|set|restart|stop|deallocate)\b"),
    ),
    (
        "systemd_process_mutation",
        re.compile(r"(?i)\b(?:systemctl\s+(?:restart|stop|start|reload|enable|disable|mask)|kill|pkill|killall|reboot|shutdown)\b"),
    ),
)

_SHELL_FS_WRITE_RE = re.compile(
    r"(?i)(?:\b(?:rm|mv|cp|mkdir|rmdir|touch|chmod|chown|truncate|dd|tee|sed\s+-i)\b|(?<![0-9<>])>{1,2}\s*[^\s&|]+)"
)

_GITOPS_ALLOWED_SHELL_RE = re.compile(
    r"(?i)^\s*(?:git\s+(?:status|diff|log|show|branch|checkout|switch|add|commit|rev-parse|remote|fetch|push\s+(?!--force|-f)\S+\s+(?!main\b|master\b|prod\b|production\b)\S+)|gh\s+pr\s+(?:create|list|view|status|diff)|glab\s+mr\s+(?:create|list|view|diff))\b"
)

_SQL_MUTATION_RE = re.compile(
    r"(?is)\b(?:INSERT\s+INTO|UPDATE\s+\S+\s+SET|DELETE\s+FROM|DROP\s+(?:TABLE|INDEX|DATABASE|SCHEMA|VIEW|ROLE)|ALTER\s+(?:TABLE|SYSTEM|DATABASE|ROLE|INDEX)|TRUNCATE\s+|GRANT\s+|REVOKE\s+|CREATE\s+(?:TABLE|INDEX|DATABASE|USER|ROLE|OR\s+REPLACE)|COPY\s+\S+\s+FROM|VACUUM\s+FULL|REINDEX\s+|FLUSHALL|FLUSHDB|CONFIG\s+SET)\b"
)

_SQL_SELECT_FROM_RE = re.compile(
    r"(?is)\b(?:SELECT|TABLE)\b[\s\S]*?\b(?:FROM|JOIN)\s+([A-Za-z0-9_.\"`\[\]]+)"
)

_ALLOWED_METADATA_RELATIONS: tuple[str, ...] = (
    "pg_stat_",
    "pg_statio_",
    "pg_locks",
    "pg_settings",
    "pg_class",
    "pg_index",
    "pg_indexes",
    "pg_namespace",
    "pg_attribute",
    "pg_constraint",
    "pg_database",
    "pg_tablespace",
    "pg_replication",
    "pg_prepared_xacts",
    "pg_catalog.",
    "information_schema.",
    "performance_schema.",
    "mysql.slow_log",
    "mysql.innodb_",
    "sys.",
    "crdb_internal.",
    "system.",
    "sqlite_master",
    "pragma_",
)

_ALLOWED_METADATA_COMMANDS_RE = re.compile(
    r"(?is)^\s*(?:SHOW\b|DESCRIBE\b|DESC\b|PRAGMA\b|EXPLAIN\b(?![\s\S]*\bANALYZE\b)|INFO\b|SLOWLOG\b|CLIENT\s+LIST\b|MEMORY\s+STATS\b|CLUSTER\s+INFO\b|PING\b|SELECT\s+(?:version|current_database|current_setting|now|pg_is_in_recovery|pg_postmaster_start_time|pg_backend_pid|txid_current|1)\s*\()"
)

_INTERNAL_NON_MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "write_todos",
        "read_todos",
        "think",
        "ask_user",
        "task",
        "record_operational_memory_rule",
        "record_hypothesis_verdict",
    }
)

_MUTATING_TOOL_NAME_RE = re.compile(
    r"(?i)(?:^|[:_\-.])(?:create|delete|remove|destroy|drop|truncate|update|patch|put|apply|install|upgrade|exec|terminate|reboot|restart|scale|rollback|uninstall|kill|write|edit)(?:$|[:_\-.])"
)

_IRREVERSIBLE_DESTRUCTIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sql_drop_or_truncate", re.compile(r"(?is)\b(?:DROP\s+(?:TABLE|DATABASE|SCHEMA|INDEX|VIEW)|TRUNCATE\s+TABLE|FLUSHALL|FLUSHDB)\b")),
    (
        "aws_irreversible_delete",
        re.compile(
            r"(?i)\baws\s+[^\n]*\b(?:terminate-instances|delete-db-instance|delete-db-cluster|delete-cluster|delete-bucket|delete-table|rb\s+s3://)"
        ),
    ),
    (
        "k8s_destructive_delete",
        re.compile(
            r"(?i)\bkubectl\s+delete\s+(?:namespace|ns|pv|pvc|crd|customresourcedefinition|clusterrole|storageclass)\b|\bkubectl\s+delete\b[^\n]*--all(?:-namespaces)?\b"
        ),
    ),
    ("iac_destroy", re.compile(r"(?i)\b(?:terraform|tofu|pulumi|cdk)\s+destroy\b")),
    ("rm_rf_destructive", re.compile(r"(?i)\brm\s+-(?:[a-zA-Z]*r[a-zA-Z]*f|[a-zA-Z]*f[a-zA-Z]*r)[a-zA-Z]*\b")),
)

_IRREVERSIBLE_DESTRUCTIVE_TOOL_RE = re.compile(
    r"(?i)(?:^|[:_\-.])(?:drop_table|drop_database|truncate_table|terminate_instance|terminate_instances|delete_database|delete_db_instance|delete_cluster|delete_namespace|destroy_stack|terraform_destroy)(?:$|[:_\-.])"
)

_GITOPS_PR_TOOL_NAME_RE = re.compile(
    r"(?i)(?:write_file|edit_file|create_pull_request|open_pull_request|create_merge_request|create_branch|create_or_update_file|push_files|git_)"
)


@dataclass(frozen=True)
class PolicyDecision:
    """Decision returned by execution profile & scope policy evaluation."""

    allowed: bool
    reason: str = ""
    violation_code: str = ""


def inspect_datastore_query(
    query: str,
    *,
    metadata_only: bool = True,
) -> PolicyDecision:
    """Validate SQL / Redis / datastore queries under Watson-style metadata-only isolation."""
    cleaned = (query or "").strip()
    if not cleaned:
        return PolicyDecision(allowed=True)

    if _SQL_MUTATION_RE.search(cleaned):
        return PolicyDecision(
            allowed=False,
            violation_code="DATASTORE_MUTATION_BLOCKED",
            reason=(
                "Datastore DML/DDL mutation blocked by execution policy. "
                "Only diagnostic read queries are permitted."
            ),
        )

    if not metadata_only:
        return PolicyDecision(allowed=True)

    # Strip SQL comments before relation inspection
    no_comments = re.sub(r"--[^\n]*", " ", cleaned)
    no_comments = re.sub(r"/\*[\s\S]*?\*/", " ", no_comments).strip()

    # Check multi-statement chaining (`; SELECT * FROM users`)
    statements = [stmt.strip() for stmt in no_comments.split(";") if stmt.strip()]
    for stmt in statements:
        if _SQL_MUTATION_RE.search(stmt):
            return PolicyDecision(
                allowed=False,
                violation_code="DATASTORE_MUTATION_BLOCKED",
                reason="Mutating SQL statement blocked by metadata-only datastore policy.",
            )

        if _ALLOWED_METADATA_COMMANDS_RE.match(stmt):
            continue

        from_matches = [m.group(1).strip('"`[]').lower() for m in _SQL_SELECT_FROM_RE.finditer(stmt)]
        if not from_matches:
            if re.match(r"(?is)^\s*SELECT\b", stmt):
                continue
            return PolicyDecision(
                allowed=False,
                violation_code="DATASTORE_NON_METADATA_COMMAND",
                reason=(
                    f"Command {stmt[:60]!r} is not on the metadata-only diagnostic allowlist "
                    "(allowed: pg_stat_*, pg_locks, information_schema.*, EXPLAIN, SHOW, Redis INFO/SLOWLOG)."
                ),
            )

        # Also extract CTE names (`WITH cte_name AS (...)`) so internal CTE references aren't flagged as user tables
        cte_names = {
            m.group(1).strip('"`[]').lower()
            for m in re.finditer(r"(?is)\b(?:WITH|,)\s+([A-Za-z0-9_]+)\s+AS\s*\(", stmt)
        }

        for rel in from_matches:
            if rel in cte_names:
                continue
            # Strip optional `public.` prefix when checking `pg_*` system views
            normalized_rel = rel.removeprefix("pg_catalog.")
            is_allowed = any(
                rel.startswith(prefix) or normalized_rel.startswith(prefix)
                for prefix in _ALLOWED_METADATA_RELATIONS
            )
            if not is_allowed:
                return PolicyDecision(
                    allowed=False,
                    violation_code="DATASTORE_USER_TABLE_BLOCKED",
                    reason=(
                        f"Metadata-only datastore policy ('watson-readonly') blocked query against "
                        f"non-catalog table/relation {rel!r}. Query system views (pg_stat_activity, "
                        "pg_locks, information_schema, EXPLAIN) instead of reading user row data."
                    ),
                )

    return PolicyDecision(allowed=True)


def check_egress_url(url: str, egress_allowlist: Sequence[str]) -> PolicyDecision:
    """Verify an outbound URL's hostname against plugin ``egressAllowlist`` patterns."""
    if not egress_allowlist:
        return PolicyDecision(allowed=True)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return PolicyDecision(
            allowed=False,
            violation_code="EGRESS_INVALID_URL",
            reason=f"Could not extract hostname from URL {url!r} under active egressAllowlist.",
        )
    for pattern in egress_allowlist:
        pat = pattern.lower().strip()
        if pat == "*" or fnmatch.fnmatchcase(host, pat):
            return PolicyDecision(allowed=True)
        if pat.startswith("*.") and host == pat[2:]:
            return PolicyDecision(allowed=True)
    return PolicyDecision(
        allowed=False,
        violation_code="EGRESS_HOST_BLOCKED",
        reason=f"Outbound host {host!r} is not permitted by active egressAllowlist {list(egress_allowlist)}.",
    )


def check_scope_guards(
    tool_name: str,
    tool_args: Mapping[str, Any],
    scope_guards: Mapping[str, Any],
) -> PolicyDecision:
    """Enforce declarative ``scopeGuards`` (blocked namespaces, protected resources, blast radius)."""
    if not scope_guards:
        return PolicyDecision(allowed=True)

    serialized_args = str(dict(tool_args)).lower()
    namespace_arg = str(
        tool_args.get("namespace")
        or tool_args.get("k8s_namespace")
        or ""
    ).strip().lower()

    # Extract `-n <ns>` or `--namespace <ns>` from shell commands if present
    cmd_str = str(tool_args.get("command") or "")
    if not namespace_arg and cmd_str:
        ns_match = re.search(r"(?:--namespace(?:=|\s+)|-n\s+)([A-Za-z0-9_.-]+)", cmd_str)
        if ns_match:
            namespace_arg = ns_match.group(1).lower()

    blocked_namespaces = [
        str(ns).lower()
        for ns in (scope_guards.get("blockedNamespaces") or scope_guards.get("blocked_namespaces") or [])
    ]
    if namespace_arg and namespace_arg in blocked_namespaces:
        # Allow read-only inspection unless blockReadsInProtectedNamespaces is set
        is_mutating = bool(_MUTATING_TOOL_NAME_RE.search(tool_name)) or any(
            pat.search(cmd_str) for _, pat in _SHELL_MUTATING_PATTERNS
        )
        if is_mutating or bool(scope_guards.get("blockReadsInProtectedNamespaces", False)):
            return PolicyDecision(
                allowed=False,
                violation_code="SCOPE_NAMESPACE_PROTECTED",
                reason=f"Namespace {namespace_arg!r} is protected by scopeGuards.blockedNamespaces.",
            )

    allowed_namespaces = [
        str(ns).lower()
        for ns in (scope_guards.get("allowedNamespaces") or scope_guards.get("allowed_namespaces") or [])
    ]
    if allowed_namespaces and namespace_arg and namespace_arg not in allowed_namespaces:
        return PolicyDecision(
            allowed=False,
            violation_code="SCOPE_NAMESPACE_OUTSIDE_ALLOWLIST",
            reason=f"Namespace {namespace_arg!r} is outside scopeGuards.allowedNamespaces {allowed_namespaces}.",
        )

    blocked_patterns = [
        str(p)
        for p in (scope_guards.get("blockedPatterns") or scope_guards.get("blocked_patterns") or [])
    ]
    for pat in blocked_patterns:
        if pat.lower() in serialized_args:
            return PolicyDecision(
                allowed=False,
                violation_code="SCOPE_PATTERN_BLOCKED",
                reason=f"Tool call matched blocked scope pattern {pat!r}.",
            )

    return PolicyDecision(allowed=True)


def evaluate_tool_call_policy(
    *,
    profile: str | ExecutionTrustProfile,
    tool_name: str,
    tool_args: Mapping[str, Any] | None = None,
    tool_metadata: Mapping[str, Any] | None = None,
    metadata_only_datastores: bool = False,
    egress_allowlist: Sequence[str] = (),
    scope_guards: Mapping[str, Any] | None = None,
) -> PolicyDecision:
    """Evaluate a tool invocation against the active Execution Trust Profile and plugin policies."""
    canonical_profile = normalize_execution_profile(str(profile))
    raw_args = dict(tool_args or {})
    meta = dict(tool_metadata or {})
    effective_metadata_only = metadata_only_datastores or (canonical_profile == "readonly-metadata")

    # Unwrap `call_mcp_tool` proxy invocations so inner MCP tool names and arguments are evaluated
    effective_tool_name = tool_name
    args = raw_args
    if tool_name == "call_mcp_tool":
        inner_tool = str(
            raw_args.get("tool_name")
            or raw_args.get("tool")
            or raw_args.get("name")
            or ""
        ).strip()
        srv_name = str(raw_args.get("server_name") or raw_args.get("server") or "").strip()
        if inner_tool:
            effective_tool_name = f"{srv_name}:{inner_tool}" if srv_name else inner_tool
        inner_args = raw_args.get("arguments") or raw_args.get("args") or raw_args.get("input")
        if isinstance(inner_args, Mapping):
            args = {**raw_args, **dict(inner_args)}
        meta.setdefault("_talkops_mcp", True)

    if effective_tool_name in _INTERNAL_NON_MUTATING_TOOLS:
        return PolicyDecision(allowed=True)

    # 1. Egress URL check (fetch_url, web_search, HTTP tools)
    for url_key in ("url", "endpoint", "target_url", "webhook_url"):
        url_val = args.get(url_key)
        if isinstance(url_val, str) and url_val.startswith(("http://", "https://")):
            egress_decision = check_egress_url(url_val, egress_allowlist)
            if not egress_decision.allowed:
                return egress_decision

    # 2. Scope guards check
    if scope_guards:
        scope_decision = check_scope_guards(effective_tool_name, args, scope_guards)
        if not scope_decision.allowed:
            return scope_decision

    # 3. Datastore query inspection (applies whenever SQL/query argument is passed or in readonly-metadata)
    for sql_key in ("sql", "query", "statement", "command_query", "redis_command"):
        raw_q = args.get(sql_key)
        if isinstance(raw_q, str) and raw_q.strip():
            looks_like_db = (
                sql_key in ("sql", "statement", "redis_command")
                or any(
                    tok in effective_tool_name.lower()
                    for tok in ("sql", "postgres", "psql", "mysql", "rds", "redis", "db", "database", "query_db")
                )
            )
            if looks_like_db:
                if canonical_profile in ("readonly-metadata", "gitops-pr") or effective_metadata_only:
                    db_decision = inspect_datastore_query(raw_q, metadata_only=effective_metadata_only)
                    if not db_decision.allowed:
                        return db_decision
                elif canonical_profile == "autonomous-tnr":
                    for label, pat in _IRREVERSIBLE_DESTRUCTIVE_PATTERNS:
                        if pat.search(raw_q):
                            return PolicyDecision(
                                allowed=False,
                                violation_code="AUTONOMOUS_TNR_DESTRUCTIVE_BLOCKED",
                                reason=(
                                    f"Irreversible destructive datastore operation ({label}) is blocked under "
                                    "'autonomous-tnr' profile. Require explicit human-in-the-loop approval."
                                ),
                            )

    # Also inspect `psql -c "..."` or `mysql -e "..."` inside shell commands!
    cmd_str = str(args.get("command") or "")
    if cmd_str and effective_tool_name in ("execute", "bash", "run_command", "shell"):
        cli_sql_match = re.search(
            r"""(?is)\b(?:psql|mysql|mariadb|sqlite3|clickhouse-client)\b[^\n]*?(?:-c|--command|-e|--execute)\s+(['"])([\s\S]*?)\1""",
            cmd_str,
        )
        if cli_sql_match and (canonical_profile in ("readonly-metadata", "gitops-pr") or effective_metadata_only):
            db_decision = inspect_datastore_query(
                cli_sql_match.group(2),
                metadata_only=effective_metadata_only,
            )
            if not db_decision.allowed:
                return db_decision

    # 4. Profile-specific enforcement
    if canonical_profile == "readonly-metadata":
        if effective_tool_name in ("write_file", "edit_file"):
            return PolicyDecision(
                allowed=False,
                violation_code="READONLY_FILESYSTEM_BLOCKED",
                reason=(
                    f"Tool {effective_tool_name!r} is blocked under execution profile 'readonly-metadata' "
                    "('watson-readonly'). Only read-only diagnostics are permitted."
                ),
            )

        if cmd_str and effective_tool_name in ("execute", "bash", "run_command", "shell"):
            for label, pattern in _SHELL_MUTATING_PATTERNS:
                if pattern.search(cmd_str):
                    return PolicyDecision(
                        allowed=False,
                        violation_code="READONLY_SHELL_MUTATION_BLOCKED",
                        reason=(
                            f"Shell command blocked under 'readonly-metadata' profile ({label}). "
                            "Live infrastructure mutations are prohibited."
                        ),
                    )
            if _SHELL_FS_WRITE_RE.search(cmd_str):
                return PolicyDecision(
                    allowed=False,
                    violation_code="READONLY_SHELL_WRITE_BLOCKED",
                    reason="Filesystem-modifying shell command blocked under 'readonly-metadata' profile.",
                )

        # MCP / generic tool metadata & name checks
        if meta.get("destructiveHint") is True:
            return PolicyDecision(
                allowed=False,
                violation_code="READONLY_DESTRUCTIVE_MCP_BLOCKED",
                reason=f"Destructive tool {effective_tool_name!r} is blocked under 'readonly-metadata' profile.",
            )
        if meta.get("_talkops_mcp") and meta.get("readOnlyHint") is False and _MUTATING_TOOL_NAME_RE.search(effective_tool_name):
            return PolicyDecision(
                allowed=False,
                violation_code="READONLY_MUTATING_MCP_BLOCKED",
                reason=f"Mutating MCP tool {effective_tool_name!r} is blocked under 'readonly-metadata' profile.",
            )
        if _MUTATING_TOOL_NAME_RE.search(effective_tool_name) and meta.get("readOnlyHint") is not True:
            return PolicyDecision(
                allowed=False,
                violation_code="READONLY_MUTATING_TOOL_BLOCKED",
                reason=f"Mutating tool {effective_tool_name!r} is blocked under 'readonly-metadata' profile.",
            )

    elif canonical_profile == "gitops-pr":
        # Allow local file edits & git/PR workflows, block direct live infra mutations
        if cmd_str and effective_tool_name in ("execute", "bash", "run_command", "shell"):
            if not _GITOPS_ALLOWED_SHELL_RE.match(cmd_str):
                for label, pattern in _SHELL_MUTATING_PATTERNS:
                    if pattern.search(cmd_str):
                        return PolicyDecision(
                            allowed=False,
                            violation_code="GITOPS_LIVE_MUTATION_BLOCKED",
                            reason=(
                                f"Direct live mutation ({label}) is blocked under 'gitops-pr' profile. "
                                "Commit IaC/manifest changes and open a Pull Request instead."
                            ),
                        )

        if (
            _MUTATING_TOOL_NAME_RE.search(effective_tool_name)
            and not _GITOPS_PR_TOOL_NAME_RE.search(effective_tool_name)
            and meta.get("readOnlyHint") is not True
        ):
            return PolicyDecision(
                allowed=False,
                violation_code="GITOPS_LIVE_TOOL_BLOCKED",
                reason=(
                    f"Direct live mutation tool {effective_tool_name!r} is blocked under 'gitops-pr' profile. "
                    "Generate a GitOps Pull Request instead."
                ),
            )

    elif canonical_profile == "autonomous-tnr":
        # Allow reversible/transactional remediations (rollout undo, scale, helm rollback, file edits),
        # while strictly blocking irreversible destructive operations without human intervention.
        if cmd_str and effective_tool_name in ("execute", "bash", "run_command", "shell"):
            for label, pattern in _IRREVERSIBLE_DESTRUCTIVE_PATTERNS:
                if pattern.search(cmd_str):
                    return PolicyDecision(
                        allowed=False,
                        violation_code="AUTONOMOUS_TNR_DESTRUCTIVE_BLOCKED",
                        reason=(
                            f"Irreversible destructive command ({label}) is blocked under 'autonomous-tnr' "
                            "profile. Escalate to human-in-the-loop ('hitl-gated') approval."
                        ),
                    )
        if _IRREVERSIBLE_DESTRUCTIVE_TOOL_RE.search(effective_tool_name):
            return PolicyDecision(
                allowed=False,
                violation_code="AUTONOMOUS_TNR_DESTRUCTIVE_TOOL_BLOCKED",
                reason=(
                    f"Irreversible destructive tool {effective_tool_name!r} is blocked under 'autonomous-tnr' "
                    "profile. Escalate to human-in-the-loop ('hitl-gated') approval."
                ),
            )

    return PolicyDecision(allowed=True)


def build_filesystem_permissions_for_profile(
    profile: str | ExecutionTrustProfile,
) -> list[FilesystemPermission]:
    """Build native DeepAgents ``FilesystemPermission`` rules for an execution trust profile."""
    canonical = normalize_execution_profile(str(profile))
    if canonical == "readonly-metadata":
        return [
            FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
            FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
        ]
    if canonical == "hitl-gated":
        return [
            FilesystemPermission(operations=["write"], paths=["/**"], mode="interrupt"),
            FilesystemPermission(operations=["read"], paths=["/**"], mode="allow"),
        ]
    return [
        FilesystemPermission(operations=["read", "write"], paths=["/**"], mode="allow"),
    ]


_PROFILES_REGISTERED = False


def register_talkops_execution_harness_profiles() -> None:
    """Register TalkOps v2's 4 execution trust profiles in DeepAgents' profile registry."""
    global _PROFILES_REGISTERED
    if _PROFILES_REGISTERED:
        return

    profiles: dict[str, HarnessProfileConfig] = {
        "talkops:readonly-metadata": HarnessProfileConfig(
            system_prompt_suffix=(
                "EXECUTION TRUST PROFILE: readonly-metadata (Watson Isolation). "
                "Zero infrastructure writes, zero file writes, and metadata-only datastore "
                "queries (pg_stat_*, pg_locks, information_schema, EXPLAIN, SHOW). "
                "Never attempt live mutations or SELECT from user business tables."
            ),
            excluded_tools=frozenset({"write_file", "edit_file"}),
        ),
        "talkops:gitops-pr": HarnessProfileConfig(
            system_prompt_suffix=(
                "EXECUTION TRUST PROFILE: gitops-pr. "
                "Diagnose live telemetry in read-only mode, author minimal IaC/manifest fixes "
                "in a local branch, and open an evidence-linked Pull Request. Never mutate live clusters directly."
            ),
        ),
        "talkops:hitl-gated": HarnessProfileConfig(
            system_prompt_suffix=(
                "EXECUTION TRUST PROFILE: hitl-gated. "
                "Gather full diagnostic evidence first and present clear blast-radius & rollback plans "
                "before requesting human approval for any mutating action."
            ),
        ),
        "talkops:autonomous-tnr": HarnessProfileConfig(
            system_prompt_suffix=(
                "EXECUTION TRUST PROFILE: autonomous-tnr. "
                "Autonomous remediation enabled within scopeGuards. Every live mutation MUST "
                "have a deterministic rollback command and post-remediation SLO verification."
            ),
        ),
    }

    for name, cfg in profiles.items():
        try:
            register_harness_profile(name, cfg, overwrite=True)
        except TypeError:
            try:
                register_harness_profile(name, cfg)
            except Exception:
                pass
        except Exception:
            pass

    _PROFILES_REGISTERED = True


def resolve_runtime_execution_profile(
    state: Mapping[str, Any] | None = None,
    default: ExecutionTrustProfile = "hitl-gated",
) -> ExecutionTrustProfile:
    """Resolve the active execution profile from state, env, or default."""
    if isinstance(state, Mapping):
        raw_state_prof = state.get("execution_profile")
        if isinstance(raw_state_prof, str) and raw_state_prof.strip():
            return normalize_execution_profile(raw_state_prof)
    env_prof = os.environ.get("TALKOPS_EXECUTION_PROFILE", "").strip()
    if env_prof:
        return normalize_execution_profile(env_prof)
    return default

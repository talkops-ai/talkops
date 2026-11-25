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

"""CI/CD, GitOps, IaC & Host Change Ingestor with automatic incident confidence decay."""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from typing import Any

from talkops.storage.protocol import ChangeEventRecord, OperationalStore


class ChangeIngestor:
    """Normalizes deployment/config change events across GitHub, GitLab, ArgoCD, Flux, Terraform, and K8s."""

    def __init__(self, store: OperationalStore, *, git_decay_factor: float = 0.65) -> None:
        """Initialize ChangeIngestor."""
        self.store = store
        self.git_decay_factor = git_decay_factor

    def normalize_payload(
        self,
        payload: Mapping[str, Any],
        *,
        source_hint: str = "generic",
        tenant_id: str = "default",
    ) -> ChangeEventRecord:
        """Execute `normalize_payload`."""
        source = str(payload.get("source") or source_hint or "generic").strip().lower()
        now_ts = float(payload.get("occurred_at") or time.time())

        # GitHub push / workflow_run / deployment_status
        if "repository" in payload and isinstance(payload.get("repository"), Mapping):
            repo = payload["repository"]
            service_name = str(
                payload.get("service_name")
                or repo.get("name")
                or "unknown-service"
            )
            head_commit = payload.get("head_commit") if isinstance(payload.get("head_commit"), Mapping) else {}
            commit_sha = str(
                payload.get("after")
                or head_commit.get("id")
                or payload.get("sha")
                or ""
            )
            tree_id = str(head_commit.get("tree_id") or commit_sha)
            author = str(
                (head_commit.get("author") or {}).get("name")
                if isinstance(head_commit.get("author"), Mapping)
                else (payload.get("sender") or {}).get("login", "")
                if isinstance(payload.get("sender"), Mapping)
                else ""
            )
            diff_summary = str(head_commit.get("message") or payload.get("description") or "GitHub change event")
            pr_url = str(
                (payload.get("pull_request") or {}).get("html_url", "")
                if isinstance(payload.get("pull_request"), Mapping)
                else payload.get("compare", "")
            )
            return ChangeEventRecord(
                event_id=str(payload.get("event_id") or f"gh-{uuid.uuid4().hex[:12]}"),
                service_name=service_name,
                source="github",
                environment=str(payload.get("environment") or "production"),
                tenant_id=tenant_id,
                commit_sha=commit_sha,
                pr_url=pr_url,
                author=author,
                diff_summary=diff_summary[:500],
                git_tree_hash=tree_id,
                occurred_at=now_ts,
            )

        # ArgoCD / FluxCD GitOps sync event
        if (
            "application" in payload
            or "app_name" in payload
            or source in ("argocd", "flux", "fluxcd")
        ):
            app_obj = payload.get("application")
            service_name = str(
                payload.get("service_name")
                or (app_obj.get("metadata", {}).get("name") if isinstance(app_obj, Mapping) else app_obj)
                or payload.get("app_name")
                or payload.get("app")
                or "gitops-app"
            )
            commit_sha = str(payload.get("revision") or payload.get("commit_sha") or "")
            return ChangeEventRecord(
                event_id=str(payload.get("event_id") or f"gitops-{uuid.uuid4().hex[:12]}"),
                service_name=service_name,
                source=source if source != "generic" else "argocd",
                environment=str(payload.get("environment") or payload.get("dest_namespace") or "production"),
                tenant_id=tenant_id,
                commit_sha=commit_sha,
                pr_url=str(payload.get("repo_url") or payload.get("pr_url") or ""),
                author=str(payload.get("author") or "gitops-controller"),
                diff_summary=str(
                    payload.get("message")
                    or payload.get("diff_summary")
                    or payload.get("summary")
                    or "GitOps sync applied"
                )[:500],
                git_tree_hash=str(payload.get("git_tree_hash") or commit_sha),
                occurred_at=now_ts,
            )

        # Generic / Terraform / CloudTrail / Ansible / Systemd change record
        commit_sha = str(payload.get("commit_sha") or payload.get("revision") or "")
        return ChangeEventRecord(
            event_id=str(payload.get("event_id") or f"chg-{uuid.uuid4().hex[:12]}"),
            service_name=str(payload.get("service_name") or payload.get("service") or "unknown-service"),
            source=source,
            environment=str(payload.get("environment") or "production"),
            tenant_id=tenant_id,
            commit_sha=commit_sha,
            pr_url=str(payload.get("pr_url") or ""),
            author=str(payload.get("author") or ""),
            diff_summary=str(payload.get("diff_summary") or payload.get("summary") or "")[:500],
            git_tree_hash=str(payload.get("git_tree_hash") or commit_sha),
            occurred_at=now_ts,
        )

    async def ingest(
        self,
        payload: Mapping[str, Any] | ChangeEventRecord,
        *,
        source_hint: str = "generic",
        tenant_id: str = "default",
    ) -> tuple[ChangeEventRecord, int]:
        """Persist change event and apply git-tree confidence decay to older incidents on that service."""
        record = (
            payload
            if isinstance(payload, ChangeEventRecord)
            else self.normalize_payload(payload, source_hint=source_hint, tenant_id=tenant_id)
        )
        await self.store.record_change_event(record)
        decayed_count = 0
        if record.git_tree_hash and record.service_name:
            decayed_count = await self.store.apply_incident_git_decay(
                record.service_name,
                record.git_tree_hash,
                decay_factor=self.git_decay_factor,
                tenant_id=record.tenant_id,
            )
        return record, decayed_count

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

"""Stack-agnostic canonical entity extraction for cross-provider alert storm correlation.

Follows OpenTelemetry Resource Semantic Conventions, Kubernetes controller naming grammar,
and standard cloud resource URI/ARN syntax so heterogeneous alerts (Prometheus/Alertmanager,
Grafana, Datadog, PagerDuty, AWS CloudWatch/EventBridge, Azure Monitor, ServiceNow,
CloudEvents, custom JSON) resolve to unified workload/resource entity keys without any
environment-specific or exporter-specific hardcoding.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from talkops.webhooks.models import NormalizedWebhookEvent

# Kubernetes Deployment pod: `<base>-<rs-hash: 6-10 alnum with >=1 digit>-<pod-suffix: 3-5 alnum>`
_K8S_DEPLOYMENT_POD_RE = re.compile(
    r"^(?P<base>[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)-(?P<rs>[a-z0-9]{6,10})-(?P<suffix>[a-z0-9]{3,5})$"
)
# Kubernetes StatefulSet pod: `<base>-<ordinal>`
_K8S_STATEFULSET_POD_RE = re.compile(
    r"^(?P<base>[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)-(?P<ord>[0-9]{1,3})$"
)
# Kubernetes DaemonSet / single-level controller pod: `<base>-<5-char mixed alnum>`
_K8S_DAEMONSET_POD_RE = re.compile(
    r"^(?P<base>[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)-(?P<suffix>[a-z0-9]{5})$"
)
# Kubernetes ReplicaSet name: `<base>-<rs-hash: 6-10 alnum with >=1 digit>`
_K8S_REPLICASET_RE = re.compile(
    r"^(?P<base>[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)-(?P<rs>[a-z0-9]{6,10})$"
)
# Version-like token that must never be stripped as a random K8s suffix (e.g. `v1`, `v2`, `v10`)
_VERSION_SUFFIX_RE = re.compile(r"^v[0-9]+$")
# Host:port pattern (IPv4 or DNS hostname with trailing numeric port)
_HOST_PORT_RE = re.compile(r"^(?P<host>[a-z0-9._-]+):[0-9]{2,5}$")

# Explicit workload/subject keys (ordered from most specific application/controller subject).
# These identify *what actually failed* rather than a telemetry collector/scraper target.
_EXPLICIT_WORKLOAD_KEYS: tuple[str, ...] = (
    "exported_service",
    "service.name",
    "service_name",
    "app.kubernetes.io/name",
    "label_app_kubernetes_io_name",
    "deployment",
    "k8s.deployment.name",
    "kube_deployment",
    "statefulset",
    "k8s.statefulset.name",
    "kube_statefulset",
    "daemonset",
    "k8s.daemonset.name",
    "kube_daemonset",
    "workload",
    "k8s.workload.name",
    "owner_name",
    "cronjob",
    "k8s.cronjob.name",
    "job_name",
    "k8s.job.name",
    "ecs_service",
    "faas.name",
    "db.instance.id",
    "cloud.resource_id",
    "resource_name",
    "cmdb_ci",
    "exported_job",
)

# Cloud resource dimension / attribute keys (AWS CloudWatch, Azure, GCP, Datadog, CMDB)
_CLOUD_RESOURCE_KEYS: tuple[str, ...] = (
    "dbinstanceidentifier",
    "dbclusteridentifier",
    "functionname",
    "servicename",
    "autoscalinggroupname",
    "loadbalancer",
    "targetgroup",
    "instanceid",
    "tablename",
    "queuename",
    "streamname",
    "cacheclusterid",
    "replicationgroupid",
    "clustername",
    "bucketname",
    "topicname",
    "apiname",
    "broker",
    "resource",
    "application",
    "component",
)

_POD_KEYS: tuple[str, ...] = (
    "exported_pod",
    "pod",
    "k8s.pod.name",
    "kube_pod",
    "pod_name",
)

_REPLICASET_KEYS: tuple[str, ...] = (
    "replicaset",
    "k8s.replicaset.name",
    "kube_replicaset",
)

_HOST_NODE_KEYS: tuple[str, ...] = (
    "node",
    "k8s.node.name",
    "host.name",
    "host",
    "hostname",
    "exported_instance",
)

# Prometheus scrape-target discovery markers: when present alongside a distinct workload/pod/node
# subject, `service` / `job` identify the scraped telemetry collector rather than the failing subject.
_SCRAPE_DISCOVERY_MARKERS: frozenset[str] = frozenset(
    {"endpoint", "metrics_path", "prometheus", "scrape_job"}
)

# Coarse partition / administrative labels that MUST NOT be used alone as correlation entity keys
# (otherwise unrelated services sharing a namespace, cluster, or cloud region would falsely merge).
_COARSE_SCOPE_KEYS: frozenset[str] = frozenset(
    {
        "namespace",
        "k8s.namespace.name",
        "kube_namespace",
        "cluster",
        "k8s.cluster.name",
        "kube_cluster_name",
        "region",
        "cloud.region",
        "availability_zone",
        "cloud.availability_zone",
        "account",
        "aws_account_id",
        "cloud.account.id",
        "environment",
        "env",
        "deployment.environment",
        "deployment.environment.name",
        "stage",
        "team",
        "tier",
        "severity",
        "priority",
        "tenant",
        "tenant_id",
    }
)

_ENVIRONMENT_KEYS: tuple[str, ...] = (
    "environment",
    "env",
    "deployment.environment.name",
    "deployment.environment",
    "stage",
)


@dataclass(frozen=True, slots=True)
class CanonicalEntitySet:
    """Normalized primary service identity and specific correlation entity keys for an alert."""

    primary_service: str | None
    entity_keys: frozenset[str]
    environment: str = "production"


def _clean_token(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _has_digit(token: str) -> bool:
    return any(ch.isdigit() for ch in token)


def _has_alpha(token: str) -> bool:
    return any(ch.isalpha() for ch in token)


def normalize_workload_from_pod(pod_name: str | None) -> str | None:
    """Derive a canonical Kubernetes controller/workload name from a pod name using pure syntax rules.

    Examples:
      - ``otel-backend-ad-575757745f-w5hkv`` -> ``otel-backend-ad`` (Deployment pod)
      - ``payment-worker-7f9d8b-x2k``        -> ``payment-worker``  (Deployment pod)
      - ``postgresql-0``                     -> ``postgresql``      (StatefulSet pod)
      - ``fluent-bit-x9k2m``                 -> ``fluent-bit``      (DaemonSet pod)
    Returns ``None`` if *pod_name* does not match a generated controller pod pattern.
    """
    raw = _clean_token(pod_name)
    if not raw or "/" in raw or " " in raw:
        return None

    m = _K8S_DEPLOYMENT_POD_RE.match(raw)
    if m and _has_digit(m.group("rs")):
        base = m.group("base")
        if base and _has_alpha(base):
            return base

    m = _K8S_STATEFULSET_POD_RE.match(raw)
    if m:
        base = m.group("base")
        if base and _has_alpha(base):
            return base

    m = _K8S_DAEMONSET_POD_RE.match(raw)
    if m:
        suffix = m.group("suffix")
        base = m.group("base")
        if base and _has_alpha(base) and _has_digit(suffix) and _has_alpha(suffix) and not _VERSION_SUFFIX_RE.match(suffix):
            return base

    return None


def normalize_workload_from_replicaset(rs_name: str | None) -> str | None:
    """Derive the parent Deployment name from a Kubernetes ReplicaSet name."""
    raw = _clean_token(rs_name)
    if not raw or "/" in raw or " " in raw:
        return None
    m = _K8S_REPLICASET_RE.match(raw)
    if m and _has_digit(m.group("rs")):
        base = m.group("base")
        if base and _has_alpha(base):
            return base
    return None


def normalize_resource_identifier(raw_value: Any) -> list[str]:
    """Extract canonical entity keys from a cloud ARN, Azure resource ID, host:port, or plain name."""
    raw = _clean_token(raw_value)
    if not raw:
        return []

    keys: list[str] = []

    # AWS ARN: arn:partition:service:region:account:resource-type/resource-id (or :resource-id)
    if raw.startswith("arn:"):
        parts = raw.split(":", 5)
        if len(parts) == 6 and parts[5]:
            tail = parts[5]
            leaf = tail.rsplit("/", 1)[-1].rsplit(":", 1)[-1].strip()
            if leaf and _has_alpha(leaf):
                keys.append(leaf)
        keys.append(raw)
        return keys

    # Azure Resource ID: /subscriptions/.../providers/<ns>/<type>/<name>
    if raw.startswith("/subscriptions/") and "/providers/" in raw:
        leaf = raw.rstrip("/").rsplit("/", 1)[-1].strip()
        if leaf:
            keys.append(leaf)
        keys.append(raw)
        return keys

    # Host:port (e.g., 10.0.1.50:9100 or db-1.internal:5432)
    m_hp = _HOST_PORT_RE.match(raw)
    if m_hp:
        host = m_hp.group("host")
        if host:
            keys.append(host)
        return keys

    keys.append(raw)
    return keys


def parse_key_value_tags(raw_tags: Any) -> dict[str, str]:
    """Parse Datadog-style tag strings (`k:v,k2:v2`) or tag lists (`['k:v', ...]`) into a dict."""
    parsed: dict[str, str] = {}
    items: list[str] = []
    if isinstance(raw_tags, str):
        items = [seg.strip() for seg in raw_tags.split(",") if seg.strip()]
    elif isinstance(raw_tags, Sequence) and not isinstance(raw_tags, (bytes, bytearray)):
        items = [str(seg).strip() for seg in raw_tags if seg is not None and str(seg).strip()]
    elif isinstance(raw_tags, Mapping):
        for k, v in raw_tags.items():
            ck = _clean_token(k)
            cv = _clean_token(v)
            if ck and cv:
                parsed[ck] = cv
        return parsed

    for item in items:
        if ":" not in item:
            continue
        k, v = item.split(":", 1)
        ck = _clean_token(k)
        cv = _clean_token(v)
        if ck and cv and ck not in parsed:
            parsed[ck] = cv
    return parsed


def extract_cloudwatch_dimensions(dimensions: Any) -> dict[str, str]:
    """Extract CloudWatch `[{"Name": ..., "Value": ...}]` dimensions into a lowercased mapping."""
    out: dict[str, str] = {}
    if isinstance(dimensions, Mapping):
        for k, v in dimensions.items():
            ck, cv = _clean_token(k), _clean_token(v)
            if ck and cv:
                out[ck] = cv
        return out
    if isinstance(dimensions, Sequence) and not isinstance(dimensions, (str, bytes, bytearray)):
        for item in dimensions:
            if not isinstance(item, Mapping):
                continue
            name = _clean_token(item.get("name") or item.get("Name"))
            val = _clean_token(item.get("value") or item.get("Value"))
            if name and val:
                out[name] = val
    return out


def _lowercase_mapping(mapping: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(mapping, Mapping):
        return {}
    out: dict[str, str] = {}
    for k, v in mapping.items():
        if v is None or isinstance(v, (dict, list, tuple, set)):
            continue
        ck = _clean_token(k)
        cv = _clean_token(v)
        if ck and cv:
            out[ck] = cv
    return out


def _has_scrape_discovery_markers(labels: Mapping[str, str]) -> bool:
    return any(k in labels for k in _SCRAPE_DISCOVERY_MARKERS)


def _is_scrape_target_pod(
    pod_val: str,
    pod_owner: str | None,
    labels: Mapping[str, str],
    explicit_non_pod_subjects: Collection[str],
) -> bool:
    """Return True if ``labels['pod']`` is structurally the Prometheus exporter/collector pod.

    When Prometheus scrapes an exporter via ServiceMonitor/PodMonitor (``endpoint`` / ``metrics_path``
    present) for a controller-level metric (e.g. ``deployment="kafka"`` or ``node="ip-10-0-1-50"``),
    target discovery attaches the *exporter's own* ``service``, ``job``, and ``pod`` labels.
    Those exporter pods normalize to ``labels['service']`` / ``labels['job']`` rather than to the
    failing subject entity — so excluding them prevents unrelated workloads scraped by the same
    collector from falsely merging.
    """
    if not explicit_non_pod_subjects or not _has_scrape_discovery_markers(labels):
        return False
    scrape_svc = labels.get("service", "")
    scrape_job = labels.get("job", "")
    if not scrape_svc and not scrape_job:
        return False
    # If any explicit subject already matches the scrape service/job, this alert IS about that service itself.
    if any(s in {scrape_svc, scrape_job} for s in explicit_non_pod_subjects):
        return False
    # Exporter pod matches the scrape target service/job (either via normalized owner or prefix)
    candidates = {c for c in (pod_owner, pod_val) if c}
    for scrape_id in (scrape_svc, scrape_job):
        if not scrape_id:
            continue
        if scrape_id in candidates or (pod_val and pod_val.startswith(f"{scrape_id}-")):
            return True
    return False


def _extract_subject_entities_from_labels(
    labels: Mapping[str, str],
) -> tuple[str | None, list[str]]:
    """Extract `(primary_subject, ordered_subject_keys)` from a single flat label/tag dict.

    Returns only specific workload/resource/pod/host subjects (never coarse namespace/region scopes
    and never raw Prometheus scrape-target `service`/`job`/exporter-`pod`).
    """
    primary: str | None = None
    keys: list[str] = []

    def _add(val: str) -> None:
        if val and val not in keys:
            keys.append(val)

    # 1. Explicit workload / OTel / K8s controller labels
    for k in _EXPLICIT_WORKLOAD_KEYS:
        val = labels.get(k)
        if val:
            norm_vals = normalize_resource_identifier(val)
            for nv in norm_vals:
                _add(nv)
            if primary is None and norm_vals:
                primary = norm_vals[0]

    # 2. ReplicaSet -> parent Deployment
    for k in _REPLICASET_KEYS:
        val = labels.get(k)
        if val:
            owner = normalize_workload_from_replicaset(val)
            if owner:
                _add(owner)
                if primary is None:
                    primary = owner
            _add(f"replicaset:{val}")

    # 3. Cloud / managed resource dimensions & attributes
    for k in _CLOUD_RESOURCE_KEYS:
        val = labels.get(k)
        if val:
            norm_vals = normalize_resource_identifier(val)
            for nv in norm_vals:
                _add(nv)
            if primary is None and norm_vals:
                primary = norm_vals[0]

    # 4. Compute host / Kubernetes node (captured before pod so node alerts can identify exporter pods)
    explicit_non_pod: list[str] = list(keys)
    for k in _HOST_NODE_KEYS:
        val = labels.get(k)
        if val:
            norm_vals = normalize_resource_identifier(val)
            for nv in norm_vals:
                explicit_non_pod.append(nv)

    # 5. Pod -> normalized controller workload + exact pod key (skip if structurally the exporter's own pod)
    for k in _POD_KEYS:
        val = labels.get(k)
        if val:
            owner = normalize_workload_from_pod(val)
            if _is_scrape_target_pod(val, owner, labels, explicit_non_pod):
                continue
            if owner:
                _add(owner)
                if primary is None:
                    primary = owner
            _add(f"pod:{val}")

    # 6. Compute host / Kubernetes node keys
    for k in _HOST_NODE_KEYS:
        val = labels.get(k)
        if val:
            norm_vals = normalize_resource_identifier(val)
            for nv in norm_vals:
                _add(f"host:{nv}")
                if primary is None:
                    primary = nv

    return primary, keys


def resolve_alertmanager_primary_service(
    merged_labels: Mapping[str, Any],
    alerts_list: Sequence[Mapping[str, Any]] = (),
) -> str | None:
    """Resolve the best affected workload/service name for an Alertmanager/Prometheus webhook.

    Structurally separates *observed subject entities* (`deployment`, `statefulset`, `daemonset`,
    `exported_service`, `service.name`, `app.kubernetes.io/name`, normalized `pod` owner, or `node`)
    from *Prometheus scrape-target metadata* (`service`/`job` when accompanied by `endpoint` or
    `metrics_path` discovery labels) without hardcoding any exporter or stack names.
    """
    # Collect label sets from firing alerts first, then merged top-level labels
    label_maps: list[dict[str, str]] = []
    firing = [a for a in alerts_list if str(a.get("status") or "firing").strip().lower() != "resolved"]
    ordered_alerts = firing if firing else list(alerts_list)
    if ordered_alerts:
        first_l = _lowercase_mapping(ordered_alerts[0].get("labels") if isinstance(ordered_alerts[0], Mapping) else None)
        merged_l = _lowercase_mapping(merged_labels)
        label_maps.append({**merged_l, **first_l})
    else:
        label_maps.append(_lowercase_mapping(merged_labels))

    labels = label_maps[0]
    raw_service = labels.get("service")
    raw_job = labels.get("job")
    scrape_meta_present = _has_scrape_discovery_markers(labels)

    # 1. Explicit workload / OTel / K8s controller label always wins over scrape-target `service`
    for k in _EXPLICIT_WORKLOAD_KEYS:
        val = labels.get(k)
        if val:
            norm = normalize_resource_identifier(val)
            if norm:
                return norm[0]

    # 2. ReplicaSet -> parent Deployment
    for k in _REPLICASET_KEYS:
        owner = normalize_workload_from_replicaset(labels.get(k))
        if owner:
            return owner

    # 3. Pod -> normalized workload (wins when `service` is absent, or scrape-target metadata is present,
    #    or `service` equals `job` / lacks workload specificity)
    pod_owner: str | None = None
    raw_pod: str | None = None
    for k in _POD_KEYS:
        val = labels.get(k)
        if val:
            raw_pod = val
            pod_owner = normalize_workload_from_pod(val)
            if pod_owner:
                break

    if pod_owner and (not raw_service or scrape_meta_present or raw_service == pod_owner):
        return pod_owner

    # 4. Node/Host subject when scrape discovery markers indicate exporter-level `service`
    if scrape_meta_present:
        for k in _HOST_NODE_KEYS:
            val = labels.get(k)
            if val:
                norm = normalize_resource_identifier(val)
                if norm:
                    return norm[0]

    # 5. Direct `service` label (e.g., application ServiceMonitor or TargetDown on `flagd`)
    if raw_service:
        return raw_service

    # 6. Normalized pod owner even if raw_service was checked above, or raw pod/node/job/app/namespace
    if pod_owner:
        return pod_owner
    for fallback_key in ("job", "app", "pod", "node", "instance", "namespace"):
        val = labels.get(fallback_key)
        if val:
            norm = normalize_resource_identifier(val)
            if norm:
                return norm[0]
    if raw_pod:
        return raw_pod
    return raw_job or None


def resolve_cloudwatch_primary_service(
    dimensions: Any,
    metric_namespace: str | None,
    alarm_name: str | None,
) -> str | None:
    """Resolve the affected resource identifier from CloudWatch Alarm dimensions before namespace fallback."""
    dims = extract_cloudwatch_dimensions(dimensions)
    for k in (*_EXPLICIT_WORKLOAD_KEYS, *_CLOUD_RESOURCE_KEYS, *_HOST_NODE_KEYS):
        val = dims.get(k)
        if val:
            norm = normalize_resource_identifier(val)
            if norm:
                return norm[0]
    # Any single remaining concrete non-scope dimension value
    for k, val in dims.items():
        if k not in _COARSE_SCOPE_KEYS and val:
            norm = normalize_resource_identifier(val)
            if norm:
                return norm[0]
    return _clean_token(metric_namespace) or _clean_token(alarm_name) or None


def is_coarse_scope_value(value: str, label_maps: Sequence[Mapping[str, str]]) -> bool:
    """Return True if *value* only matches a coarse partition label (e.g. namespace/region/cluster)."""
    clean = _clean_token(value)
    if not clean:
        return True
    # Cloud metric namespace strings like `aws/rds`, `aws/ec2`, `aws/lambda` are service-type categories, not entities
    if clean.startswith(("aws/", "gcp/", "azure/", "microsoft.")):
        return True
    for lm in label_maps:
        for scope_k in _COARSE_SCOPE_KEYS:
            if lm.get(scope_k) == clean:
                # Only coarse if it doesn't also appear as an explicit workload/service entity
                explicit_match = any(
                    lm.get(ek) == clean
                    for ek in (*_EXPLICIT_WORKLOAD_KEYS, *_CLOUD_RESOURCE_KEYS)
                )
                if not explicit_match:
                    return True
    return False


def extract_canonical_entities(event: NormalizedWebhookEvent) -> CanonicalEntitySet:
    """Extract canonical correlation entity keys & environment generically from any webhook event.

    Works across Alertmanager/Prometheus/Grafana, PagerDuty, Datadog, AWS CloudWatch/EventBridge/SNS,
    Azure Monitor, ServiceNow, CloudEvents, GitHub, Jenkins, and generic JSON payloads.
    """
    meta = event.metadata if isinstance(event.metadata, Mapping) else {}
    raw = event.raw_payload if isinstance(event.raw_payload, Mapping) else {}

    label_maps: list[dict[str, str]] = []

    # 1. Alertmanager / Prometheus / Grafana labels (per firing alert + merged metadata labels)
    raw_alerts = raw.get("alerts")
    if isinstance(raw_alerts, Sequence) and not isinstance(raw_alerts, (str, bytes, bytearray)):
        firing_alerts = [
            a for a in raw_alerts
            if isinstance(a, Mapping) and str(a.get("status") or "firing").strip().lower() != "resolved"
        ]
        target_alerts = firing_alerts if firing_alerts else [a for a in raw_alerts if isinstance(a, Mapping)]
        for alert in target_alerts[:20]:
            al = _lowercase_mapping(alert.get("labels") if isinstance(alert.get("labels"), Mapping) else None)
            if al:
                label_maps.append(al)

    if isinstance(meta.get("labels"), Mapping):
        lm = _lowercase_mapping(meta["labels"])
        if lm:
            label_maps.append(lm)

    # 2. AWS CloudWatch Dimensions
    if meta.get("dimensions"):
        dims = extract_cloudwatch_dimensions(meta["dimensions"])
        if dims:
            label_maps.append(dims)

    # 3. Datadog scope & tags
    dd_tags: dict[str, str] = {}
    if meta.get("scope"):
        dd_tags.update(parse_key_value_tags(meta["scope"]))
    if meta.get("tags"):
        dd_tags.update(parse_key_value_tags(meta["tags"]))
    if dd_tags:
        label_maps.append(dd_tags)

    # 4. Flat scalar metadata fields (e.g. ServiceNow cmdb_ci, PagerDuty service, Azure resource)
    flat_meta = _lowercase_mapping(meta)
    if flat_meta:
        label_maps.append(flat_meta)

    # Resolve environment generically across all maps
    environment = "production"
    for lm in label_maps:
        for ek in _ENVIRONMENT_KEYS:
            if lm.get(ek):
                environment = lm[ek]
                break
        if environment != "production":
            break

    entities: list[str] = []
    primary_subject: str | None = None

    def _add_entity(val: str) -> None:
        clean = _clean_token(val)
        if clean and clean not in entities:
            entities.append(clean)

    # Extract concrete subject entities from every label map
    scrape_target_services: set[str] = set()
    for lm in label_maps:
        subj_primary, subj_keys = _extract_subject_entities_from_labels(lm)
        if primary_subject is None and subj_primary:
            primary_subject = subj_primary
        for k in subj_keys:
            _add_entity(k)

        # Structurally identify Prometheus scrape-target metadata so unrelated workloads scraped
        # by the same exporter/collector never falsely merge on the collector's `service` label.
        svc_val = lm.get("service")
        if svc_val:
            if _has_scrape_discovery_markers(lm) and subj_keys and svc_val not in subj_keys:
                scrape_target_services.add(svc_val)
            elif not is_coarse_scope_value(svc_val, label_maps):
                _add_entity(svc_val)
                if primary_subject is None:
                    primary_subject = svc_val

    # Extract cloud resources list (e.g., AWS EventBridge `resources`, Azure `alertTargetIDs`)
    for res_container in (raw.get("resources"), meta.get("resources"), meta.get("target_ids")):
        if isinstance(res_container, Sequence) and not isinstance(res_container, (str, bytes, bytearray)):
            for r_item in res_container[:10]:
                for nk in normalize_resource_identifier(r_item):
                    if not is_coarse_scope_value(nk, label_maps):
                        _add_entity(nk)
                        if primary_subject is None:
                            primary_subject = nk

    # Include event.service_name unless it is a detected scrape-target collector or a coarse scope
    # when we already have concrete subject entities.
    raw_event_svc = _clean_token(event.service_name)
    if raw_event_svc:
        # Handle comma-separated Datadog scope strings passed directly as `event.service_name`
        if ":" in raw_event_svc and not raw_event_svc.startswith("arn:"):
            parsed_svc_tags = parse_key_value_tags(raw_event_svc)
            if parsed_svc_tags:
                t_primary, t_keys = _extract_subject_entities_from_labels(parsed_svc_tags)
                for tk in t_keys:
                    _add_entity(tk)
                if parsed_svc_tags.get("service"):
                    _add_entity(parsed_svc_tags["service"])
                    if primary_subject is None:
                        primary_subject = parsed_svc_tags["service"]
                elif primary_subject is None and t_primary:
                    primary_subject = t_primary
            else:
                for nk in normalize_resource_identifier(raw_event_svc):
                    if nk not in scrape_target_services and not (entities and is_coarse_scope_value(nk, label_maps)):
                        _add_entity(nk)
                        if primary_subject is None:
                            primary_subject = nk
        elif raw_event_svc not in scrape_target_services:
            if not (entities and is_coarse_scope_value(raw_event_svc, label_maps)):
                for nk in normalize_resource_identifier(raw_event_svc):
                    if nk not in scrape_target_services:
                        _add_entity(nk)
                        if primary_subject is None:
                            primary_subject = nk

    # Final guard: remove any scrape-target collector labels if concrete subject entities exist
    filtered: list[str] = [e for e in entities if e not in scrape_target_services] if entities else []
    if not filtered and raw_event_svc and not is_coarse_scope_value(raw_event_svc, label_maps):
        filtered = [raw_event_svc]

    resolved_primary = primary_subject or (filtered[0] if filtered else (raw_event_svc or None))
    return CanonicalEntitySet(
        primary_service=resolved_primary,
        entity_keys=frozenset(filtered),
        environment=environment,
    )


def normalize_entity_keys(
    service_name: str | None,
    entity_keys: Collection[str] | None = None,
) -> set[str]:
    """Normalize explicit entity keys + primary service name for correlator indexing."""
    keys: set[str] = set()
    if service_name:
        clean = _clean_token(service_name)
        if clean:
            keys.add(clean)
    if entity_keys:
        for k in entity_keys:
            clean = _clean_token(k)
            if clean:
                keys.add(clean)
    return keys

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

"""Unit tests for stack-agnostic canonical entity extraction and sliding/cooldown storm correlation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from talkops.context_graph.graph import ContextGraph
from talkops.storage.sqlite_store import SqliteOperationalStore
from talkops.webhooks.correlation import AlertStormCorrelator
from talkops.webhooks.dispatcher import WebhookDispatcher, WebhookRunResult
from talkops.webhooks.entities import (
    extract_canonical_entities,
    normalize_resource_identifier,
    normalize_workload_from_pod,
)
from talkops.webhooks.models import WebhookConfig
from talkops.webhooks.providers import create_default_registry


def test_generic_pod_and_resource_normalization() -> None:
    """Pure syntax rules normalize K8s pods & cloud resource URIs without vendor hardcoding."""
    assert normalize_workload_from_pod("otel-backend-ad-575757745f-w5hkv") == "otel-backend-ad"
    assert normalize_workload_from_pod("payment-worker-7f9d8b-x2k") == "payment-worker"
    assert normalize_workload_from_pod("postgresql-0") == "postgresql"
    assert normalize_workload_from_pod("fluent-bit-x9k2m") == "fluent-bit"
    # Must NOT mangle versioned or ordinary names
    assert normalize_workload_from_pod("billing-api-v2") is None
    assert normalize_workload_from_pod("checkout-service") is None

    assert "orders-db" in normalize_resource_identifier("arn:aws:rds:us-east-1:123456789012:db:orders-db")
    assert "checkout-api" in normalize_resource_identifier(
        "/subscriptions/sub-1/resourceGroups/rg-prod/providers/Microsoft.Web/sites/checkout-api"
    )
    assert normalize_resource_identifier("10.0.1.50:9100") == ["10.0.1.50"]


def test_alertmanager_structurally_separates_scraper_service_from_workload() -> None:
    """Prometheus scrape-target `service` (with `endpoint`) must not shadow failing workload/pod/node."""
    registry = create_default_registry()
    am = registry.get("alertmanager")
    assert am is not None
    cfg = WebhookConfig()

    # 1. Deployment metric scraped via any arbitrary collector service name (zero hardcoding!)
    #    Note: real Prometheus ServiceMonitor scrape discovery attaches the *collector's own pod* as `pod`!
    dep_payload = {
        "version": "4",
        "status": "firing",
        "groupKey": '{}:{alertname="DeploymentUnavailable", namespace="shop"}',
        "commonLabels": {
            "alertname": "DeploymentUnavailable",
            "namespace": "shop",
            "deployment": "checkout-api",
            "pod": "custom-metrics-collector-svc-5987684664-7hqnk",
            "service": "custom-metrics-collector-svc",
            "job": "custom-metrics-collector",
            "endpoint": "http",
            "severity": "critical",
        },
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "DeploymentUnavailable",
                    "namespace": "shop",
                    "deployment": "checkout-api",
                    "pod": "custom-metrics-collector-svc-5987684664-7hqnk",
                    "service": "custom-metrics-collector-svc",
                    "job": "custom-metrics-collector",
                    "endpoint": "http",
                },
                "annotations": {"summary": "Deployment has unavailable replicas"},
            }
        ],
    }
    ev_dep = am.parse({}, json.dumps(dep_payload).encode(), cfg)
    assert ev_dep.service_name == "checkout-api"
    assert "checkout-api" in ev_dep.summary
    ent_dep = extract_canonical_entities(ev_dep)
    assert "checkout-api" in ent_dep.entity_keys
    assert "custom-metrics-collector-svc" not in ent_dep.entity_keys
    assert "pod:custom-metrics-collector-svc-5987684664-7hqnk" not in ent_dep.entity_keys

    # 2. Pod CrashLoop alert with resolved + firing siblings and NO deployment label
    crash_payload = {
        "version": "4",
        "status": "firing",
        "groupKey": '{}:{alertname="PodCrashLooping", namespace="shop"}',
        "commonLabels": {
            "alertname": "PodCrashLooping",
            "namespace": "shop",
            "service": "custom-metrics-collector-svc",
            "endpoint": "http",
            "severity": "critical",
        },
        "alerts": [
            {
                "status": "resolved",
                "labels": {"pod": "old-other-pod-9999999999-aaaaa"},
            },
            {
                "status": "firing",
                "labels": {
                    "pod": "checkout-api-6d8f7b9c4a-k9p2q",
                    "service": "custom-metrics-collector-svc",
                    "endpoint": "http",
                },
            },
        ],
    }
    ev_crash = am.parse({}, json.dumps(crash_payload).encode(), cfg)
    assert ev_crash.service_name == "checkout-api"
    ent_crash = extract_canonical_entities(ev_crash)
    assert "checkout-api" in ent_crash.entity_keys
    assert "custom-metrics-collector-svc" not in ent_crash.entity_keys

    # 3. KubePodNotReady with NO service and NO deployment label (only pod + namespace)
    not_ready_payload = {
        "version": "4",
        "status": "firing",
        "groupKey": '{}:{alertname="KubePodNotReady", namespace="shop"}',
        "commonLabels": {"alertname": "KubePodNotReady", "namespace": "shop", "severity": "warning"},
        "alerts": [
            {
                "status": "firing",
                "labels": {"pod": "checkout-api-6d8f7b9c4a-k9p2q", "namespace": "shop"},
            }
        ],
    }
    ev_nr = am.parse({}, json.dumps(not_ready_payload).encode(), cfg)
    assert ev_nr.service_name == "checkout-api"
    ent_nr = extract_canonical_entities(ev_nr)
    assert "checkout-api" in ent_nr.entity_keys
    assert "shop" not in ent_nr.entity_keys  # coarse namespace never used as correlation key

    # 4. Bare-metal / Node exporter alert (no pod/deployment, `node` + `endpoint`)
    node_payload = {
        "version": "4",
        "status": "firing",
        "groupKey": '{}:{alertname="NodeHighMemory"}',
        "commonLabels": {
            "alertname": "NodeHighMemory",
            "node": "ip-10-0-1-50",
            "service": "host-telemetry-agent",
            "endpoint": "metrics",
        },
        "alerts": [{"status": "firing", "labels": {"node": "ip-10-0-1-50"}}],
    }
    ev_node = am.parse({}, json.dumps(node_payload).encode(), cfg)
    assert ev_node.service_name == "ip-10-0-1-50"
    ent_node = extract_canonical_entities(ev_node)
    assert "host:ip-10-0-1-50" in ent_node.entity_keys
    assert "host-telemetry-agent" not in ent_node.entity_keys


@pytest.mark.asyncio
async def test_unrelated_workloads_on_same_scraper_and_namespace_do_not_falsely_merge(
    tmp_path: Path,
) -> None:
    """Two independent workloads in the same namespace scraped by the same collector stay separate."""
    store = SqliteOperationalStore(tmp_path / "isolated.db")
    await store.setup()
    correlator = AlertStormCorrelator(store)

    run_started = asyncio.Event()
    release_run = asyncio.Event()

    async def _slow_executor(event: Any, thread_id: str, delivery_id: str, config: Any) -> WebhookRunResult:
        run_started.set()
        await release_run.wait()
        return WebhookRunResult(thread_id=thread_id, run_id=delivery_id, status="completed", analysis_text="ok")

    dispatcher = WebhookDispatcher(
        config=WebhookConfig(dedup_ttl_seconds=60.0),
        run_executor=_slow_executor,
    )
    dispatcher.bind_storm_correlator(correlator)

    payload_alpha = {
        "version": "4",
        "status": "firing",
        "groupKey": '{}:{alertname="PodCrashLooping", pod="alpha-svc-575757745f-aaaaa"}',
        "commonLabels": {
            "alertname": "PodCrashLooping",
            "namespace": "shared-prod",
            "service": "shared-scraper-svc",
            "endpoint": "http",
            "severity": "critical",
        },
        "alerts": [{"status": "firing", "labels": {"pod": "alpha-svc-575757745f-aaaaa", "namespace": "shared-prod"}}],
    }
    payload_beta = {
        "version": "4",
        "status": "firing",
        "groupKey": '{}:{alertname="PodCrashLooping", pod="beta-worker-696969696a-bbbbb"}',
        "commonLabels": {
            "alertname": "PodCrashLooping",
            "namespace": "shared-prod",
            "service": "shared-scraper-svc",
            "endpoint": "http",
            "severity": "critical",
        },
        "alerts": [{"status": "firing", "labels": {"pod": "beta-worker-696969696a-bbbbb", "namespace": "shared-prod"}}],
    }
    payload_alpha_second_rule = {
        "version": "4",
        "status": "firing",
        "groupKey": '{}:{alertname="KubePodNotReady", namespace="shared-prod"}',
        "commonLabels": {"alertname": "KubePodNotReady", "namespace": "shared-prod", "severity": "warning"},
        "alerts": [{"status": "firing", "labels": {"pod": "alpha-svc-575757745f-aaaaa", "namespace": "shared-prod"}}],
    }

    code1, body1 = await dispatcher.handle_webhook("alertmanager", {}, json.dumps(payload_alpha).encode())
    assert code1 == 202 and body1["status"] == "accepted"
    await asyncio.wait_for(run_started.wait(), timeout=2.0)

    # Same workload (`alpha-svc`) via completely different rule with NO `service` label -> MUST correlate!
    code2, body2 = await dispatcher.handle_webhook("alertmanager", {}, json.dumps(payload_alpha_second_rule).encode())
    assert code2 == 202 and body2["status"] == "correlated_into_active_run"
    assert body2["thread_id"] == body1["thread_id"]

    # Unrelated workload (`beta-worker`) in SAME namespace & SAME scraper -> MUST NOT merge!
    code3, body3 = await dispatcher.handle_webhook("alertmanager", {}, json.dumps(payload_beta).encode())
    assert code3 == 202 and body3["status"] == "accepted"
    assert body3["thread_id"] != body1["thread_id"]

    release_run.set()
    await dispatcher.drain_background_tasks()
    await store.aclose()


@pytest.mark.asyncio
async def test_cross_provider_cloudwatch_datadog_and_k8s_snapshot_topology_correlation(
    tmp_path: Path,
) -> None:
    """AWS CloudWatch RDS + Datadog + Alertmanager correlate across stacks via canonical entities + ContextGraph."""
    store = SqliteOperationalStore(tmp_path / "cross_stack.db")
    await store.setup()
    graph = ContextGraph(store)

    # Topology node stored with K8s-prefixed node_id (`k8s:prod:deployment:checkout-api`), linked to RDS `orders-db`
    await graph.upsert_resource(
        node_id="k8s:prod:deployment:checkout-api",
        node_type="k8s_deployment",
        service_name="checkout-api",
    )
    await graph.upsert_resource(
        node_id="arn:aws:rds:us-east-1:111122223333:db:orders-db",
        node_type="aws_rds",
        service_name="orders-db",
    )
    await graph.link_dependency(
        "k8s:prod:deployment:checkout-api",
        "arn:aws:rds:us-east-1:111122223333:db:orders-db",
        rel_type="depends_on",
    )

    correlator = AlertStormCorrelator(store, context_graph=graph)
    registry = create_default_registry()
    cfg = WebhookConfig()

    # Alert 1: Datadog monitor with comma-separated tag scope
    dd_body = json.dumps(
        {
            "alert_id": "991",
            "alert_title": "Checkout p99 Latency High",
            "alert_transition": "Triggered",
            "alert_scope": "env:production,kube_deployment:checkout-api,host:ip-10-0-1-50",
            "priority": "P1",
        }
    ).encode()
    ev_dd = registry.get("datadog").parse({}, dd_body, cfg)  # type: ignore[union-attr]
    ent_dd = extract_canonical_entities(ev_dd)
    assert "checkout-api" in ent_dd.entity_keys

    d1 = await correlator.route_incoming_alert(
        candidate_thread_id="thread-cross-1",
        provider="datadog",
        dedup_key="dd-991",
        service_name=ent_dd.primary_service or "",
        entity_keys=ent_dd.entity_keys,
        environment=ent_dd.environment,
        severity=ev_dd.severity.value,
        normalized_event={"title": ev_dd.title},
    )
    assert d1.action == "start_primary_run"

    # Alert 2: AWS CloudWatch RDS Alarm (`Namespace="AWS/RDS"`, `DBInstanceIdentifier="orders-db"`)
    cw_alarm = {
        "AlarmName": "prod-orders-db-cpu-high",
        "NewStateValue": "ALARM",
        "NewStateReason": "CPUUtilization > 95%",
        "Region": "us-east-1",
        "AWSAccountId": "111122223333",
        "Trigger": {
            "Namespace": "AWS/RDS",
            "MetricName": "CPUUtilization",
            "Dimensions": [{"name": "DBInstanceIdentifier", "value": "orders-db"}],
        },
    }
    sns_body = json.dumps(
        {
            "Type": "Notification",
            "MessageId": "msg-cw-1",
            "TopicArn": "arn:aws:sns:us-east-1:111122223333:sre-alerts",
            "Message": json.dumps(cw_alarm),
        }
    ).encode()
    ev_cw = registry.get("sns").parse({}, sns_body, cfg)  # type: ignore[union-attr]
    assert ev_cw.service_name == "orders-db"
    ent_cw = extract_canonical_entities(ev_cw)
    assert "orders-db" in ent_cw.entity_keys

    d2 = await correlator.route_incoming_alert(
        candidate_thread_id="thread-cross-2",
        provider="sns",
        dedup_key="cw-1",
        service_name=ent_cw.primary_service or "",
        entity_keys=ent_cw.entity_keys,
        environment=ent_cw.environment,
        severity=ev_cw.severity.value,
        normalized_event={"title": ev_cw.title},
    )
    assert d2.action == "queued_sidecar_inbox"
    assert d2.thread_id == "thread-cross-1"
    await store.aclose()


@pytest.mark.asyncio
async def test_inflight_session_never_evicted_mid_run_and_post_rca_cooldown(tmp_path: Path) -> None:
    """Active 20-minute runs remain correlated past 15-min window; completed runs suppress trailing symptoms."""
    store = SqliteOperationalStore(tmp_path / "windows.db")
    await store.setup()
    correlator = AlertStormCorrelator(
        store,
        correlation_window_sec=900.0,   # 15m sliding window
        max_session_window_sec=3600.0,  # 60m hard safety ceiling
        cooldown_window_sec=300.0,      # 5m post-completion cooldown
    )

    t0 = 1_700_000_000.0
    d1 = await correlator.route_incoming_alert(
        candidate_thread_id="thread-long-rca",
        provider="alertmanager",
        dedup_key="am-1",
        service_name="billing-api",
        severity="warning",
        normalized_event={"title": "Billing Latency"},
        now_ts=t0,
    )
    assert d1.action == "start_primary_run"

    # At t + 19 minutes (1140s > 900s!), primary RCA is STILL in_flight -> MUST still correlate!
    d2 = await correlator.route_incoming_alert(
        candidate_thread_id="thread-dup-at-19m",
        provider="alertmanager",
        dedup_key="am-2",
        service_name="billing-api",
        severity="warning",
        normalized_event={"title": "Billing CrashLoop repeat"},
        now_ts=t0 + 1140.0,
    )
    assert d2.action == "queued_sidecar_inbox"
    assert d2.thread_id == "thread-long-rca"

    # Drain and finalize run at t + 20 minutes (1200s)
    await correlator.finalize_run_or_continue("thread-long-rca", now_ts=t0 + 1200.0)

    # At t + 22 minutes (within 5m cooldown), trailing warning symptom -> suppressed!
    d3 = await correlator.route_incoming_alert(
        candidate_thread_id="thread-trailing-symptom",
        provider="alertmanager",
        dedup_key="am-3",
        service_name="billing-api",
        severity="warning",
        normalized_event={"title": "Trailing PodNotReady"},
        now_ts=t0 + 1320.0,
    )
    assert d3.action == "suppressed_cooldown"
    assert d3.thread_id == "thread-long-rca"

    # At t + 23 minutes (still in cooldown), a CRITICAL escalation MUST NOT be suppressed!
    d4 = await correlator.route_incoming_alert(
        candidate_thread_id="thread-escalation",
        provider="pagerduty",
        dedup_key="pd-esc",
        service_name="billing-api",
        severity="critical",
        normalized_event={"title": "P1 Total Outage"},
        now_ts=t0 + 1380.0,
    )
    assert d4.action == "start_primary_run"
    assert d4.thread_id == "thread-escalation"

    await store.aclose()

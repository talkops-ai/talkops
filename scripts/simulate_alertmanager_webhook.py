#!/usr/bin/env python3
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

"""Simulate a Prometheus Alertmanager v4 webhook firing into TalkOps Server (`POST /webhooks/alertmanager`)
for the live OpenTelemetry Demo (`otel-demo`) Kubernetes + Helm scenario.

Scenarios included (`--scenario`):
  - `all` (default): Fires a grouped Alertmanager incident covering both:
      1. Missing Helm chart dependencies (`product-catalog` CrashLoopBackOff because `postgresql` is not installed;
         `cart` stuck in `Init:0/1` because `valkey-cart` is not installed).
      2. Under-provisioned memory limit (`ad` service OOMKilled / Exit Code 137 with `10Mi` memory limit vs `300Mi` chart default).
  - `dependency`: Fires only the missing dependency (`product-catalog` + `cart`) alerts.
  - `oom`: Fires only the `ad` service `OOMKilled` (exit code 137) alert.

Usage:
    # 1. Fire combined OTEL-APP incident against local TalkOps server (default: http://127.0.0.1:2024):
    python scripts/simulate_alertmanager_webhook.py

    # 2. Fire only Scenario 1 (missing Helm dependency) or Scenario 2 (low memory OOMKilled):
    python scripts/simulate_alertmanager_webhook.py --scenario dependency
    python scripts/simulate_alertmanager_webhook.py --scenario oom

    # 3. Fire against your ngrok URL and wait for the RCA:
    python scripts/simulate_alertmanager_webhook.py --url https://undecided-crucial-bush.ngrok-free.dev --wait
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime

import httpx

OTEL_CHARTS_DIR = "/Users/structbinary/Documents/work/talkops/OTEL-APP/charts"
DEFAULT_NAMESPACE = "otel-demo"
DEFAULT_CLUSTER = "docker-desktop"


def _discover_live_pod_name(namespace: str, app_name: str, fallback: str) -> str:
    """Discover the actual pod name in Kubernetes so the alert matches live cluster state."""
    try:
        proc = subprocess.run(
            [
                "kubectl",
                "get",
                "pods",
                "-n",
                namespace,
                "-l",
                f"app.kubernetes.io/name={app_name}",
                "-o",
                "jsonpath={.items[0].metadata.name}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        pod_name = proc.stdout.strip()
        if proc.returncode == 0 and pod_name:
            return pod_name
    except Exception:
        pass
    return fallback


def build_alertmanager_v4_payload(
    *,
    incident_suffix: str,
    scenario: str = "all",
    namespace: str = DEFAULT_NAMESPACE,
    cluster: str = DEFAULT_CLUSTER,
) -> dict:
    """Build a realistic CNCF Prometheus Alertmanager v4 firing webhook payload for OTEL-APP in `otel-demo`."""
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    fingerprint = f"fp-otel-{incident_suffix}"

    product_catalog_pod = _discover_live_pod_name(
        namespace, "product-catalog", "product-catalog-bc96cfd4-npmm6"
    )
    cart_pod = _discover_live_pod_name(
        namespace, "cart", "cart-75f5c7b8cd-wr5w6"
    )
    ad_pod = _discover_live_pod_name(
        namespace, "ad", "ad-6d5cbd5774-5mtvf"
    )

    dependency_alerts = [
        {
            "status": "firing",
            "labels": {
                "alertname": "KubePodCrashLooping",
                "severity": "critical",
                "namespace": namespace,
                "cluster": cluster,
                "pod": product_catalog_pod,
                "container": "product-catalog",
                "service": "product-catalog",
                "helm_chart": f"{OTEL_CHARTS_DIR}/product-catalog",
                "reason": "Error",
            },
            "annotations": {
                "summary": (
                    f"Pod {product_catalog_pod} in namespace {namespace} is in CrashLoopBackOff "
                    "(Exit Code 1 — database connection failure)"
                ),
                "description": (
                    f"Container `product-catalog` in pod `{product_catalog_pod}` (namespace `{namespace}`, "
                    f"context `{cluster}`) is crash-looping because its required PostgreSQL dependency "
                    f"(`postgresql:5432`) is missing from the `{namespace}` namespace. "
                    f"Helm charts repository is located at `{OTEL_CHARTS_DIR}`."
                ),
            },
            "startsAt": now_iso,
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "http://prometheus.monitoring.svc:9090/graph?g0.expr=kube_pod_container_status_waiting_reason",
            "fingerprint": f"{fingerprint}-dep-pg",
        },
        {
            "status": "firing",
            "labels": {
                "alertname": "KubePodInitContainerStuck",
                "severity": "warning",
                "namespace": namespace,
                "cluster": cluster,
                "pod": cart_pod,
                "init_container": "wait-for-valkey",
                "service": "cart",
                "helm_chart": f"{OTEL_CHARTS_DIR}/cart",
                "reason": "PodInitializing",
            },
            "annotations": {
                "summary": (
                    f"Pod {cart_pod} in namespace {namespace} is stuck in Init:0/1 (wait-for-valkey)"
                ),
                "description": (
                    f"Init container `wait-for-valkey` in pod `{cart_pod}` (namespace `{namespace}`) is "
                    f"blocked polling `valkey-cart:6379` because the `valkey-cart` Helm release is not installed "
                    f"from `{OTEL_CHARTS_DIR}/valkey-cart`."
                ),
            },
            "startsAt": now_iso,
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "http://prometheus.monitoring.svc:9090/graph?g0.expr=kube_pod_init_container_status_waiting",
            "fingerprint": f"{fingerprint}-dep-valkey",
        },
    ]

    oom_alerts = [
        {
            "status": "firing",
            "labels": {
                "alertname": "KubePodOOMKilled",
                "severity": "critical",
                "namespace": namespace,
                "cluster": cluster,
                "pod": ad_pod,
                "container": "ad",
                "service": "ad",
                "helm_chart": f"{OTEL_CHARTS_DIR}/ad",
                "reason": "OOMKilled",
                "exit_code": "137",
            },
            "annotations": {
                "summary": (
                    f"Pod {ad_pod} in namespace {namespace} is CrashLoopBackOff "
                    "(Last Termination: OOMKilled, Exit Code 137)"
                ),
                "description": (
                    f"Java container `ad` in pod `{ad_pod}` (namespace `{namespace}`, context `{cluster}`) "
                    f"was terminated with `OOMKilled` (exit code 137). The Helm release `ad` in `{namespace}` "
                    f"was deployed with a misconfigured low memory limit (`resources.limits.memory=10Mi`, "
                    f"`resources.requests.memory=6Mi`) instead of the chart default (`300Mi` limit / `200Mi` request "
                    f"in `{OTEL_CHARTS_DIR}/ad/values.yaml`)."
                ),
            },
            "startsAt": now_iso,
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "http://prometheus.monitoring.svc:9090/graph?g0.expr=kube_pod_container_status_last_terminated_reason",
            "fingerprint": f"{fingerprint}-oom-ad",
        }
    ]

    if scenario == "dependency":
        alerts = dependency_alerts
        summary = (
            f"[OTEL-APP] Missing Helm dependencies in namespace `{namespace}`: "
            f"`{product_catalog_pod}` CrashLoopBackOff (missing `postgresql`) & `{cart_pod}` stuck in Init:0/1 (missing `valkey-cart`)"
        )
        description = (
            f"Multiple OpenTelemetry Demo microservices in Kubernetes namespace `{namespace}` (context `{cluster}`) "
            f"are failing to start due to unprovisioned Phase-0 infrastructure Helm charts in `{OTEL_CHARTS_DIR}`. "
            f"Inspect live pods, Helm releases, logs, and chart values in `{OTEL_CHARTS_DIR}` to verify the root cause and provide exact Helm remediation commands."
        )
    elif scenario == "oom":
        alerts = oom_alerts
        summary = (
            f"[OTEL-APP] Pod `{ad_pod}` in namespace `{namespace}` is OOMKilled (Exit Code 137 — memory limit set to 10Mi)"
        )
        description = (
            f"The OpenTelemetry Demo `ad` microservice in Kubernetes namespace `{namespace}` (context `{cluster}`) "
            f"is repeatedly crashing with `OOMKilled` (exit code 137). Inspect the live pod spec, Helm release values, "
            f"and `{OTEL_CHARTS_DIR}/ad/values.yaml` to confirm the memory misconfiguration and provide the exact `helm upgrade` fix."
        )
    else:
        alerts = dependency_alerts + oom_alerts
        summary = (
            f"[OTEL-APP] Critical Kubernetes Outage in `{namespace}`: `{product_catalog_pod}` CrashLoopBackOff "
            f"(missing `postgresql`), `{cart_pod}` Init:0/1 (missing `valkey-cart`), and `{ad_pod}` OOMKilled (Exit Code 137, 10Mi limit)"
        )
        description = (
            f"OpenTelemetry Demo microservices in Kubernetes namespace `{namespace}` (cluster `{cluster}`, "
            f"Helm charts at `{OTEL_CHARTS_DIR}`) have 3 active failures:\n"
            f"1. `{product_catalog_pod}` is in `CrashLoopBackOff` (Exit Code 1) because required dependency `postgresql` is not installed.\n"
            f"2. `{cart_pod}` is stuck in `Init:0/1` (`wait-for-valkey`) because `valkey-cart` is not installed.\n"
            f"3. `{ad_pod}` is crashing with `OOMKilled` (Exit Code 137) because its Helm release memory limit was lowered to `10Mi` (chart default in `{OTEL_CHARTS_DIR}/ad/values.yaml` is `300Mi`).\n"
            f"Run read-only `kubectl` and `helm` diagnostics against namespace `{namespace}` and `{OTEL_CHARTS_DIR}` to verify all failure modes and produce a complete Root Cause Analysis with exact Helm remediation commands."
        )

    group_key = f'{{}}:{{alertname="OtelDemoServiceOutage", namespace="{namespace}", scenario="{scenario}", id="{incident_suffix}"}}'

    return {
        "version": "4",
        "groupKey": group_key,
        "truncatedAlerts": 0,
        "status": "firing",
        "receiver": "talkops-autonomous-sre",
        "groupLabels": {
            "alertname": "OtelDemoServiceOutage",
            "namespace": namespace,
            "service": "otel-demo",
        },
        "commonLabels": {
            "alertname": "OtelDemoServiceOutage",
            "severity": "critical",
            "namespace": namespace,
            "service": "otel-demo",
            "cluster": cluster,
            "charts_dir": OTEL_CHARTS_DIR,
            "scenario": scenario,
        },
        "commonAnnotations": {
            "summary": summary,
            "description": description,
            "runbook_url": "https://github.com/talkops-ai/OTEL-APP#dependency-graph--deployment-order",
        },
        "externalURL": "http://alertmanager.monitoring.svc:9093",
        "alerts": alerts,
    }


def main() -> int:
    """Execute main."""
    parser = argparse.ArgumentParser(
        description="Trigger a simulated Alertmanager v4 webhook against TalkOps for the live OTEL-APP Kubernetes scenario."
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:2024",
        help="Base URL of the TalkOps server (e.g. http://127.0.0.1:2024 or your ngrok URL).",
    )
    parser.add_argument(
        "--scenario",
        choices=["all", "dependency", "oom"],
        default="all",
        help="Which failure scenario to fire: 'all' (both missing dependency + OOMKilled), 'dependency', or 'oom'.",
    )
    parser.add_argument(
        "--namespace",
        default=DEFAULT_NAMESPACE,
        help="Kubernetes namespace where OTEL-APP charts are deployed (default: otel-demo).",
    )
    parser.add_argument(
        "--secret",
        default="",
        help="Optional webhook secret if TALKOPS_WEBHOOK_SECRET_ALERTMANAGER is configured.",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Poll /webhooks/deliveries until the autonomous RCA run completes.",
    )
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    endpoint = f"{base_url}/webhooks/alertmanager"
    incident_suffix = uuid.uuid4().hex[:8]
    payload = build_alertmanager_v4_payload(
        incident_suffix=incident_suffix,
        scenario=args.scenario,
        namespace=args.namespace,
    )

    headers = {"Content-Type": "application/json"}
    if args.secret:
        headers["Authorization"] = f"Bearer {args.secret}"

    print(f"--> Sending Alertmanager v4 FIRING webhook (scenario={args.scenario}, ns={args.namespace}) to: {endpoint}")
    with httpx.Client(timeout=20.0) as client:
        resp = client.post(endpoint, json=payload, headers=headers)
        print(f"<-- HTTP {resp.status_code}")
        try:
            body = resp.json()
            print(json.dumps(body, indent=2))
        except Exception:
            print(resp.text)
            return 1

        if resp.status_code not in (200, 202):
            return 1

        delivery_id = body.get("delivery_id")
        thread_id = body.get("thread_id")
        print(f"\n[OK] Alert accepted! LangGraph Thread ID: {thread_id}")
        print(f"[TIP] To attach to this investigation in your terminal, run:")
        print(f"      talkops --server-url {base_url} -r {thread_id}")

        if args.wait and delivery_id:
            print("\nWaiting for autonomous agent RCA to complete (polling /webhooks/deliveries)...")
            for _ in range(120):
                time.sleep(2.0)
                deliv_resp = client.get(f"{base_url}/webhooks/deliveries")
                if deliv_resp.status_code == 200:
                    deliveries = deliv_resp.json().get("deliveries", [])
                    match = next((d for d in deliveries if d.get("delivery_id") == delivery_id), None)
                    if match:
                        status = match.get("run_status")
                        if status and status != "running":
                            print(f"\n=== RCA Completed (status={status}) ===")
                            if match.get("analysis_preview"):
                                print(match["analysis_preview"])
                            if match.get("error"):
                                print(f"Error: {match['error']}")
                            break
    return 0


if __name__ == "__main__":
    sys.exit(main())


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

"""Simulate a Prometheus Alertmanager v4 webhook firing into TalkOps Server (`POST /webhooks/alertmanager`).

Usage:
    # 1. Fire against local TalkOps server (default: http://127.0.0.1:2024):
    python scripts/simulate_alertmanager_webhook.py

    # 2. Fire against your ngrok URL:
    python scripts/simulate_alertmanager_webhook.py --url https://undecided-crucial-bush.ngrok-free.dev

    # 3. Poll until the autonomous RCA completes and print the analysis:
    python scripts/simulate_alertmanager_webhook.py --wait
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import UTC, datetime

import httpx


def build_alertmanager_v4_payload(*, incident_suffix: str) -> dict:
    """Build a realistic CNCF Prometheus Alertmanager v4 firing webhook payload."""
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    fingerprint = f"fp-{incident_suffix}"
    group_key = f'{{}}:{{alertname="KubePodCrashLooping", namespace="prod-payments", id="{incident_suffix}"}}'

    return {
        "version": "4",
        "groupKey": group_key,
        "truncatedAlerts": 0,
        "status": "firing",
        "receiver": "talkops-autonomous-sre",
        "groupLabels": {
            "alertname": "KubePodCrashLooping",
            "namespace": "prod-payments",
            "service": "payments-checkout-api",
        },
        "commonLabels": {
            "alertname": "KubePodCrashLooping",
            "severity": "critical",
            "namespace": "prod-payments",
            "service": "payments-checkout-api",
            "cluster": "eks-prod-ap-south-1",
            "deployment": "payments-checkout-api",
        },
        "commonAnnotations": {
            "summary": "Pod payments-checkout-api-6d8f9b7c4-x9k2m in prod-payments is CrashLoopBackOff (exit code 137 / OOMKilled)",
            "description": (
                "Container `checkout-worker` in pod `payments-checkout-api-6d8f9b7c4-x9k2m` "
                "(namespace `prod-payments`) has restarted 6 times in the last 10 minutes with "
                "last termination reason `OOMKilled` (exit code 137, memory limit 512Mi). "
                "HTTP 5xx error rate on `/api/v1/checkout` is currently 18.4%."
            ),
            "runbook_url": "https://runbooks.internal.example/k8s/KubePodCrashLooping",
        },
        "externalURL": "http://alertmanager.monitoring.svc:9093",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "KubePodCrashLooping",
                    "severity": "critical",
                    "namespace": "prod-payments",
                    "pod": "payments-checkout-api-6d8f9b7c4-x9k2m",
                    "container": "checkout-worker",
                    "service": "payments-checkout-api",
                    "reason": "OOMKilled",
                },
                "annotations": {
                    "summary": "Pod payments-checkout-api-6d8f9b7c4-x9k2m is CrashLooping (OOMKilled, exit code 137)",
                    "description": (
                        "Container checkout-worker exceeded memory limit (512Mi) following deployment v2.14.0 "
                        "rollout and was terminated by the Linux OOM killer."
                    ),
                },
                "startsAt": now_iso,
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitoring.svc:9090/graph?g0.expr=kube_pod_container_status_waiting_reason",
                "fingerprint": fingerprint,
            },
            {
                "status": "firing",
                "labels": {
                    "alertname": "HighHttp5xxErrorRate",
                    "severity": "critical",
                    "namespace": "prod-payments",
                    "pod": "payments-checkout-api-6d8f9b7c4-q4p8n",
                    "service": "payments-checkout-api",
                },
                "annotations": {
                    "summary": "HTTP 5xx error rate on payments-checkout-api is 18.4% (SLO threshold: 1.0%)",
                    "description": "Upstream connection resets due to checkout-worker pods restarting under memory pressure.",
                },
                "startsAt": now_iso,
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitoring.svc:9090/graph?g0.expr=http_requests_5xx_rate5m",
                "fingerprint": f"{fingerprint}-5xx",
            },
        ],
    }


def main() -> int:
    """Execute main."""
    parser = argparse.ArgumentParser(description="Trigger a simulated Alertmanager v4 webhook against TalkOps.")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:2024",
        help="Base URL of the TalkOps server (e.g. http://127.0.0.1:2024 or your ngrok URL).",
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
    payload = build_alertmanager_v4_payload(incident_suffix=incident_suffix)

    headers = {"Content-Type": "application/json"}
    if args.secret:
        headers["Authorization"] = f"Bearer {args.secret}"

    print(f"--> Sending Alertmanager v4 FIRING webhook to: {endpoint}")
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

        if args.wait and delivery_id:
            print("\nWaiting for autonomous agent RCA to complete (polling /webhooks/deliveries)...")
            for _ in range(90):
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

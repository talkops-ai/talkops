# Kubernetes & Container Security Compliance Rubric

1. **No Privileged or Root Containers:** Every Kubernetes `Deployment`, `StatefulSet`, `DaemonSet`, or `Job` in the changed manifests must set `securityContext.runAsNonRoot: true` and `allowPrivilegeEscalation: false`, and must not set `privileged: true`.
2. **Explicit Resource Requests & Limits:** Every container spec must define CPU and memory `requests` and `limits` to prevent noisy-neighbor starvation and node `OOMKilled` cascades.
3. **Pinned Image Tags:** No container image may use the mutable `:latest` tag; all images must use an explicit semantic version tag or immutable SHA-256 digest.
4. **Health Probes Configured:** Production-facing workloads must define both `readinessProbe` and `livenessProbe` (or `startupProbe`).
5. **Actionable Markdown Summary Table:** Output a Markdown table listing each modified workload, pass/fail status per criterion, exact file/line numbers, and a copy-pasteable YAML fix for any violation.

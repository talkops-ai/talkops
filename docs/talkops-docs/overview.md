# TalkOps Overview

TalkOps is an autonomous DevOps operations framework built on LangGraph and the TypeSafe AI Jev engine. It executes end-to-end operational workflows across multi-cloud infrastructure (AWS, Azure, GCP), Kubernetes clusters, Infrastructure as Code (Terraform, OpenTofu, CloudFormation, Ansible), CI/CD pipelines, observability stacks, FinOps, and application repositories.

Deploy TalkOps locally in your terminal (`talkops`), headlessly inside CI/CD pipelines (`talkops -p`), or centrally as a multi-tenant server (`talkops serve`) that listens on production webhooks (`/webhooks/{provider}`) and serves multiple connected operator clients (`--server-url`).

> [!NOTE]
> 📸 **Image Placeholder — `docs/assets/overview-architecture-tui.png`**
> **What to capture:** High-resolution screenshot of the TalkOps TUI connected to a central server, displaying the status bar (`Smart Mode • Tier 2 Powerful [effort=max] • aws:prod-admin • k8s:prod-eks`), a multi-agent delegation tree (`k8s-agent`, `observability-agent`, `iac-agent`), and real-time Jev routing/safety badges.

<table>
  <tr>
    <td width="50%" valign="top">
      <h3>⚡ Jev Smart Mode (Cost, Latency &amp; Safety)</h3>
      <p>Routes prompts across 3 model tiers and reasoning levels in <code>&lt;70ms</code> (slashing token cost by 80–90% on reflex lookups with <code>effort="off"</code>) and classifies tool blast radius in <code>&lt;100ms</code> with zero LLM prompt token overhead.</p>
      <a href="approval-mode.md">Explore Smart Mode →</a>
    </td>
    <td width="50%" valign="top">
      <h3>🌐 Central Server &amp; Multi-Client Runtime</h3>
      <p>Run <code>talkops serve</code> with PostgreSQL state persistence, 1-step API-key-to-JWT token exchange, and multi-user thread isolation so multiple engineers can connect via <code>--server-url</code>.</p>
      <a href="central-server.md">Deploy Central Server →</a>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <h3>🔔 Event-Driven Webhooks &amp; Write-Backs</h3>
      <p>Listen on 9 built-in webhook adapters (PagerDuty, Alertmanager, Datadog, AWS SNS, CloudEvents, ServiceNow, Jenkins, GitHub, Azure Monitor) and post automated RCAs back to tickets, PRs, and Slack.</p>
      <a href="webhooks.md">Configure Webhooks →</a>
    </td>
    <td width="50%" valign="top">
      <h3>🤖 Dynamic DevOps Subagents</h3>
      <p>Delegate complex investigations in parallel across dynamic plugin subagents, project (<code>.talkops/agents/</code>), user, and async remote subagents with independent Smart Mode routing.</p>
      <a href="subagents.md">Explore Subagents →</a>
    </td>
  </tr>
</table>

---

## What TalkOps can do across DevOps operations

TalkOps is not limited to writing code or running single-cloud CLI commands. Anything practically achievable by a senior SRE, platform engineer, or cloud architect from a terminal or API can be orchestrated, verified, and governed through TalkOps:

- **Autonomous Incident Triage & RCA**: Receive real-time webhooks from PagerDuty, Prometheus Alertmanager, Datadog, AWS SNS, or ServiceNow; deduplicate alert storms; correlate metrics, logs, and recent deployments in a stateful thread; and write the Root Cause Analysis back to the incident ticket or Slack.
- **Multi-Cloud Operations (AWS, Azure, GCP)**: Inspect and remediate EC2/EKS/RDS/IAM on AWS, AKS/VNet/Monitor on Azure, and GKE/Cloud Run/IAM on GCP using native SDKs and CLI credential chains.
- **Kubernetes & Container Orchestration**: Diagnose `CrashLoopBackOff`, `OOMKilled`, PVC binding failures, CoreDNS latency, and Ingress/NetworkPolicy misconfigurations; audit and upgrade Helm releases.
- **Infrastructure as Code & GitOps**: Author, validate, plan, and refactor Terraform, OpenTofu, Terragrunt, AWS CloudFormation, and Ansible playbooks; reconcile ArgoCD/Flux drift.
- **CI/CD Pipeline Recovery & Code Review**: Analyze failed GitHub Actions workflows and Jenkins builds, fix underlying code or pipeline YAML issues, run diff-aware `/review` checks, and comment directly on pull requests.
- **FinOps Cost Optimization & Security Auditing**: Identify unattached volumes, idle load balancers, and over-provisioned compute; audit IAM policies for least-privilege compliance.

---

## Core architecture

### 1. Three deployment topologies

TalkOps runs the exact same LangGraph agent architecture across three operational modes:

| Topology | How to Launch | Best For |
| :--- | :--- | :--- |
| **Local Interactive TUI** | `talkops --smart` | Individual engineers diagnosing clusters, writing IaC, or reviewing code locally with SQLite persistence (`~/.talkops/sessions.db`). |
| **Headless CLI & CI/CD** | `talkops -p "<prompt>" --smart --criteria "..."` | Automated CI/CD gates, drift detection cron jobs, and scripted remediation loops with `--max-turns` and rubric evaluation. |
| **Central Server + Multi-Client** | Server: `talkops serve --postgres-uri ...`<br>Client: `talkops --server-url http://host:2024` | Platform teams sharing threads, ingesting 24/7 production webhooks, and enforcing centralized JWT auth and PostgreSQL persistence. |

### 2. Jev Smart Mode: Dual-engine cost, latency & safety optimization

When running in **Smart Mode** (`--smart`, `/smart`, or `Shift+Tab`), TalkOps uses the **TypeSafe AI Jev** engine in two distinct places on every turn:

1. **Dynamic Model & Reasoning Router (`<70ms` via `JevDynamicModelRouterMiddleware`)**:
   - Classifies the incoming prompt and `routing_state` (`agent_role`, `active_capabilities`, `available_subagents`, `workspace_profile`) in `<70ms`.
   - Routes **Tier 0 (`fast`)** queries (greetings, status checks, single-item lookups) to lightweight models (`haiku`, `mini`, `flash-lite`) with **`effort = "off"`**—eliminating reasoning latency and reducing token cost by **80–90%**.
   - Routes **Tier 1 (`standard`)** single-domain DevOps and coding tasks to balanced models (`sonnet`, `gpt-5.4`, `flash`) with **`effort = "medium"`** or **`"low"`**.
   - Routes **Tier 2 (`powerful`)** complex tasks (multi-agent delegation, SRE incident root-cause correlation, FinOps optimization, security audits, cross-service refactoring) to frontier models (`opus`, `o3-pro`, `gemini-3.1-pro`) with **`effort = "max"`** or **`"xhigh"`**.
   - Includes **confidence escalation safeguards** (escalating to `powerful` if confidence `< 0.55` on high-complexity markers) and **automatic baseline restoration** when switching back to `/manual` or `/auto`.
2. **System One Tool Safety Classifier (`<100ms` via `JevSecurityClassifier`)**:
   - Evaluates every proposed shell command, file edit, MCP tool call, and subagent dispatch in `<100ms` with **zero LLM prompt token overhead** across `is_mutating` (`Noul`), `blast_radius` (`Score` 0–2), and `intent_alignment` (`Noul`).
   - Auto-approves aligned read-only and controlled operations while failing closed to human approval on production mutations or critical blast radius (`>= 1.2`).

### 3. Dynamic subagent orchestration

Instead of overloading a single context window, the TalkOps orchestrator delegates domain-specific work to dynamic subagents (`src/talkops/subagents/loader.py`) with isolated memory (`SubagentMemoryStore`), scoped skills, and dedicated `SUBAGENT_ROUTE_CRITERIA` Smart Mode routing:

| Subagent Source | Path / Config | Purpose |
| :--- | :--- | :--- |
| **Agent Plugins** | `~/.talkops/plugins/<plugin>/agents/*.md` | Domain specialist packs installed via `talkops plugin install` (e.g., AWS SRE, FinOps, EKS, Security, Observability). |
| **Project Subagents** | `.talkops/agents/*.md` or `.agents/*.md` | Version-controlled repository and infrastructure specialists shared across the team. |
| **User Subagents** | `~/.talkops/agents/*.md` | Personal workstation subagents available across all repositories. |
| **Async Remote Subagents** | `[async_subagents]` in `config.toml` | Background LangGraph subagent graphs running on remote endpoints. |

---

## Documentation map

### Getting Started
- **[Quickstart](quickstart.md)** — Install TalkOps, configure credentials, and run your first local, headless, or remote server session
- **[CLI & Slash Command Reference](cli-reference.md)** — Dedicated mini-sections for every CLI subcommand (`serve`, `auth`, `threads`, `mcp`, `plugin`, `skills`, `doctor`) and interactive `/slash` command

### Core Operations, Server & Safety
- **[Approval Modes & Jev Smart Mode](approval-mode.md)** — How Smart Mode optimizes model cost/latency (`<70ms`) and gates tool blast radius (`<100ms`)
- **[Central Server & Multi-Client Architecture](central-server.md)** — Deploying `talkops serve` with PostgreSQL, JWT token exchange, and multi-user client attach
- **[Webhooks & Event-Driven Automation](webhooks.md)** — Configuring the 9 webhook adapters, storm deduplication, and RCA write-back sinks
- **[Goal Mode & Rubrics](goal-and-rubrics.md)** — Autonomous multi-turn goal verification (`/goal`, `/rubric`, `--criteria`)
- **[Remote Sandboxes](remote-sandboxes.md)** — Running commands in `local`, `docker`, or `k8s` sandboxes

### Configuration & Providers
- **[Configuration Guide](Configuration.md)** — Understanding the 4-layer configuration hierarchy
- **[`config.toml` Reference](config.toml.md)** — Complete schema for all 11 TOML sections
- **[Credentials & Authentication](credentials.md)** — Managing LLM keys, Control Plane JWTs, cloud chains, and webhook secrets
- **[Model Providers](model-providers.md)** — Configuring Anthropic, OpenAI, Gemini, Bedrock, Azure, Ollama, and Smart Mode model pools

### Customization & Extensibility
- **[Subagents](subagents.md)** — Dynamic plugin subagents, custom `.talkops/agents/*.md` definitions, and Smart Mode subagent routing
- **[Memory & Skills](memory-and-skills.md)** — Persistent `AGENTS.md` memory, reusable `SKILL.md` operational runbooks, and cryptographically verified scripts
- **[Custom Commands](custom-commands.md)** — Creating project and user slash commands in `.talkops/commands/`
- **[Plugins](plugins.md)** — Installing and packaging multi-component DevOps plugins
- **[MCP Tools](mcp-tools.md)** — Attaching external `stdio`, `sse`, and `http` Model Context Protocol tool servers
- **[Hooks](hooks.md)** — Running deterministic scripts on `SessionStart`, `PreToolUse`, `PostToolUse`, and `webhook.*` lifecycle events

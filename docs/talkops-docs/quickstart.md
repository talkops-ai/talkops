# Quickstart

Get TalkOps installed, verified, and executing autonomous DevOps operations—locally in your terminal, headlessly in CI/CD, or centrally as a multi-client server listening on production webhooks—in under five minutes.

> [!NOTE]
> 📸 **Image Placeholder — `docs/assets/quickstart-first-run.png`**
> **What to capture:** Screenshot of a terminal running `talkops doctor` (showing green checkmarks for LLM keys, TypeSafe AI Jev, AWS/K8s CLIs, and SQLite/Postgres) followed by launching `talkops --smart`.

---

## 1. Install TalkOps

Run the standalone installer script on macOS, Linux, or WSL:

```bash
curl -LsSf https://raw.githubusercontent.com/talkops-ai/talkops/ui-enhancement/scripts/install.sh | bash
```

<details>
<summary>Alternative installation methods (<code>uv</code>, <code>pipx</code>, <code>pip</code>, or pinned version)</summary>

```bash
# Using uv (recommended for Python 3.12+ environments)
uv tool install --prerelease=allow talkops-ai

# Using pipx
pipx install --pip-args="--pre" talkops-ai

# Using pip
pip install --pre -U talkops-ai

# Install a specific version via install.sh
curl -LsSf https://raw.githubusercontent.com/talkops-ai/talkops/ui-enhancement/scripts/install.sh | bash -s -- 0.3.0b16
```

</details>

Verify the binary (and its `ops` shorthand alias) is on your `PATH`:

```bash
talkops --version
ops --version
```

---

## 2. Configure credentials

TalkOps requires at least one LLM provider key (such as Anthropic, OpenAI, Google Gemini, or AWS Bedrock) and optionally a **TypeSafe AI Jev** key (`TYPESAFE_API_KEY`) to enable `<70ms` Smart Mode model routing and `<100ms` tool safety classification.

```bash
# Store keys encrypted in ~/.talkops/.state/config.db
talkops auth set anthropic sk-ant-...
talkops auth set typesafe ts-...

# Optional: Bind your default AWS profile and region
talkops auth aws --profile default --region us-east-1
```

<details>
<summary>Or configure via environment variables</summary>

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export TYPESAFE_API_KEY="ts-..."
export AWS_PROFILE="default"
export AWS_REGION="us-east-1"
```

</details>

---

## 3. Verify your environment with `talkops doctor`

Run the built-in 9-point diagnostic check to confirm your LLM credentials, Jev connectivity, cloud CLIs (`aws`, `az`, `gcloud`, `kubectl`, `terraform`, `helm`), and state database are ready:

```bash
talkops doctor
```

---

## 4. Choose your operational workflow

### Workflow A: Interactive TUI in Smart Mode

Launch TalkOps inside any project or infrastructure repository with **Smart Mode** enabled:

```bash
talkops --smart
```

Try these prompts inside the TUI:
- **Quick status lookup** *(routes to Tier 0 `fast` with `effort="off"` in `<70ms`—zero thinking latency and 80–90% lower token cost)*:
  ```text
  List all non-running pods across namespaces in the current Kubernetes context
  ```
- **Multi-agent incident & cost investigation** *(automatically escalates to Tier 2 `powerful` with `effort="max"`)*:
  ```text
  Correlate why checkout-service pods are restarting in prod-eks, check recent Helm revisions, and audit our top 3 AWS EC2 cost drivers this week
  ```

Useful slash commands while in the TUI:
- **`/smart`**, **`/manual`**, **`/auto`** (or `Shift+Tab`) — Switch approval and routing modes
- **`/pool`** — Inspect the 3-tier (`fast`, `standard`, `powerful`) model pool and cost savings
- **`/cloud`** — Switch active AWS profile, Azure subscription, GCP project, or Kubernetes context
- **`/review`** — Run a security and IaC/code quality review on your uncommitted `git diff`
- **`/fork`** — Branch the current investigation thread to test an alternative hypothesis

---

### Workflow B: Central Server + Multiple Connected Clients

Platform teams can launch `talkops serve` centrally so multiple engineers and CI runners can connect to a shared runtime backed by PostgreSQL:

1. **Start the Central Server**:

   ```bash
   talkops serve \
     --host 0.0.0.0 \
     --port 2024 \
     --postgres-uri "postgresql://ops:secret@db:5432/talkops" \
     --auth-mode token \
     --approval-mode smart
   ```

2. **Authenticate and connect from any operator machine**:

   ```bash
   # Exchange your long-lived API key once for a cached, auto-refreshing JWT
   talkops auth login --server-url http://talkops.internal:2024 --api-key oc_live_... --user-id alice

   # Launch the local TUI attached to the central server
   talkops --server-url http://talkops.internal:2024

   # List shared or personal remote threads
   talkops threads list --server-url http://talkops.internal:2024
   ```

---

### Workflow C: Event-Driven Webhook Incident Triage

While `talkops serve` is running, send a test alert to `/webhooks/{provider}` (supports `pagerduty`, `alertmanager`, `datadog`, `sns`, `cloudevents`, `servicenow`, `jenkins`, `github`, `azure-monitor`, and `generic`):

```bash
curl -X POST http://localhost:2024/webhooks/alertmanager \
  -H "Content-Type: application/json" \
  -d '{
    "status": "firing",
    "groupKey": "prod-eks:checkout-5xx",
    "alerts": [{
      "status": "firing",
      "labels": {
        "alertname": "HighCheckout5xxRate",
        "severity": "critical",
        "namespace": "production",
        "service": "checkout-api"
      },
      "annotations": {
        "summary": "5xx error rate exceeded 12% for 5 minutes on checkout-api"
      }
    }]
  }'
```

TalkOps immediately returns `202 Accepted` with a `thread_id`, executes the Root Cause Analysis in the background, posts the RCA to your configured Slack/PagerDuty/ServiceNow/GitHub sink, and lets you attach to the live thread from your terminal:

```bash
talkops --server-url http://localhost:2024 -r <thread_id>
```

---

### Workflow D: Headless CI/CD Execution (`-p`)

Run non-interactive checks inside GitHub Actions, GitLab CI, or Jenkins with structured acceptance criteria:

```bash
talkops -p "Validate all Terraform modules in ./infra and verify no security group opens port 22 to 0.0.0.0/0" \
  --smart \
  --max-turns 10 \
  --criteria "All modules validated and security group compliance table printed"
```

---

## Next steps

- **[CLI & Slash Command Reference](cli-reference.md)** — Browse individual mini-sections for every CLI subcommand and `/slash` command
- **[Approval Modes & Jev Smart Mode](approval-mode.md)** — Understand how `<70ms` dynamic model routing and `<100ms` tool safety classification work
- **[Central Server & Multi-Client Architecture](central-server.md)** — Configure PostgreSQL persistence, JWT auth, and multi-user isolation
- **[Webhooks & Event-Driven Automation](webhooks.md)** — Connect PagerDuty, Alertmanager, Datadog, AWS SNS, ServiceNow, and GitHub webhooks

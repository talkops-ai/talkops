# `config.toml` Reference

TalkOps reads declarative TOML configuration from two locations—merging project-level overrides on top of user-level defaults (`src/talkops/config/toml_config.py` and `src/talkops/config/manifest.py`):
1. **User-global config**: `~/.talkops/config.toml`
2. **Project-workspace config**: `.talkops/config.toml`

Any string value in `config.toml` can reference an environment variable using the `"env:VAR_NAME"` prefix (for example, `secret = "env:TALKOPS_WEBHOOK_SECRET"`).

> [!NOTE]
> 📸 **Image Placeholder — `docs/assets/config-toml-tui-inspector.png`**
> **What to capture:** Screenshot of the `/config` interactive TUI inspector showing grouped settings (`Cloud`, `AWS`, `Azure`, `GCP`, `Kubernetes`, `DevOps`, `Model`, `Security`, `Sandbox`, `Webhooks`) and their resolved source layer (`config.toml`, `env`, or `config.db`).

---

## Complete `config.toml` template

```toml
# ── 1. Multi-Cloud & Kubernetes Context ──────────────────────────────────────
[cloud]
default_provider = "aws"               # "aws" | "azure" | "gcp" | "multi"

[aws]
profile = "prod-admin"
region = "us-east-1"
# endpoint_url = "http://localhost:4566" # Optional LocalStack / custom endpoint

[azure]
subscription_id = "00000000-0000-0000-0000-000000000000"
tenant_id = "11111111-1111-1111-1111-111111111111"
resource_group = "rg-production-eastus"
location = "eastus"

[gcp]
project_id = "my-prod-gcp-project"
region = "us-central1"
zone = "us-central1-a"
# credentials_file = "/path/to/service-account.json"

[k8s]
kubeconfig = "~/.kube/config"
context = "arn:aws:eks:us-east-1:123456789012:cluster/prod-eks"
namespace = "production"
helm_binary = "helm"

# ── 2. Full-Spectrum DevOps Tooling & Platform Endpoints ─────────────────────
[devops]
terraform_binary = "terraform"         # Or "tofu" for OpenTofu
terragrunt_binary = "terragrunt"
ansible_binary = "ansible-playbook"
docker_host = "unix:///var/run/docker.sock"
github_host = "github.com"
gitlab_url = "https://gitlab.com"
argocd_server = "argocd.internal.company.com"
prometheus_url = "http://prometheus.monitoring.svc:9090"
grafana_url = "https://grafana.internal.company.com"
loki_url = "http://loki.monitoring.svc:3100"
datadog_site = "datadoghq.com"
pagerduty_url = "https://api.pagerduty.com"
jira_url = "https://company.atlassian.net"
vault_addr = "https://vault.internal.company.com:8200"
jenkins_url = "https://jenkins.internal.company.com"
servicenow_url = "https://company.service-now.com"
sonarqube_url = "https://sonarqube.internal.company.com"

# ── 3. Models & Jev Smart Mode 3-Tier Pool ───────────────────────────────────
[models]
default = "anthropic:claude-sonnet-4-6"
reasoning_effort = "medium"            # "off" | "low" | "medium" | "high" | "xhigh" | "max"
pool = [
  "anthropic:claude-haiku-4-5",        # Tier 0 (fast)     -> effort = "off"
  "anthropic:claude-sonnet-4-6",       # Tier 1 (standard) -> effort = "medium"
  "anthropic:claude-opus-4-6"          # Tier 2 (powerful) -> effort = "max"
]

# ── 4. Security & Jev System One Approval Guardrails ─────────────────────────
[security]
approval_mode = "smart"                # "manual" | "smart" | "auto" | "yolo"
environment = "production"             # "development" | "staging" | "production"
shell_allow_list = ["kubectl", "helm", "terraform", "aws", "az", "gcloud", "git", "jq"]
blocked_commands = ["rm -rf /", "mkfs"]
dangerous_patterns = ["terraform destroy", "kubectl delete namespace"]

# ── 5. Execution Sandbox ─────────────────────────────────────────────────────
[sandbox]
type = "none"                          # "none" | "local" | "docker" | "k8s"
image = "ghcr.io/talkops/talkops-sandbox:latest"
memory_limit = "2g"
cpu_limit = 2.0
network_enabled = true
# setup_script = ".talkops/sandbox-init.sh"

# ── 6. Programmatic Tool Call (PTC) Interpreter ──────────────────────────────
[interpreter]
timeout_seconds = 30
memory_limit_mb = 64
max_ptc_calls = 50
max_result_chars = 50000
ptc = "safe"                           # "safe" | "all" | comma-separated tool list
ptc_acknowledge_unsafe = false

# ── 7. Inbound Webhooks & Outbound RCA Write-Back Sinks ──────────────────────
[webhooks]
host = "0.0.0.0"
port = 2024
dedupe_ttl_seconds = 300
max_concurrent_runs = 5
sns_auto_confirm = true
secret = "env:TALKOPS_WEBHOOK_SECRET"
pagerduty_secret = "env:TALKOPS_WEBHOOK_PAGERDUTY_SECRET"
github_secret = "env:TALKOPS_WEBHOOK_GITHUB_SECRET"
gitlab_token = "env:TALKOPS_WEBHOOK_GITLAB_TOKEN"
jenkins_token = "env:TALKOPS_WEBHOOK_JENKINS_TOKEN"
slack_url = "env:TALKOPS_WEBHOOK_SLACK_URL"
pagerduty_api_key = "env:TALKOPS_WEBHOOK_PAGERDUTY_API_KEY"
pagerduty_from_email = "talkops-bot@company.com"
github_token = "env:TALKOPS_WEBHOOK_GITHUB_TOKEN"
callback_url = "https://events.internal.company.com/talkops/rca"

# ── 8. Terminal UI & Display ─────────────────────────────────────────────────
[ui]
theme = "dark"
show_timestamps = true
auto_scroll = true
show_turn_duration = true
notifications_enabled = true
verbose_output = false
auto_compact = false
```

---

## Section-by-section reference

### `[cloud]`, `[aws]`, `[azure]`, `[gcp]`, and `[k8s]`

Configures the active multi-cloud and Kubernetes context injected into subagents (`aws-agent`, `azure-agent`, `gcp-agent`, `k8s-agent`, `helm-agent`) and displayed in the TUI status bar.

| Canonical Key | TOML Path | Env Var | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `cloud.default_provider` | `[cloud].default_provider` | `TALKOPS_CLOUD_PROVIDER` | `"aws"` | Primary cloud provider (`aws`, `azure`, `gcp`, `multi`). |
| `aws.profile` | `[aws].profile` | `AWS_PROFILE` | `None` | AWS CLI named profile. |
| `aws.region` | `[aws].region` | `AWS_REGION` | `"us-east-1"` | Default AWS region. |
| `aws.endpoint_url` | `[aws].endpoint_url` | `AWS_ENDPOINT_URL` | `None` | Custom endpoint URL (e.g., LocalStack). |
| `azure.subscription_id` | `[azure].subscription_id` | `AZURE_SUBSCRIPTION_ID` | `None` | Default Azure Subscription ID. |
| `azure.tenant_id` | `[azure].tenant_id` | `AZURE_TENANT_ID` | `None` | Azure Active Directory / Entra Tenant ID. |
| `azure.resource_group` | `[azure].resource_group` | `AZURE_RESOURCE_GROUP` | `None` | Default Azure Resource Group. |
| `azure.location` | `[azure].location` | `AZURE_LOCATION` | `None` | Default Azure region (e.g., `eastus`). |
| `gcp.project_id` | `[gcp].project_id` | `GCP_PROJECT_ID` | `None` | Google Cloud Project ID. |
| `gcp.region` | `[gcp].region` | `GCP_REGION` | `None` | Default GCP region (e.g., `us-central1`). |
| `gcp.zone` | `[gcp].zone` | `GCP_ZONE` | `None` | Default GCP compute zone. |
| `gcp.credentials_file` | `[gcp].credentials_file` | `GOOGLE_APPLICATION_CREDENTIALS` | `None` | Path to GCP Service Account JSON key. |
| `k8s.kubeconfig` | `[k8s].kubeconfig` | `KUBECONFIG` | `"~/.kube/config"` | Path to kubeconfig file. |
| `k8s.context` | `[k8s].context` | `KUBE_CONTEXT` | Active context | Target Kubernetes context name. |
| `k8s.namespace` | `[k8s].namespace` | `KUBE_NAMESPACE` | `"default"` | Default Kubernetes namespace. |
| `k8s.helm_binary` | `[k8s].helm_binary` | `HELM_BINARY` | `"helm"` | Helm CLI binary name or path. |

---

### `[devops]`

Configures binaries and API endpoints for Infrastructure as Code, GitOps, CI/CD, observability, and ITSM integrations.

| Canonical Key | TOML Path | Env Var | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `devops.terraform_binary` | `[devops].terraform_binary` | `TERRAFORM_BINARY` | `"terraform"` | Terraform or OpenTofu (`tofu`) binary. |
| `devops.terragrunt_binary` | `[devops].terragrunt_binary` | `TERRAGRUNT_BINARY` | `"terragrunt"` | Terragrunt binary name or path. |
| `devops.ansible_binary` | `[devops].ansible_binary` | `ANSIBLE_BINARY` | `"ansible-playbook"` | Ansible playbook binary. |
| `devops.docker_host` | `[devops].docker_host` | `DOCKER_HOST` | `None` | Docker daemon socket or TCP host. |
| `devops.github_host` | `[devops].github_host` | `GITHUB_HOST` | `"github.com"` | GitHub or GitHub Enterprise hostname. |
| `devops.gitlab_url` | `[devops].gitlab_url` | `GITLAB_URL` | `"https://gitlab.com"` | GitLab instance base URL. |
| `devops.argocd_server` | `[devops].argocd_server` | `ARGOCD_SERVER` | `None` | ArgoCD API server address. |
| `devops.prometheus_url` | `[devops].prometheus_url` | `PROMETHEUS_URL` | `None` | Prometheus HTTP API base URL. |
| `devops.grafana_url` | `[devops].grafana_url` | `GRAFANA_URL` | `None` | Grafana base URL. |
| `devops.loki_url` | `[devops].loki_url` | `LOKI_URL` | `None` | Grafana Loki log aggregation URL. |
| `devops.datadog_site` | `[devops].datadog_site` | `DD_SITE` | `"datadoghq.com"` | Datadog regional site domain. |
| `devops.pagerduty_url` | `[devops].pagerduty_url` | `PAGERDUTY_URL` | `"https://api.pagerduty.com"` | PagerDuty REST API base URL. |
| `devops.jira_url` | `[devops].jira_url` | `JIRA_URL` | `None` | Atlassian Jira base URL. |
| `devops.vault_addr` | `[devops].vault_addr` | `VAULT_ADDR` | `None` | HashiCorp Vault server URL. |
| `devops.jenkins_url` | `[devops].jenkins_url` | `JENKINS_URL` | `None` | Jenkins CI server base URL. |
| `devops.servicenow_url` | `[devops].servicenow_url` | `SERVICENOW_URL` | `None` | ServiceNow instance base URL. |
| `devops.sonarqube_url` | `[devops].sonarqube_url` | `SONARQUBE_URL` | `None` | SonarQube server base URL. |

---

### `[models]` and `[security]`

Configures LLM selection, the 3-tier Jev Smart Mode pool, and System One safety guardrails.

| Canonical Key | TOML Path | Env Var | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `model.default` | `[models].default` | `TALKOPS_MODEL` | Auto-detected | Default baseline model (`provider:model`). |
| `model.pool` | `[models].pool` | `TALKOPS_MODEL_POOL` | Auto 3-tier | 3-tier model list `[fast, standard, powerful]` used by Smart Mode `<70ms` routing. |
| `model.reasoning_effort` | `[models].reasoning_effort` | `TALKOPS_REASONING_EFFORT` | `"medium"` | Default thinking budget (`off`, `low`, `medium`, `high`, `xhigh`, `max`). |
| `security.approval_mode` | `[security].approval_mode` | `TALKOPS_APPROVAL_MODE` | `"manual"` | Active approval mode (`manual`, `smart`, `auto`, `yolo`). |
| `security.environment` | `[security].environment` | `TALKOPS_ENVIRONMENT` | `"development"` | Target environment (`development`, `staging`, `production`). Production forces fail-closed approval on mutating actions. |
| `security.shell_allow_list` | `[security].shell_allow_list` | `TALKOPS_SHELL_ALLOW_LIST` | `[]` | Explicitly permitted shell command prefixes. |

---

### `[webhooks]`

Configures the `talkops serve` / `talkops webhooks` listener, inbound verification secrets, alert storm deduplication, and outbound RCA write-back sinks.

| Canonical Key | TOML Path | Env Var | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `webhooks.host` | `[webhooks].host` | `TALKOPS_WEBHOOK_HOST` | `"0.0.0.0"` | Default bind host for `talkops serve`. |
| `webhooks.port` | `[webhooks].port` | `TALKOPS_WEBHOOK_PORT` | `2024` | Default bind port for `talkops serve`. |
| `webhooks.secret` | `[webhooks].secret` | `TALKOPS_WEBHOOK_SECRET` | `None` | Fallback shared secret / bearer token for inbound webhooks. |
| `webhooks.pagerduty_secret` | `[webhooks].pagerduty_secret` | `TALKOPS_WEBHOOK_PAGERDUTY_SECRET` | `None` | PagerDuty v3 HMAC-SHA256 signing secret. |
| `webhooks.github_secret` | `[webhooks].github_secret` | `TALKOPS_WEBHOOK_GITHUB_SECRET` | `None` | GitHub `X-Hub-Signature-256` HMAC secret. |
| `webhooks.gitlab_token` | `[webhooks].gitlab_token` | `TALKOPS_WEBHOOK_GITLAB_TOKEN` | `None` | GitLab `X-Gitlab-Token` verification secret. |
| `webhooks.jenkins_token` | `[webhooks].jenkins_token` | `TALKOPS_WEBHOOK_JENKINS_TOKEN` | `None` | Jenkins webhook bearer/header token. |
| `webhooks.dedupe_ttl_seconds` | `[webhooks].dedupe_ttl_seconds` | `TALKOPS_WEBHOOK_DEDUPE_TTL_SECONDS` | `300` | Alert storm deduplication window in seconds. |
| `webhooks.max_concurrent_runs` | `[webhooks].max_concurrent_runs` | `TALKOPS_WEBHOOK_MAX_CONCURRENT_RUNS` | `5` | Max simultaneous webhook-triggered LangGraph runs. |
| `webhooks.sns_auto_confirm` | `[webhooks].sns_auto_confirm` | `TALKOPS_WEBHOOK_SNS_AUTO_CONFIRM` | `true` | Automatically confirm AWS SNS `SubscriptionConfirmation` requests. |
| `webhooks.slack_url` | `[webhooks].slack_url` | `TALKOPS_WEBHOOK_SLACK_URL` | `None` | Slack Incoming Webhook URL for RCA write-backs. |
| `webhooks.pagerduty_api_key` | `[webhooks].pagerduty_api_key` | `TALKOPS_WEBHOOK_PAGERDUTY_API_KEY` | `None` | PagerDuty REST API token for posting incident RCA notes. |
| `webhooks.pagerduty_from_email` | `[webhooks].pagerduty_from_email` | `TALKOPS_WEBHOOK_PAGERDUTY_FROM_EMAIL` | `None` | PagerDuty `From` email header required when posting incident notes. |
| `webhooks.github_token` | `[webhooks].github_token` | `TALKOPS_WEBHOOK_GITHUB_TOKEN` | `None` | GitHub token for posting RCA comments on PRs and commits. |
| `webhooks.callback_url` | `[webhooks].callback_url` | `TALKOPS_WEBHOOK_CALLBACK_URL` | `None` | Generic outbound HTTP callback URL for webhook RCA results. |

---

## Next steps

- **[Configuration Guide](Configuration.md)** — Learn how the 4-layer configuration precedence resolves overrides
- **[Webhooks & Event-Driven Automation](webhooks.md)** — See full examples of inbound webhook payloads and outbound RCA sinks
- **[Central Server & Multi-Client Architecture](central-server.md)** — Configure `talkops serve` with PostgreSQL and multi-user authentication

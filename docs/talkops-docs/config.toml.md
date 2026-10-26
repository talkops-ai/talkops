# `config.toml` Reference

TalkOps stores your persistent preferences in `~/.talkops/config.toml`.

When you change settings in the TUI—such as saving a default model with `/model --default`, configuring your model pool with `/pool`, switching cloud profiles with `/cloud`, or updating values via `/config set`—TalkOps automatically creates and updates `~/.talkops/config.toml` for you. You can also edit this file directly in any text editor and apply your changes live with `/reload`.

---

## Starter `config.toml` Template

Below is a complete, copy-pasteable example of `~/.talkops/config.toml` showing the most common sections and options:

```toml
# ~/.talkops/config.toml

[cloud]
# Active cloud provider: "aws" | "azure" | "gcp" | "k8s"
provider = "aws"
# Cloud execution safety mode: "elevated" | "read_only"
execution_mode = "elevated"

[aws]
region = "us-east-1"
profile = "default"
# Optional IAM role ARN to assume for AWS operations
# role_arn = "arn:aws:iam::123456789012:role/TalkOpsOperatorRole"

[azure]
# subscription_id = "00000000-0000-0000-0000-000000000000"
# tenant_id = "00000000-0000-0000-0000-000000000000"
location = "eastus"
# resource_group = "rg-production"

[gcp]
# project_id = "my-gcp-project-id"
config_name = "default"
region = "us-central1"
# credentials_file = "/path/to/service-account.json"

[k8s]
# context = "arn:aws:eks:us-east-1:123456789012:cluster/prod"
namespace = "default"
kubeconfig = "~/.kube/config"

[devops]
tf_workspace = "default"
# tf_cli_args = "-compact-warnings"
helm_namespace = "default"
helm_driver = "secret"
# docker_host = "unix:///var/run/docker.sock"
# prometheus_url = "http://localhost:9090"
# grafana_url = "http://localhost:3000"
datadog_site = "datadoghq.com"

[models]
# Default model loaded at startup (provider:model)
default = "anthropic:claude-sonnet-4-5"

[effort]
# Default reasoning effort: "low" | "medium" | "high" | "max"
default = "medium"

[agent_pool]
# 3-tier model pool used for dynamic routing in Smart Mode
provider = "multi"
fast = "anthropic:claude-haiku-4-5"
standard = "anthropic:claude-sonnet-4-5"
powerful = "anthropic:claude-opus-4-6"

[security]
# Tool approval mode: "manual" | "smart" | "auto" | "yolo"
approval_mode = "manual"

[sandboxes]
# Default execution environment: "local" | "daytona" | "modal" | "runloop"
default = "local"
sync_workspace = true

[interpreter]
enable_interpreter = true
timeout_seconds = 30
memory_limit_mb = 64
ptc = "safe"

[startup]
read_project_dotenv = true

[ui]
theme = "talkops-dark"
show_timestamps = true
auto_scroll = true
show_turn_duration = true
notifications_enabled = true
verbose_output = false
auto_compact = false
```

---

## Section-by-Section Reference

### 1. Cloud & Infrastructure (`[cloud]`, `[aws]`, `[azure]`, `[gcp]`, `[k8s]`)

Controls which cloud environment and Kubernetes cluster TalkOps targets by default, as well as whether cloud commands run in `elevated` or `read_only` mode.

| TOML Key | `/config` Key | Environment Variable | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `[cloud].provider` | `cloud.provider` | `TALKOPS_CLOUD_PROVIDER` | `"aws"` | Active cloud provider (`aws`, `azure`, `gcp`, or `k8s`). |
| `[cloud].execution_mode` | `cloud.execution_mode` | `TALKOPS_CLOUD_EXECUTION_MODE` | `"elevated"` | Cloud safety mode: `elevated` (normal approval policy) or `read_only` (prompts before any cloud mutation). |
| `[aws].region` | `aws.region` | `AWS_REGION` *(or `AWS_DEFAULT_REGION`)* | `"us-east-1"` | Target AWS region for CLI and SDK calls. |
| `[aws].profile` | `aws.profile` | `AWS_PROFILE` | `"default"` | Named profile from `~/.aws/credentials` or `~/.aws/config` (use `@ambient` for instance/pod IAM roles). |
| `[aws].role_arn` | `aws.role_arn` | `AWS_ROLE_ARN` | `""` | Optional IAM role ARN to assume via STS. |
| `[azure].subscription_id` | `azure.subscription_id` | `AZURE_SUBSCRIPTION_ID` | `""` | Target Azure Subscription UUID. |
| `[azure].tenant_id` | `azure.tenant_id` | `AZURE_TENANT_ID` | `""` | Azure Active Directory / Entra ID Tenant UUID. |
| `[azure].location` | `azure.location` | `AZURE_DEFAULTS_LOCATION` | `"eastus"` | Default Azure region (e.g., `eastus`, `westeurope`). |
| `[azure].resource_group` | `azure.resource_group` | `AZURE_DEFAULTS_GROUP` | `""` | Default Azure Resource Group name (`-g`) for `az` commands. |
| `[azure].client_id` | `azure.client_id` | `AZURE_CLIENT_ID` | `""` | Service Principal or Managed Identity Client ID. |
| `[gcp].project_id` | `gcp.project_id` | `GOOGLE_CLOUD_PROJECT` | `""` | Target Google Cloud Project ID. |
| `[gcp].config_name` | `gcp.config_name` | `CLOUDSDK_ACTIVE_CONFIG_NAME` | `"default"` | Active `gcloud` CLI named configuration. |
| `[gcp].region` | `gcp.region` | `CLOUDSDK_COMPUTE_REGION` | `"us-central1"` | Default Google Cloud compute region. |
| `[gcp].credentials_file` | `gcp.credentials_file` | `GOOGLE_APPLICATION_CREDENTIALS` | `""` | Path to a GCP Service Account JSON key or Workload Identity config file. |
| `[k8s].context` | `k8s.context` | `KUBE_CONTEXT` | `""` | Target Kubernetes context name from your kubeconfig. |
| `[k8s].namespace` | `k8s.namespace` | `KUBE_NAMESPACE` | `"default"` | Default Kubernetes namespace for `kubectl` and Helm operations. |
| `[k8s].kubeconfig` | `k8s.kubeconfig` | `KUBECONFIG` | `"~/.kube/config"` | Path to your Kubernetes configuration file(s). |

---

### 2. DevOps & Observability Tools (`[devops]`)

Configures defaults for Terraform, Helm, Docker, GitHub, and monitoring integrations.

| TOML Key | `/config` Key | Environment Variable | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `[devops].tf_workspace` | `devops.tf_workspace` | `TF_WORKSPACE` | `"default"` | Active Terraform state workspace. |
| `[devops].tf_cli_args` | `devops.tf_cli_args` | `TF_CLI_ARGS` | `""` | Extra flags appended to Terraform CLI commands (e.g., `-compact-warnings`). |
| `[devops].helm_namespace` | `devops.helm_namespace` | `HELM_NAMESPACE` | `"default"` | Default Kubernetes namespace for Helm releases. |
| `[devops].helm_driver` | `devops.helm_driver` | `HELM_DRIVER` | `"secret"` | Helm release storage backend (`secret`, `configmap`, or `sql`). |
| `[devops].docker_host` | `devops.docker_host` | `DOCKER_HOST` | `""` | Custom Docker daemon socket URI (e.g., `unix:///var/run/docker.sock`). |
| `[devops].github_token` | `devops.github_token` | `GITHUB_TOKEN` | `""` | GitHub Personal Access Token for `gh` CLI and repository operations. |
| `[devops].prometheus_url` | `devops.prometheus_url` | `PROMETHEUS_URL` | `""` | Base URL for Prometheus / Thanos / Mimir metric queries. |
| `[devops].grafana_url` | `devops.grafana_url` | `GRAFANA_URL` | `""` | Base URL for Grafana dashboards and alerts. |
| `[devops].datadog_site` | `devops.datadog_site` | `DD_SITE` | `"datadoghq.com"` | Target Datadog regional site domain (e.g., `datadoghq.com`, `datadoghq.eu`). |

---

### 3. Models, Reasoning Effort & Agent Pool (`[models]`, `[effort]`, `[agent_pool]`)

Controls your default language model, reasoning depth, and the 3-tier model pool used by Smart Mode.

| TOML Key | `/config` Key | Environment Variable | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `[models].default` | `models.default` | `TALKOPS_MODEL` | `""` | Default model loaded at startup in `provider:model` format (e.g., `"anthropic:claude-sonnet-4-5"`). |
| `[models].recent` | `models.recent` | — | `""` | Most recently selected model in the TUI (updated automatically when you switch models). |
| `[effort].default` | — | — | `"medium"` | Default reasoning effort level (`"low"`, `"medium"`, `"high"`, or `"max"`). |
| `[agent_pool].provider` | — | — | `""` | Pool provider scope (`"multi"` for cross-provider pools, or a single provider name). |
| `[agent_pool].fast` | — | — | `""` | Tier 0 (**Fast**) model for quick lookups and status checks in Smart Mode. |
| `[agent_pool].standard` | — | — | `""` | Tier 1 (**Standard**) model for day-to-day infrastructure tasks in Smart Mode. |
| `[agent_pool].powerful` | — | — | `""` | Tier 2 (**Powerful**) model for complex root-cause analysis and multi-step planning in Smart Mode. |

#### Custom OpenAI-Compatible Providers (`[models.providers.<name>]`)

You can also register custom OpenAI-compatible endpoints (such as vLLM, LM Studio, or an internal enterprise LLM gateway) directly in `config.toml`:

```toml
[models.providers.internal_gateway]
class_path = "langchain_openai:ChatOpenAI"
base_url = "https://llm-gateway.internal.company.com/v1"
api_key_env = "INTERNAL_LLM_API_KEY"
models = ["llama-3.3-70b-instruct", "qwen-2.5-coder-32b"]

[models.providers.internal_gateway.params]
temperature = 0.1
```

Once defined, you can select your custom model with `/model internal_gateway:llama-3.3-70b-instruct`.

---

### 4. Security, Sandboxes & Code Interpreter (`[security]`, `[sandboxes]`, `[interpreter]`, `[permissions]`)

Controls tool approval behavior, sandbox isolation, and the embedded Python interpreter.

| TOML Key | `/config` Key | Environment Variable | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `[security].approval_mode` | `security.approval_mode` | `TALKOPS_APPROVAL_MODE` | `"manual"` | Tool approval policy: `"manual"`, `"smart"`, `"auto"`, or `"yolo"`. See **[Approval Modes](./approval-mode.md)**. |
| — | `shell.allow_list` | `SHELL_ALLOW_LIST` | `"recommended"` | Comma-separated list of pre-approved shell commands (or `"recommended"`). |
| `[sandboxes].default` | `sandboxes.default` | `SANDBOX_TYPE` | `"local"` | Default execution sandbox: `"local"`, `"daytona"`, `"modal"`, or `"runloop"`. |
| `[sandboxes].sync_workspace` | `sandboxes.sync_workspace` | `SANDBOX_SYNC_WORKSPACE` | `true` | Automatically sync your workspace directory when launching a remote cloud sandbox. |
| `[interpreter].enable_interpreter` | `interpreter.enable_interpreter` | `ENABLE_INTERPRETER` | `true` | Enable the embedded sandboxed Python interpreter tool (`run_python`). |
| `[interpreter].timeout_seconds` | `interpreter.timeout_seconds` | `INTERPRETER_TIMEOUT` | `30` | Maximum execution time (in seconds) per Python interpreter run. |
| `[interpreter].memory_limit_mb` | `interpreter.memory_limit_mb` | `INTERPRETER_MEMORY_LIMIT` | `64` | Maximum memory allocation (in MB) for the embedded interpreter. |
| `[interpreter].ptc` | `interpreter.ptc` | `INTERPRETER_PTC` | `"safe"` | Programmatic tool calling mode inside Python scripts: `"off"`, `"safe"` (read-only tools), or `"all"`. |

#### Persistent Permission Rules (`[permissions]`)

When you save permanent **Allow**, **Ask**, or **Deny** rules using `/permissions` or by pressing `A` (**Always Allow**) on an approval card, TalkOps stores them under `[permissions]`:

```toml
[permissions]
mode = "manual"
allow = [
  "Bash(kubectl get *)",
  "Bash(terraform plan*)",
]
ask = [
  "Bash(terraform apply*)",
]
deny = [
  "Bash(kubectl delete namespace production*)",
]
```

---

### 5. Startup, UI Display & Webhooks (`[startup]`, `[ui]`, `[webhooks]`)

Controls TUI appearance, startup behavior, and webhook signature verification.

| TOML Key | `/config` Key | Environment Variable | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `[startup].read_project_dotenv` | `startup.read_project_dotenv` | `READ_PROJECT_DOTENV` | `true` | Automatically load variables from a `.env` file in your current working directory on startup. |
| `[ui].theme` | `ui.theme` | `TALKOPS_UI_THEME` | `"talkops-dark"` | Active TUI color theme. |
| `[ui].show_timestamps` | `ui.show_timestamps` | — | `true` | Show timestamps next to chat messages in the TUI. |
| `[ui].auto_scroll` | `ui.auto_scroll` | — | `true` | Automatically scroll the chat view as new output streams in. |
| `[ui].show_turn_duration` | `ui.show_turn_duration` | — | `true` | Show elapsed execution time at the end of each agent turn. |
| `[ui].notifications_enabled` | `ui.notifications_enabled` | — | `true` | Send a desktop notification when a background task finishes or a tool awaits your approval. |
| `[ui].verbose_output` | `ui.verbose_output` | — | `false` | Expand full tool call arguments and output payloads by default. |
| `[ui].auto_compact` | `ui.auto_compact` | — | `false` | Automatically summarize and compact conversation history when approaching the model's context limit. |
| `[webhooks].github_secret` | `webhooks.github_secret` | `GITHUB_WEBHOOK_SECRET` | `""` | HMAC secret (`X-Hub-Signature-256`) for verifying GitHub webhook payloads. |
| `[webhooks].pagerduty_secret` | `webhooks.pagerduty_secret` | `PAGERDUTY_WEBHOOK_SECRET` | `""` | Secret (`X-PagerDuty-Signature`) for verifying PagerDuty V3 webhooks. |
| `[webhooks].alertmanager_secret` | `webhooks.alertmanager_secret` | `ALERTMANAGER_WEBHOOK_SECRET` | `""` | Bearer token or secret for authenticating Prometheus Alertmanager webhooks. |
| `[webhooks].slack_signing_secret` | `webhooks.slack_signing_secret` | `SLACK_SIGNING_SECRET` | `""` | Slack Signing Secret (`X-Slack-Signature`) for verifying Slack events and slash commands. |

# Configuration Overview

TalkOps is designed to work out of the box with sensible defaults while giving you full control over your models, cloud targets, approval rules, and DevOps tools. You can configure TalkOps interactively inside the terminal UI (TUI), through environment variables, or in a persistent `~/.talkops/config.toml` file.

---

## Configuration Priority

When the same setting is defined in more than one place, TalkOps resolves it in the following order (from highest priority to lowest):

1. **CLI Flags & Live Slash Commands (Highest Priority)**
   Flags passed when launching `talkops` (such as `talkops -M openai:gpt-5.2`) or slash commands run during a live TUI session (such as `/model`, `/approval`, `/cloud`, or `/config set`).
2. **Environment Variables & `.env` Files**
   Variables exported in your shell (`export AWS_REGION=us-west-2`) or loaded from `.env` files.
3. **TOML Configuration (`~/.talkops/config.toml`)**
   Your persistent user configuration file on disk.
4. **Built-in Defaults (Lowest Priority)**
   Safe fallback values shipped with TalkOps (such as `manual` approval mode and local sandbox execution).

---

## Managing Settings in the TUI (`/config`)

You don't need to edit configuration files by hand to customize TalkOps. Inside an interactive session, use the `/config` command to inspect or update settings on the fly:

| Command | Description |
| :--- | :--- |
| `/config` *(or `/config show`)* | Opens the interactive **Configuration Manager** modal, where you can browse settings by category, edit values, and see whether each value comes from your environment, `config.toml`, or built-in defaults. |
| `/config set <key> <value>` | Updates a setting immediately and saves it (for example, `/config set security.approval_mode smart` or `/config set aws.region us-west-2`). |
| `/config reset <key>` | Removes a custom override and restores the setting to its built-in default (for example, `/config reset aws.region`). |
| `/config path` | Displays the exact file paths for your active `config.toml`, `.env` file, and local state database. |
| `/reload` | Hot-reloads changes made to `~/.talkops/config.toml` or `.env` files on disk without restarting your session. |

### Dedicated Shortcut Commands

For the settings you change most often, TalkOps also provides dedicated slash commands with interactive modals:

- **`/model`** — Switch your active LLM or save a default model.
- **`/effort`** — Adjust model reasoning effort (`low`, `medium`, `high`, `max`).
- **`/pool`** — Configure the **Fast**, **Standard**, and **Powerful** model tiers used in Smart Mode.
- **`/approval`** — Switch between `manual`, `smart`, `auto`, and `yolo` approval modes.
- **`/permissions`** — Manage permanent **Allow**, **Ask**, and **Deny** rules for tools and shell commands.
- **`/cloud`** *(plus `/aws`, `/azure`, `/gcp`, `/k8s`)* — Switch active cloud profiles, regions, Kubernetes contexts, and execution modes (`read-only` vs. `elevated`).
- **`/auth`** *(or `/login`)* — Manage API keys for model providers, TypeSafe AI, and the TalkOps Control Plane.

---

## Configuration Categories at a Glance

All settings in the `/config` manager and `~/.talkops/config.toml` are organized into logical groups:

| Category | Key Prefix | What It Controls |
| :--- | :--- | :--- |
| **Credentials** | `credentials.*` | API keys for LLM providers (`anthropic`, `openai`, `google`, etc.) and `TYPESAFE_API_KEY`. |
| **Cloud Context** | `cloud.*` | Active cloud provider (`aws`, `azure`, `gcp`, `k8s`), target profile, and execution mode (`elevated` or `read_only`). |
| **AWS** | `aws.*` | Default AWS profile (`AWS_PROFILE`), region (`AWS_REGION`), and optional IAM role ARN (`AWS_ROLE_ARN`). |
| **Azure** | `azure.*` | Subscription ID, Tenant ID, default location, resource group, and service principal Client ID. |
| **Google Cloud** | `gcp.*` | Project ID (`GOOGLE_CLOUD_PROJECT`), active `gcloud` configuration, region, and service account key file. |
| **Kubernetes** | `k8s.*` | Active kubeconfig context (`KUBE_CONTEXT`), default namespace (`KUBE_NAMESPACE`), and `KUBECONFIG` path. |
| **DevOps Tools** | `devops.*` | Terraform workspace & CLI args, Helm namespace & storage driver, Docker host, GitHub token, and observability URLs (Prometheus, Grafana, Datadog). |
| **Models & Pool** | `models.*` | Default model (`provider:model`), recent model, reasoning effort (`[effort]`), and 3-tier model pool (`[agent_pool]`). |
| **Security** | `security.*` | Tool approval mode (`manual`, `smart`, `auto`, `yolo`) and shell command allow-lists. |
| **Sandboxes** | `sandboxes.*` | Default execution sandbox (`local`, `daytona`, `modal`, `runloop`) and workspace syncing. |
| **Interpreter** | `interpreter.*` | Embedded Python interpreter limits (timeout, memory limit, and programmatic tool calling mode). |
| **Startup** | `startup.*` | Whether to automatically load a `.env` file from your current working directory (`read_project_dotenv`). |
| **Display & UI** | `ui.*` | TUI theme, message timestamps, turn duration display, auto-scroll, desktop notifications, and auto-compaction. |
| **Webhooks** | `webhooks.*` | Signing secrets for incoming GitHub, PagerDuty, Alertmanager, and Slack webhooks. |
| **Tracing** | `tracing.*` | Optional LangSmith tracing (`LANGSMITH_TRACING_V2`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`). |

For a complete list of every TOML key, environment variable, and default value, see the **[config.toml Reference](./config.toml.md)**.

---

## Using `.env` Files

In addition to `~/.talkops/config.toml`, TalkOps automatically loads environment variables from `.env` files on startup in the following order (higher entries win):

1. **Exported Shell Variables** (`export ANTHROPIC_API_KEY=...` in your terminal)
2. **Project `.talkops/.env`** (`<current-directory>/.talkops/.env`)
3. **Global `~/.talkops/.env`** (automatically kept in sync when you save keys via `/auth` or `talkops auth set`)
4. **Project `.env`** (`<current-directory>/.env`, enabled when `startup.read_project_dotenv = true`)

> [!TIP]
> If you work on repos that have their own application `.env` files and you don't want TalkOps to read them automatically, run `/config set startup.read_project_dotenv false` or set `READ_PROJECT_DOTENV=false`.

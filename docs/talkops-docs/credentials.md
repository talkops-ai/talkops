# Credentials & Authentication

TalkOps manages four distinct classes of credentials across local TUI sessions, headless CI/CD pipelines, and centralized `talkops serve` deployments:
1. **LLM Provider & TypeSafe AI Jev Keys** (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `TYPESAFE_API_KEY`, etc.)
2. **Central Server & Control Plane JWT Authentication** (`talkops auth login`, `POST /api/v1/auth/token`, JWKS, and OIDC discovery)
3. **Multi-Cloud & Kubernetes Identities** (AWS IAM/SSO, Azure Service Principals, GCP ADC, `kubeconfig`)
4. **Webhook Verification Secrets & Outbound Write-Back Tokens** (PagerDuty, ServiceNow, GitHub, GitLab, Jenkins, Slack)

> [!NOTE]
> 📸 **Image Placeholder — `docs/assets/auth-status-and-login-modal.png`**
> **What to capture:** Screenshot of `talkops auth status` in the terminal (showing an active Central Server JWT session, expiration TTL, offline grace window, and configured LLM/Cloud credentials) alongside the TUI `/login` modal.

---

## 1. Managing LLM & Jev API keys (`talkops auth`)

Use `talkops auth set` to store API keys in `~/.talkops/.state/auth.json` (`0600` file permissions) and the OS Keyring, or pass them via environment variables.

```bash
# Store LLM provider keys and TypeSafe AI Jev key
talkops auth set anthropic sk-ant-...
talkops auth set openai sk-proj-...
talkops auth set google_genai AIza...
talkops auth set typesafe ts-...

# List all stored and environment-detected credentials (redacted)
talkops auth list

# Remove a stored credential
talkops auth remove openai
```

| Provider ID (`talkops auth set <id>`) | Environment Variable | Purpose |
| :--- | :--- | :--- |
| `anthropic` | `ANTHROPIC_API_KEY` | Anthropic Claude models (`claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5`) |
| `openai` | `OPENAI_API_KEY` | OpenAI models (`gpt-5.4`, `o3-pro`, `o3`, `gpt-5.4-mini`) |
| `google_genai` *(alias `google`)* | `GOOGLE_API_KEY` / `GEMINI_API_KEY` | Google Gemini models (`gemini-3.1-pro`, `gemini-3-flash`, `gemini-3.1-flash-lite`) |
| `typesafe` | `TYPESAFE_API_KEY` | **TypeSafe AI Jev** (`<70ms` Smart Mode 3-tier model routing & `<100ms` System One tool safety) |
| `openrouter` | `OPENROUTER_API_KEY` | OpenRouter unified model gateway |
| `deepseek`, `mistralai` *(alias `mistral`)*, `groq`, `together`, `fireworks`, `cohere`, `perplexity`, `xai`, `cerebras`, `sambanova`, `nvidia`, `huggingface`, `ibm` *(alias `watsonx`)*, `baseten`, `litellm` | `<PROVIDER>_API_KEY` (`PPLX_API_KEY`, `HUGGINGFACEHUB_API_TOKEN`, `WATSONX_APIKEY`, etc.) | Additional model providers |
| `langsmith` | `LANGSMITH_API_KEY` | LangSmith distributed tracing (`/trace`) |

---

## 2. Central Server & Control Plane JWT authentication

When connecting to a remote **Central Server** (`talkops serve` with `--auth-mode token`), TalkOps uses a **1-step API-key-to-JWT token exchange** (`src/talkops/state/remote_session.py` & `src/talkops/server/auth.py`).

### Client-side login & token commands

```bash
# 1. Log in once: exchanges your long-lived API key (oc_live_...) for a signed JWT (eyJ...)
talkops auth login \
  --server-url https://talkops.internal:2024 \
  --api-key oc_live_9a8b7c6d5e4f... \
  --user-id alice@company.com

# 2. Check your active JWT session, TTL, and 7-day offline grace status
talkops auth status

# 3. Print your active bearer JWT (or force an immediate refresh)
talkops auth token
talkops auth token --refresh

# 4. Log out and clear ~/.talkops/.state/auth.json
talkops auth logout
```

### Server-side verification & OIDC workload federation

On the server (`talkops serve`), you can configure three verification tiers:
- **Control Plane JWKS / Static Public Key (`<1ms` local verification)**: Set `TALKOPS_CONTROL_PLANE_JWKS_URL`, `TALKOPS_JWT_PUBLIC_KEY` (`RS256`/`ES256`), or `TALKOPS_JWT_SECRET` (`HS256`).
- **Control Plane Token Introspection**: Set `TALKOPS_CONTROL_PLANE_URL` to validate opaque `oc_live_...` tokens via `/v1/auth/introspect` with a 300-second in-memory cache (`TALKOPS_AUTH_CACHE_TTL`).
- **Static Multi-User API Keys**: Set `TALKOPS_SERVER_API_KEYS="alice:oc_key_1,bob:oc_key_2"` (or single-key `TALKOPS_SERVER_API_KEY`).
- **OIDC Discovery Endpoints**: The server exposes `GET /.well-known/openid-configuration` and `GET /.well-known/jwks.json` so AWS IAM (`AssumeRoleWithWebIdentity`), GCP Workload Identity Federation, and Azure Workload Identity can trust tokens issued by your TalkOps Control Plane.

---

## 3. Multi-cloud & Kubernetes credentials

TalkOps never requires you to copy static cloud credentials into plain-text files—it integrates directly with native cloud SDK and CLI credential chains (`boto3`, `aws`, `az`, `gcloud`, `kubectl`).

### AWS

```bash
# Persist your default AWS CLI profile and region
talkops auth aws --profile prod-admin --region us-east-1
```

Or switch profiles live inside the TUI using **`/cloud prod-admin us-west-2`** (alias: `/aws`). TalkOps honors `~/.aws/credentials`, `~/.aws/config`, `aws sso login`, IAM instance/task roles, and `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`.

### Microsoft Azure

TalkOps honors active `az login` sessions as well as Service Principal environment variables:
- `AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_RESOURCE_GROUP`, `AZURE_LOCATION`.

### Google Cloud Platform (GCP)

TalkOps honors `gcloud auth application-default login` as well as Service Account key files:
- `GCP_PROJECT_ID`, `GCP_REGION`, `GCP_ZONE`, `GOOGLE_APPLICATION_CREDENTIALS`.

### Kubernetes

TalkOps reads `KUBECONFIG` (default `~/.kube/config`), `KUBE_CONTEXT`, and `KUBE_NAMESPACE` (or `[k8s]` in `config.toml`), and automatically uses in-cluster service account tokens when `talkops serve` runs inside a Kubernetes pod.

---

## 4. Webhook verification secrets & write-back credentials

When running `talkops serve` / `talkops webhooks`, configure these environment variables (or `[webhooks]` in `config.toml`) to verify inbound webhook signatures and authenticate outbound RCA write-backs:

| Variable | Direction | Used By |
| :--- | :--- | :--- |
| `TALKOPS_WEBHOOK_SECRET` | Inbound | Default shared secret / bearer token for Alertmanager, Datadog, SNS, CloudEvents, ServiceNow, and Generic webhooks. |
| `TALKOPS_WEBHOOK_PAGERDUTY_SECRET` | Inbound | Verifies `X-PagerDuty-Signature` (`v1=...` HMAC-SHA256). |
| `TALKOPS_WEBHOOK_GITHUB_SECRET` | Inbound | Verifies `X-Hub-Signature-256` (`sha256=...` HMAC-SHA256). |
| `TALKOPS_WEBHOOK_GITLAB_TOKEN` | Inbound | Verifies `X-Gitlab-Token` header. |
| `TALKOPS_WEBHOOK_JENKINS_TOKEN` | Inbound | Verifies Jenkins bearer/header token. |
| `TALKOPS_WEBHOOK_SLACK_URL` | Outbound Sink | Slack Incoming Webhook URL for posting severity-badged RCA reports. |
| `TALKOPS_WEBHOOK_PAGERDUTY_API_KEY` + `TALKOPS_WEBHOOK_PAGERDUTY_FROM_EMAIL` | Outbound Sink | PagerDuty REST API token and requester email for posting RCA Incident Notes. |
| `SERVICENOW_TOKEN` *(or `SERVICENOW_USER` + `SERVICENOW_PASSWORD`)* | Outbound Sink | ServiceNow credentials for updating Incident `work_notes`. |
| `TALKOPS_WEBHOOK_GITHUB_TOKEN` *(or `GITHUB_TOKEN`)* | Outbound Sink | GitHub token for posting automated RCA comments on PRs and commits. |

---

## Next steps

- **[Central Server & Multi-Client Architecture](central-server.md)** — Deploy `talkops serve` with multi-tier JWT authentication
- **[Webhooks & Event-Driven Automation](webhooks.md)** — Configure webhook providers and outbound RCA write-backs
- **[Approval Modes & Jev Smart Mode](approval-mode.md)** — Configure `TYPESAFE_API_KEY` for `<70ms` routing and `<100ms` tool safety

# Server Addons & Marketplace

> Extend `talkops serve` with multi-channel ChatOps (Slack, Microsoft Teams, Discord), autonomous webhook receivers, custom authentication providers, and organization-specific integrations.

When you run TalkOps in Server Mode (`talkops serve`), every HTTP endpoint, authentication provider, webhook listener, background worker, and ChatOps integration runs as a modular **Server Addon** managed by the TalkOps **Addon Microkernel** ([`src/talkops/addons/`](../../src/talkops/addons/__init__.py)).

TalkOps combines **three built-in default addons**—so remote CLI/TUI sessions, team authentication, and 9+ alert webhooks work out of the box with zero configuration—with a **Central Addon Marketplace** (`talkops-ai/talkops-addons`) and a pluggable runtime loader for custom or private organization addons.

<table>
  <tr>
    <td width="50%" valign="top">
      <h3>Ready Out of the Box</h3>
      <p>Ships with <b><code>core-auth</code></b>, <b><code>client-gateway</code></b>, and <b><code>webhooks</code></b> enabled by default so <code>talkops serve</code> works immediately with zero configuration.</p>
    </td>
    <td width="50%" valign="top">
      <h3>One-Command Marketplace Install</h3>
      <p>Install official or custom organization addons (such as <b><code>channels</code></b> for Slack, Microsoft Teams, and Discord) with <code>talkops addon install &lt;name&gt;</code>—including automatic Python dependency installation.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <h3>Alert-to-Chat Thread Binding</h3>
      <p>Connect built-in <b><code>webhooks</code></b> to <b><code>channels</code></b> so PagerDuty, Alertmanager, Datadog, or AWS alerts post an live incident card in Slack/Teams/Discord, reply with the full RCA, and let engineers continue investigating in the same thread.</p>
    </td>
    <td width="50%" valign="top">
      <h3>Separate Ports &amp; Zero Loopback</h3>
      <p>Mount public-facing addons on dedicated firewall ports (for example, <code>--webhook-port 8080</code>) while executing agent runs directly against the in-memory LangGraph runtime.</p>
    </td>
  </tr>
</table>

---

## How Server Addons Work

```mermaid
flowchart TB
    subgraph Sources["1. Addon Sources"]
        B["Default Built-in Addons\n• core-auth\n• client-gateway\n• webhooks"]
        MP["Addon Marketplace (.talkops/marketplace.json)\n• channels (Slack, Teams, Discord)\n• webhooks (Standalone package)\n• Custom Org Addons"]
        EXT["Local & External Addons\n• .talkops/addons.json (package / path)\n• Python entry_points ('talkops.addons')\n• .talkops/addons/*.py & --custom-addon"]
    end

    subgraph Runtime["2. TalkOps Server Microkernel (talkops serve)"]
        AM["AddonManager\n• 4-Tier Config & .talkops/.env Secret Resolution\n• Auto-Installs Missing pythonDependencies\n• Dependency DAG Sort & Multi-Port uvicorn Listeners"]
        GW["AgentRunGateway (In-Memory LangGraph)\n• submit_run() / trigger_run() / stream_run()\n• resume_run() (Interactive HITL Approvals)\n• Deterministic UUIDv5 Thread Mapping"]
        BUS["HookEventBus (Async Pub/Sub)\n• webhook.received / webhook.completed\n• addon.run.started / interrupted / completed"]
    end

    Sources --> AM
    AM <--> GW
    GW <--> BUS
```

---

## 1. Default Built-In Addons

When you run `talkops serve` with no extra flags, TalkOps automatically loads and enables three built-in Open-Source (`oss`) addons from [`src/talkops/addons/builtin/`](../../src/talkops/addons/builtin/__init__.py):

| Addon Name | Class | Enabled by Default | Mounted HTTP Endpoints | What It Does |
| :--- | :--- | :--- | :--- | :--- |
| **`core-auth`** | [`CoreAuthAddon`](../../src/talkops/addons/builtin/core_auth.py) | **Yes** | `POST /api/v1/auth/token`<br>`GET /.well-known/openid-configuration`<br>`GET /.well-known/jwks.json` | Verifies static API keys (`TALKOPS_SERVER_API_KEYS`), exchanges API keys for short-lived signed JWTs, serves OIDC/JWKS discovery metadata, and enforces per-user thread owner isolation. |
| **`client-gateway`** | [`ClientGatewayAddon`](../../src/talkops/addons/builtin/client_gateway.py) | **Yes** *(depends on `core-auth`)* | `GET /api/v1/server/info`<br>`GET /api/v1/threads`<br>`GET /api/v1/threads/recent`<br>`GET /api/v1/threads/similar`<br>`GET /api/v1/threads/{thread_id}`<br>`PATCH /api/v1/threads/{thread_id}/title`<br>`POST /api/v1/threads/{thread_id}/fork`<br>`POST /api/v1/threads/{thread_id}/prune`<br>`DELETE /api/v1/threads/{thread_id}`<br>`GET /api/v1/threads/{thread_id}/tasks`<br>`GET /api/v1/threads/{thread_id}/tasks/stream`<br>`GET /api/v1/threads/{thread_id}/tasks/{task_id}/logs`<br>`POST /api/v1/threads/{thread_id}/tasks/{task_id}/action` | Powers remote `talkops --server-url ...` terminal sessions, multi-user conversation history, thread forking/pruning, and real-time Server-Sent Events (SSE) streaming for background shell tasks. |
| **`webhooks`** | [`WebhookAddon`](../../src/talkops/addons/builtin/webhooks.py) | **Yes** | `GET /webhooks`<br>`GET /webhooks/health`<br>`GET /webhooks/deliveries`<br>`POST /webhooks/{provider}` | Ingests alerts from **PagerDuty v3**, **Prometheus Alertmanager / Grafana**, **AWS SNS / CloudWatch / EventBridge**, **CNCF CloudEvents**, **Jenkins**, **GitHub**, **Datadog**, **ServiceNow**, and **Azure Monitor / Generic JSON**. Verifies HMAC signatures, deduplicates alert storms, runs autonomous Root Cause Analysis (RCA), and publishes `webhook.received` and `webhook.completed` events. |

> [!NOTE]
> In addition to the routes contributed by each addon, `AddonManager` always exposes `GET /api/v1/addons` on the main server port so operators and monitoring probes can inspect the active profile, enabled addons, and live health checks.

### Server Profiles (`--profile` / `TALKOPS_SERVER_PROFILE`)

You can switch which built-in addons start by default using a **Server Profile**:

| Profile | CLI / Env Usage | Active Default Addons | When to Use It |
| :--- | :--- | :--- | :--- |
| **`default`** | `talkops serve` *(or `--profile default`)* | `core-auth`, `client-gateway`, `webhooks` | Standard all-in-one team server and webhook receiver. |
| **`api-only`** | `talkops serve --profile api-only` | `core-auth`, `client-gateway` | Internal team server for remote CLI/TUI users when external webhooks are not needed. |
| **`webhook-worker`** | `talkops serve --profile webhook-worker` | `webhooks` | Dedicated autonomous webhook ingestion worker pods behind an external load balancer. |
| **`enterprise-full`** | `talkops serve --profile enterprise-full` | `core-auth`, `client-gateway`, `webhooks` | Base profile for layering enterprise or marketplace addons in `.talkops/addons.json`. |

You can also define your own custom profiles in `.talkops/addons.json` (see [Step 5: Custom Server Profiles](#step-5-defining-custom-server-profiles-and-dedicated-ports)).

---

## 2. Official TalkOps Addon Marketplace (`talkops-addons`)

Beyond the built-in defaults, TalkOps connects to the official **TalkOps Addon Marketplace** (`talkops-ai/talkops-addons`), which follows the unified **`.talkops/`** directory standard:
- **`.talkops/marketplace.json`** at the repository root catalogs all available addons.
- **`addons/<name>/.talkops/addon.json`** inside each addon directory declares its entry point, `pythonDependencies`, capabilities, mounted routes, agent tools, and required environment variables.

### Available Official Marketplace Addons

| Addon | Package & Entry Point | Category | Summary |
| :--- | :--- | :--- | :--- |
| **`channels`** | `talkops-channels`<br>`talkops_channels.addon:ChannelsAddon` | `chatops` | Multi-channel ChatOps hub for **Slack**, **Microsoft Teams**, and **Discord** with interactive HITL tool approval cards, emoji reaction approvals, DM sender pairing, slash commands, live edit-in-place streaming, and automatic thread binding for `webhooks` alerts. |
| **`webhooks`** | `talkops-webhooks`<br>`talkops_webhooks.addon:WebhookAddon` | `observability` | Standalone marketplace distribution of the autonomous webhook & alert ingestion server (`POST /webhooks/{provider}`) for 9+ DevOps, Cloud, and ITSM providers. |

### Inside the `channels` Addon (`talkops-channels`)

Installing and enabling the **`channels`** addon adds full two-way ChatOps to `talkops serve`:

- **Mounted HTTP Routes**:
  - `POST /channels/{provider}/events` — Inbound messages, `@mentions`, thread replies, emoji reactions, and URL verification challenges (`slack`, `teams`, `discord`).
  - `POST /channels/{provider}/interactions` — Interactive button clicks (Slack Block Kit, Microsoft Teams Adaptive Cards v1.4, Discord ActionRow Buttons) and Discord Ed25519 interactions.
  - `POST /channels/{provider}/commands` — Native slash command callbacks (such as `/talkops`).
  - `GET /channels/status` & `GET /channels/health` — Adapter configuration diagnostics, active tasks, and pending HITL approvals.
  - `POST /channels/notify` — Proactive outbound notification and incident thread creation endpoint.
- **Multi-Modal Human-in-the-Loop (HITL) Approvals**:
  - When a tool call requires approval in Smart or Manual mode, `channels` renders an interactive approval card in Slack, Teams, or Discord. Engineers can approve or reject by **clicking Approve / Reject buttons**, **reacting with 👍 / 👎**, or **replying `approve` / `deny <reason>`** in the thread.
- **Built-In Chat Commands**:
  - `/help`, `/new`, `/stop`, `/status`, `/model`, `/mode`, `/pair`, and `/context-doctor`.
- **3-Tier Exposure Policy & DM Sender Pairing**:
  - Configure each chat platform with `self` (operator only), `allowlist` (operator + allowlisted users/channels + paired senders), or `open` (requires explicit `open_ack: true`). Unrecognized DM senders receive a one-time pairing code (e.g. `K7QM-3XRD`) that an operator can approve via `/pair approve K7QM-3XRD`.

---

## 3. Managing Addons from the CLI

TalkOps provides two CLI command groups for working with server addons:
1. **`talkops addon` (or `talkops addons`)** — Browse marketplaces, install addons, and manage user/project/local enablement.
2. **`talkops serve addons`** — Inspect the resolved addon table, active profile, ports, and sources that `talkops serve` will load.

### Browsing, Installing & Enabling Marketplace Addons

```bash
# 1. List available and installed addons across registered marketplaces
talkops addon list

# 2. Search marketplace addons by name, category, or keyword
talkops addon search channels
talkops addon search slack

# 3. Install and enable an addon (defaults to --scope user)
#    Automatically checks and installs missing pythonDependencies from .talkops/addon.json!
talkops addon install channels

#    Or install for a specific repository (--scope project or --scope local)
talkops addon install channels --scope project

# 4. Disable or re-enable an installed addon without deleting its cached files
talkops addon disable channels --scope user
talkops addon enable channels --scope user

# 5. Refresh marketplace catalogs or uninstall an addon
talkops addon refresh
talkops addon uninstall channels
```

> [!TIP]
> **Automatic Python Dependency Resolution:** When you run `talkops addon install <name>` (or when `AddonManager` loads a `.talkops/addon.json` manifest at server startup), TalkOps inspects the addon's `pythonDependencies` array against the active Python interpreter (`sys.executable`). Any missing packages (such as `cryptography` or `PyJWT`) are automatically installed via `pip` or `uv pip`. Pass `--no-deps` to `talkops addon install` if you manage dependencies externally in a locked container image.

### Registering Custom or Organization Marketplaces

Your organization can host a private or forked addon repository containing `.talkops/marketplace.json` at its root:

```bash
# Add the official GitHub marketplace, a custom organization repository, or a local directory
talkops addon marketplace add talkops-ai/talkops-addons
talkops addon marketplace add my-org/talkops-addons
talkops addon marketplace add https://gitlab.internal.org/platform/talkops-addons.git#main
talkops addon marketplace add ./team-addons-marketplace

# List or remove registered addon marketplaces
talkops addon marketplace list
talkops addon marketplace remove talkops-addons
```

You can also override the default addon marketplace globally via environment variables:

```bash
export TALKOPS_ADDON_MARKETPLACE_REPO="my-org/talkops-addons"
export TALKOPS_ADDON_MARKETPLACE_REF="main"
```

### Inspecting Active Server Addons (`talkops serve addons`)

Before starting `talkops serve`, verify which addons are discovered and enabled:

```bash
# Pretty table output showing addon name, status, tier, version, port, and source
talkops serve addons

# Machine-readable JSON output
talkops serve addons --json
```

While `talkops serve` is running, query live addon health over HTTP:

```bash
curl -s http://127.0.0.1:2024/api/v1/addons | jq .
```

---

## 4. Step-by-Step Configuration Guide

TalkOps configures addons through a **4-tier precedence hierarchy** and loads secrets from `.talkops/.env` or environment variables so you never hardcode credentials in JSON files.

### Step 1: Store Secrets in `.talkops/.env`

Create `<project_root>/.talkops/.env` (or `~/.talkops/.env` for user-global secrets) and add your webhook and chat channel credentials:

```dotenv
# ── Built-in Core Auth & Webhook Secrets ─────────────────────────────────
TALKOPS_SERVER_API_KEYS=oc_live_team_key_1:alice,oc_live_team_key_2:bob
TALKOPS_WEBHOOK_SECRET=whsec_shared_default_secret
TALKOPS_WEBHOOK_PAGERDUTY_SECRET=pd_v3_hmac_secret
TALKOPS_WEBHOOK_PAGERDUTY_API_KEY=pd_rest_api_token
TALKOPS_WEBHOOK_PAGERDUTY_FROM_EMAIL=sre-bot@acme.io
TALKOPS_WEBHOOK_GITHUB_SECRET=gh_webhook_hmac_secret
TALKOPS_WEBHOOK_GITHUB_TOKEN=ghp_writeback_token

# ── Marketplace `channels` Addon Secrets (Slack, Teams, Discord) ─────────
TALKOPS_SLACK_BOT_TOKEN=xoxb-1234567890-abcdef
TALKOPS_SLACK_SIGNING_SECRET=9876543210fedcba
TALKOPS_TEAMS_APP_ID=00000000-0000-0000-0000-000000000000
TALKOPS_TEAMS_APP_PASSWORD=teams_bot_client_secret
TALKOPS_DISCORD_BOT_TOKEN=MTIzNDU2Nzg5MDEyMzQ1Njc4OQ.GhIjKl.MnOpQrStUvWxYz
TALKOPS_DISCORD_PUBLIC_KEY=a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90
```

In `.talkops/addons.json`, you can reference any environment variable in three ways:
1. **`${ENV_VAR}` interpolation**: `"bot_token": "${TALKOPS_SLACK_BOT_TOKEN}"`
2. **`env://ENV_VAR` URI**: `"bot_token": "env://TALKOPS_SLACK_BOT_TOKEN"`
3. **`<field>_env` pointer**: `"signing_secret_env": "TALKOPS_SLACK_SIGNING_SECRET"` (populates `signing_secret` automatically)

---

### Step 2: Configure Default Built-In Addons (`core-auth`, `client-gateway`, `webhooks`)

All three default addons work with zero configuration, but you can customize their behavior in `.talkops/addons.json` (or `~/.talkops/addons.json`):

```json
{
  "profile": "default",
  "addons": {
    "core-auth": {
      "enabled": true,
      "default_token_ttl_seconds": 3600
    },
    "client-gateway": {
      "enabled": true,
      "max_threads_page_size": 500,
      "sse_keepalive_seconds": 15.0
    },
    "webhooks": {
      "enabled": true,
      "max_concurrent_runs": 5,
      "dedup_ttl_seconds": 300.0,
      "default_secret": "${TALKOPS_WEBHOOK_SECRET}",
      "auto_confirm_sns": true
    }
  }
}
```

#### Default Addon Settings Reference

| Addon | Setting | Default | Description |
| :--- | :--- | :--- | :--- |
| **`core-auth`** | `default_token_ttl_seconds` | `3600` | Default lifetime (in seconds, up to `86400`) for short-lived JWTs issued by `POST /api/v1/auth/token`. |
| **`client-gateway`** | `max_threads_page_size` | `500` | Maximum number of threads returned by `GET /api/v1/threads` (`1`–`5000`). |
| **`client-gateway`** | `sse_keepalive_seconds` | `15.0` | Keepalive ping interval for `GET /api/v1/threads/{thread_id}/tasks/stream`. |
| **`webhooks`** | `max_concurrent_runs` | `5` | Maximum number of background webhook RCA investigations allowed to run simultaneously. |
| **`webhooks`** | `dedup_ttl_seconds` | `300.0` | Time window (in seconds) used to deduplicate repeated alerts with the same correlation key. |
| **`webhooks`** | `default_secret` | `None` | Fallback shared secret used when a provider-specific secret (`TALKOPS_WEBHOOK_<PROVIDER>_SECRET`) is not set. |
| **`webhooks`** | `auto_confirm_sns` | `True` | Automatically confirm AWS SNS `SubscriptionConfirmation` handshakes (with SSRF hostname verification). |
| **`webhooks`** | `outbound_channels` | `[]` | List of outbound channel routing rules (or shorthand string) for forwarding alerts and RCA reports to `channels`. |
| **All Addons** | `port` | `None` *(main port)* | Optional dedicated TCP port. When set, `AddonManager` binds a separate `uvicorn.Server` on the same event loop. |
| **All Addons** | `mount_prefix` | `""` | Optional URL path prefix prepended to all routes registered by the addon. |

---

### Step 3: Install and Configure the `channels` Addon (Slack, Teams, Discord)

First, install the `channels` addon from the official [`talkops-ai/talkops-addons`](https://github.com/talkops-ai/talkops-addons) marketplace:

```bash
# Install from the default official marketplace (talkops-ai/talkops-addons)
talkops addon install channels

# Or explicitly specify the marketplace name:
talkops addon install channels@talkops-addons
```

Next, configure your Slack, Microsoft Teams, and/or Discord adapters in `.talkops/addons.json`:

```json
{
  "profile": "default",
  "addons": {
    "channels": {
      "enabled": true,
      "config": {
        "verbosity": 2,
        "pairing_enabled": true,
        "default_approval_mode": "smart",
        "edit_debounce_seconds": 1.0,
        "slack": {
          "bot_token": "${TALKOPS_SLACK_BOT_TOKEN}",
          "signing_secret": "${TALKOPS_SLACK_SIGNING_SECRET}",
          "verify_signatures": true,
          "exposure": {
            "mode": "allowlist",
            "operator_id": "U0123ADMIN",
            "allowed_users": ["U0123ADMIN", "U0456SRE"],
            "allowed_chats": ["C0123456789"],
            "require_mention_in_channels": true
          }
        },
        "teams": {
          "app_id": "${TALKOPS_TEAMS_APP_ID}",
          "app_password": "${TALKOPS_TEAMS_APP_PASSWORD}",
          "tenant_id": "botframework.com",
          "verify_signatures": true,
          "exposure": {
            "mode": "allowlist",
            "allowed_users": ["*"]
          }
        },
        "discord": {
          "bot_token": "${TALKOPS_DISCORD_BOT_TOKEN}",
          "application_id": "123456789012345678",
          "public_key": "${TALKOPS_DISCORD_PUBLIC_KEY}",
          "verify_signatures": true,
          "exposure": {
            "mode": "allowlist",
            "allowed_users": ["*"]
          }
        }
      }
    }
  }
}
```

Point your chat platform webhook settings at your `talkops serve` URL:
- **Slack**:
  - Events API Request URL: `https://<your-talkops-host>/channels/slack/events`
  - Interactivity Request URL: `https://<your-talkops-host>/channels/slack/interactions`
  - Slash Commands Request URL: `https://<your-talkops-host>/channels/slack/commands`
- **Microsoft Teams (Azure Bot Messaging Endpoint)**:
  - Messaging Endpoint: `https://<your-talkops-host>/channels/teams/events`
- **Discord (Developer Portal)**:
  - Interactions Endpoint URL: `https://<your-talkops-host>/channels/discord/interactions`

---

### Step 4: Connect `webhooks` Alerts to `channels` for Automated Incident Threads

When the built-in `webhooks` addon receives an alert from PagerDuty, Alertmanager, Datadog, AWS SNS, GitHub, Jenkins, or ServiceNow, it emits `webhook.received` and `webhook.completed` on the server's internal `HookEventBus`.

When `channels` is enabled, it subscribes to those events and performs a **two-stage incident workflow**:
1. **On `webhook.received`**: Posts an initial incident card (`⏳ TalkOps Autonomous Investigation Started`) in your configured Slack, Teams, or Discord channel and binds a new chat thread to the LangGraph investigation thread (`lg_thread_id`).
2. **On `webhook.completed`**: Posts the full **Automated RCA** report inside that chat thread, updates the root incident card in place to **`RCA Complete (in thread)`**, and links the thread so any engineer who replies in Slack/Teams/Discord (`@talkops check the recent rollout`) continues the **exact same LangGraph investigation thread**.

#### Option A: Zero-Config Environment Variable Shorthand

Set `TALKOPS_WEBHOOK_NOTIFY_CHANNELS` to a comma-separated list of `<channel>:<conversation_id>` targets:

```bash
export TALKOPS_WEBHOOK_NOTIFY_CHANNELS="slack:C0123456789,teams:19:ops_alerts@thread.tacv2,discord:112233445566778899"
talkops serve --host 0.0.0.0 --port 2024 --smart
```

#### Option B: Fine-Grained Routing Rules in `.talkops/addons.json`

Use `outbound_channels` under `addons.webhooks` to route specific alert providers and severities to different team channels:

```json
{
  "profile": "default",
  "addons": {
    "webhooks": {
      "enabled": true,
      "outbound_channels": [
        {
          "channel": "slack",
          "conversation_id": "C0123456789",
          "providers": ["pagerduty", "alertmanager", "sns", "datadog"],
          "severities": ["critical", "high", "warning"],
          "notify_on": ["received", "completed"],
          "bind_thread": true
        },
        {
          "channel": "teams",
          "conversation_id": "19:ops_alerts@thread.tacv2",
          "providers": ["*"],
          "severities": ["critical"],
          "notify_on": ["completed"],
          "bind_thread": true
        },
        {
          "channel": "discord",
          "conversation_id": "112233445566778899",
          "providers": ["github", "jenkins"],
          "severities": ["*"],
          "notify_on": ["completed"],
          "bind_thread": true
        }
      ]
    },
    "channels": {
      "enabled": true,
      "config": {
        "slack": {
          "bot_token": "${TALKOPS_SLACK_BOT_TOKEN}",
          "signing_secret": "${TALKOPS_SLACK_SIGNING_SECRET}"
        }
      }
    }
  }
}
```

---

### Step 5: Defining Custom Server Profiles and Dedicated Ports

In production deployments, you often want to keep the team session API (`client-gateway` on port `2024`) on your private VPN while exposing `webhooks` (port `8080`) and `channels` (port `8090`) through an ingress controller or API gateway.

Any addon in `.talkops/addons.json` can specify `"port"` and `"mount_prefix"`, and you can define custom `"profiles"`:

```json
{
  "profiles": {
    "sre-production": ["core-auth", "client-gateway", "webhooks", "channels"]
  },
  "profile": "sre-production",
  "addons": {
    "webhooks": {
      "enabled": true,
      "port": 8080
    },
    "channels": {
      "enabled": true,
      "port": 8090
    }
  }
}
```

Start the server with:

```bash
talkops serve --addons-config ./.talkops/addons.json --smart
```

`AddonManager` automatically spins up secondary `uvicorn.Server` listeners on ports `8080` and `8090` sharing the same in-memory `asyncio` event loop and `AgentRunGateway`.

---

## 5. 4-Tier Configuration Precedence

When `AddonManager` resolves each addon's configuration at startup, higher tiers override lower tiers:

| Tier | Source | Examples |
| :--- | :--- | :--- |
| **Tier 1 (Lowest)** | **Addon Pydantic Defaults** | Default field values on `CoreAuthSettings`, `ClientGatewaySettings`, `WebhookAddonSettings`, `ChannelsAddonSettings`. |
| **Tier 2** | **Server Profile** | `--profile default\|api-only\|webhook-worker` or `TALKOPS_SERVER_PROFILE`. |
| **Tier 3** | **Config Files & Marketplace State** | Merged in order:<br>1. `~/.talkops/addons.json` & `~/.talkops/settings.json`<br>2. `<repo>/.talkops/addons.json`, `<repo>/addons.json`, `<repo>/.talkops/settings.json`, `<repo>/.talkops/settings.local.json`<br>3. `--addons-config <path>` (`TALKOPS_ADDONS_CONFIG`)<br>4. Enabled marketplace addons (`~/.talkops/state/enabled_addons.json`). |
| **Tier 4 (Highest)** | **Environment Variables & CLI Flags** | • `TALKOPS_ADDON_<NAME>_<FIELD>=<value>` (e.g., `TALKOPS_ADDON_WEBHOOKS_MAX_CONCURRENT_RUNS=10`)<br>• `--webhook-port <port>` (`TALKOPS_WEBHOOK_PORT`)<br>• `--addons a,b` (`TALKOPS_ADDONS=a,b`)<br>• `--disable-addons c` (`TALKOPS_DISABLE_ADDONS=c`) |

---

## 6. Writing Custom Addons

You can write custom Server Addons in a standalone Git repository (like `talkops-addons`), package them with `.talkops/addon.json` and `.talkops/marketplace.json`, or load a single `.py` file directly via `.talkops/addons/*.py` or `--custom-addon`.

For a complete, step-by-step developer walkthrough—including the `BaseServerAddon` lifecycle protocol, `AgentRunGateway` and `HookEventBus` APIs, and real-world code examples from `talkops-addons`—see the **[Server Addon Developer Guide](developer-guide/server_addon_developer_guide.md)**.

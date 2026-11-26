# Server Addons & Marketplace

> Extend `talkops serve` with multi-channel ChatOps (Slack, Microsoft Teams, Discord), autonomous webhook receivers, custom authentication providers, and organization-specific integrations.

When you run TalkOps in Server Mode (`talkops serve`), every HTTP endpoint, authentication provider, webhook listener, background worker, and ChatOps integration runs as a modular **Server Addon** managed by the TalkOps **Addon Microkernel** ([`src/talkops/addons/`](../../src/talkops/addons/__init__.py)).

TalkOps combines **built-in default addons**—so remote CLI/TUI sessions, team authentication, 9+ alert webhooks, and multi-channel ChatOps (Slack, Microsoft Teams, Discord) work out of the box—with a **Central Addon Marketplace** (`talkops-ai/talkops-addons`) and a pluggable runtime loader for custom or private organization addons.

<table>
  <tr>
    <td width="50%" valign="top">
      <h3>Ready Out of the Box</h3>
      <p>Ships with <b><code>core-auth</code></b>, <b><code>client-gateway</code></b>, <b><code>webhooks</code></b>, and <b><code>channels</code></b> enabled by default (plus <b><code>context-graph</code></b> and <b><code>intelligence</code></b> in the <code>sre</code> profile).</p>
    </td>
    <td width="50%" valign="top">
      <h3>Declarative <code>addons.json</code> or Zero-File Env Config</h3>
      <p>Configure addons via Git-committable <code>.talkops/addons.json</code> with <code>${ENV_VAR}</code> secret indirection, embed under <code>settings.json</code>, or configure purely with environment variables (<code>TALKOPS_ADDON_*</code>).</p>
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
        B["<b>Default Built-in Addons</b><br/>• core-auth (JWT &amp; SSO)<br/>• client-gateway (SSE &amp; Threads)<br/>• webhooks (9+ Alert Providers)"]
        MP["<b>Addon Marketplace</b><br/>• .talkops/marketplace.json<br/>• channels (Slack, Teams, Discord)<br/>• webhooks &amp; Custom Org Addons"]
        EXT["<b>Local &amp; External Addons</b><br/>• .talkops/addons.json (package / path)<br/>• Python entry_points ('talkops.addons')<br/>• .talkops/addons/*.py &amp; --custom-addon"]
    end

    subgraph Runtime["2. TalkOps Server Microkernel (talkops serve)"]
        AM["<b>AddonManager</b><br/>• 4-Tier Config &amp; .talkops/.env Secret Resolution<br/>• Auto-Installs Missing pythonDependencies<br/>• Dependency DAG Sort &amp; Multi-Port uvicorn Listeners"]
        GW["<b>AgentRunGateway (In-Memory LangGraph)</b><br/>• submit_run() / trigger_run() / stream_run()<br/>• resume_run() (Interactive HITL Approvals)<br/>• Deterministic UUIDv5 Thread Mapping"]
        BUS["<b>HookEventBus (Async Pub/Sub)</b><br/>• webhook.received / webhook.completed<br/>• addon.run.started / interrupted / completed"]
    end

    B & MP & EXT --> AM
    AM <--> GW
    GW <--> BUS
```

---

## 1. Default Built-In Addons

When you run `talkops serve` with no extra flags, TalkOps automatically loads its built-in Open-Source (`oss`) addons from [`src/talkops/addons/builtin/`](../../src/talkops/addons/builtin/__init__.py):

| Addon Name | Class | Enabled in `default` | Mounted HTTP Endpoints | What It Does |
| :--- | :--- | :--- | :--- | :--- |
| **`core-auth`** | [`CoreAuthAddon`](../../src/talkops/addons/builtin/core_auth.py) | **Yes** | `POST /api/v1/auth/token`<br>`GET /.well-known/openid-configuration`<br>`GET /.well-known/jwks.json` | Verifies static API keys (`TALKOPS_SERVER_API_KEYS`), exchanges API keys for short-lived signed JWTs, serves OIDC/JWKS discovery metadata, and enforces per-user thread owner isolation. |
| **`client-gateway`** | [`ClientGatewayAddon`](../../src/talkops/addons/builtin/client_gateway.py) | **Yes** *(depends on `core-auth`)* | `GET /api/v1/server/info`<br>`GET /api/v1/threads` (`recent`, `similar`, `{thread_id}`)<br>`POST /api/v1/threads/{thread_id}/{fork,prune}` & `PATCH / DELETE`<br>`GET / POST /api/v1/threads/{thread_id}/tasks` (`stream`, `logs`, `action`) | Powers remote `talkops --server-url ...` terminal sessions, multi-user conversation history, thread forking/pruning, and real-time Server-Sent Events (SSE) streaming for background shell tasks. |
| **`webhooks`** | [`WebhookAddon`](../../src/talkops/addons/builtin/webhooks.py) | **Yes** | `GET /webhooks`<br>`GET /webhooks/health`<br>`GET /webhooks/deliveries`<br>`POST /webhooks/{provider}` | Ingests alerts from **PagerDuty v3**, **Prometheus Alertmanager / Grafana**, **AWS SNS / CloudWatch / EventBridge**, **CNCF CloudEvents**, **Jenkins**, **GitHub**, **Datadog**, **ServiceNow**, and **Azure Monitor / Generic JSON**. Verifies HMAC signatures, deduplicates alert storms, runs autonomous Root Cause Analysis (RCA), and publishes `webhook.received` and `webhook.completed` events. |
| **`channels`** | [`ChannelsAddon`](../../src/talkops/addons/builtin/channels.py) | **Yes** | `POST /channels/{provider}/events`<br>`POST /channels/{provider}/interactions`<br>`POST /channels/{provider}/commands`<br>`POST /channels/{provider}/sync-commands`<br>`GET /channels/status` & `GET /channels/health`<br>`POST /channels/notify` | Multi-channel ChatOps hub for **Slack**, **Microsoft Teams**, and **Discord** (including Discord Gateway v10 WebSocket + HTTP Interactions), interactive HITL tool approval cards, emoji reaction approvals, DM pairing, slash commands, and automated incident thread binding. |
| **`context-graph`** | [`ContextGraphAddon`](../../src/talkops/addons/builtin/context_graph.py) | In `webhook-worker`, `sre`, `enterprise-full` | Context graph API & incident correlation workers | Persists incident/entity context graphs across investigations. |
| **`intelligence`** | [`IntelligenceAddon`](../../src/talkops/addons/builtin/intelligence.py) | In `sre`, `enterprise-full` | `GET / POST /api/v1/intelligence/autoqa`<br>`POST /api/v1/intelligence/autoqa/dispute`<br>`GET /api/v1/intelligence/kpis` | Runs 3-Stage Stratified AutoQA, exposes split SRE KPIs, and subscribes to ChatOps RCA feedback events (`feedback.helped` / `feedback.rejected`) to persist operator confirmations, disputes, and reflection memory rules. |

> [!NOTE]
> In addition to the routes contributed by each addon, `AddonManager` always exposes `GET /api/v1/addons` on the main server port so operators and monitoring probes can inspect the active profile, enabled addons, and live health checks.

### Server Profiles (`--profile` / `TALKOPS_SERVER_PROFILE`)

You can switch which built-in addons start by default using a **Server Profile**:

| Profile | CLI / Env Usage | Active Default Addons | When to Use It |
| :--- | :--- | :--- | :--- |
| **`default`** | `talkops serve` *(or `--profile default`)* | `client-gateway`, `core-auth`, `webhooks`, `channels` | Standard all-in-one team server, webhook receiver, and ChatOps hub. |
| **`api-only`** | `talkops serve --profile api-only` | `client-gateway`, `core-auth` | Internal team server for remote CLI/TUI users when webhooks and chat channels are not needed. |
| **`webhook-worker`** | `talkops serve --profile webhook-worker` | `webhooks`, `channels`, `context-graph` | Dedicated autonomous webhook ingestion and ChatOps notification worker pods behind an external load balancer. |
| **`sre`** | `talkops serve --profile sre` | `client-gateway`, `core-auth`, `webhooks`, `channels`, `context-graph`, `intelligence` | Full autonomous SRE & RCA stack with context graph correlation and feedback reflection. |
| **`enterprise-full`** | `talkops serve --profile enterprise-full` | `client-gateway`, `core-auth`, `webhooks`, `channels`, `context-graph`, `intelligence` | Base profile for layering enterprise or marketplace addons in `.talkops/addons.json`. |

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
  - `POST /channels/{provider}/interactions` — Interactive button clicks (Slack Block Kit, Microsoft Teams Adaptive Cards v1.5, Discord ActionRow Buttons) for HITL approvals and RCA feedback (`Confirm RCA` / `Dispute Hypothesis`), plus Discord Ed25519 interactions.
  - `POST /channels/{provider}/commands` — Native slash command callbacks (such as `/talkops`).
  - `GET /channels/status` & `GET /channels/health` — Adapter configuration diagnostics, active tasks, and pending HITL approvals.
  - `POST /channels/notify` — Proactive outbound notification and incident thread creation endpoint.
- **Multi-Modal Human-in-the-Loop (HITL) Approvals**:
  - When a tool call requires approval in Smart or Manual mode, `channels` renders an interactive approval card in Slack, Teams, or Discord. Engineers can approve or reject by **clicking Approve / Reject buttons**, **reacting with 👍 / 👎** (or Teams `like`), or **replying `approve` / `deny <reason>`** in the thread.
- **Interactive RCA Feedback, In-Place Card Locking & Reactions**:
  - Every automated RCA card posted in Slack, Discord, or Microsoft Teams includes **Confirm RCA** and **Dispute Hypothesis** buttons alongside **View Alert** (and accepts **👍 / 👎** reactions, including Teams `like`, directly on the RCA message).
  - Clicking a button or reacting locks the RCA card in place — removing or disabling the feedback buttons, recording who confirmed or disputed it, returning an ephemeral/toast acknowledgment, preventing conflicting double-clicks, and persisting the verdict (`feedback.helped` / `feedback.rejected`) to `OperationalStore` (`talkops_autoqa_scores`) via `IntelligenceAddon` (with automatic fallback persistence in the `default` profile).
  - On human chat messages (`@mention` or thread replies), the bot adds a `👀` reaction while running and swaps to `✅` when complete.
- **Built-In Chat Commands**:
  - `/help`, `/new`, `/stop`, `/status`, `/model`, `/mode`, `/pair`, and `/context-doctor`.
- **3-Tier Exposure Policy & DM Sender Pairing**:
  - Configure each chat platform with `self` (operator only), `allowlist` (operator + allowlisted users/channels + paired senders), or `open` (requires explicit `open_ack: true`). Unrecognized DM senders receive a one-time pairing code (e.g. `K7QM-3XRD`) that an operator can approve via `/pair approve K7QM-3XRD`.
- **Platform Portal Setup Guides**:
  - **[Slack App Portal & Server Setup Guide](slack-portal-setup.md)**
  - **[Discord Developer Portal & Server Setup Guide](discord-portal-setup.md)**
  - **[Microsoft Teams Portal & Server Setup Guide](teams-portal-setup.md)**

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

## 4. `addons.json` Reference & Step-by-Step Configuration Guide

TalkOps configures server addons through a **4-tier precedence hierarchy** and loads secrets from `.talkops/.env` or environment variables so you never hardcode credentials in config files.

### Why `addons.json` Is Separate from `config.toml`

TalkOps intentionally keeps **personal CLI/TUI preferences** (`~/.talkops/config.toml`) separate from **server addon deployment configuration** (`addons.json`):

| Aspect | `~/.talkops/config.toml` | `addons.json` *(or `settings.json`)* |
| :--- | :--- | :--- |
| **Scope** | **User-global only** (`~/.talkops/config.toml`) for your personal workstation CLI/TUI defaults (model, cloud profile, approval mode, theme, sandboxes). | **Multi-layer deployment config**: merges user global (`~/.talkops/addons.json`) → Git-committable project scope (`.talkops/addons.json`) → local overrides (`.talkops/settings.local.json`) → explicit runtime path (`--addons-config` / `TALKOPS_ADDONS_CONFIG`). |
| **Mutability** | Rewritten atomically in-place by interactive TUI commands (`/model --default`, `/cloud`, `/pool`, `/config set`) via `tomli_w.dump()`, which strips `#` comments and reformats the file. | Read-only at runtime; safe to version-control in Git, mount as a Kubernetes `ConfigMap`, or template in CI/CD without personal CLI actions mutating server settings. |
| **Secret Indirection** | Stores static user values (secrets live in `.env` / `/auth`). | Recursively resolves `${ENV_VAR}`, `env://ENV_VAR`, and `<field>_env` pointers against `.talkops/.env` and `os.environ` so team configs can be committed safely without secrets. |
| **Plugin & Schema Extensibility** | Fixed core CLI/TUI sections (`[aws]`, `[models]`, `[ui]`, etc.). | Mirrors `.mcp.json` so arbitrary external/marketplace addons (`package`, `module`, `path`, `python_path`), custom `profiles`, and nested routing arrays (`outbound_channels`) can be plugged in without modifying TalkOps core. |

> [!TIP]
> **`addons.json` is optional!** If you prefer not to maintain a separate `addons.json` file:
> 1. **Pure environment variables / `.talkops/.env`**: Configure any built-in addon via `TALKOPS_ADDON_<NAME>_<FIELD>` (for example, `TALKOPS_ADDON_CHANNELS_VERBOSITY=1`, `TALKOPS_ADDON_WEBHOOKS_MAX_CONCURRENT_RUNS=10`) plus provider env vars (`TALKOPS_CHANNELS_DISCORD_*`, `TALKOPS_SLACK_*`, `TALKOPS_WEBHOOK_NOTIFY_CHANNELS`).
> 2. **Existing `settings.json` / `settings.local.json`**: Place the exact same `"addons": { ... }` block inside `~/.talkops/settings.json`, `.talkops/settings.json`, or `.talkops/settings.local.json` — `AddonManager` merges them automatically.

### `addons.json` File Discovery & Structure

`load_merged_addons_settings_json()` ([`src/talkops/addons/config.py`](../../src/talkops/addons/config.py)) discovers and deep-merges files from lowest to highest precedence:

1. **User global**: `~/.talkops/addons.json`, `~/.talkops/.addons.json`, `~/.talkops/settings.json`
2. **Project scope**: `<repo>/.talkops/addons.json`, `<repo>/.talkops/.addons.json`, `<repo>/.addons.json`, `<repo>/addons.json`, `<repo>/.talkops/settings.json`, `<repo>/.talkops/settings.local.json`
3. **Explicit runtime override**: `--addons-config <path>` or `TALKOPS_ADDONS_CONFIG=<path>`

Each addon entry under `"addons"` supports:
- **Direct settings or nested `"config"`**: Fields placed under `"config": { ... }` are automatically flattened onto the addon's settings model while remaining accessible under `.config`.
- **Boolean toggle shorthand**: `"webhooks": false` disables an addon; `"channels": true` enables it.
- **External package shorthand**: `"my-addon": "my_org_pkg.addon:MyAddon"` enables and imports an external repository addon (or use full keys `"package"` / `"module"` / `"path"` / `"python_path"`).
- **Custom `"profiles"` map**: Define named server profiles selectable via `--profile <name>` or `TALKOPS_SERVER_PROFILE`.

---

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

# ── Built-in / Marketplace `channels` Addon Secrets (Slack, Teams, Discord) ──
TALKOPS_SLACK_BOT_TOKEN=xoxb-1234567890-abcdef
TALKOPS_SLACK_SIGNING_SECRET=9876543210fedcba
TALKOPS_TEAMS_APP_ID=00000000-0000-0000-0000-000000000000
TALKOPS_TEAMS_APP_PASSWORD=teams_bot_client_secret
TALKOPS_DISCORD_BOT_TOKEN=MTIzNDU2Nzg5MDEyMzQ1Njc4OQ.GhIjKl.MnOpQrStUvWxYz
TALKOPS_DISCORD_APPLICATION_ID=123456789012345678
# Optional for Discord: omit for zero-tunnel Gateway WebSocket mode, or set for Hybrid HTTP Interactions
TALKOPS_DISCORD_PUBLIC_KEY=a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90
```

In `.talkops/addons.json`, you can reference any environment variable in string values or nested objects in three ways:
1. **`${ENV_VAR}` interpolation**: `"bot_token": "${TALKOPS_SLACK_BOT_TOKEN}"`
2. **`env://ENV_VAR` URI**: `"bot_token": "env://TALKOPS_SLACK_BOT_TOKEN"`
3. **`<field>_env` pointer**: `"signing_secret_env": "TALKOPS_SLACK_SIGNING_SECRET"` (populates `signing_secret` automatically)

> [!NOTE]
> `${ENV_VAR}` interpolation resolves string values and nested JSON objects (not individual elements inside JSON arrays). For allowlist fields like `allowlist_chats` or `allowlist_users`, pass either a literal JSON array (`["1574383562176729138"]`) or a comma-separated string reference (`"${TALKOPS_CHANNELS_DISCORD_ALLOWLIST_CHATS}"`).

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
| **`channels`** | `verbosity` | `2` | Output streaming verbosity (`1` = final answer only without live status edits; `2` = live status placeholder + edited final reply; `3` = verbose tool traces). |
| **`channels`** | `pairing_enabled` | `True` | Issue one-time pairing codes to unrecognized DM senders when using `allowlist` exposure mode. |
| **`channels`** | `default_approval_mode` | `"smart"` | Default HITL tool approval policy for inbound chat turns (`manual`, `smart`, `auto`, `yolo`). |
| **`channels`** | `multitask_strategy` | `"interrupt"` | Behavior when a new message arrives in an active thread (`"interrupt"` cancels/replaces the running turn; `"enqueue"` queues it). |
| **`channels`** | `edit_debounce_seconds` | `1.0` | Minimum interval between in-place streaming message edits (`PATCH` / `chat.update`). |
| **`channels`** | `providers` | `{}` | Optional map of custom third-party channel adapter configs (registered via `talkops.channel_adapters` entry points). |
| **All Addons** | `port` | `None` *(main port)* | Optional dedicated TCP port. When set, `AddonManager` binds a separate `uvicorn.Server` on the same event loop. |
| **All Addons** | `mount_prefix` | `""` | Optional URL path prefix prepended to all routes registered by the addon. |

---

### Step 3: Configure the `channels` Addon (Slack, Teams, Discord)

`channels` is shipped built-in with `talkops serve` (and can also be installed or updated from [`talkops-ai/talkops-addons`](https://github.com/talkops-ai/talkops-addons) via `talkops addon install channels`).

Configure your Slack, Microsoft Teams, and/or Discord adapters in `.talkops/addons.json` (or omit `addons.json` and configure solely via `.talkops/.env`):

```json
{
  "addons": {
    "channels": {
      "enabled": true,
      "verbosity": 2,
      "pairing_enabled": true,
      "default_approval_mode": "smart",
      "multitask_strategy": "interrupt",
      "edit_debounce_seconds": 1.0,
      "slack": {
        "bot_token": "${TALKOPS_SLACK_BOT_TOKEN}",
        "signing_secret": "${TALKOPS_SLACK_SIGNING_SECRET}",
        "verify_signatures": true,
        "exposure_mode": "allowlist",
        "operator_id": "U0123ADMIN",
        "allowlist_users": ["U0123ADMIN", "U0456SRE"],
        "allowlist_chats": ["C0123456789"],
        "require_mention_in_channels": true
      },
      "teams": {
        "app_id": "${TALKOPS_TEAMS_APP_ID}",
        "app_password": "${TALKOPS_TEAMS_APP_PASSWORD}",
        "tenant_id": "botframework.com",
        "verify_signatures": true,
        "exposure_mode": "allowlist",
        "allowlist_users": ["*"]
      },
      "discord": {
        "bot_token": "${TALKOPS_DISCORD_BOT_TOKEN}",
        "application_id": "${TALKOPS_DISCORD_APPLICATION_ID}",
        "public_key": "${TALKOPS_DISCORD_PUBLIC_KEY}",
        "transport_mode": "auto",
        "auto_create_threads": true,
        "verify_signatures": true,
        "exposure_mode": "allowlist",
        "operator_id": "${TALKOPS_CHANNELS_DISCORD_OPERATOR_ID}",
        "allowlist_chats": ["1574383562176729138"]
      }
    }
  }
}
```

Point your chat platform webhook settings at your `talkops serve` URL:
- **Slack**:
  - Events API Request URL: `https://<your-talkops-host>/channels/slack/events` *(subscribe to `app_mention`, `message.channels`, `message.groups`, `message.im`, and `reaction_added`)*
  - Interactivity Request URL: `https://<your-talkops-host>/channels/slack/interactions` *(required for `Confirm RCA` / `Dispute Hypothesis` buttons, HITL approval buttons, ephemeral `response_url` acks, and in-place card updates)*
  - Slash Commands Request URL: `https://<your-talkops-host>/channels/slack/commands`
  - Recommended Bot Token Scopes: `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`, `im:history`, `reactions:read`, `reactions:write`, `commands`, `files:read`, and `files:write` *(for auto-uploading downloadable `.md` reports on long RCAs)*.
  - For full Slack App dashboard steps (OAuth scopes, Events API, Interactivity, Slash Commands, and DM setup), see the **[Slack App Portal & Server Setup Guide](slack-portal-setup.md)**.
- **Microsoft Teams (Azure Bot Messaging Endpoint)**:
  - Messaging Endpoint: `https://<your-talkops-host>/channels/teams/events` *(handles messages, `like`/`👎` reactions, and Adaptive Card v1.5 `Action.Execute` / `Action.Submit` invokes for both HITL approvals and `Confirm RCA` / `Dispute Hypothesis` on one route; set `TALKOPS_TEAMS_TENANT_ID` to your Entra Tenant UUID for Single-Tenant bots)*.
  - For full Azure Bot, `manifest.json`, and Teams Admin Center steps, see the **[Microsoft Teams Portal & Server Setup Guide](teams-portal-setup.md)**.
- **Discord (Developer Portal)**:
  - **Zero-tunnel Gateway WebSocket mode** (`transport_mode: "auto"` with `public_key` omitted): leave **Interactions Endpoint URL** blank in the Discord Portal — messages, `👍`/`👎` reactions, slash commands, and button clicks (`Confirm RCA` / `Dispute Hypothesis` / HITL approvals) all flow over outbound Gateway v10 WSS.
  - **Hybrid HTTP + Gateway mode** (`public_key` set): set Interactions Endpoint URL to `https://<your-talkops-host>/channels/discord/interactions`.
  - For full Discord Developer Portal permissions, intents, and `/channels/discord/sync-commands` steps, see the **[Discord Developer Portal & Server Setup Guide](discord-portal-setup.md)**.

---

### Step 4: Connect `webhooks` Alerts to `channels` for Automated Incident Threads

When the built-in `webhooks` addon receives an alert from PagerDuty, Alertmanager, Datadog, AWS SNS, GitHub, Jenkins, or ServiceNow, it emits `webhook.received` and `webhook.completed` on the server's internal `HookEventBus`.

When `channels` is enabled, it subscribes to those events and performs a **two-stage incident workflow**:
1. **On `webhook.received`**: Posts an initial incident card (`Status: Investigating`) in your configured Slack, Teams, or Discord channel (with a Red severity accent bar on Slack and Discord) and binds a new chat thread to the LangGraph investigation thread (`lg_thread_id`).
2. **On `webhook.completed`**:
   - Posts the full **Root Cause Analysis** report inside that chat thread with **Triage Time**, native tables, downloadable `.md` attachment uploads for long RCAs (Slack/Discord) or 28 KB Adaptive Card overflow chunks (Teams), and interactive **Confirm RCA** / **Dispute Hypothesis** buttons (plus **👍 / 👎** reaction support).
   - Updates the root incident card in place to **`RCA Complete (in thread)`** (flipping the Slack accent bar from Red to Green and adding Triage Time + Root Cause Takeaway) and links the thread so any engineer who replies in Slack/Teams/Discord continues the **exact same LangGraph investigation thread**.

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
| **Tier 2** | **Server Profile** | `--profile default\|api-only\|webhook-worker\|sre\|enterprise-full` or `TALKOPS_SERVER_PROFILE`. |
| **Tier 3** | **Config Files & Marketplace State** | Merged in order:<br>1. `~/.talkops/addons.json`, `~/.talkops/.addons.json`, `~/.talkops/settings.json`<br>2. `<repo>/.talkops/addons.json`, `<repo>/.talkops/.addons.json`, `<repo>/.addons.json`, `<repo>/addons.json`, `<repo>/.talkops/settings.json`, `<repo>/.talkops/settings.local.json`<br>3. `--addons-config <path>` (`TALKOPS_ADDONS_CONFIG`)<br>4. Enabled marketplace addons (`~/.talkops/state/enabled_addons.json`). |
| **Tier 4 (Highest)** | **Environment Variables & CLI Flags** | • `TALKOPS_ADDON_<NAME>_<FIELD>=<value>` (e.g., `TALKOPS_ADDON_CHANNELS_VERBOSITY=1`, `TALKOPS_ADDON_WEBHOOKS_MAX_CONCURRENT_RUNS=10`)<br>• `--webhook-port <port>` (`TALKOPS_WEBHOOK_PORT`)<br>• `--addons a,b` (`TALKOPS_ADDONS=a,b`)<br>• `--disable-addons c` (`TALKOPS_DISABLE_ADDONS=c`) |

---

## 6. Writing Custom Addons

You can write custom Server Addons in a standalone Git repository (like `talkops-addons`), package them with `.talkops/addon.json` and `.talkops/marketplace.json`, or load a single `.py` file directly via `.talkops/addons/*.py` or `--custom-addon`.

For a complete, step-by-step developer walkthrough—including the `BaseServerAddon` lifecycle protocol, `AgentRunGateway` and `HookEventBus` APIs, and real-world code examples from `talkops-addons`—see the **[Server Addon Developer Guide](developer-guide/server_addon_developer_guide.md)**.

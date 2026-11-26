# Microsoft Teams Portal & Server Setup Guide for TalkOps

This guide walks through the **exact Azure Portal, Microsoft Teams Developer Portal, Teams Admin Center, App Manifest (`manifest.json`), and TalkOps server configuration** required to enable full **two-way conversational chat** (1:1 personal DMs, group chats, and team channels), threaded webhook incident war-rooms, and Adaptive Card v1.5 `Action.Execute` workflows for both Human-in-the-Loop (HITL) tool approvals and RCA feedback (`Confirm RCA` / `Dispute Hypothesis`) in Microsoft Teams.

> **Why an Azure Bot Messaging Endpoint is required:** Native Teams **Incoming Webhooks / Workflows** are strictly one-way (`Agent → Teams`), and **Outgoing Webhooks** only trigger on `@mention` in public channels, block 1:1 personal DMs, lack multi-turn state, and enforce a hard **10-second synchronous HTTP timeout** that breaks multi-step SRE tool calls. Wiring your TalkOps server (`POST /channels/teams/events`) as a **Single-Tenant Azure Bot Messaging Endpoint** decouples ingestion (immediate `HTTP 200 OK` ACK) from asynchronous Bot Connector REST v3 delivery.

---

## 1. Prerequisites & Internal Settings Checklist

If Microsoft Teams shows **"Cannot send messages"** or hides the compose box, one of these internal settings across Azure, your App Manifest, or Teams Admin Center is misconfigured:

| Layer | Where to Configure | Exact Internal Setting | Required Value & Purpose |
| :--- | :--- | :--- | :--- |
| **1. Azure Bot** | Azure Portal → **Azure Bot** → **Configuration** | **Type of App** | **Single Tenant** *(new Multi-Tenant bot registrations were deprecated after July 2025)*. |
| **1. Azure Bot** | Azure Portal → **Azure Bot** → **Configuration** | **Messaging endpoint** | `https://<your-talkops-domain>/channels/teams/events` *(handles messages, reactions, and Adaptive Card `Action.Execute` / `Action.Submit` on one route)*. |
| **1. Bot Channel** | Azure Portal → **Azure Bot** → **Settings → Channels** | **Microsoft Teams** channel | **Enabled / Applied** — bridges Azure Bot Connector routing to Teams. |
| **2. App Manifest** | `manifest.json` (`bots[]`) or Teams Developer Portal | `isNotificationOnly` | **`false`** — enables the chat compose box. If `true`, Teams locks the chat into a one-way alert feed. |
| **2. App Manifest** | `manifest.json` (`bots[]`) | `scopes` | **`["personal", "team", "groupChat"]`** — `personal` enables 1:1 DMs; `team` and `groupChat` enable channel/group `@mention` threads. |
| **3. Tenant Policy** | Teams Admin Center → **Teams apps → Manage apps → Org-wide app settings** | **Let users interact with custom apps** | **On** — master tenant switch allowing users to chat with custom/in-house bots. |
| **4. Sideloading** | Teams Admin Center → **Teams apps → Setup policies** | **Upload custom apps** | **On** (Global or developer policy) — required to sideload `.zip` app packages during testing. |
| **4. Team Policy** | Teams Admin Center → **Permission policies** & Team client → **Manage team → Settings → Member permissions** | **Custom apps** & **Allow members to upload custom apps** | **Allowed / Checked** — prevents org or Team-level policies from blocking the bot inside channels. |

---

## 2. Step-by-Step Azure & Teams Portal Configuration

### Step 2.1: Create a Single-Tenant Azure Bot Resource

1. Open the [Azure Portal](https://portal.azure.com) and click **Create a resource** → search for **Azure Bot** → **Create**.
2. Configure the resource:
   - **Bot handle**: e.g., `talkops-sre-bot`
   - **Type of App**: Select **Single Tenant**
   - **Creation type**: Select **Create new Microsoft App ID** *(or bind an existing Microsoft Entra App Registration)*
3. Click **Review + create** → **Create**, then open the created **Azure Bot** resource.
4. Go to **Settings → Configuration**:
   - Copy **Microsoft App ID** → save as `TALKOPS_TEAMS_APP_ID`.
   - Copy **App Tenant ID** (Directory / Tenant UUID) → save as `TALKOPS_TEAMS_TENANT_ID`.
   - Click **Manage Password** next to **Microsoft App ID** → **Certificates & secrets** → **New client secret** → copy the secret **Value** immediately and save as `TALKOPS_TEAMS_APP_PASSWORD`.
   - Set **Messaging endpoint** to your public HTTPS TalkOps server URL:
     ```text
     https://<your-talkops-domain>/channels/teams/events
     ```
   - Click **Apply**.

> **Local Development Tunnel Note:** Unlike Discord's outbound Gateway WebSocket mode, production Microsoft Teams bots require a publicly reachable HTTPS **Messaging endpoint**. For local testing, expose your local TalkOps port (`2024`) over HTTPS via a dev tunnel (e.g., `devtunnel`, Cloudflare Tunnel, or `ngrok`) and set that HTTPS URL in **Messaging endpoint**.

---

### Step 2.2: Enable the Microsoft Teams Channel on the Azure Bot

1. Inside your **Azure Bot** resource, go to **Settings → Channels**.
2. Click **Microsoft Teams** under **Available channels**.
3. Accept the Terms of Service, confirm your cloud environment under the **Messaging** tab (Commercial / GCC / GCC High), and click **Apply**.

---

### Step 2.3: Build the Teams App Package (`manifest.json`)

Create a Teams App package via the [Teams Developer Portal](https://dev.teams.microsoft.com) (**Apps → New app**) or package a `.zip` archive containing:
- `manifest.json`
- `color.png` (`192x192` full-color icon)
- `outline.png` (`32x32` transparent monochrome outline icon)

Ensure **`botId`** matches your Azure Bot's **`TALKOPS_TEAMS_APP_ID`**, **`isNotificationOnly`** is **`false`**, and **`validDomains`** includes your TalkOps server domain:

```json
{
  "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.19/MicrosoftTeams.schema.json",
  "manifestVersion": "1.19",
  "version": "1.0.0",
  "id": "<YOUR-TALKOPS-TEAMS-APP-ID>",
  "developer": {
    "name": "TalkOps SRE",
    "websiteUrl": "https://<your-talkops-domain>",
    "privacyUrl": "https://<your-talkops-domain>/privacy",
    "termsOfUseUrl": "https://<your-talkops-domain>/terms"
  },
  "name": {
    "short": "TalkOps",
    "full": "TalkOps Autonomous SRE Agent"
  },
  "description": {
    "short": "Two-way conversational SRE & RCA agent",
    "full": "Chat with TalkOps in 1:1 DMs, group chats, or channels for live diagnostics, RCA war-rooms, and Adaptive Card approvals."
  },
  "icons": {
    "color": "color.png",
    "outline": "outline.png"
  },
  "accentColor": "#2563EB",
  "bots": [
    {
      "botId": "<YOUR-TALKOPS-TEAMS-APP-ID>",
      "scopes": [
        "personal",
        "team",
        "groupChat"
      ],
      "supportsFiles": false,
      "isNotificationOnly": false
    }
  ],
  "validDomains": [
    "<your-talkops-domain>"
  ]
}
```

---

### Step 2.4: Enable Tenant Policies, Sideload, and Roll Out

1. **Tenant Admin Policies (`admin.teams.microsoft.com`):**
   - **Teams apps → Manage apps → Org-wide app settings** → toggle **Let users interact with custom apps** **On**.
   - **Teams apps → Setup policies** → select Global (or your developer policy) → toggle **Upload custom apps** **On**.
   - **Teams apps → Permission policies** → verify **Custom apps** are allowed.
   - *(Allow 15 minutes to a few hours for Teams Admin Center policy changes to propagate.)*
2. **Sideload for Testing:**
   - In Microsoft Teams, open **Apps → Manage your apps → Upload an app → Upload a custom app**.
   - Select your `.zip` package and click **Add** (and optionally **Add to a team** to install it into your SRE/war-room channel).
3. **Org-Wide Distribution (Production):**
   - Upload the verified `.zip` in **Teams Admin Center → Teams apps → Manage apps → Upload new app** (or click **Publish to your org** in Developer Portal) so everyone in your tenant can install TalkOps from **Built for your org**.

---

## 3. Configure TalkOps Server (`talkops serve`)

### Option A: Zero-JSON Setup via `.talkops/.env` (Recommended for Quick Start)

Add your Single-Tenant Entra Bot credentials to `.talkops/.env` (or export in your shell):

```bash
# Required Single-Tenant Azure Bot credentials
export TALKOPS_TEAMS_APP_ID="<your-microsoft-app-id-uuid>"
export TALKOPS_TEAMS_APP_PASSWORD="<your-entra-client-secret>"
export TALKOPS_TEAMS_TENANT_ID="<your-entra-directory-tenant-uuid>"

# Optional: regional Bot Connector fallback before first inbound message arrives
# (TalkOps automatically persists the exact tenant serviceUrl from inbound activities in SQLite/PostgreSQL!)
# Examples: https://smba.trafficmanager.net/amer | /emea | /apac | /in
export TALKOPS_CHANNELS_TEAMS_SERVICE_URL="https://smba.trafficmanager.net/amer"

# Security & allowlist (recommended for production)
export TALKOPS_CHANNELS_TEAMS_EXPOSURE_MODE="allowlist"
export TALKOPS_CHANNELS_TEAMS_OPERATOR_ID="<your-teams-user-id>"
export TALKOPS_CHANNELS_TEAMS_ALLOWLIST_CHATS="19:<ops_channel_id>@thread.tacv2"

# Optional: route Alertmanager/PagerDuty/SNS webhook incidents to a Teams channel
export TALKOPS_WEBHOOK_NOTIFY_CHANNELS="teams:19:<ops_channel_id>@thread.tacv2"

# Optional: set verbosity=1 to send final replies cleanly without an initial "Working..." edit
# export TALKOPS_ADDON_CHANNELS_VERBOSITY=1
```

### Option B: Declarative `.talkops/addons.json` (Team / GitOps / Kubernetes ConfigMap)

> **Why `.talkops/addons.json` instead of `~/.talkops/config.toml`?**
> `~/.talkops/config.toml` stores user-global CLI/TUI preferences and is rewritten by interactive TUI commands (`/model`, `/cloud`, `/config set`). Server Mode addons use `.talkops/addons.json` so deployment configs can be version-controlled with `${ENV_VAR}` references or mounted as a Kubernetes ConfigMap without mixing in personal CLI settings (see [Server Addons & `addons.json`](server-addons.md#why-addonsjson-is-separate-from-configtoml)).

```json
{
  "addons": {
    "channels": {
      "enabled": true,
      "verbosity": 2,
      "pairing_enabled": true,
      "default_approval_mode": "smart",
      "multitask_strategy": "interrupt",
      "teams": {
        "app_id": "${TALKOPS_TEAMS_APP_ID}",
        "app_password": "${TALKOPS_TEAMS_APP_PASSWORD}",
        "tenant_id": "${TALKOPS_TEAMS_TENANT_ID}",
        "service_url": "${TALKOPS_CHANNELS_TEAMS_SERVICE_URL}",
        "verify_signatures": true,
        "exposure_mode": "allowlist",
        "operator_id": "${TALKOPS_CHANNELS_TEAMS_OPERATOR_ID}",
        "allowlist_chats": "${TALKOPS_CHANNELS_TEAMS_ALLOWLIST_CHATS}",
        "require_mention_in_channels": true
      }
    }
  }
}
```

> **How TalkOps Secures & Persists Teams Traffic Automatically:**
> - **RS256 JWKS Verification:** Every inbound `POST /channels/teams/events` request with a Bearer token is cryptographically verified against Microsoft's OpenID/JWKS (`https://login.botframework.com/v1/.well-known/openidconfiguration` and your Entra tenant JWKS), strictly enforcing `RS256`, `iss`, `aud == app_id`, `tid == tenant_id` (Single Tenant), and `exp`/`nbf`.
> - **`serviceUrl` SSRF Guard & Persistence:** Inbound `serviceUrl` values are validated against trusted Microsoft Bot Connector hosts (`smba.trafficmanager.net`, `*.botframework.com`, `*.teams.microsoft.com`, etc.) and persisted alongside the bot identity in `PersistentChannelStateStore` (SQLite WAL or PostgreSQL via `TALKOPS_POSTGRES_URI`), so regional endpoints (`/emea`, `/apac`, `/in`, `/amer`) survive server restarts and multi-replica scaling.
> - **Channel Thread Binding:** Incident cards posted in a Teams channel (`19:...@thread.tacv2`) are automatically bound to `19:...@thread.tacv2;messageid=<root_activity_id>`, so engineers replying inside that incident thread seamlessly continue the same LangGraph RCA session.

---

## 4. Start & Smoke-Test End-to-End

1. **Start TalkOps Server:**
   ```bash
   talkops serve --profile sre --host 0.0.0.0 --port 2024
   ```
2. **Verify Teams adapter status:**
   ```bash
   curl -s http://127.0.0.1:2024/channels/status | python3 -m json.tool
   ```
   Confirm `"channel": "teams"`, `"configured": true`, `"has_app_credentials": true`, and `"tenant_id"` matches your Entra Directory Tenant UUID (not `"botframework.com"`).
3. **Bootstrap regional `serviceUrl` & find your Teams IDs:**
   - Send a quick `Hi` or `/status` in a **1:1 personal DM** with TalkOps (or `@TalkOps /status` in your ops channel).
   - TalkOps immediately caches and persists your tenant's exact `serviceUrl` and bot account, and `/status` prints the exact `conversation_id` (`19:...@thread.tacv2`) to use in `TALKOPS_CHANNELS_TEAMS_ALLOWLIST_CHATS` and `TALKOPS_WEBHOOK_NOTIFY_CHANNELS`.
4. **Smoke-Test All Flows in Teams:**
   - **1:1 Personal DM (`personal`):** Send `What is your capability?` directly to TalkOps — verify typing indicator + clean reply (structured responses render as a single Adaptive Card with no duplicated text above it).
   - **Channel `@mention` Thread (`team`):** In your ops channel, send `@TalkOps Check checkout-api health` — verify `<at>TalkOps</at>` is stripped and TalkOps replies inside the channel thread.
   - **HITL Tool Approval (`Action.Execute`):** Trigger a shell/write tool in `--approval-mode manual` — verify the Adaptive Card displays **Approve** and **Reject** buttons, clicking **Approve** returns a valid `adaptiveCard/action` invoke ACK (no *"Something went wrong"* banner), replaces the card with the resolved approval text, and resumes the agent run.
   - **Webhook RCA Card & Feedback (`Action.Execute` & `like` / `👎` Reactions):** Trigger a webhook alert to your ops channel — verify the RCA renders as an Adaptive Card v1.5 (strictly capped under Teams' 28 KB limit with clean Markdown thread overflow for long reports) with **Confirm RCA**, **Dispute Hypothesis**, and **View Alert** actions. Clicking **Confirm RCA** or **Dispute Hypothesis** (or reacting with `like` / `👎` on the RCA card) returns an invoke toast ACK, updates the card in place via `PUT` to remove the feedback buttons and insert a semantic `good`/`attention` status container, blocks duplicate clicks, and persists the verdict in `OperationalStore` (`talkops_autoqa_scores`).
   - **DM Pairing:** Have a non-allowlisted user DM the bot → they receive an 8-character pairing code (`ABCD-EFGH`); approve it as operator via `/pair approve ABCD-EFGH`.

---

## 5. Complete Microsoft Teams Environment Variable Reference

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `TALKOPS_TEAMS_APP_ID` *(or `TALKOPS_CHANNELS_TEAMS_APP_ID`, `MICROSOFT_APP_ID`)* | *(required)* | Microsoft App ID (Client ID) from Azure Bot → **Configuration**. |
| `TALKOPS_TEAMS_APP_PASSWORD` *(or `TALKOPS_CHANNELS_TEAMS_APP_PASSWORD`, `MICROSOFT_APP_PASSWORD`)* | *(required)* | Entra App Registration Client Secret value. |
| `TALKOPS_TEAMS_TENANT_ID` *(or `TALKOPS_CHANNELS_TEAMS_TENANT_ID`, `MICROSOFT_APP_TENANT_ID`)* | `"botframework.com"` | **Required for all new Single-Tenant bots:** your Entra Directory (Tenant) UUID. |
| `TALKOPS_CHANNELS_TEAMS_SERVICE_URL` | `"https://smba.trafficmanager.net/amer"` | Fallback Bot Connector `serviceUrl` before the first inbound activity persists your tenant's regional URL (`/emea`, `/apac`, `/in`, `/amer`). |
| `TALKOPS_CHANNELS_TEAMS_VERIFY_SIGNATURES` | `"1"` | Keep `"1"` in production (enforces RS256 JWKS JWT / HMAC verification). |
| `TALKOPS_CHANNELS_TEAMS_OPENID_METADATA_URL` | `https://login.botframework.com/v1/.well-known/openidconfiguration` | Override for sovereign/government clouds (e.g., `https://login.botframework.azure.us/v1/.well-known/openid-configuration`). |
| `TALKOPS_CHANNELS_TEAMS_ALLOWED_SERVICE_URL_HOSTS` | `""` | Optional comma-separated extra trusted hostnames (e.g., `localhost` for Bot Framework Emulator or an internal proxy). |
| `TALKOPS_TEAMS_WEBHOOK_SECRET` *(or `TALKOPS_CHANNELS_TEAMS_WEBHOOK_SECRET`)* | `""` | Optional base64 HMAC secret only if also receiving legacy Teams Outgoing Webhooks. |
| `TALKOPS_CHANNELS_TEAMS_EXPOSURE_MODE` | `"allowlist"` | `"self"`, `"allowlist"`, or `"open"` (`open` requires `TALKOPS_CHANNELS_TEAMS_OPEN_ACK=1`). |
| `TALKOPS_CHANNELS_TEAMS_OPERATOR_ID` | `""` | Operator Teams user ID (`29:...` from `activity.from.id`) allowed to run `/pair` and approve HITL cards. |
| `TALKOPS_CHANNELS_TEAMS_ALLOWLIST_USERS` | `""` | Comma-separated Teams user IDs allowed in `allowlist` mode. |
| `TALKOPS_CHANNELS_TEAMS_ALLOWLIST_CHATS` | `""` | Comma-separated Teams channel/chat IDs (`19:...@thread.tacv2`; child threads inherit parent channel admission). |
| `TALKOPS_CHANNELS_TEAMS_REQUIRE_MENTION` | `"1"` | Require `@TalkOps` mention for new conversations in channels (`"0"` to respond to all messages). |

---

## 6. Troubleshooting Quick Reference

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| Teams shows **"Cannot send messages"** or no chat input box | `isNotificationOnly` is `true`, `scopes` missing `"personal"`, or custom apps disabled in Teams Admin Center. | Set `"isNotificationOnly": false`, `"scopes": ["personal", "team", "groupChat"]`, and enable **Let users interact with custom apps** + **Upload custom apps** in Teams Admin Center. |
| Inbound webhook returns `401 invalid_teams_authorization` | Wrong `TALKOPS_TEAMS_APP_ID` (`aud` mismatch), wrong `TALKOPS_TEAMS_TENANT_ID` (`tid` mismatch), or server cannot reach Microsoft's OpenID/JWKS endpoint. | Verify exact App ID & Tenant UUID; confirm outbound HTTPS access to `login.botframework.com` and `login.microsoftonline.com` (or set `TALKOPS_CHANNELS_TEAMS_OPENID_METADATA_URL` for sovereign clouds). |
| Outbound token exchange fails (`AADSTS700016` / `401`) | `TALKOPS_TEAMS_TENANT_ID` left at default `"botframework.com"` on a Single-Tenant Azure Bot. | Set `TALKOPS_TEAMS_TENANT_ID` to your Entra Directory (Tenant) UUID and restart `talkops serve`. |
| Proactive webhook alerts fail with `401` or `404` on EMEA/APAC/India tenants | Default `serviceUrl` points to `/amer` before any inbound message from your tenant was persisted. | Either set `TALKOPS_CHANNELS_TEAMS_SERVICE_URL` to your region (`/emea`, `/apac`, `/in`) or send one `@TalkOps /status` in the target channel so TalkOps persists the exact `serviceUrl` in SQLite/PostgreSQL. |
| Clicking **Approve / Reject** shows *"Something went wrong"* | Messaging endpoint unreachable, expired/invalid JWT, or custom proxy modifying the `200 OK` JSON invoke response. | Ensure your reverse proxy forwards `POST /channels/teams/events` JSON bodies unmodified and returns TalkOps's `{"statusCode": 200, "type": "application/vnd.microsoft.activity.message", ...}` response within 15s. |
| Messages show `(Edited)` in Teams | `ChannelsAddon` defaults to `verbosity = 2`, which posts `"⏳ Working on your request..."` and updates it via `PUT`. | Set `TALKOPS_ADDON_CHANNELS_VERBOSITY=1` (or `"verbosity": 1` in `.talkops/addons.json`) to post only the final response without editing. |
| Replies in a Teams channel incident thread start a new conversation | TalkOps restarted without shared persistence or channel was created before thread binding. | Ensure SQLite (single node) or `TALKOPS_POSTGRES_URI` (multi-replica Kubernetes) is configured so `ChannelStateStore` persists incident refs and `serviceUrl` across pods. |

---

## See Also

- **[Server Addons & ChatOps Hub](server-addons.md)** — Full `channels` addon architecture, multi-channel routing, and server profiles.
- **[Webhooks & Automated Incident Response](webhooks.md)** — Connecting PagerDuty, Alertmanager, Datadog, AWS SNS, ServiceNow, and GitHub alerts.
- **[Slack App Portal Setup Guide](slack-portal-setup.md)** · **[Discord Developer Portal Setup Guide](discord-portal-setup.md)** — Companion setup guides for Slack and Discord.

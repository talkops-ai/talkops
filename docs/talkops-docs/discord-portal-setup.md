# Discord Developer Portal & Server Setup Guide for TalkOps

This guide walks through the **exact Discord Developer Portal settings, Bot permissions, slash command registration, and TalkOps server configuration** required to enable full two-way conversational chat, automatic thread-per-session isolation, Human-in-the-Loop (HITL) approval buttons, and autonomous webhook incident war-rooms in Discord.

> **Why a Discord Bot + Gateway WebSocket is required:** Standard Discord channel webhooks are outbound-only (`Agent → Discord`), and Discord's HTTP **Interactions Endpoint URL** only sends slash commands (`/ask`) and button clicks—it **never** sends free-form channel or thread messages (`MESSAGE_CREATE`) or emoji reactions (`MESSAGE_REACTION_ADD`) over HTTP. TalkOps automatically runs a persistent Discord API v10 Gateway WebSocket transport alongside its HTTP adapter so both natural chat and structured interactions work seamlessly.

---

## 1. Choose Your Inbound Transport Mode (Recommended Defaults)

TalkOps defaults to `transport_mode = "auto"`, which selects the right mode automatically based on the credentials you configure:

| Mode | When TalkOps Selects It | Public HTTPS URL Needed? | How Events Flow | Best For |
| :--- | :--- | :--- | :--- | :--- |
| **`websocket`** *(Zero-Tunnel Default)* | `TALKOPS_DISCORD_BOT_TOKEN` is set, `TALKOPS_DISCORD_PUBLIC_KEY` is **unset** | **No** (outbound WSS only!) | Messages, reactions, slash commands, **and** button clicks all flow over Discord Gateway v10 WebSocket. | Private VPCs, local development, laptops behind NAT, air-gapped enterprise networks. |
| **`hybrid`** *(Public HTTPS Default)* | Both `TALKOPS_DISCORD_BOT_TOKEN` **and** `TALKOPS_DISCORD_PUBLIC_KEY` are set | **Yes** (`https://<host>/channels/discord/interactions`) | Slash commands & button clicks arrive via Ed25519-verified HTTP Interactions; messages & reactions arrive via Gateway WebSocket. | Publicly fronted production deployments where HTTP interactions load-balance across API replicas. |
| **`webhook`** *(HTTP-Only)* | `TALKOPS_CHANNELS_DISCORD_GATEWAY_ENABLED=0` or `transport_role=http_only` | **Yes** | HTTP Interactions + optional HMAC-signed external Gateway bridge posting to `/channels/discord/events`. | Multi-replica API pods when a separate `gateway_worker` pod owns the Gateway socket. |

---

## 2. Step-by-Step Discord Developer Portal Configuration

Open the [Discord Developer Portal](https://discord.com/developers/applications) and either select your existing TalkOps application or click **New Application** (e.g., `TalkOps SRE`).

### Step 2.1: `Bot` Tab (Token & Privileged Gateway Intents)

1. In the left sidebar, click **Bot**.
2. Under **Token**, click **Reset Token**, copy the token immediately, and store it securely as `TALKOPS_DISCORD_BOT_TOKEN`.
3. *(Enterprise recommendation)* Under **Authorization Flow**, toggle **Public Bot** **OFF** so only authorized admins can invite the bot to your organization's Discord servers.
4. Scroll down to **Privileged Gateway Intents** and configure:
   - **`MESSAGE CONTENT INTENT`** $\rightarrow$ **Toggle ON (Required)**
     - *Why:* Without this toggle, Discord strips `content`, `attachments`, and `embeds` from `MESSAGE_CREATE` events in regular channels and threads, and closes TalkOps's Gateway connection with close code **`4014` (`DisallowedIntents`)**.
     - *Verification note:* Apps in fewer than 100 servers / 10,000 users can toggle this ON immediately without Discord review.
   - **`SERVER MEMBERS INTENT`** $\rightarrow$ **Optional**
     - Leave **OFF** unless you enable `TALKOPS_CHANNELS_DISCORD_MEMBERS_INTENT=1` for Discord-role-based RBAC mapping.
   - **`PRESENCE INTENT`** $\rightarrow$ Leave **OFF** (not needed by TalkOps).
5. Click **Save Changes**.

---

### Step 2.2: `General Information` Tab (IDs & Interactions Routing)

1. In the left sidebar, click **General Information**.
2. Copy **Application ID** $\rightarrow$ save as `TALKOPS_DISCORD_APPLICATION_ID`.
3. Copy **Public Key** (64-character hex string) $\rightarrow$ save as `TALKOPS_DISCORD_PUBLIC_KEY` *(only required for `hybrid` or `webhook` modes)*.
4. Configure **Interactions Endpoint URL** carefully based on your chosen mode from Section 1:
   - **For `websocket` (Zero-Tunnel Gateway-Only Mode):** Leave **Interactions Endpoint URL completely BLANK**. Discord will route slash commands and button clicks over the Gateway WebSocket directly into TalkOps.
   - **For `hybrid` (Public HTTPS Mode):** Set **Interactions Endpoint URL** to:
     ```text
     https://<your-talkops-domain>/channels/discord/interactions
     ```
     Start `talkops serve` *first*, then click **Save Changes** in the Discord Portal—Discord will send an Ed25519-signed `PING` (`type: 1`) and TalkOps will automatically return `{"type": 1}` (`PONG`).

---

### Step 2.3: `OAuth2` $\rightarrow$ `URL Generator` (Scopes & Least-Privilege Permissions)

1. In the left sidebar, open **OAuth2** $\rightarrow$ **URL Generator**.
2. Under **Scopes**, check **both**:
   - `bot`
   - `applications.commands`
3. Under **Bot Permissions**, enable this exact least-privilege set:

| Permission | Why TalkOps Needs It |
| :--- | :--- |
| **`View Channels`** | See allowed channels, threads, and active guild thread lists. |
| **`Send Messages`** | Post root incident cards, replies, and status updates. |
| **`Send Messages in Threads`** | Stream agent responses and RCA reports inside conversation/incident threads. |
| **`Create Public Threads`** | Auto-spawn 1 isolated Discord thread per `@mention` query and per webhook incident. |
| **`Create Private Threads`** | Support private incident/triage threads when used in restricted channels. |
| **`Read Message History`** | Hydrate prior thread context on restart and detect bot participation for unmentioned follow-ups. |
| **`Embed Links`** | Render structured incident cards, RCA summaries, and HITL approval embeds. |
| **`Attach Files`** | Upload log bundles, diffs, charts, and Markdown postmortem artifacts. |
| **`Add Reactions`** | Add `👀` while investigating and `✅` when completed on human chat messages, and read `👍` / `👎` reactions for both HITL tool approvals and RCA feedback (`Confirm RCA` / `Dispute Hypothesis`). |
| **`Manage Webhooks`** *(Optional)* | Only required if using `persona_webhook_url` for multi-agent custom names/avatars. |

4. Copy the **Generated URL** at the bottom, open it in your browser, select your SRE Discord server, and click **Authorize**.
5. In Discord, add the bot to your target channels (for private channels, open **Channel Settings** $\rightarrow$ **Permissions** $\rightarrow$ add the TalkOps Bot role).

---

### Step 2.4: Copy Snowflake IDs in Discord (For Allowlist & War-Room Routing)

1. Open Discord **User Settings** $\rightarrow$ **Advanced** $\rightarrow$ toggle **Developer Mode** ON.
2. Right-click and copy these IDs:
   - **Your Discord Server (Guild) icon** $\rightarrow$ **Copy Server ID** (`GUILD_ID`, used for instant slash command registration).
   - **Your Operator User Profile** $\rightarrow$ **Copy User ID** (`TALKOPS_CHANNELS_DISCORD_OPERATOR_ID`).
   - **Your Ops / War-Room Channel(s)** $\rightarrow$ **Copy Channel ID** (`TALKOPS_CHANNELS_DISCORD_ALLOWLIST_CHATS` and `TALKOPS_WEBHOOK_NOTIFY_CHANNELS`).

---

## 3. Configure TalkOps Server (`talkops serve`)

### Option A: Zero-Tunnel Gateway-Only Setup (Simplest — Works Anywhere, No ngrok/Tunnel!)

Add to `.talkops/.env` (or export in your shell):

```bash
# Required for Gateway-only two-way chat + slash commands + buttons
export TALKOPS_DISCORD_BOT_TOKEN="<your_bot_token>"
export TALKOPS_DISCORD_APPLICATION_ID="<your_application_id>"
# Leave TALKOPS_DISCORD_PUBLIC_KEY unset so transport_mode="auto" picks "websocket"

# Security & allowlist (recommended for production)
export TALKOPS_CHANNELS_DISCORD_EXPOSURE_MODE="allowlist"
export TALKOPS_CHANNELS_DISCORD_OPERATOR_ID="<your_user_id>"
export TALKOPS_CHANNELS_DISCORD_ALLOWLIST_CHATS="<ops_channel_id>"

# Optional: route Alertmanager/PagerDuty/SNS webhook incidents to a Discord war-room channel
export TALKOPS_WEBHOOK_NOTIFY_CHANNELS="discord:<warroom_channel_id>"
```

### Option B: Hybrid Setup (Public HTTPS Interactions Endpoint + Gateway WebSocket)

Add `TALKOPS_DISCORD_PUBLIC_KEY` (and set the **Interactions Endpoint URL** in the Discord Portal):

```bash
export TALKOPS_DISCORD_BOT_TOKEN="<your_bot_token>"
export TALKOPS_DISCORD_APPLICATION_ID="<your_application_id>"
export TALKOPS_DISCORD_PUBLIC_KEY="<your_64_char_ed25519_public_key_hex>"
export TALKOPS_CHANNELS_DISCORD_TRANSPORT_MODE="hybrid"   # auto-selected when public_key is set
export TALKOPS_CHANNELS_DISCORD_OPERATOR_ID="<your_user_id>"
export TALKOPS_CHANNELS_DISCORD_ALLOWLIST_CHATS="<ops_channel_id>"
```

### Option C: Declarative `.talkops/addons.json` (Optional — Including Multi-Replica HA Settings)

> **Why `.talkops/addons.json` instead of `~/.talkops/config.toml`?**
> `~/.talkops/config.toml` stores user-global CLI/TUI preferences and is rewritten by interactive commands (`/model`, `/cloud`, `/config set`). Server Mode addons use `.talkops/addons.json` so team/production channel configs can be committed to Git with `${ENV_VAR}` secret pointers or mounted as a Kubernetes ConfigMap without mixing in personal CLI settings (see [Server Addons & `addons.json`](server-addons.md#why-addonsjson-is-separate-from-configtoml)). If you prefer **zero JSON config**, Option A / Option B environment variables in `.talkops/.env` (plus `TALKOPS_ADDON_CHANNELS_VERBOSITY=1` if you want to skip the `"Working on your request..."` placeholder edit) cover every setting.

```json
{
  "addons": {
    "channels": {
      "enabled": true,
      "verbosity": 2,
      "pairing_enabled": true,
      "default_approval_mode": "smart",
      "multitask_strategy": "interrupt",
      "discord": {
        "bot_token": "${TALKOPS_DISCORD_BOT_TOKEN}",
        "application_id": "${TALKOPS_DISCORD_APPLICATION_ID}",
        "public_key": "${TALKOPS_DISCORD_PUBLIC_KEY}",
        "transport_mode": "auto",
        "auto_create_threads": true,
        "exposure_mode": "allowlist",
        "operator_id": "${TALKOPS_CHANNELS_DISCORD_OPERATOR_ID}",
        "allowlist_chats": "${TALKOPS_CHANNELS_DISCORD_ALLOWLIST_CHATS}"
      }
    }
  }
}
```

> **Note on `allowlist_chats`:** `${ENV_VAR}` interpolation resolves string values and nested JSON objects (not elements inside JSON arrays), so pass either a string env reference (`"${TALKOPS_CHANNELS_DISCORD_ALLOWLIST_CHATS}"`, comma-separated) or a literal JSON array of Snowflake IDs (`["1574383562176729138"]`).

> **Multi-Replica / Kubernetes / LangGraph Agent Server Note:**
> When `TALKOPS_POSTGRES_URI` is configured, TalkOps automatically coordinates a distributed leader lease (`discord:gateway:shard:0:1`) in PostgreSQL so only **one** replica holds the active Gateway socket while standby pods automatically fail over if the leader exits. Alternatively, you can split roles explicitly by setting `TALKOPS_CHANNELS_DISCORD_TRANSPORT_ROLE=http_only` on API pods and `gateway_worker` on a dedicated 1-replica worker deployment.

---

## 4. Register Slash Commands & Verify End-to-End

1. **Start TalkOps Server:**
   ```bash
   talkops serve --profile sre --port 2024
   ```
2. **Verify Gateway connection & adapter status:**
   ```bash
   curl -s http://127.0.0.1:2024/channels/status | python3 -m json.tool
   ```
   Confirm `"channel": "discord"`, `"configured": true`, and `"transport": {"state": "connected", "connected": true}`.
3. **Sync Slash Commands (`/ask`, `/help`, `/status`, `/mode`, `/effort`, `/new`, `/stop`, `/model`, `/context-doctor`, `/pair`):**
   - **Instant guild registration (recommended for testing & single-org servers):**
     ```bash
     curl -s -X POST "http://127.0.0.1:2024/channels/discord/sync-commands" \
       -H "Content-Type: application/json" \
       -d '{"guild_id": "<YOUR_GUILD_ID>"}' | python3 -m json.tool
     ```
   - **Global registration** *(takes up to ~1 hour to propagate across all servers)*:
     ```bash
     curl -s -X POST "http://127.0.0.1:2024/channels/discord/sync-commands" \
       -H "Content-Type: application/json" -d '{}' | python3 -m json.tool
     ```
4. **Smoke-Test All Flows in Discord:**
   - **Natural Thread Chat:** In `#ops-chat`, send `@TalkOps Check checkout-api health`. TalkOps auto-creates a thread anchored to your message and streams the response inside it.
   - **Unmentioned Follow-Up:** Reply inside that thread **without** `@TalkOps` (e.g., `What changed in the last 2 hours?`). TalkOps replies in the same LangGraph session.
   - **Slash Command:** Run `/ask prompt: Summarize recent incidents` — verify the *"TalkOps is thinking..."* deferred state updates cleanly in-place with the final report.
   - **HITL Tool Approval:** Trigger a write/shell action in `--approval-mode manual` — approve via the **Approve** button (buttons clear immediately after clicking), 👍 reaction, or replying `approve` in thread.
   - **Webhook RCA Feedback (`Confirm RCA` / `Dispute Hypothesis` & `👍`/`👎`):** Trigger a webhook alert to your war-room channel — verify the RCA embed renders **View Alert**, **Confirm RCA**, and **Dispute Hypothesis** buttons (plus an automatic `.md` file attachment for long reports). Clicking **Confirm RCA** or **Dispute Hypothesis** (or reacting with `👍` / `👎` on the RCA message) returns an ephemeral acknowledgment, updates the embed in place with a disabled locked status button + status field, prevents duplicate/conflicting clicks, and persists the verdict to `OperationalStore` (`talkops_autoqa_scores`).
   - **DM Pairing:** Have a non-allowlisted user DM the bot $\rightarrow$ they receive an 8-character pairing code (`ABCD-EFGH`); approve it as operator via `/pair approve ABCD-EFGH`.

---

## 5. Complete Discord Environment Variable Reference

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `TALKOPS_DISCORD_BOT_TOKEN` *(or `DISCORD_BOT_TOKEN`)* | *(required)* | Bot token from Developer Portal $\rightarrow$ **Bot**. |
| `TALKOPS_DISCORD_APPLICATION_ID` *(or `DISCORD_APPLICATION_ID`)* | *(auto-filled via `/users/@me`)* | Required for `/ask` `@original` edits and `sync-commands`. |
| `TALKOPS_DISCORD_PUBLIC_KEY` *(or `DISCORD_PUBLIC_KEY`)* | `""` | 64-hex Ed25519 key (needed only for `hybrid`/`webhook` HTTP Interactions). |
| `TALKOPS_CHANNELS_DISCORD_TRANSPORT_MODE` | `"auto"` | `"auto"` (`hybrid` w/ public key, `websocket` without), `"websocket"`, `"hybrid"`, or `"webhook"`. |
| `TALKOPS_CHANNELS_DISCORD_GATEWAY_ENABLED` | *(auto)* | Set `"0"` to disable embedded Gateway WebSocket; `"1"` to force-enable. |
| `TALKOPS_CHANNELS_DISCORD_TRANSPORT_ROLE` | `"all"` | `"all"`, `"http_only"` (API replicas), or `"gateway_worker"` (dedicated socket pod). |
| `TALKOPS_CHANNELS_DISCORD_AUTO_CREATE_THREADS` | `"1"` | Auto-spawn 1 Discord thread per authorized `@mention` and per webhook incident. |
| `TALKOPS_CHANNELS_DISCORD_MEMBERS_INTENT` | `"0"` | Set `"1"` only if `SERVER MEMBERS INTENT` is enabled in Portal. |
| `TALKOPS_CHANNELS_DISCORD_SHARD_ID` / `..._NUM_SHARDS` | `0` / `1` | Gateway sharding tuple `[shard_id, num_shards]` for large multi-guild deployments. |
| `TALKOPS_CHANNELS_DISCORD_WEBHOOK_SECRET` | `""` | Shared HMAC-SHA256 secret if using an external bridge posting to `/channels/discord/events`. |
| `TALKOPS_CHANNELS_DISCORD_EXPOSURE_MODE` | `"allowlist"` | `"self"`, `"allowlist"`, or `"open"`. |
| `TALKOPS_CHANNELS_DISCORD_OPERATOR_ID` | `""` | Operator Discord user snowflake (can run `/pair` and approve HITL cards). |
| `TALKOPS_CHANNELS_DISCORD_ALLOWLIST_CHATS` | `""` | Comma-separated parent channel IDs (child threads inherit parent channel admission). |
| `TALKOPS_CHANNELS_DISCORD_PERSONA_WEBHOOK_URL` | `""` | Optional channel webhook URL for custom per-subagent `username`/`avatar_url` posts. |

---

## 6. Troubleshooting Quick Reference

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `/channels/status` shows `state: "error"`, `fatal_code: 4014` | `MESSAGE CONTENT INTENT` (or `SERVER MEMBERS INTENT`) is OFF in Discord Portal. | Toggle **MESSAGE CONTENT INTENT** ON in **Bot** tab, save, and restart `talkops serve`. |
| `/channels/status` shows `fatal_code: 4004` | Bot token is invalid or was reset. | Regenerate token in **Bot** tab and update `TALKOPS_DISCORD_BOT_TOKEN`. |
| Messages arrive with empty text | `MESSAGE CONTENT INTENT` not enabled on Portal. | Toggle ON in **Bot** tab (code already requests bitmask `1 << 15`). |
| Discord Portal rejects Interactions Endpoint URL with `401` | `TALKOPS_DISCORD_PUBLIC_KEY` missing or mismatched, or server unreachable. | Verify exact 64-hex Public Key from **General Information** and confirm HTTPS reaches `/channels/discord/interactions`. |
| Slash commands don't appear in Discord | Commands not yet synced, or global sync still propagating. | Call `POST /channels/discord/sync-commands` with `{"guild_id": "<id>"}` for instant guild sync. |
| Bot replies in channel instead of creating a thread | Missing `Create Public Threads` / `Send Messages in Threads` permission in that channel. | Grant both permissions to the bot role on the server or channel overrides. |
| Duplicate replies across multiple Kubernetes pods | Pods running SQLite locally instead of shared PostgreSQL. | Configure `TALKOPS_POSTGRES_URI` so distributed leader leases ensure one active Gateway pod per shard. |

---

## See Also

- **[Server Addons & ChatOps Hub](server-addons.md)** — Full `channels` addon architecture, multi-channel routing, and server profiles.
- **[Webhooks & Automated Incident Response](webhooks.md)** — Connecting PagerDuty, Alertmanager, Datadog, AWS SNS, ServiceNow, and GitHub alerts.
- **[Slack App Portal Setup Guide](slack-portal-setup.md)** · **[Microsoft Teams Portal Setup Guide](teams-portal-setup.md)** — Companion setup guides for Slack and Teams.
